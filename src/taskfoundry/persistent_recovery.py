"""Resume one interrupted persistent Harbor trial in its original LBG sandbox."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from uuid import UUID

from .labwright import atomic_json
from .policy import file_sha256
from .researcher import (
    ResearcherReceipt,
    _capture_provider_identities,
    extract_attempt_identifiers,
)
from .supervisor_json import read_object


_SANDBOX_CREATED = re.compile(r"^Sandbox created: ([A-Za-z0-9][A-Za-z0-9._-]*)$")
_TERMINAL_ACTIONS = frozenset({"STOP_PASSED", "STOP_BLOCKED", "STOP_TOO_EASY"})


def _install_runtime_paths(runtime_path: Path) -> None:
    value = read_object(runtime_path)
    paths = value.get("adapter_pythonpath")
    if not isinstance(paths, str) or not paths:
        raise RuntimeError("persistent recovery runtime lacks adapter_pythonpath")
    for path in reversed(paths.split(os.pathsep)):
        if path and path not in sys.path:
            sys.path.insert(0, path)


def _resolve_agent_environment(config: dict[str, Any]) -> dict[str, Any]:
    agent = config.get("agent")
    if not isinstance(agent, dict):
        raise RuntimeError("persistent recovery requires one Agent")
    values = agent.get("env")
    if not isinstance(values, dict):
        raise RuntimeError("persistent recovery Agent env is invalid")
    for key, raw in list(values.items()):
        if not isinstance(raw, str):
            raise RuntimeError("persistent recovery Agent env value is invalid")
        match = re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", raw)
        if match is not None:
            resolved = os.environ.get(match.group(1))
            if not resolved:
                raise RuntimeError(f"required runtime variable is missing: {match.group(1)}")
            values[key] = resolved
    return config


def _canonical_job(request_dir: Path) -> tuple[Path, Path, dict[str, Any]]:
    config = read_object(request_dir / "job-config.json")
    jobs_dir = Path(str(config.get("jobs_dir", ""))).resolve()
    job_name = config.get("job_name")
    expected = (request_dir / "harbor-jobs").resolve()
    if jobs_dir != expected or not isinstance(job_name, str) or not job_name:
        raise RuntimeError("persistent recovery JobConfig is not canonical")
    job_dir = jobs_dir / job_name
    trial_dirs = sorted(path for path in job_dir.iterdir() if path.is_dir())
    if len(trial_dirs) != 1:
        raise RuntimeError("persistent recovery requires one existing trial directory")
    return job_dir, trial_dirs[0], config


def _bound_identities(
    request_dir: Path, job_dir: Path, action_path: Path
) -> tuple[str, str, str]:
    request = read_object(request_dir / "request.json")
    action = read_object(action_path)
    if (
        action.get("action") != "CONTINUE_HINT"
        or action.get("teacher_declares_non_answer") is not True
        or not isinstance(action.get("hint"), str)
        or not action["hint"].strip()
    ):
        raise RuntimeError("persistent recovery requires a Teacher non-answer hint")
    controller = request_dir / "validation-control"
    trial_ids: set[str] = set()
    for index in range(1, 4):
        result = read_object(controller / f"round-{index:02d}-result.json")
        decision = read_object(controller / f"round-{index:02d}-decision.json")
        if (
            result.get("classification") != "SCIENTIFIC_RESULT"
            or result.get("phase") != "BLIND"
            or result.get("round_index") != index
            or result.get("validation_session_id") != request.get("validation_session_id")
            or decision.get("result_sha256")
            != file_sha256(controller / f"round-{index:02d}-result.json")
        ):
            raise RuntimeError("persistent recovery blind history binding failed")
        trial_ids.add(str(result.get("trial_id") or ""))
    if len(trial_ids) != 1 or "" in trial_ids:
        raise RuntimeError("persistent recovery blind rounds changed trial identity")
    log_lines = (job_dir / "job.log").read_text(encoding="utf-8").splitlines()
    sandboxes = [match.group(1) for line in log_lines if (match := _SANDBOX_CREATED.fullmatch(line))]
    if len(sandboxes) < 4 or len(sandboxes) != len(set(sandboxes)):
        raise RuntimeError("persistent recovery provider identities are incomplete")
    return trial_ids.pop(), sandboxes[0], str(action["hint"])


def _prior_step_results(trial_dir: Path, controller: Path) -> list[Any]:
    from harbor.models.trial.result import StepResult, TimingInfo
    from harbor.models.verifier.result import VerifierResult

    steps = []
    for index in range(1, 4):
        result_path = controller / f"round-{index:02d}-result.json"
        value = read_object(result_path)
        observed = datetime.fromtimestamp(result_path.stat().st_mtime, tz=UTC)
        steps.append(
            StepResult(
                step_name=f"round-{index:02d}",
                verifier_result=VerifierResult(rewards=dict(value["verifier_result"])),
                agent_execution=TimingInfo(started_at=observed, finished_at=observed),
                verifier=TimingInfo(started_at=observed, finished_at=observed),
            )
        )
    return steps


def _completed_hint_turn(trial_dir: Path, instruction: str) -> bool:
    session_roots = (
        trial_dir / "steps/round-04/agent/sessions",
        trial_dir / "agent/sessions",
    )
    for root in session_roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            requested_turns: set[str] = set()
            completed_turns: set[str] = set()
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = value.get("payload")
                if not isinstance(payload, dict):
                    continue
                if value.get("type") == "response_item" and payload.get("type") == "message":
                    if payload.get("role") != "user":
                        continue
                    content = payload.get("content")
                    texts = [
                        item.get("text")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "input_text"
                    ] if isinstance(content, list) else []
                    metadata = payload.get("internal_chat_message_metadata_passthrough")
                    if instruction in texts and isinstance(metadata, dict):
                        turn_id = metadata.get("turn_id")
                        if isinstance(turn_id, str):
                            requested_turns.add(turn_id)
                if value.get("type") == "event_msg" and payload.get("type") == "task_complete":
                    turn_id = payload.get("turn_id")
                    if (
                        isinstance(turn_id, str)
                        and payload.get("error") is None
                        and isinstance(payload.get("last_agent_message"), str)
                        and payload["last_agent_message"].strip()
                    ):
                        completed_turns.add(turn_id)
            if requested_turns & completed_turns:
                return True
    return False


async def _grade_completed_hint_turn(trial: Any, round_index: int) -> Any:
    from harbor.models.trial.result import ExceptionInfo, StepResult, TimingInfo
    from harbor.trial.hooks import TrialEvent

    name = f"round-{round_index:02d}"
    step = StepResult(step_name=name)
    observed = datetime.now(UTC)
    step.agent_execution = TimingInfo(started_at=observed, finished_at=observed)
    trial._are_agent_logs_downloaded = False
    await trial._sync_agent_output(step)
    await trial._upload_agent_logs()
    await trial._collect_artifacts_phased(artifacts_dir=trial.paths.artifacts_dir)
    if not trial.config.verifier.disable:
        step.verifier = TimingInfo(started_at=trial._now())
        try:
            await trial._emit(TrialEvent.VERIFICATION_START)
            step.verifier_result = await trial._run_separate_verifier(
                key=name,
                timeout_sec=trial._verifier_timeout_sec,
                artifacts_dir=trial.paths.artifacts_dir,
                user=trial.task.config.verifier.user,
            )
        except Exception as error:
            step.exception_info = ExceptionInfo.from_exception(error)
        finally:
            step.verifier.finished_at = trial._now()
    trial._archive_round_outputs(name)
    return step


def _append_recovery_sandbox_identities(job_dir: Path, trial_dir: Path) -> None:
    job_log = job_dir / "job.log"
    existing = job_log.read_text(encoding="utf-8").splitlines()
    known = {match.group(1) for line in existing if (match := _SANDBOX_CREATED.fullmatch(line))}
    recovered = []
    for line in (trial_dir / "trial.log").read_text(encoding="utf-8").splitlines():
        match = _SANDBOX_CREATED.fullmatch(line)
        if match and match.group(1) not in known:
            recovered.append(line)
            known.add(match.group(1))
    if not recovered:
        raise RuntimeError("persistent recovery produced no fresh verifier sandbox")
    with job_log.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(recovered) + "\n")


def _write_job_result(job_dir: Path, trial: Any, reward: float) -> Path:
    finished = datetime.now(UTC)
    previous = read_object(job_dir / "result.json")
    started_at = previous.get("started_at") or finished.isoformat()
    value = {
        "id": str(trial.config.job_id),
        "started_at": started_at,
        "updated_at": finished.isoformat(),
        "finished_at": finished.isoformat(),
        "n_total_trials": 1,
        "stats": {
            "n_completed_trials": 1,
            "n_errored_trials": 0,
            "n_running_trials": 0,
            "n_pending_trials": 0,
            "n_cancelled_trials": 0,
            "n_retries": 0,
            "evals": {
                "codex__gpt-5.6-sol__adhoc": {
                    "n_trials": 1,
                    "n_errors": 0,
                    "metrics": [{"mean": reward}],
                    "pass_at_k": {},
                    "reward_stats": {"reward": {str(reward): [trial.config.trial_name]}},
                    "exception_stats": {},
                }
            },
        },
    }
    atomic_json(job_dir / "result.json", value)
    return job_dir / "result.json"


async def _resume(args: argparse.Namespace) -> None:
    from dotenv import load_dotenv
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial

    load_dotenv(args.env_file, override=True)
    request_dir = args.run_dir / "researcher-requests" / args.request_id
    job_dir, trial_dir, _ = _canonical_job(request_dir)
    trial_id, agent_sandbox_id, hint = _bound_identities(
        request_dir, job_dir, args.teacher_action
    )
    trial_value = _resolve_agent_environment(read_object(trial_dir / "config.json"))
    config = TrialConfig.model_validate(trial_value)
    trial = await Trial.create(config)
    trial._id = UUID(trial_id)
    trial.agent.context_id = trial._id
    trial.agent_environment.context_id = trial._id
    trial.agent._persistent_session_started = True
    trial.agent_environment._sandbox_id = agent_sandbox_id
    trial._init_result()
    prior_steps = _prior_step_results(trial_dir, request_dir / "validation-control")
    trial.result.step_results = list(prior_steps)
    try:
        final_reward = await _run_hint_rounds(trial, hint, trial_dir)
        await trial._finalize()
    finally:
        trial._close_logger_handler()
    _append_recovery_sandbox_identities(job_dir, trial_dir)
    _capture_provider_identities(job_dir)
    result_path = _write_job_result(job_dir, trial, final_reward)
    identifiers = extract_attempt_identifiers(result_path)
    finished = datetime.now(UTC)
    receipt = ResearcherReceipt(
        request_id=args.request_id,
        classification="SCIENTIFIC_RESULT",
        reward=final_reward,
        result_path=str(result_path.resolve()),
        started_at=str(read_object(job_dir / "result.json")["started_at"]),
        finished_at=finished.isoformat(),
        exit_code=0,
        result_sha256=file_sha256(result_path),
        wall_time_sec=max(0.0, (finished - datetime.fromtimestamp(result_path.stat().st_mtime, tz=UTC)).total_seconds()),
        **identifiers,
    )
    atomic_json(args.receipt, asdict(receipt))


async def _run_hint_rounds(
    trial: Any, instruction: str, trial_dir: Path
) -> float:
    from harbor.trial.validation_control import RoundPhase, RoundResult

    round_index = 4
    while True:
        existing_path = trial._controller.result_path(round_index)
        decision_path = trial._controller.decision_path(round_index)
        if existing_path.is_file() and decision_path.is_file():
            existing_value = read_object(existing_path)
            if existing_value.get("classification") != "SCIENTIFIC_RESULT":
                raise RuntimeError("persistent recovery found an unarchived platform result")
            existing_value["phase"] = RoundPhase(existing_value["phase"])
            existing = RoundResult(**existing_value)
            decision = trial._controller.read_decision(existing)
            if decision.action.value in _TERMINAL_ACTIONS:
                return float(existing.reward)
            round_index += 1
            instruction = decision.hint or ""
            continue
        if round_index == 4 and _completed_hint_turn(trial_dir, instruction):
            step = await _grade_completed_hint_turn(trial, round_index)
        else:
            step = await trial._run_round(
                round_index=round_index,
                phase=RoundPhase.HINT,
                instruction=instruction,
            )
        trial.result.step_results.append(step)
        trial.result.verifier_result = step.verifier_result
        result = trial._public_round_result(round_index, RoundPhase.HINT, step)
        trial._controller.publish_result(result)
        if result.classification != "SCIENTIFIC_RESULT":
            raise RuntimeError("persistent recovery round was not scientific")
        decision = await trial._controller.wait_for_decision(result)
        if decision.action.value in _TERMINAL_ACTIONS:
            return float(result.reward)
        round_index += 1
        instruction = decision.hint or ""


def main() -> None:
    """Validate bindings and resume the original persistent Harbor trial."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--teacher-action", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    _install_runtime_paths(args.runtime)
    asyncio.run(_resume(args))


if __name__ == "__main__":
    main()

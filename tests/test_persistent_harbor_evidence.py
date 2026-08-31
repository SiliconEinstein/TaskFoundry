from __future__ import annotations

import hashlib
import json
from pathlib import Path

from taskfoundry.harbor_evidence import HarborEvidenceImporter
from taskfoundry.package import package_sha256
from taskfoundry.researcher import ResearcherRequest


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def test_importer_binds_persistent_round_to_same_agent_and_fresh_verifier(
    tmp_path,
) -> None:
    handoff = tmp_path / "researcher-requests"
    request_id = "persistent-request-1"
    request_dir = handoff / request_id
    package = tmp_path / "task"
    package.mkdir()
    (package / "instruction.md").write_text("solve\n")
    controller = request_dir / "validation-control"
    jobs = request_dir / "harbor-jobs"
    job_name = "persistent_job"
    config_path = request_dir / "job-config.json"
    config = {
        "job_name": job_name,
        "jobs_dir": str(jobs),
        "tasks": [{"path": str(package)}],
        "agents": [
            {
                "model_name": "matmaster/gpt-5.6-sol",
                "kwargs": {
                    "persistent_validation": {
                        "schema_version": 1,
                        "validation_session_id": "validation-session-1",
                        "controller_dir": str(controller),
                        "pass_threshold": 0.85,
                        "max_blind_rounds": 3,
                        "max_hint_rounds": 2,
                        "decision_timeout_sec": 10800,
                    }
                },
            }
        ],
        "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
        "extra_instruction_paths": [],
    }
    _write(config_path, config)
    request = ResearcherRequest(
        request_id=request_id,
        run_id="run-1",
        question_revision="r1",
        attempt_index=1,
        mode="interactive",
        package_path=str(package),
        package_sha256=package_sha256(package),
        job_config_path=str(config_path),
        job_config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        context_digests=(),
        researcher_thread_id="researcher-thread-1",
        validation_session_id="validation-session-1",
        controller_dir=str(controller),
        schema_version=3,
    )
    request_path = request_dir / "request.json"
    _write(request_path, request.to_dict())
    _write(
        request_dir / "capability.json",
        {
            "schema_version": 2,
            "status": "CONSUMED",
            "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
            "researcher_thread_id": "researcher-thread-1",
            "consumed_by_thread_id": "researcher-thread-1",
        },
    )
    job = jobs / job_name
    trial = job / "trial-1"
    manifest = trial / "steps/round-01/artifacts/manifest.json"
    _write(manifest, {"artifacts": []})
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    result_path = controller / "round-01-result.json"
    _write(
        result_path,
        {
            "schema_version": 1,
            "validation_session_id": "validation-session-1",
            "trial_id": "trial-1",
            "round_index": 1,
            "phase": "BLIND",
            "classification": "SCIENTIFIC_RESULT",
            "reward": 1.0,
            "verifier_result": {"reward": 1.0},
            "artifact_manifest_sha256": manifest_sha,
        },
    )
    _write(
        controller / "round-01-decision.json",
        {
            "schema_version": 1,
            "validation_session_id": "validation-session-1",
            "round_index": 1,
            "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "action": "STOP_TOO_EASY",
            "hint": None,
            "teacher_declares_non_answer": None,
        },
    )
    _write(
        trial / "result.json",
        {
            "id": "trial-1",
            "step_results": [
                {
                    "step_name": "round-01",
                    "agent_result": {
                        "metadata": {"trace": {"session_id": "codex-session-1"}}
                    },
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "agent_execution": {
                        "started_at": "2026-08-28T00:00:00+00:00",
                        "finished_at": "2026-08-28T00:00:05+00:00",
                    },
                    "verifier": {
                        "started_at": "2026-08-28T00:00:05+00:00",
                        "finished_at": "2026-08-28T00:00:06+00:00",
                    },
                }
            ],
        },
    )
    _write(
        job / "result.json",
        {
            "id": "job-1",
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": "2026-08-28T00:00:07+00:00",
            "stats": {"n_errored_trials": 0},
        },
    )
    (job / "job.log").write_text(
        "Sandbox created: agent-sandbox-1\nSandbox created: verifier-sandbox-1\n"
    )
    _write(
        job / "provider-identities.json",
        {
            "schema_version": 2,
            "source": "harbor-adapter-job-log",
            "job_log_sha256": hashlib.sha256(
                (job / "job.log").read_bytes()
            ).hexdigest(),
            "agent_sandbox_id": "agent-sandbox-1",
            "verifier_sandbox_id": "verifier-sandbox-1",
            "verifier_sandbox_ids": ["verifier-sandbox-1"],
        },
    )

    verified = HarborEvidenceImporter(handoff).import_persistent_session(request_id)

    assert verified.agent_sandbox_id == "agent-sandbox-1"
    assert verified.harness_session_id == "codex-session-1"
    assert verified.rounds[0].verifier_sandbox_id == "verifier-sandbox-1"
    assert verified.rounds[0].decision_action == "STOP_TOO_EASY"

    (controller / "round-01-decision.json").unlink()
    _write(
        result_path,
        {
            "schema_version": 1,
            "validation_session_id": "validation-session-1",
            "trial_id": "trial-1",
            "round_index": 1,
            "phase": "BLIND",
            "classification": "PLATFORM_FAILURE",
            "reward": None,
            "verifier_result": None,
            "artifact_manifest_sha256": manifest_sha,
        },
    )
    trial_result = json.loads((trial / "result.json").read_text())
    trial_result["step_results"][0]["verifier_result"] = None
    trial_result["step_results"][0]["exception_info"] = {
        "exception_type": "RuntimeError",
        "exception_message": "provider failed before grading",
    }
    _write(trial / "result.json", trial_result)
    job_result = json.loads((job / "result.json").read_text())
    job_result["stats"]["n_errored_trials"] = 1
    _write(job / "result.json", job_result)
    (job / "job.log").write_text("Sandbox created: agent-sandbox-1\n")
    _write(
        job / "provider-identities.json",
        {
            "schema_version": 2,
            "source": "harbor-adapter-job-log",
            "job_log_sha256": hashlib.sha256(
                (job / "job.log").read_bytes()
            ).hexdigest(),
            "agent_sandbox_id": "agent-sandbox-1",
            "verifier_sandbox_id": "",
            "verifier_sandbox_ids": [],
        },
    )

    failed = HarborEvidenceImporter(handoff).import_persistent_session(request_id)

    assert failed.harness_session_id == "codex-session-1"
    assert failed.rounds[0].classification.value == "PLATFORM_FAILURE"
    assert failed.rounds[0].verifier_sandbox_id is None
    assert failed.rounds[0].decision_action is None

    (job / "job.log").write_text(
        "Sandbox created: agent-sandbox-1\nSandbox created: failed-verifier-sandbox-1\n"
    )
    _write(
        job / "provider-identities.json",
        {
            "schema_version": 2,
            "source": "harbor-adapter-job-log",
            "job_log_sha256": hashlib.sha256(
                (job / "job.log").read_bytes()
            ).hexdigest(),
            "agent_sandbox_id": "agent-sandbox-1",
            "verifier_sandbox_id": "failed-verifier-sandbox-1",
            "verifier_sandbox_ids": ["failed-verifier-sandbox-1"],
        },
    )

    verifier_failed = HarborEvidenceImporter(handoff).import_persistent_session(
        request_id
    )

    assert verifier_failed.rounds[0].verifier_sandbox_id == "failed-verifier-sandbox-1"

    _write(
        trial / "result.json",
        {
            "id": "trial-1",
            "agent_result": None,
            "verifier_result": None,
            "step_results": None,
            "exception_info": {
                "exception_type": "RuntimeError",
                "exception_message": "provider failed before sandbox creation",
            },
        },
    )
    _write(
        job / "result.json",
        {
            "id": "job-1",
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": "2026-08-28T00:00:07+00:00",
            "stats": {
                "n_errored_trials": 1,
                "evals": {
                    "codex": {
                        "exception_stats": {"RuntimeError": ["trial-1"]},
                        "metrics": [{"mean": 0.0}],
                    }
                },
            },
        },
    )
    (job / "job.log").write_text("missing provider credential\n")
    (job / "provider-identities.json").unlink()

    pre_round_failure = HarborEvidenceImporter(handoff).import_persistent_session(
        request_id
    )

    assert pre_round_failure.agent_sandbox_id == ""
    assert pre_round_failure.harness_session_id == ""
    assert len(pre_round_failure.rounds) == 1
    assert pre_round_failure.rounds[0].classification.value == "PLATFORM_FAILURE"
    assert pre_round_failure.rounds[0].decision_action is None
    assert pre_round_failure.rounds[0].result_path == str(
        (trial / "result.json").resolve()
    )

    (trial / "result.json").unlink()
    _write(
        job / "result.json",
        {
            "id": "job-1",
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": None,
            "stats": {
                "n_completed_trials": 0,
                "n_errored_trials": 0,
                "n_pending_trials": 1,
                "evals": {},
            },
        },
    )
    receipt_path = tmp_path / "validation/session-1/researcher-receipt.json"
    _write(
        receipt_path,
        {
            "schema_version": 1,
            "request_id": request_id,
            "classification": "PLATFORM_FAILURE",
            "failure_stage": "PLATFORM",
            "exit_code": 1,
            "reward": None,
            "job_id": "job-1",
            "trial_id": None,
            "sandbox_id": None,
            "session_id": None,
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": "2026-08-28T00:00:04+00:00",
            "wall_time_sec": 4.0,
            "result_path": str((job / "result.json").resolve()),
            "result_sha256": hashlib.sha256(
                (job / "result.json").read_bytes()
            ).hexdigest(),
        },
    )

    pre_trial_failure = HarborEvidenceImporter(handoff).import_persistent_session(
        request_id
    )

    assert pre_trial_failure.trial_id == ""
    assert pre_trial_failure.rounds[0].classification.value == "PLATFORM_FAILURE"
    assert pre_trial_failure.rounds[0].result_path == str(receipt_path.resolve())

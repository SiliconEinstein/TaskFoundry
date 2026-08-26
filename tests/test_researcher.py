from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from taskfoundry.model import Actor, ContractError
from taskfoundry.package import package_sha256
from taskfoundry.researcher import (
    CapabilityStore,
    ResearcherError,
    ResearcherRequest,
    classify_job_result,
    classify_job_outcome,
    execute_harbor,
    extract_attempt_identifiers,
)
from taskfoundry.harbor import DshRuntime


def request(tmp_path, **changes):
    package = tmp_path / "task"
    package.mkdir(exist_ok=True)
    (package / "input").write_text("x")
    config = tmp_path / "job.json"
    config_value = {
        "tasks": [{"path": str(package.resolve())}],
        "agents": [{"model_name": "deepseek/deepseek-v4-pro"}],
        "environment": {"type": "lbg", "kwargs": {"project_id": 1}},
        "extra_instruction_paths": [],
    }
    config.write_text(json.dumps(config_value))
    values = {
        "request_id": "request-1",
        "run_id": "run-1",
        "question_revision": "r1",
        "attempt_index": 1,
        "mode": "blind",
        "package_path": str(package),
        "package_sha256": package_sha256(package),
        "job_config_path": str(config),
        "job_config_sha256": hashlib.sha256(json.dumps(config_value).encode()).hexdigest(),
        "context_digests": (),
        "researcher_thread_id": "breaker-thread",
        "harness": "dsh",
        "model": "deepseek-v4-pro",
    }
    return ResearcherRequest(**(values | changes))


def test_teacher_issues_and_designated_researcher_redeems_once(tmp_path) -> None:
    store = CapabilityStore(tmp_path / "handoffs")
    handoff = store.issue(Actor.TEACHER, request(tmp_path))
    redeemed = store.redeem(handoff, "breaker-thread")
    assert redeemed.request_id == "request-1"
    with pytest.raises(ResearcherError, match="not active"):
        store.redeem(handoff, "breaker-thread")


def test_teacher_cannot_redeem_researcher_capability(tmp_path) -> None:
    store = CapabilityStore(tmp_path / "handoffs")
    handoff = store.issue(Actor.TEACHER, request(tmp_path))
    with pytest.raises(ResearcherError, match="designated"):
        store.redeem(handoff, "teacher-thread")


def test_only_teacher_can_issue_and_ids_are_one_use(tmp_path) -> None:
    store = CapabilityStore(tmp_path / "handoffs")
    value = request(tmp_path)
    with pytest.raises(ResearcherError, match="Teacher"):
        store.issue(Actor.RESEARCHER, value)
    store.issue(Actor.TEACHER, value)
    with pytest.raises(ResearcherError, match="already"):
        store.issue(Actor.TEACHER, value)


def test_request_detects_package_or_config_drift(tmp_path) -> None:
    value = request(tmp_path)
    (tmp_path / "task/input").write_text("changed")
    with pytest.raises(ContractError, match="package bytes"):
        value.validate()


def test_hint_requires_reviewed_context(tmp_path) -> None:
    with pytest.raises(ContractError, match="reviewed context"):
        request(tmp_path, mode="hint").validate()


def test_blind_requires_empty_context(tmp_path) -> None:
    with pytest.raises(ContractError, match="empty context"):
        request(tmp_path, context_digests=("a" * 64,)).validate()


def write_result(job_dir, *, errors=0, exception=None, reward=0.4):
    job_dir.mkdir(parents=True)
    evaluation = {"metrics": [{"mean": reward}], "exception_stats": {}}
    if exception:
        evaluation["exception_stats"] = {exception: 1}
    (job_dir / "result.json").write_text(
        json.dumps({"id": "job-id", "stats": {"n_errored_trials": errors, "evals": {"eval": evaluation}}})
    )


def test_classifies_scientific_result(tmp_path) -> None:
    job = tmp_path / "job"
    write_result(job, reward=0.7)
    assert classify_job_result(job, 0)[0:2] == ("SCIENTIFIC_RESULT", 0.7)


@pytest.mark.parametrize(
    "errors,exception,expected",
    [
        (1, "AgentTimeoutError", "DEFERRED_TIMEOUT"),
        (1, "AgentSetupError", "HARNESS_FAILURE"),
        (1, "SandboxError", "ENVIRONMENT_FAILURE"),
    ],
)
def test_classifies_failure_layers(tmp_path, errors, exception, expected) -> None:
    job = tmp_path / "job"
    write_result(job, errors=errors, exception=exception)
    assert classify_job_result(job, 1)[0] == expected


@pytest.mark.parametrize(
    "exception,stage",
    [
        ("ImagePullError", "IMAGE"),
        ("SandboxError", "SANDBOX"),
        ("AgentSetupError", "HARNESS_BOOTSTRAP"),
        ("ApiAuthError", "MODEL_CONNECTION"),
        ("VerifierError", "VERIFIER"),
    ],
)
def test_classifies_failure_stage(tmp_path, exception, stage) -> None:
    job = tmp_path / exception
    write_result(job, errors=1, exception=exception)
    assert classify_job_outcome(job, 1).failure_stage.value == stage


def test_missing_or_nonzero_result_is_platform_failure(tmp_path) -> None:
    assert classify_job_result(tmp_path / "missing", 1)[0] == "PLATFORM_FAILURE"
    job = tmp_path / "job"
    write_result(job)
    assert classify_job_result(job, 1)[0] == "PLATFORM_FAILURE"


def test_execute_harbor_captures_logs_and_parses_result(tmp_path, monkeypatch) -> None:
    value = request(tmp_path)
    job_dir = tmp_path / "jobs/job-1"
    config = {
        "job_name": "job-1",
        "jobs_dir": str(tmp_path / "jobs"),
        "tasks": [{"path": str(Path(value.package_path).resolve())}],
        "agents": [{"model_name": "deepseek/deepseek-v4-pro"}],
        "environment": {"type": "lbg", "kwargs": {"project_id": 1}},
        "extra_instruction_paths": [],
    }
    Path(value.job_config_path).write_text(json.dumps(config))
    value = ResearcherRequest(
        **(
            value.__dict__
            | {"job_config_sha256": hashlib.sha256(json.dumps(config).encode()).hexdigest()}
        )
    )
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=secret\n")

    def fake_run(command, **kwargs):
        write_result(job_dir, reward=0.6)
        trial = job_dir / "trial-one"
        trial.mkdir()
        (trial / "result.json").write_text(json.dumps({
            "id": "trial-id",
            "environment_setup": {"started_at": "one", "finished_at": "two"},
            "agent_result": {"metadata": {"trace": {"session_id": "session-id"}}},
        }))
        kwargs["stdout"].write(b"ok\n")
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("taskfoundry.researcher.subprocess.run", fake_run)
    artifact = tmp_path / "artifact"
    node = tmp_path / "node"
    artifact.write_bytes(b"artifact")
    node.write_bytes(b"node")
    runtime = DshRuntime(
        "a:A",
        str(tmp_path),
        str(artifact),
        hashlib.sha256(b"artifact").hexdigest(),
        "1",
        str(node),
        hashlib.sha256(b"node").hexdigest(),
        "v",
    )
    receipt = execute_harbor(request=value, runtime=runtime, env_file=env_file)
    assert receipt.classification == "SCIENTIFIC_RESULT"
    assert receipt.reward == 0.6
    assert (receipt.job_id, receipt.trial_id, receipt.session_id) == ("job-id", "trial-id", "session-id")
    assert (tmp_path / "researcher-execution/stdout.log").read_text() == "ok\n"


def test_codex_thread_id_is_a_fresh_session_identity(tmp_path) -> None:
    job = tmp_path / "job"
    write_result(job, reward=0.6)
    trial = job / "trial-one"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "id": "trial-id",
        "environment_setup": {"started_at": "one", "finished_at": "two"},
        "agent_result": {"metadata": None},
    }))
    (trial / "agent" / "codex.txt").write_text(
        '{"type":"thread.started","thread_id":"codex-thread-id"}\n'
    )

    identifiers = extract_attempt_identifiers(job / "result.json")

    assert identifiers["session_id"] == "codex-thread-id"

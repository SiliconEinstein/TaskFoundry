from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from taskfoundry.model import Actor, ContractError
from taskfoundry.package import package_sha256
from taskfoundry.researcher import (
    ApprovedHint,
    CapabilityStore,
    ResearcherError,
    ResearcherRequest,
    classify_job_result,
    classify_job_outcome,
    execute_harbor,
    extract_attempt_identifiers,
    validate_launch_contract,
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


def history_chain(tmp_path: Path, count: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    paths = []
    digests = []
    for index in range(1, count + 1):
        path = tmp_path / f"round-{index}" / "round-history.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"round": index}))
        paths.append(str(path))
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return tuple(paths), tuple(digests)


def test_teacher_issues_and_designated_researcher_redeems_once(tmp_path) -> None:
    store = CapabilityStore(tmp_path / "handoffs")
    handoff = store.issue(Actor.TEACHER, request(tmp_path))
    redeemed = store.redeem(handoff, "breaker-thread")
    assert redeemed.request_id == "request-1"
    capability = json.loads(Path(handoff.capability_path).read_text(encoding="utf-8"))
    assert capability["schema_version"] == 2
    assert capability["consumed_by_thread_id"] == "breaker-thread"
    with pytest.raises(ResearcherError, match="not active"):
        store.redeem(handoff, "breaker-thread")


def test_launch_contract_allows_env_owned_lbg_project_id(tmp_path) -> None:
    value = request(tmp_path)
    config_path = Path(value.job_config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["environment"]["kwargs"]["project_id"] = None
    config_path.write_text(json.dumps(config), encoding="utf-8")
    validate_launch_contract(value)


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


def test_linear_round_allows_fresh_request_only_after_prior_request_is_closed(tmp_path) -> None:
    store = CapabilityStore(tmp_path / "handoffs")
    linear = {
        "schema_version": 2,
        "validation_session_id": "session-1",
        "round_index": 1,
    }
    first = request(tmp_path, request_id="round-1-platform", **linear)
    store.issue(Actor.TEACHER, first)
    retry = request(tmp_path, request_id="round-1-retry", **linear)

    with pytest.raises(ResearcherError, match="round already has"):
        store.issue(Actor.TEACHER, retry)
    handoff = store.issue(
        Actor.TEACHER,
        retry,
        closed_request_ids=("round-1-platform",),
    )
    assert Path(handoff.request_path).parent.name == "round-1-retry"


def test_issue_rejects_invalid_existing_request_ledger(tmp_path) -> None:
    root = tmp_path / "handoffs"
    bad = root / "broken"
    bad.mkdir(parents=True)
    (bad / "request.json").write_text("{", encoding="utf-8")
    value = request(
        tmp_path,
        request_id="round-1",
        schema_version=2,
        validation_session_id="session-1",
        round_index=1,
    )

    with pytest.raises(ResearcherError, match="ledger is invalid"):
        CapabilityStore(root).issue(Actor.TEACHER, value)


def test_issue_rejects_job_config_that_cannot_be_frozen(tmp_path) -> None:
    value = request(tmp_path)
    config = Path(value.job_config_path)
    config.write_text("{", encoding="utf-8")
    value = ResearcherRequest(
        **(value.__dict__ | {"job_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest()})
    )

    with pytest.raises(ResearcherError, match="cannot be frozen"):
        CapabilityStore(tmp_path / "handoffs").issue(Actor.TEACHER, value)


@pytest.mark.parametrize("request_id", ["../escape", "/tmp/escape", ".", "a/b"])
def test_request_id_must_be_one_safe_path_component(
    tmp_path,
    request_id: str,
) -> None:
    with pytest.raises(ContractError, match="request_id"):
        request(tmp_path, request_id=request_id).validate()


def test_request_detects_package_or_config_drift(tmp_path) -> None:
    value = request(tmp_path)
    (tmp_path / "task/input").write_text("changed")
    with pytest.raises(ContractError, match="package bytes"):
        value.validate()


def test_hint_requires_reviewed_context(tmp_path) -> None:
    with pytest.raises(ContractError, match="reviewed context"):
        request(tmp_path, mode="hint").validate()


@pytest.mark.parametrize(
    "value,message",
    [
        ({"schema_version": 2, "validation_session_id": "s", "round_index": 1, "content": "x", "teacher_declares_non_answer": True}, "schema_version"),
        ({"schema_version": 1, "validation_session_id": "", "round_index": 1, "content": "x", "teacher_declares_non_answer": True}, "identity"),
        ({"schema_version": 1, "validation_session_id": "s", "round_index": 0, "content": "x", "teacher_declares_non_answer": True}, "identity"),
        ({"schema_version": 1, "validation_session_id": "s", "round_index": 1, "content": " ", "teacher_declares_non_answer": True}, "identity"),
        ({"schema_version": 1, "validation_session_id": "s", "round_index": 1, "content": " x", "teacher_declares_non_answer": True}, "identity"),
        ({"schema_version": 1, "validation_session_id": "s", "round_index": 1, "content": "x", "teacher_declares_non_answer": False}, "non-answer"),
    ],
)
def test_teacher_hint_declaration_fails_closed(tmp_path, value, message) -> None:
    path = tmp_path / "hint.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ContractError, match=message):
        ApprovedHint.from_path(path)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"mode": "invalid"}, "mode"),
        ({"harness": "unknown"}, "pairing"),
        ({"scientific_timeout_sec": 1}, "3600"),
        ({"target_solution_time_sec": 0}, "1800"),
        ({"run_id": ""}, "incomplete"),
    ],
)
def test_request_general_contract_rejects_invalid_values(tmp_path, changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        request(tmp_path, **changes).validate()


def test_blind_requires_empty_context(tmp_path) -> None:
    with pytest.raises(ContractError, match="empty context"):
        request(tmp_path, context_digests=("a" * 64,)).validate()


def test_linear_blind_rounds_share_outer_thread_and_bind_prior_records(tmp_path) -> None:
    round_one = request(
        tmp_path,
        schema_version=2,
        validation_session_id="validation-session-1",
        round_index=1,
        prior_round_receipt_sha256s=(),
    )
    round_one.validate()

    paths, digests = history_chain(tmp_path, 1)
    round_two = request(
        tmp_path,
        request_id="request-2",
        attempt_index=2,
        schema_version=2,
        validation_session_id="validation-session-1",
        round_index=2,
        context_digests=digests,
        prior_round_receipt_sha256s=digests,
        prior_round_history_paths=paths,
    )
    round_two.validate()


def test_linear_round_rejects_missing_or_symlinked_history_bundle(tmp_path) -> None:
    missing = tmp_path / "missing/round-history.json"
    with pytest.raises(ContractError, match="regular file"):
        request(
            tmp_path,
            request_id="request-missing-history",
            attempt_index=2,
            schema_version=2,
            validation_session_id="validation-session-1",
            round_index=2,
            context_digests=("a" * 64,),
            prior_round_receipt_sha256s=("a" * 64,),
            prior_round_history_paths=(str(missing),),
        ).validate()
    target = tmp_path / "target-history.json"
    target.write_text("{}")
    linked = tmp_path / "round-history.json"
    linked.symlink_to(target)
    with pytest.raises(ContractError, match="regular file"):
        request(
            tmp_path,
            request_id="request-linked-history",
            attempt_index=2,
            schema_version=2,
            validation_session_id="validation-session-1",
            round_index=2,
            context_digests=(hashlib.sha256(target.read_bytes()).hexdigest(),),
            prior_round_receipt_sha256s=(hashlib.sha256(target.read_bytes()).hexdigest(),),
            prior_round_history_paths=(str(linked),),
        ).validate()


def test_linear_blind_rejects_missing_prior_round_records(tmp_path) -> None:
    with pytest.raises(ContractError, match="prior round records"):
        request(
            tmp_path,
            attempt_index=2,
            schema_version=2,
            validation_session_id="validation-session-1",
            round_index=2,
            context_digests=(),
            prior_round_receipt_sha256s=(),
        ).validate()


def test_linear_hint_requires_exact_teacher_non_answer_declaration(tmp_path) -> None:
    hint = tmp_path / "approved-hint.json"
    hint.write_text(json.dumps(ApprovedHint(
        validation_session_id="validation-session-1",
        round_index=4,
        content="先检查边界条件是否满足。",
        teacher_declares_non_answer=True,
    ).to_dict()))
    digest = hashlib.sha256(hint.read_bytes()).hexdigest()
    history_paths, prior = history_chain(tmp_path, 3)

    value = request(
        tmp_path,
        request_id="hint-request",
        mode="hint",
        attempt_index=4,
        schema_version=2,
        validation_session_id="validation-session-1",
        round_index=4,
        context_digests=prior + (digest,),
        prior_round_receipt_sha256s=prior,
        prior_round_history_paths=history_paths,
        approved_hint_path=str(hint),
        approved_hint_sha256=digest,
    )

    value.validate()


def test_linear_hint_rejects_extra_context_or_answer_declaration(tmp_path) -> None:
    hint = tmp_path / "bad-hint.json"
    hint.write_text(json.dumps({
        "schema_version": 1,
        "validation_session_id": "validation-session-1",
        "round_index": 4,
        "teacher_declares_non_answer": False,
        "content": "答案是 A",
    }))
    digest = hashlib.sha256(hint.read_bytes()).hexdigest()
    history_paths, prior = history_chain(tmp_path, 3)
    with pytest.raises(ContractError, match="non-answer"):
        request(
            tmp_path,
            request_id="bad-hint-request",
            mode="hint",
            attempt_index=4,
            schema_version=2,
            validation_session_id="validation-session-1",
            round_index=4,
            context_digests=prior + (digest,),
            prior_round_receipt_sha256s=prior,
            prior_round_history_paths=history_paths,
            approved_hint_path=str(hint),
            approved_hint_sha256=digest,
        ).validate()

    hint.write_text(json.dumps({
        "schema_version": 1,
        "validation_session_id": "validation-session-1",
        "round_index": 4,
        "content": "未显式声明的提示",
    }))
    with pytest.raises(ContractError, match="approved hint is invalid"):
        ApprovedHint.from_path(hint)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"schema_version": 4}, "unsupported"),
        ({"schema_version": 2, "validation_session_id": None, "round_index": 1}, "matching session"),
        ({"schema_version": 2, "validation_session_id": "s", "round_index": 2}, "matching session"),
        ({
            "schema_version": 2,
            "validation_session_id": "s",
            "round_index": 1,
            "context_digests": ("a" * 64,),
        }, "limited to prior"),
        ({
            "schema_version": 2,
            "validation_session_id": "s",
            "round_index": 2,
            "attempt_index": 2,
            "context_digests": ("z" * 64,),
            "prior_round_receipt_sha256s": ("z" * 64,),
        }, "history bundles"),
    ],
)
def test_linear_request_rejects_broken_session_contract(tmp_path, changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        request(tmp_path, **changes).validate()


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


def test_classifies_harbor_metric_reward_without_legacy_mean(tmp_path) -> None:
    """接受 Harbor 0.18 中以 reward 命名的聚合指标。"""
    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": "job-id",
                "stats": {
                    "n_completed_trials": 1,
                    "n_errored_trials": 0,
                    "evals": {
                        "eval": {
                            "metrics": [
                                {
                                    "formal_score": 0.99,
                                    "recommendation_accuracy": 1.0,
                                    "reward": 0.975,
                                }
                            ],
                            "exception_stats": {},
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    assert classify_job_result(job, 0)[0:2] == ("SCIENTIFIC_RESULT", 0.975)


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


def test_agent_timeout_with_model_transport_log_is_harness_failure(tmp_path: Path) -> None:
    job = tmp_path / "job"
    write_result(job, errors=1, exception="AgentTimeoutError")
    log = job / "trial-one/agent/codex.txt"
    log.parent.mkdir(parents=True)
    log.write_text(
        '{"type":"error","message":"Falling back from WebSockets to HTTPS transport. '
        'request timed out"}\n'
        '{"type":"error","message":"Reconnecting... waiting for network '
        '(Connection failed: error sending request)"}\n',
        encoding="utf-8",
    )

    outcome = classify_job_outcome(job, 1)

    assert outcome.classification == "HARNESS_FAILURE"
    assert outcome.failure_stage.value == "MODEL_CONNECTION"


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
        (job_dir / "job.log").write_text(
            "Sandbox created: agent-sandbox\n"
            "Sandbox created: verifier-sandbox\n"
        )
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
    identities = json.loads((job_dir / "provider-identities.json").read_text())
    assert identities["agent_sandbox_id"] == "agent-sandbox"
    assert identities["verifier_sandbox_id"] == "verifier-sandbox"


def test_execute_timeout_preserves_single_created_provider_sandbox(tmp_path, monkeypatch) -> None:
    value = request(tmp_path)
    job_dir = tmp_path / "timeout-jobs/job-1"
    config = {
        "job_name": "job-1",
        "jobs_dir": str(tmp_path / "timeout-jobs"),
        "tasks": [{"path": str(Path(value.package_path).resolve())}],
        "agents": [{"model_name": "deepseek/deepseek-v4-pro"}],
        "environment": {"type": "lbg", "kwargs": {"project_id": 1}},
        "extra_instruction_paths": [],
    }
    Path(value.job_config_path).write_text(json.dumps(config))
    value = ResearcherRequest(
        **(value.__dict__ | {"job_config_sha256": hashlib.sha256(json.dumps(config).encode()).hexdigest()})
    )

    def timeout_run(command, **kwargs):
        job_dir.mkdir(parents=True)
        (job_dir / "job.log").write_text("Sandbox created: agent-only\n", encoding="utf-8")
        raise subprocess.TimeoutExpired(command, timeout=1)

    monkeypatch.setattr("taskfoundry.researcher.subprocess.run", timeout_run)
    artifact = tmp_path / "artifact-timeout"
    node = tmp_path / "node-timeout"
    artifact.write_bytes(b"artifact")
    node.write_bytes(b"node")
    runtime = DshRuntime(
        "a:A", str(tmp_path), str(artifact), hashlib.sha256(b"artifact").hexdigest(),
        "1", str(node), hashlib.sha256(b"node").hexdigest(), "v",
    )

    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=secret\n", encoding="utf-8")
    receipt = execute_harbor(request=value, runtime=runtime, env_file=env_file)

    assert receipt.classification == "PLATFORM_FAILURE"
    identities = json.loads((job_dir / "provider-identities.json").read_text())
    assert identities["agent_sandbox_id"] == "agent-only"
    assert identities["verifier_sandbox_id"] == ""


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

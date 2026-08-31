"""Harbor 权威产物导入契约。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from taskfoundry.harbor_evidence import (
    HarborEvidenceError,
    HarborEvidenceImporter,
    compact_legacy_round_history,
)
from taskfoundry.package import package_sha256
from taskfoundry.researcher import ResearcherRequest, sha256_file


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def canonical_run(tmp_path: Path) -> tuple[Path, str]:
    """建立一个只有权威 request、兑换记录和 Harbor 原始结果的目录。"""
    package = tmp_path / "package"
    package.mkdir()
    (package / "input.txt").write_text("frozen", encoding="utf-8")
    request_id = "request-one"
    handoff = tmp_path / "handoffs" / request_id
    jobs = handoff / "harbor-jobs"
    config = handoff / "job-config.json"
    write_json(
        config,
        {
            "jobs_dir": str(jobs),
            "job_name": "job-one",
            "tasks": [{"path": str(package)}],
            "agents": [{"model_name": "matmaster/gpt-5.6-sol"}],
            "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
            "extra_instruction_paths": [],
        },
    )
    request = ResearcherRequest(
        request_id=request_id,
        run_id="run-one",
        question_revision="r1",
        attempt_index=1,
        mode="blind",
        package_path=str(package),
        package_sha256=package_sha256(package),
        job_config_path=str(config),
        job_config_sha256=sha256_file(config),
        context_digests=(),
        researcher_thread_id="researcher-thread-one",
        validation_session_id="validation-session-one",
        round_index=1,
        prior_round_receipt_sha256s=(),
        schema_version=2,
    )
    write_json(handoff / "request.json", request.to_dict())
    write_json(
        handoff / "capability.json",
        {
            "schema_version": 2,
            "request_sha256": sha256_file(handoff / "request.json"),
            "researcher_thread_id": request.researcher_thread_id,
            "status": "CONSUMED",
            "consumed_by_thread_id": request.researcher_thread_id,
        },
    )
    job = jobs / "job-one"
    write_json(
        job / "result.json",
        {
            "id": "job-id",
            "started_at": "2026-08-27T00:00:00+00:00",
            "finished_at": "2026-08-27T00:00:10+00:00",
            "stats": {
                "n_errored_trials": 0,
                "evals": {"solver": {"metrics": [{"mean": 0.4}], "exception_stats": {}}},
            },
        },
    )
    trial = job / "trial-one"
    write_json(
        trial / "result.json",
        {
            "id": "trial-id",
            "agent_result": {"metadata": None},
        },
    )
    (trial / "agent").mkdir()
    (trial / "agent/codex.txt").write_text(
        '{"type":"thread.started","thread_id":"harness-session-id"}\n',
        encoding="utf-8",
    )
    (job / "job.log").write_text(
        "Sandbox created: provider-agent-sandbox\n"
        "Sandbox created: provider-verifier-sandbox\n",
        encoding="utf-8",
    )
    write_json(job / "provider-identities.json", {
        "schema_version": 1,
        "source": "harbor-adapter-job-log",
        "job_log_sha256": sha256_file(job / "job.log"),
        "agent_sandbox_id": "provider-agent-sandbox",
        "verifier_sandbox_id": "provider-verifier-sandbox",
    })
    return handoff.parent, request.request_id


def test_importer_recomputes_scientific_result_from_canonical_harbor_bytes(tmp_path: Path) -> None:
    handoffs, request_id = canonical_run(tmp_path)

    evidence = HarborEvidenceImporter(handoffs).import_round(request_id)

    assert evidence.classification.value == "SCIENTIFIC_RESULT"
    assert evidence.score == 0.4
    assert evidence.job_id == "job-id"
    assert evidence.trial_id == "trial-id"
    assert evidence.agent_sandbox_id == "provider-agent-sandbox"
    assert evidence.verifier_sandbox_id == "provider-verifier-sandbox"
    assert evidence.harness_session_id == "harness-session-id"


def test_importer_rejects_handwritten_consumed_capability(tmp_path: Path) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    capability = handoffs / request_id / "capability.json"
    value = json.loads(capability.read_text(encoding="utf-8"))
    value.pop("consumed_by_thread_id")
    write_json(capability, value)

    with pytest.raises(HarborEvidenceError, match="兑换身份"):
        HarborEvidenceImporter(handoffs).import_round(request_id)


def test_importer_classifies_missing_real_provider_sandbox_as_incomplete(tmp_path: Path) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    job_log = handoffs / request_id / "harbor-jobs/job-one/job.log"
    job_log.write_text("Harbor completed without provider identity\n", encoding="utf-8")

    evidence = HarborEvidenceImporter(handoffs).import_round(request_id)

    assert evidence.classification.value == "EVIDENCE_INCOMPLETE"
    assert evidence.job_result_sha256 is not None


def test_importer_keeps_pre_sandbox_platform_failure_non_scientific(tmp_path: Path) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    job = handoffs / request_id / "harbor-jobs/job-one"
    (job / "trial-one/agent/codex.txt").unlink()
    (job / "trial-one/agent").rmdir()
    (job / "trial-one/result.json").unlink()
    (job / "trial-one").rmdir()
    (job / "job.log").unlink()
    (job / "provider-identities.json").unlink()
    write_json(job / "result.json", {
        "id": "job-id",
        "started_at": "2026-08-27T00:00:00+00:00",
        "finished_at": "2026-08-27T00:00:01+00:00",
        "stats": {
            "n_errored_trials": 1,
            "evals": {"solver": {
                "metrics": [],
                "exception_stats": {"ProviderTransportError": 1},
            }},
        },
    })

    evidence = HarborEvidenceImporter(handoffs).import_round(request_id)

    assert evidence.classification.value == "PLATFORM_FAILURE"
    assert evidence.score is None
    assert evidence.agent_sandbox_id == ""
    assert evidence.verifier_sandbox_id == ""


def test_importer_preserves_partial_provider_identity_for_platform_retry_freshness(
    tmp_path: Path,
) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    job = handoffs / request_id / "harbor-jobs/job-one"
    result = json.loads((job / "result.json").read_text(encoding="utf-8"))
    result["stats"] = {
        "n_errored_trials": 1,
        "evals": {"solver": {"metrics": [], "exception_stats": {"ProviderError": 1}}},
    }
    write_json(job / "result.json", result)
    identity = json.loads((job / "provider-identities.json").read_text(encoding="utf-8"))
    identity["verifier_sandbox_id"] = ""
    write_json(job / "provider-identities.json", identity)

    evidence = HarborEvidenceImporter(handoffs).import_round(request_id)

    assert evidence.classification.value == "PLATFORM_FAILURE"
    assert evidence.agent_sandbox_id == "provider-agent-sandbox"
    assert evidence.verifier_sandbox_id == ""


@pytest.mark.parametrize(
    "exception,expected",
    [
        ("AgentTimeoutError", "DEFERRED_TIMEOUT"),
        ("ImagePullError", "ENVIRONMENT_FAILURE"),
        ("HarnessBootstrapError", "HARNESS_FAILURE"),
        ("ProviderTransportError", "PLATFORM_FAILURE"),
    ],
)
def test_importer_classifies_non_scientific_harbor_failures(
    tmp_path: Path, exception: str, expected: str
) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    job = handoffs / request_id / "harbor-jobs/job-one/result.json"
    value = json.loads(job.read_text(encoding="utf-8"))
    value["stats"] = {
        "n_errored_trials": 1,
        "evals": {"solver": {"metrics": [], "exception_stats": {exception: 1}}},
    }
    write_json(job, value)

    evidence = HarborEvidenceImporter(handoffs).import_round(request_id)

    assert evidence.classification.value == expected
    assert evidence.score is None


def test_importer_treats_agent_timeout_with_model_transport_failure_as_harness_failure(
    tmp_path: Path,
) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    job = handoffs / request_id / "harbor-jobs/job-one"
    result = json.loads((job / "result.json").read_text(encoding="utf-8"))
    result["stats"] = {
        "n_errored_trials": 1,
        "evals": {
            "solver": {
                "metrics": [],
                "exception_stats": {"AgentTimeoutError": 1},
            }
        },
    }
    write_json(job / "result.json", result)
    (job / "trial-one/agent/codex.txt").write_text(
        '{"type":"error","message":"Reconnecting... waiting for network '
        '(Connection failed: error sending request)"}\n',
        encoding="utf-8",
    )

    evidence = HarborEvidenceImporter(handoffs).import_round(request_id)

    assert evidence.classification.value == "HARNESS_FAILURE"
    assert evidence.score is None


def test_importer_prefers_real_trace_session_and_marks_duplicate_trial_incomplete(
    tmp_path: Path,
) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    trial = handoffs / request_id / "harbor-jobs/job-one/trial-one/result.json"
    value = json.loads(trial.read_text(encoding="utf-8"))
    value["agent_result"] = {"metadata": {"trace": {"session_id": "metadata-session"}}}
    write_json(trial, value)
    assert HarborEvidenceImporter(handoffs).import_round(request_id).harness_session_id == "metadata-session"
    write_json(
        handoffs / request_id / "harbor-jobs/job-one/trial-two/result.json",
        {"id": "trial-two"},
    )
    assert (
        HarborEvidenceImporter(handoffs).import_round(request_id).classification.value
        == "EVIDENCE_INCOMPLETE"
    )


def test_importer_rejects_token_and_marks_bad_harbor_result_incomplete(tmp_path: Path) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    token = handoffs / request_id / "token"
    token.write_text("still active", encoding="utf-8")
    with pytest.raises(HarborEvidenceError, match="仍保留"):
        HarborEvidenceImporter(handoffs).import_round(request_id)
    token.unlink()
    job = handoffs / request_id / "harbor-jobs/job-one/result.json"
    original = json.loads(job.read_text(encoding="utf-8"))
    write_json(job, {"id": "job-id"})
    assert (
        HarborEvidenceImporter(handoffs).import_round(request_id).classification.value
        == "EVIDENCE_INCOMPLETE"
    )
    original["finished_at"] = "not-a-time"
    write_json(job, original)
    assert (
        HarborEvidenceImporter(handoffs).import_round(request_id).classification.value
        == "EVIDENCE_INCOMPLETE"
    )
    job.write_text('{"id":"a","id":"b"}', encoding="utf-8")
    assert (
        HarborEvidenceImporter(handoffs).import_round(request_id).classification.value
        == "EVIDENCE_INCOMPLETE"
    )


def test_importer_marks_missing_job_result_incomplete(tmp_path: Path) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    (handoffs / request_id / "harbor-jobs/job-one/result.json").unlink()

    evidence = HarborEvidenceImporter(handoffs).import_round(request_id)

    assert evidence.classification.value == "EVIDENCE_INCOMPLETE"
    assert evidence.job_id == ""
    assert evidence.job_result_sha256 is None
    assert evidence.agent_sandbox_id == "provider-agent-sandbox"
    assert evidence.verifier_sandbox_id == "provider-verifier-sandbox"


@pytest.mark.parametrize("missing", ["trial_id", "harness_session"])
def test_importer_marks_missing_scientific_identity_incomplete(
    tmp_path: Path,
    missing: str,
) -> None:
    handoffs, request_id = canonical_run(tmp_path)
    trial = handoffs / request_id / "harbor-jobs/job-one/trial-one/result.json"
    if missing == "trial_id":
        value = json.loads(trial.read_text(encoding="utf-8"))
        value.pop("id")
        write_json(trial, value)
    else:
        (trial.parent / "agent/codex.txt").unlink()

    evidence = HarborEvidenceImporter(handoffs).import_round(request_id)

    assert evidence.classification.value == "EVIDENCE_INCOMPLETE"
    assert evidence.score is None
    assert evidence.agent_sandbox_id == "provider-agent-sandbox"
    assert evidence.verifier_sandbox_id == "provider-verifier-sandbox"


@pytest.mark.parametrize("request_id", ["../escape", "/tmp/escape", ".", "a/b"])
def test_importer_rejects_request_path_escape(tmp_path: Path, request_id: str) -> None:
    with pytest.raises(HarborEvidenceError, match="request_id"):
        HarborEvidenceImporter(tmp_path / "handoffs").import_round(request_id)


def test_compact_schema_two_history_preserves_identity_and_strips_outputs(
    tmp_path: Path,
) -> None:
    """Schema-2 delivery keeps provenance while removing cumulative output bodies."""
    source = tmp_path / "source/round-history.json"
    destination = tmp_path / "delivery/round-history.json"
    command = "python - <<'PY'\n" + "x = 1\n" * 1000 + "PY"
    write_json(
        source,
        {
            "schema_version": 2,
            "validation_session_id": "session-one",
            "round_index": 4,
            "request_id": "request-four",
            "question_revision": "r3",
            "package_sha256": "a" * 64,
            "transcript_sha256": "b" * 64,
            "transcript_format": "codex-jsonl-lossless-actions-bounded-outputs",
            "transcript_events": [
                {"type": "thread_started", "line": 1, "thread_id": "thread-one"},
                {"type": "agent_message", "line": 2, "text": "keep this strategy"},
                {
                    "type": "command_execution",
                    "line": 3,
                    "command": command,
                    "exit_code": 0,
                    "output": "sensitive bulk output" * 1000,
                    "output_bytes": 21000,
                    "output_sha256": "c" * 64,
                },
                {"type": "turn_completed", "line": 4, "usage": {"input_tokens": 9}},
            ],
        },
    )

    first = compact_legacy_round_history(source, destination)
    second = compact_legacy_round_history(source, destination)
    compact = json.loads(destination.read_text(encoding="utf-8"))

    assert first == second == sha256_file(destination)
    assert compact["schema_version"] == 2
    assert compact["validation_session_id"] == "session-one"
    assert compact["round_index"] == 4
    assert compact["transcript_format"] == "codex-jsonl-semantic-compact-v1"
    assert compact["source_round_history_sha256"] == sha256_file(source)
    execution = compact["transcript_events"][2]
    assert execution["command_sha256"] == hashlib.sha256(command.encode()).hexdigest()
    assert execution["output_sha256"] == "c" * 64
    assert "output" not in execution
    assert destination.stat().st_size < source.stat().st_size


def test_compact_schema_one_history_and_rejects_unknown_schema(tmp_path: Path) -> None:
    """The legacy branch stays supported and unknown persisted schemas fail closed."""
    transcript = '{"type":"thread.started","thread_id":"thread-one"}\n'
    source = tmp_path / "legacy/round-history.json"
    destination = tmp_path / "delivery/round-history.json"
    write_json(
        source,
        {
            "schema_version": 1,
            "validation_session_id": "session-one",
            "round_index": 1,
            "request_id": "request-one",
            "question_revision": "r1",
            "package_sha256": "a" * 64,
            "transcript_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
            "transcript": transcript,
        },
    )

    compact_legacy_round_history(source, destination)
    assert json.loads(destination.read_text())["transcript_format"] == (
        "codex-jsonl-lossless-actions-bounded-outputs"
    )

    write_json(source, {"schema_version": 3})
    with pytest.raises(HarborEvidenceError, match="schema-1/2"):
        compact_legacy_round_history(source, tmp_path / "other/round-history.json")

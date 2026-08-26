from __future__ import annotations

from dataclasses import replace
import json

import pytest

from taskfoundry.harbor import DshRuntime, HarborJobSpec, command_for
from taskfoundry.labwright import (
    ArtifactIdentity,
    FileLabwrightRegistry,
    LabwrightError,
    RuntimeSnapshot,
)
from taskfoundry.model import Actor, ContractError, RunEvent, RunSnapshot, RunState, SourceQuestion
from taskfoundry.policy import PolicyRepository, RuleAmendment
from taskfoundry.store import RunStore
from taskfoundry.validation import AttemptEvidence


def test_model_contract_error_edges() -> None:
    with pytest.raises(ContractError, match="source question"):
        SourceQuestion("", "p", "g", "m", "role").validate()
    with pytest.raises(ContractError, match="version"):
        RunEvent(1, "now", Actor.SYSTEM, "event", "key", schema_version=2).validate()
    with pytest.raises(ContractError, match="required"):
        RunEvent(1, "now", Actor.SYSTEM, "", "key").validate()


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"mode": "bad"}, "stage"),
        ({"classification": "PLATFORM_FAILURE", "score": 0.1}, "cannot carry"),
        ({"wall_time_sec": -1}, "identity"),
        ({"leakage_evidence_sha256": None}, "leakage-free"),
    ],
)
def test_attempt_contract_error_edges(changes, message) -> None:
    values = dict(
        request_id="r", attempt_index=1, mode="blind", classification="SCIENTIFIC_RESULT",
        score=0.1, frozen_contract_digest="f" * 64, job_id="j", trial_id="t",
        sandbox_id="s", session_id="x", wall_time_sec=1, leakage_free=True,
        leakage_evidence_sha256="e" * 64,
    )
    with pytest.raises(ContractError, match=message):
        AttemptEvidence(**(values | changes)).validate()


def test_harbor_contract_error_edges(tmp_path) -> None:
    missing = DshRuntime("a:A", str(tmp_path), "missing", "a" * 64, "1", "missing", "b" * 64, "v")
    with pytest.raises(ContractError, match="missing"):
        missing.validate()
    artifact = tmp_path / "artifact"
    node = tmp_path / "node"
    artifact.write_bytes(b"a")
    node.write_bytes(b"n")
    invalid_digest = replace(missing, development_artifact_path=str(artifact), node_runtime_path=str(node))
    with pytest.raises(ContractError, match="mismatch"):
        invalid_digest.validate()
    with pytest.raises(ContractError, match="does not exist"):
        HarborJobSpec("job", str(tmp_path / "jobs"), str(tmp_path / "task"), (), 1).validate()
    jobs = tmp_path / "jobs"
    task = tmp_path / "task"
    jobs.mkdir()
    task.mkdir()
    with pytest.raises(ContractError, match="approved context"):
        HarborJobSpec("job", str(jobs), str(task), (str(tmp_path / "missing"),), 1).validate()
    with pytest.raises(ContractError, match="must exist"):
        command_for(tmp_path / "config", tmp_path / "env")


def test_store_recovery_rejects_unreconcilable_journal(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run")
    event = RunEvent(1, "now", Actor.SYSTEM, "event", "key", payload={})
    store.events_path.write_text(json.dumps(event.to_dict()) + "\n")
    with pytest.raises(ContractError, match="recoverable"):
        store.read_snapshot()
    with pytest.raises(ContractError, match="initialized"):
        RunStore(tmp_path / "empty").commit(
            actor=Actor.SYSTEM, event_type="x", idempotency_key="x", payload={},
            snapshot=RunSnapshot("empty", RunState.DESIGNING, 1),
        )


def test_policy_contract_error_edges(tmp_path) -> None:
    with pytest.raises(ContractError, match="invalid amendment_id"):
        RuleAmendment("BAD ID", "q", "a" * 64, "p", "c", ("e",), "proposed", "now").validate()
    with pytest.raises(ContractError, match="status"):
        RuleAmendment("id", "q", "a" * 64, "p", "c", ("e",), "bad", "now").validate()
    with pytest.raises(ContractError, match="SHA-256"):
        RuleAmendment("id", "q", "bad", "p", "c", ("e",), "proposed", "now").validate()
    with pytest.raises(ContractError, match="unknown"):
        PolicyRepository(tmp_path).decide("missing", "accepted")


def test_labwright_lifecycle_error_edges(tmp_path) -> None:
    artifact = ArtifactIdentity("lbg", "lbg://prod", "42", "12", "registry/task:fixed")
    with pytest.raises(ContractError, match="runtime snapshot"):
        RuntimeSnapshot("r", "s", artifact, "now", "BAD").validate()
    RuntimeSnapshot("r", "s", artifact, "now", "READY").validate()
    registry = FileLabwrightRegistry(tmp_path / "state")
    missing = tmp_path / "missing.json"
    with pytest.raises(LabwrightError, match="missing"):
        registry._verify_clean_evidence(missing, artifact)
    evidence = tmp_path / "clean.json"
    evidence.write_text(json.dumps({
        "image_record_id": 12, "image_url": artifact.image_url,
        "clean_sandboxes_verified": 1, "runs": [{"sandbox_id": "one"}],
    }))
    with pytest.raises(LabwrightError, match="two distinct"):
        registry._verify_clean_evidence(evidence, artifact)
    evidence.write_text(json.dumps({
        "image_record_id": 12, "image_url": "registry/other:fixed",
        "clean_sandboxes_verified": 2,
        "runs": [{"sandbox_id": "one"}, {"sandbox_id": "two"}],
    }))
    with pytest.raises(LabwrightError, match="different image"):
        registry._verify_clean_evidence(evidence, artifact)

from pathlib import Path
import hashlib

import pytest

from taskfoundry.labwright import (
    ArtifactIdentity,
    EnvironmentDeltaRequest,
    FileLabwrightRegistry,
    RuntimeSnapshot,
)
from taskfoundry.labwright_runtime import (
    DeltaReceipt,
    DeltaState,
    FileLabwrightRuntimeService,
    ImageSealPlan,
)
from taskfoundry.model import Actor, ContractError


def _request(root: Path) -> None:
    FileLabwrightRegistry(root).request_delta(
        Actor.RESEARCHER,
        EnvironmentDeltaRequest(
            request_id="delta-1",
            run_id="run-1",
            question_revision="r1",
            kind="package",
            name="scipy",
            version_constraint="==1.14.0",
            reason="需要稀疏求解器",
            researcher_request_id="researcher-1",
            sandbox_id="sandbox-1",
        ),
    )


def _runtime() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        request_id="runtime-1",
        sandbox_id="sandbox-1",
        artifact=ArtifactIdentity(
            provider="lbg",
            endpoint_identity="lbg://production",
            project_id="42",
            record_id="1",
            image_url="registry/task:fixed",
        ),
        started_at="2026-08-24T00:00:00+00:00",
        status="READY",
    )


def test_labwright_claims_and_completes_same_sandbox_delta(tmp_path: Path) -> None:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)
    claim = service.claim(
        Actor.LABWRIGHT,
        "delta-1",
        worker_id="worker-1",
        fencing_token="fence-1",
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"scipy":"1.14.0"}')

    receipt = service.complete(
        Actor.LABWRIGHT,
        "delta-1",
        fencing_token="fence-1",
        runtime=_runtime(),
        capability_version="1.14.0",
        evidence_path=evidence,
    )

    assert claim.request_id == receipt.request_id == "delta-1"
    assert receipt.sandbox_id == "sandbox-1"
    assert service.resolve("delta-1") == receipt

    plan_path = tmp_path / "seal-plan.json"
    plan = service.create_seal_plan(
        Actor.LABWRIGHT,
        run_id="run-1",
        question_revision="r1",
        base_environment_key="a" * 64,
        request_ids=("delta-1",),
        output_path=plan_path,
    )
    assert plan_path.is_file()
    assert plan.delta_receipts[0]["capability_name"] == "scipy"


def test_labwright_rejects_wrong_sandbox_or_fence(tmp_path: Path) -> None:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)
    service.claim(
        Actor.LABWRIGHT,
        "delta-1",
        worker_id="worker-1",
        fencing_token="fence-1",
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}")

    with pytest.raises(ContractError, match="fence"):
        service.complete(
            Actor.LABWRIGHT,
            "delta-1",
            fencing_token="wrong",
            runtime=_runtime(),
            capability_version="1.14.0",
            evidence_path=evidence,
        )


def test_delta_receipt_and_seal_plan_validate_contracts(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}")
    base = DeltaReceipt(
        request_id="delta-1",
        researcher_request_id="researcher-1",
        sandbox_id="sandbox-1",
        state=DeltaState.READY,
        capability_name="scipy",
        capability_version="1",
        evidence_path=str(evidence),
        evidence_sha256="0" * 64,
        scientific_clock_paused_at="p",
        scientific_clock_resumed_at="r",
        pause_duration_sec=1,
        fencing_token="f",
    )
    with pytest.raises(ContractError, match="evidence changed"):
        base.validate()
    with pytest.raises(ContractError, match="only READY"):
        DeltaReceipt(**(base.__dict__ | {"state": DeltaState.FAILED})).validate()
    with pytest.raises(ContractError, match="identity"):
        DeltaReceipt(**(base.__dict__ | {"request_id": ""})).validate()
    with pytest.raises(ContractError, match="negative"):
        DeltaReceipt(**(base.__dict__ | {
            "pause_duration_sec": -1,
            "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        })).validate()

    with pytest.raises(ContractError, match="identity"):
        ImageSealPlan("", "r1", "a" * 64, ({},), "now").validate()
    with pytest.raises(ContractError, match="Stable base"):
        ImageSealPlan("run", "r1", "bad", ({},), "now").validate()
    with pytest.raises(ContractError, match="at least one"):
        ImageSealPlan("run", "r1", "a" * 64, (), "now").validate()


def test_labwright_rejects_invalid_roles_and_missing_requests(tmp_path: Path) -> None:
    root = tmp_path / "labwright"
    service = FileLabwrightRuntimeService(root)
    with pytest.raises(ContractError, match="only Labwright"):
        service.claim(Actor.TEACHER, "delta", worker_id="w", fencing_token="f")
    with pytest.raises(ContractError, match="worker"):
        service.claim(Actor.LABWRIGHT, "delta", worker_id="", fencing_token="f")
    with pytest.raises(ContractError, match="unknown"):
        service.claim(Actor.LABWRIGHT, "delta", worker_id="w", fencing_token="f")
    with pytest.raises(ContractError, match="not ready"):
        service.resolve("delta")
    with pytest.raises(ContractError, match="only Labwright"):
        service.create_seal_plan(
            Actor.TEACHER,
            run_id="run",
            question_revision="r1",
            base_environment_key="a" * 64,
            request_ids=("delta",),
            output_path=tmp_path / "plan.json",
        )

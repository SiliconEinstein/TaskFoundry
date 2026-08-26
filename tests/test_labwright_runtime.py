from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

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
    artifact_identity_sha256,
)
from taskfoundry.model import Actor, ContractError


RUN_ID = "run-1"
REVISION = "r1"
PACKAGE_SHA256 = "b" * 64
RESEARCHER_REQUEST_ID = "researcher-1"
SANDBOX_ID = "sandbox-1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact() -> ArtifactIdentity:
    return ArtifactIdentity(
        provider="lbg",
        endpoint_identity="lbg://production",
        project_id="42",
        record_id="1",
        image_url="registry/task:fixed",
        digest="sha256:" + "a" * 64,
    )


def _builder_artifact() -> ArtifactIdentity:
    return ArtifactIdentity(
        provider="lbg",
        endpoint_identity="lbg://production",
        project_id="42",
        record_id="builder-1",
        image_url="registry/labwright:fixed",
        digest="sha256:" + "c" * 64,
    )


def _request(root: Path) -> None:
    FileLabwrightRegistry(root).request_delta(
        Actor.RESEARCHER,
        EnvironmentDeltaRequest(
            request_id="delta-1",
            run_id=RUN_ID,
            question_revision=REVISION,
            kind="package",
            name="scipy",
            version_constraint="==1.14.0",
            reason="需要稀疏求解器",
            researcher_request_id=RESEARCHER_REQUEST_ID,
            sandbox_id=SANDBOX_ID,
        ),
    )


def _runtime(**changes) -> RuntimeSnapshot:
    values = {
        "request_id": "builder-runtime-1",
        "sandbox_id": "builder-sandbox-1",
        "artifact": _builder_artifact(),
        "started_at": "2026-08-24T00:00:00+00:00",
        "status": "READY",
    }
    return RuntimeSnapshot(**(values | changes))


def _source_failure_trace(tmp_path: Path) -> Path:
    path = tmp_path / "source-failure-trace.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "classification": "ENVIRONMENT_FAILURE",
        "run_id": RUN_ID,
        "question_revision": REVISION,
        "package_sha256": PACKAGE_SHA256,
        "researcher_request_id": RESEARCHER_REQUEST_ID,
        "sandbox_id": SANDBOX_ID,
        "baseline_identity_sha256": artifact_identity_sha256(_artifact()),
    }), encoding="utf-8")
    return path


def _bound_delta_evidence(
    tmp_path: Path,
    *,
    request_sha256: str,
    **changes,
) -> Path:
    inventory_before = tmp_path / "inventory-before.json"
    inventory_before.write_text('{"packages":[]}', encoding="utf-8")
    inventory_after = tmp_path / "inventory-after.json"
    inventory_after.write_text('{"packages":["scipy==1.14.0"]}', encoding="utf-8")
    probes = tmp_path / "probes.json"
    probes.write_text('{"import_scipy":true}', encoding="utf-8")
    source_trace = _source_failure_trace(tmp_path)
    values = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "question_revision": REVISION,
        "package_sha256": PACKAGE_SHA256,
        "researcher_request_id": RESEARCHER_REQUEST_ID,
        "sandbox_id": SANDBOX_ID,
        "request_sha256": request_sha256,
        "baseline_artifact": asdict(_artifact()),
        "baseline_identity_sha256": artifact_identity_sha256(_artifact()),
        "builder_runtime_request_id": "builder-runtime-1",
        "builder_sandbox_id": "builder-sandbox-1",
        "builder_identity_sha256": artifact_identity_sha256(_builder_artifact()),
        "capability_name": "scipy",
        "capability_version": "1.14.0",
        "source_trace": {"path": str(source_trace), "sha256": _sha256(source_trace)},
        "inventory_before": {
            "path": str(inventory_before),
            "sha256": _sha256(inventory_before),
        },
        "inventory_after": {
            "path": str(inventory_after),
            "sha256": _sha256(inventory_after),
        },
        "probes": {"path": str(probes), "sha256": _sha256(probes)},
    }
    path = tmp_path / "delta-evidence.json"
    path.write_text(json.dumps(values | changes), encoding="utf-8")
    return path


def _scientific_trace(
    tmp_path: Path,
    *,
    runtime_delta_request_ids: tuple[str, ...] = (),
) -> Path:
    path = tmp_path / "scientific-trace.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "classification": "SCIENTIFIC_RESULT",
                "run_id": RUN_ID,
                "question_revision": REVISION,
                "package_sha256": PACKAGE_SHA256,
                "researcher_request_id": "fresh-retry-request-1",
                "sandbox_id": "fresh-retry-sandbox-1",
                "baseline_identity_sha256": artifact_identity_sha256(_artifact()),
                "runtime_delta_request_ids": list(runtime_delta_request_ids),
            }
        ),
        encoding="utf-8",
    )
    return path


def _completed_delta(tmp_path: Path) -> tuple[FileLabwrightRuntimeService, DeltaReceipt]:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)
    claim = service.claim(
        Actor.LABWRIGHT,
        "delta-1",
        worker_id="worker-1",
        fencing_token="fence-1",
    )
    evidence = _bound_delta_evidence(tmp_path, request_sha256=claim.request_sha256)
    receipt = service.complete(
        Actor.LABWRIGHT,
        "delta-1",
        fencing_token="fence-1",
        runtime=_runtime(),
        capability_version="1.14.0",
        evidence_path=evidence,
    )
    return service, receipt


def test_labwright_binds_source_failure_and_independent_builder_runtime(
    tmp_path: Path,
) -> None:
    service, receipt = _completed_delta(tmp_path)

    assert receipt.run_id == RUN_ID
    assert receipt.question_revision == REVISION
    assert receipt.package_sha256 == PACKAGE_SHA256
    assert receipt.source_researcher_request_id == RESEARCHER_REQUEST_ID
    assert receipt.source_sandbox_id == SANDBOX_ID
    assert receipt.builder_runtime_request_id == "builder-runtime-1"
    assert receipt.builder_sandbox_id == "builder-sandbox-1"
    assert receipt.baseline_artifact == _artifact()
    assert receipt.baseline_identity_sha256 == artifact_identity_sha256(_artifact())
    assert receipt.builder_artifact == _builder_artifact()
    assert receipt.builder_identity_sha256 == artifact_identity_sha256(_builder_artifact())
    assert receipt.inventory_before_sha256 != receipt.inventory_after_sha256
    assert service.resolve("delta-1") == receipt


def test_same_fence_claim_is_idempotent_and_preserves_recovery_start(tmp_path: Path) -> None:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)

    first = service.claim(
        Actor.LABWRIGHT, "delta-1", worker_id="worker-1", fencing_token="fence-1"
    )
    replay = service.claim(
        Actor.LABWRIGHT, "delta-1", worker_id="worker-1", fencing_token="fence-1"
    )

    assert replay == first


@pytest.mark.parametrize(
    "runtime_change,message",
    [
        ({"status": "CLOSED"}, "READY runtime"),
    ],
)
def test_labwright_rejects_unready_builder_runtime(
    tmp_path: Path,
    runtime_change: dict,
    message: str,
) -> None:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)
    claim = service.claim(
        Actor.LABWRIGHT, "delta-1", worker_id="worker-1", fencing_token="fence-1"
    )
    evidence = _bound_delta_evidence(tmp_path, request_sha256=claim.request_sha256)

    with pytest.raises(ContractError, match=message):
        service.complete(
            Actor.LABWRIGHT,
            "delta-1",
            fencing_token="fence-1",
            runtime=_runtime(**runtime_change),
            capability_version="1.14.0",
            evidence_path=evidence,
        )


@pytest.mark.parametrize(
    "evidence_change,message",
    [
        ({"run_id": "another-run"}, "run or revision"),
        ({"package_sha256": "bad"}, "package SHA-256"),
        ({"request_sha256": "0" * 64}, "request SHA-256"),
        ({"baseline_identity_sha256": "0" * 64}, "baseline identity"),
        ({"builder_identity_sha256": "0" * 64}, "builder identity"),
        ({"capability_version": "different"}, "capability version"),
    ],
)
def test_delta_completion_rejects_unbound_evidence(
    tmp_path: Path,
    evidence_change: dict,
    message: str,
) -> None:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)
    claim = service.claim(
        Actor.LABWRIGHT, "delta-1", worker_id="worker-1", fencing_token="fence-1"
    )
    changes = dict(evidence_change)
    request_sha256 = changes.pop("request_sha256", claim.request_sha256)
    evidence = _bound_delta_evidence(
        tmp_path,
        request_sha256=request_sha256,
        **changes,
    )

    with pytest.raises(ContractError, match=message):
        service.complete(
            Actor.LABWRIGHT,
            "delta-1",
            fencing_token="fence-1",
            runtime=_runtime(),
            capability_version="1.14.0",
            evidence_path=evidence,
        )


def test_delta_completion_rejects_changed_inventory_or_probe(tmp_path: Path) -> None:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)
    claim = service.claim(
        Actor.LABWRIGHT, "delta-1", worker_id="worker-1", fencing_token="fence-1"
    )
    evidence = _bound_delta_evidence(tmp_path, request_sha256=claim.request_sha256)
    value = json.loads(evidence.read_text(encoding="utf-8"))
    Path(value["inventory_after"]["path"]).write_text("changed", encoding="utf-8")

    with pytest.raises(ContractError, match="inventory_after evidence changed"):
        service.complete(
            Actor.LABWRIGHT,
            "delta-1",
            fencing_token="fence-1",
            runtime=_runtime(),
            capability_version="1.14.0",
            evidence_path=evidence,
        )


def test_runtime_closure_allows_zero_delta_without_stable_environment_key(
    tmp_path: Path,
) -> None:
    service = FileLabwrightRuntimeService(tmp_path / "labwright")
    trace = _scientific_trace(tmp_path)
    output = tmp_path / "seal-plan.json"

    plan = service.create_seal_plan(
        Actor.LABWRIGHT,
        run_id=RUN_ID,
        question_revision=REVISION,
        package_sha256=PACKAGE_SHA256,
        baseline_artifact=_artifact(),
        scientific_trace_path=trace,
        request_ids=(),
        output_path=output,
    )

    assert output.is_file()
    assert plan.package_sha256 == PACKAGE_SHA256
    assert plan.baseline_artifact == _artifact()
    assert plan.delta_receipts == ()
    assert "base_environment_key" not in plan.to_dict()


def test_runtime_closure_accepts_only_matching_delta_receipts(tmp_path: Path) -> None:
    service, receipt = _completed_delta(tmp_path)
    trace = _scientific_trace(tmp_path, runtime_delta_request_ids=("delta-1",))

    plan = service.create_seal_plan(
        Actor.LABWRIGHT,
        run_id=RUN_ID,
        question_revision=REVISION,
        package_sha256=PACKAGE_SHA256,
        baseline_artifact=_artifact(),
        scientific_trace_path=trace,
        request_ids=("delta-1",),
        output_path=tmp_path / "seal-plan.json",
    )

    assert plan.delta_receipts == (receipt.to_dict(),)


def test_runtime_closure_rejects_unapplied_delta_in_fresh_retry_trace(tmp_path: Path) -> None:
    service, _ = _completed_delta(tmp_path)
    trace = _scientific_trace(tmp_path)

    with pytest.raises(ContractError, match="delta list does not match"):
        service.create_seal_plan(
            Actor.LABWRIGHT,
            run_id=RUN_ID,
            question_revision=REVISION,
            package_sha256=PACKAGE_SHA256,
            baseline_artifact=_artifact(),
            scientific_trace_path=trace,
            request_ids=("delta-1",),
            output_path=tmp_path / "seal-plan.json",
        )


def test_delta_receipt_and_seal_plan_validate_contracts(tmp_path: Path) -> None:
    _, receipt = _completed_delta(tmp_path)
    with pytest.raises(ContractError, match="package SHA-256"):
        DeltaReceipt(**(receipt.__dict__ | {"package_sha256": "bad"})).validate()
    with pytest.raises(ContractError, match="baseline identity"):
        DeltaReceipt(
            **(receipt.__dict__ | {"baseline_identity_sha256": "0" * 64})
        ).validate()
    with pytest.raises(ContractError, match="only READY"):
        DeltaReceipt(**(receipt.__dict__ | {"state": DeltaState.FAILED})).validate()

    trace = _scientific_trace(tmp_path)
    plan = ImageSealPlan(
        run_id=RUN_ID,
        question_revision=REVISION,
        package_sha256=PACKAGE_SHA256,
        baseline_artifact=_artifact(),
        baseline_identity_sha256=artifact_identity_sha256(_artifact()),
        scientific_trace_path=str(trace),
        scientific_trace_sha256=_sha256(trace),
        delta_receipts=(),
        created_at="2026-08-24T00:00:00+00:00",
    )
    plan.validate()
    with pytest.raises(ContractError, match="package SHA-256"):
        ImageSealPlan(**(plan.__dict__ | {"package_sha256": "bad"})).validate()


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
            run_id=RUN_ID,
            question_revision=REVISION,
            package_sha256=PACKAGE_SHA256,
            baseline_artifact=_artifact(),
            scientific_trace_path=_scientific_trace(tmp_path),
            request_ids=(),
            output_path=tmp_path / "plan.json",
        )


def test_delta_receipt_rejects_identity_digest_and_recovery_timing_drift(
    tmp_path: Path,
) -> None:
    _, receipt = _completed_delta(tmp_path)
    without_digest = ArtifactIdentity(**(asdict(receipt.baseline_artifact) | {"digest": None}))

    with pytest.raises(ContractError, match="identity is incomplete"):
        DeltaReceipt(**(receipt.__dict__ | {"request_id": ""})).validate()
    with pytest.raises(ContractError, match="immutable digest"):
        DeltaReceipt(
            **(
                receipt.__dict__
                | {
                    "baseline_artifact": without_digest,
                    "baseline_identity_sha256": artifact_identity_sha256(without_digest),
                }
            )
        ).validate()
    with pytest.raises(ContractError, match="recovery duration"):
        DeltaReceipt(
            **(
                receipt.__dict__
                | {"recovery_duration_sec": float(receipt.recovery_duration_sec) + 1}
            )
        ).validate()
    with pytest.raises(ContractError, match="invalid recovery start timestamp"):
        DeltaReceipt(
            **(receipt.__dict__ | {"recovery_started_at": "not-a-time"})
        ).validate()
    DeltaReceipt(
        **(
            receipt.__dict__
            | {
                "recovery_started_at": None,
                "recovery_finished_at": None,
                "recovery_duration_sec": None,
            }
        )
    ).validate()


def test_image_seal_plan_rejects_identity_baseline_and_receipt_drift(tmp_path: Path) -> None:
    _, receipt = _completed_delta(tmp_path)
    trace = _scientific_trace(tmp_path)
    plan = ImageSealPlan(
        run_id=RUN_ID,
        question_revision=REVISION,
        package_sha256=PACKAGE_SHA256,
        baseline_artifact=_artifact(),
        baseline_identity_sha256=artifact_identity_sha256(_artifact()),
        scientific_trace_path=str(trace),
        scientific_trace_sha256=_sha256(trace),
        delta_receipts=(),
        created_at="2026-08-24T00:00:00+00:00",
    )

    with pytest.raises(ContractError, match="identity is incomplete"):
        ImageSealPlan(**(plan.__dict__ | {"run_id": ""})).validate()
    without_digest = ArtifactIdentity(**(asdict(_artifact()) | {"digest": None}))
    with pytest.raises(ContractError, match="immutable digest"):
        ImageSealPlan(
            **(
                plan.__dict__
                | {
                    "baseline_artifact": without_digest,
                    "baseline_identity_sha256": artifact_identity_sha256(without_digest),
                }
            )
        ).validate()
    with pytest.raises(ContractError, match="baseline identity does not match"):
        ImageSealPlan(
            **(plan.__dict__ | {"baseline_identity_sha256": "0" * 64})
        ).validate()
    other_receipt = DeltaReceipt(
        **(receipt.__dict__ | {"package_sha256": "c" * 64})
    )
    with pytest.raises(ContractError, match="another runtime closure"):
        ImageSealPlan(
            **(plan.__dict__ | {"delta_receipts": (other_receipt.to_dict(),)})
        ).validate()


def test_claim_and_complete_reject_invalid_lifecycle_transitions(tmp_path: Path) -> None:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)
    placeholder = tmp_path / "placeholder.json"
    placeholder.write_text("{}", encoding="utf-8")

    with pytest.raises(ContractError, match="not being applied"):
        service.complete(
            Actor.LABWRIGHT,
            "delta-1",
            fencing_token="fence-1",
            runtime=_runtime(),
            capability_version="1.14.0",
            evidence_path=placeholder,
        )
    claim = service.claim(
        Actor.LABWRIGHT, "delta-1", worker_id="worker-1", fencing_token="fence-1"
    )
    with pytest.raises(ContractError, match="another fence"):
        service.claim(
            Actor.LABWRIGHT, "delta-1", worker_id="worker-2", fencing_token="fence-1"
        )
    with pytest.raises(ContractError, match="fence does not match"):
        service.complete(
            Actor.LABWRIGHT,
            "delta-1",
            fencing_token="wrong",
            runtime=_runtime(),
            capability_version="1.14.0",
            evidence_path=placeholder,
        )
    with pytest.raises(ContractError, match="only Labwright"):
        service.complete(
            Actor.TEACHER,
            "delta-1",
            fencing_token="fence-1",
            runtime=_runtime(),
            capability_version="1.14.0",
            evidence_path=placeholder,
        )
    evidence = _bound_delta_evidence(tmp_path, request_sha256=claim.request_sha256)
    service.complete(
        Actor.LABWRIGHT,
        "delta-1",
        fencing_token="fence-1",
        runtime=_runtime(),
        capability_version="1.14.0",
        evidence_path=evidence,
    )
    with pytest.raises(ContractError, match="cannot be claimed from READY"):
        service.claim(
            Actor.LABWRIGHT, "delta-1", worker_id="worker-1", fencing_token="fence-1"
        )


@pytest.mark.parametrize(
    "change,message",
    [
        ({"researcher_request_id": "another"}, "another Researcher runtime"),
        ({"capability_name": "numpy"}, "capability name"),
        ({"unexpected": True}, "schema is incomplete"),
        ({"inventory_before": []}, "inventory_before evidence reference"),
        ({"inventory_before": {"path": 1, "sha256": 2}}, "inventory_before evidence reference"),
    ],
)
def test_delta_evidence_schema_fails_closed(
    tmp_path: Path,
    change: dict,
    message: str,
) -> None:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)
    claim = service.claim(
        Actor.LABWRIGHT, "delta-1", worker_id="worker-1", fencing_token="fence-1"
    )
    evidence = _bound_delta_evidence(
        tmp_path,
        request_sha256=claim.request_sha256,
        **change,
    )

    with pytest.raises(ContractError, match=message):
        service.complete(
            Actor.LABWRIGHT,
            "delta-1",
            fencing_token="fence-1",
            runtime=_runtime(),
            capability_version="1.14.0",
            evidence_path=evidence,
        )


@pytest.mark.parametrize(
    "raw,message",
    [
        ('{"schema_version":2,"schema_version":2}', "duplicate JSON evidence key"),
        ('{"schema_version":NaN}', "non-finite JSON evidence number"),
        ("[]", "JSON evidence must be an object"),
    ],
)
def test_delta_evidence_strict_json_decoder(
    tmp_path: Path,
    raw: str,
    message: str,
) -> None:
    root = tmp_path / "labwright"
    _request(root)
    service = FileLabwrightRuntimeService(root)
    service.claim(
        Actor.LABWRIGHT, "delta-1", worker_id="worker-1", fencing_token="fence-1"
    )
    evidence = tmp_path / "bad-evidence.json"
    evidence.write_text(raw, encoding="utf-8")

    with pytest.raises(ContractError, match=message):
        service.complete(
            Actor.LABWRIGHT,
            "delta-1",
            fencing_token="fence-1",
            runtime=_runtime(),
            capability_version="1.14.0",
            evidence_path=evidence,
        )


def test_runtime_closure_rejects_duplicate_delta_and_bad_trace(tmp_path: Path) -> None:
    service, _ = _completed_delta(tmp_path)
    trace = _scientific_trace(tmp_path)
    with pytest.raises(ContractError, match="cannot repeat"):
        service.create_seal_plan(
            Actor.LABWRIGHT,
            run_id=RUN_ID,
            question_revision=REVISION,
            package_sha256=PACKAGE_SHA256,
            baseline_artifact=_artifact(),
            scientific_trace_path=trace,
            request_ids=("delta-1", "delta-1"),
            output_path=tmp_path / "plan.json",
        )

    trace.write_text('{"classification":"PLATFORM_FAILURE"}', encoding="utf-8")
    with pytest.raises(ContractError, match="does not bind"):
        service.create_seal_plan(
            Actor.LABWRIGHT,
            run_id=RUN_ID,
            question_revision=REVISION,
            package_sha256=PACKAGE_SHA256,
            baseline_artifact=_artifact(),
            scientific_trace_path=trace,
            request_ids=(),
            output_path=tmp_path / "plan.json",
        )

    trace.write_text(json.dumps({
        "schema_version": 1,
        "classification": "SCIENTIFIC_RESULT",
        "run_id": RUN_ID,
        "question_revision": REVISION,
        "package_sha256": PACKAGE_SHA256,
        "baseline_identity_sha256": artifact_identity_sha256(_artifact()),
    }), encoding="utf-8")
    with pytest.raises(ContractError, match="lacks Researcher execution identity"):
        service.create_seal_plan(
            Actor.LABWRIGHT,
            run_id=RUN_ID,
            question_revision=REVISION,
            package_sha256=PACKAGE_SHA256,
            baseline_artifact=_artifact(),
            scientific_trace_path=trace,
            request_ids=(),
            output_path=tmp_path / "plan.json",
        )

    with pytest.raises(ContractError, match="JSON evidence is missing"):
        service.create_seal_plan(
            Actor.LABWRIGHT,
            run_id=RUN_ID,
            question_revision=REVISION,
            package_sha256=PACKAGE_SHA256,
            baseline_artifact=_artifact(),
            scientific_trace_path=tmp_path / "missing.json",
            request_ids=(),
            output_path=tmp_path / "plan.json",
        )

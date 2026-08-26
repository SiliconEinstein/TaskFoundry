from __future__ import annotations

import hashlib
import json

import pytest

from taskfoundry.labwright import (
    ArtifactIdentity,
    BaseImagePolicy,
    DEFAULT_CPU_BASE_IMAGE,
    EnvironmentDeltaRequest,
    FileLabwrightRegistry,
    LabwrightError,
)
from taskfoundry.model import Actor, ContractError


def manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "status": "ENVIRONMENT_READY",
                "spec_sha256": "b" * 64,
                "image": {
                    "provider": "lbg",
                    "record_id": 12,
                    "url": "registry.example/task:fixed",
                    "immutable": True,
                    "reproducibility_digest": "a" * 64,
                    "digest": None,
                },
                "resources": [{"sha256": "c" * 64}],
            }
        )
    )
    return path


def clean_evidence(tmp_path):
    path = tmp_path / "clean.json"
    exclusions = [
        {"id": check_id, "passed": True, "exit_code": 0, "observed_count": 0}
        for check_id in (
            "exclusion:app-allowlist",
            "exclusion:task-privileged-material",
            "exclusion:harness-executables",
            "exclusion:credential-material",
            "exclusion:researcher-residue",
        )
    ]
    validation = [{"id": "tool:python", "passed": True, "exit_code": 0}]
    path.write_text(json.dumps({
        "image_record_id": 12,
        "image_url": "registry.example/task:fixed",
        "clean_sandboxes_verified": 2,
        "install_commands_executed": 0,
        "public_asset_uploads_executed": 0,
        "runs": [
            {"sandbox_id": "s1", "validation": validation, "exclusions": exclusions},
            {"sandbox_id": "s2", "validation": validation, "exclusions": exclusions},
        ],
    }))
    return path


def attach_runtime_closure(tmp_path, manifest_path):
    closure = tmp_path / "runtime-closure.json"
    closure.write_text('{"schema_version":2}')
    value = json.loads(manifest_path.read_text())
    value["runtime_closure"] = {
        "path": str(closure.resolve()),
        "sha256": hashlib.sha256(closure.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(value))
    return closure


def test_import_and_resolve_stable_manifest(tmp_path) -> None:
    registry = FileLabwrightRegistry(tmp_path / "state")
    receipt = registry.import_stable(
        manifest(tmp_path), clean_evidence_path=clean_evidence(tmp_path), fencing_token="fence-1",
        endpoint_identity="lbg://production", project_id="42"
    )
    assert receipt.lifecycle == "STABLE"
    assert registry.resolve(receipt.environment_key) == receipt
    assert registry.import_stable(
        manifest(tmp_path), clean_evidence_path=clean_evidence(tmp_path), fencing_token="fence-1",
        endpoint_identity="lbg://production", project_id="42"
    ) == receipt


def test_runtime_first_stable_receipt_binds_closure(tmp_path) -> None:
    source = manifest(tmp_path)
    closure = attach_runtime_closure(tmp_path, source)
    registry = FileLabwrightRegistry(tmp_path / "state")

    receipt = registry.import_stable(
        source,
        clean_evidence_path=clean_evidence(tmp_path),
        fencing_token="fence-1",
        endpoint_identity="lbg://production",
        project_id="42",
    )

    assert receipt.schema_version == 2
    assert receipt.runtime_closure_path == str(closure.resolve())
    assert receipt.runtime_closure_sha256 == hashlib.sha256(closure.read_bytes()).hexdigest()


def test_resolve_detects_manifest_drift(tmp_path) -> None:
    source = manifest(tmp_path)
    registry = FileLabwrightRegistry(tmp_path / "state")
    receipt = registry.import_stable(source, clean_evidence_path=clean_evidence(tmp_path),
                                     fencing_token="fence-1", endpoint_identity="lbg://production", project_id="42")
    source.write_text("{}")
    with pytest.raises(LabwrightError, match="changed"):
        registry.resolve(receipt.environment_key)


def test_registry_rejects_unready_and_unknown_environment(tmp_path) -> None:
    source = manifest(tmp_path)
    value = json.loads(source.read_text())
    value["status"] = "BUILDING"
    source.write_text(json.dumps(value))
    registry = FileLabwrightRegistry(tmp_path / "state")
    with pytest.raises(LabwrightError, match="not an immutable ready"):
        registry.import_stable(source, clean_evidence_path=clean_evidence(tmp_path),
                               fencing_token="fence-1", endpoint_identity="lbg://production", project_id="42")
    with pytest.raises(LabwrightError, match="not registered"):
        registry.resolve("a" * 64)


@pytest.mark.parametrize(
    "change,message",
    [
        ({"image_url": "registry/task:latest"}, "latest"),
        ({"endpoint_identity": "http://unsafe"}, "https or lbg"),
        ({"digest": "bad"}, "sha256"),
    ],
)
def test_artifact_identity_guards(change, message) -> None:
    values = {
        "provider": "lbg",
        "endpoint_identity": "lbg://production",
        "project_id": "42",
        "record_id": "12",
        "image_url": "registry/task:fixed",
        "digest": None,
    }
    with pytest.raises(ContractError, match=message):
        ArtifactIdentity(**(values | change)).validate()


def test_only_researcher_can_request_typed_delta(tmp_path) -> None:
    registry = FileLabwrightRegistry(tmp_path / "state")
    request = EnvironmentDeltaRequest(
        request_id="delta-1",
        run_id="run-1",
        question_revision="r1",
        kind="package",
        name="scipy",
        version_constraint="==1.14.0",
        reason="sparse solver",
        researcher_request_id="researcher-1",
        sandbox_id="sandbox-1",
    )
    path = registry.request_delta(Actor.RESEARCHER, request)
    assert registry.request_delta(Actor.RESEARCHER, request) == path
    with pytest.raises(LabwrightError, match="Researcher"):
        registry.request_delta(Actor.TEACHER, request)


@pytest.mark.parametrize(
    "change,message",
    [
        ({"name": "pip install scipy"}, "capability"),
        ({"source_uri": "http://example/x", "source_sha256": "a" * 64}, "HTTPS"),
        ({"source_uri": "https://example/x", "source_sha256": None}, "SHA-256"),
    ],
)
def test_delta_rejects_unsafe_input(change, message) -> None:
    values = {
        "request_id": "delta-1",
        "run_id": "run-1",
        "question_revision": "r1",
        "kind": "data",
        "name": "dataset",
        "version_constraint": "1",
        "reason": "needed",
        "researcher_request_id": "researcher-1",
        "sandbox_id": "sandbox-1",
        "source_uri": None,
        "source_sha256": None,
    }
    with pytest.raises(ContractError, match=message):
        EnvironmentDeltaRequest(**(values | change)).validate()


def test_base_policy_accepts_schema_compatible_harness_neutral_spec() -> None:
    BaseImagePolicy().validate_spec(
        {
            "base_image": DEFAULT_CPU_BASE_IMAGE,
            "platform": "linux/amd64",
        }
    )


@pytest.mark.parametrize(
    "change,message",
    [
        ({"platform": "linux/arm64"}, "linux/amd64"),
        ({"base_image": "registry/task:latest"}, "latest"),
        ({"base_image": ""}, "non-empty"),
    ],
)
def test_base_policy_rejects_unsafe_or_incompatible_spec(change, message) -> None:
    spec = {
        "base_image": DEFAULT_CPU_BASE_IMAGE,
        "platform": "linux/amd64",
    }
    with pytest.raises(ContractError, match=message):
        BaseImagePolicy().validate_spec(spec | change)


def test_base_policy_accepts_specialized_pinned_tag_from_schema() -> None:
    BaseImagePolicy().validate_spec(
        {
            "base_image": "registry.example/cuda:12.6.3-runtime",
            "platform": "linux/amd64",
        }
    )


def test_clean_evidence_requires_every_harness_neutral_exclusion(tmp_path) -> None:
    evidence_path = clean_evidence(tmp_path)
    evidence = json.loads(evidence_path.read_text())
    evidence["runs"][1]["exclusions"] = evidence["runs"][1]["exclusions"][:-1]
    evidence_path.write_text(json.dumps(evidence))
    artifact = ArtifactIdentity("lbg", "lbg://prod", "42", "12", "registry.example/task:fixed")
    with pytest.raises(LabwrightError, match="negative scans"):
        FileLabwrightRegistry._verify_clean_evidence(evidence_path, artifact)


def test_legacy_positive_harness_checks_do_not_replace_negative_scans(tmp_path) -> None:
    evidence_path = clean_evidence(tmp_path)
    evidence = json.loads(evidence_path.read_text())
    legacy = [
        {"id": "harness-codex", "passed": True, "exit_code": 0},
        {"id": "harness-claude", "passed": True, "exit_code": 0},
    ]
    for run in evidence["runs"]:
        run["validation"] = legacy
        run["exclusions"] = legacy
    evidence_path.write_text(json.dumps(evidence))
    artifact = ArtifactIdentity("lbg", "lbg://prod", "42", "12", "registry.example/task:fixed")
    with pytest.raises(LabwrightError, match="negative scans"):
        FileLabwrightRegistry._verify_clean_evidence(evidence_path, artifact)


def test_clean_evidence_rejects_nonzero_forbidden_material_count(tmp_path) -> None:
    evidence_path = clean_evidence(tmp_path)
    evidence = json.loads(evidence_path.read_text())
    evidence["runs"][0]["exclusions"][2]["observed_count"] = 1
    evidence_path.write_text(json.dumps(evidence))
    artifact = ArtifactIdentity("lbg", "lbg://prod", "42", "12", "registry.example/task:fixed")
    with pytest.raises(LabwrightError, match="forbidden material"):
        FileLabwrightRegistry._verify_clean_evidence(evidence_path, artifact)

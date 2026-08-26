from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import importlib
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys

import pytest

from taskfoundry.package import package_sha256
from taskfoundry.labwright import ArtifactIdentity, EnvironmentReceipt
from taskfoundry.labwright_runtime import ImageSealPlan, artifact_identity_sha256


SCRIPT_ROOT = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))
qfv = importlib.import_module("question_family_validation")
promoter = importlib.import_module("promote_question_family")
checker = importlib.import_module("check_new_question_publication")


def hold_promotion_lock(
    lock_path: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    """Hold a real process lock until the parent releases the worker."""
    with promoter.promotion_lock(Path(lock_path)):
        ready.set()
        release.wait(timeout=10)


def acquire_promotion_lock(
    lock_path: str,
    acquired: multiprocessing.synchronize.Event,
) -> None:
    """Signal only after a competing process lock has been acquired."""
    with promoter.promotion_lock(Path(lock_path)):
        acquired.set()


def make_package(root: Path, name: str) -> str:
    root.mkdir(parents=True)
    (root / "instruction.md").write_text("Read /app/environment/resources.yaml and solve the scientific task.\n")
    (root / "task.toml").write_text(
        'schema_version = "1.3"\n'
        'artifacts = ["/app/outputs"]\n'
        f'[task]\nname = "test/{name}"\n'
        '[agent]\ntimeout_sec = 3600\n'
        '[verifier]\ntimeout_sec = 300\nenvironment_mode = "separate"\n'
        '[environment]\ndocker_image = "registry/task@sha256:' + "a" * 64 + '"\nworkdir = "/app"\n'
    )
    environment = root / "environment"
    environment.mkdir()
    (environment / "resources.yaml").write_text("resources: []\n")
    solution = root / "solution"
    solution.mkdir()
    (solution / "solve.sh").write_text("#!/bin/sh\nexit 0\n")
    (solution / "solve.sh").chmod(0o755)
    tests = root / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("#!/bin/sh\nmkdir -p /logs/verifier\necho 1 > /logs/verifier/reward.txt\n")
    (tests / "test.sh").chmod(0o755)
    (tests / "private-reference.json").write_text('{"answer":42}\n')
    (tests / "grader.py").write_text("def grade(value):\n    return float(value == 42)\n")
    (root / "OUTPUT_CONTRACT.json").write_text('{"required":["answer"]}\n')
    contract = {
        "schema_version": 1,
        "ground_truth": [
            {
                "path": "tests/private-reference.json",
                "sha256": hashlib.sha256((tests / "private-reference.json").read_bytes()).hexdigest(),
            }
        ],
        "grader": [
            {
                "path": "tests/grader.py",
                "sha256": hashlib.sha256((tests / "grader.py").read_bytes()).hexdigest(),
            }
        ],
        "tolerance": {"absolute": 1e-9, "relative": 1e-7},
        "weights": {"decision": 0.5, "numeric": 0.5},
        "output_contract": [
            {
                "path": "OUTPUT_CONTRACT.json",
                "sha256": hashlib.sha256((root / "OUTPUT_CONTRACT.json").read_bytes()).hexdigest(),
            }
        ],
    }
    (root / qfv.SCIENTIFIC_CONTRACT).write_bytes(qfv.canonical_json_bytes(contract))
    return package_sha256(root)


def write_evidence(
    evidence_root: Path,
    *,
    name: str,
    evidence_type: str,
    package: str,
    level: str,
    contract: str,
    **extra: object,
) -> dict[str, str]:
    value = {
        "schema_version": 1,
        "evidence_type": evidence_type,
        "question_id": "Q03",
        "package_sha256": package,
        "scientific_contract_sha256": contract,
        "level": level,
        **extra,
    }
    path = evidence_root / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(qfv.canonical_json_bytes(value))
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def execution(prefix: str) -> dict[str, str]:
    return {
        "job_id": f"job-{prefix}",
        "trial_id": f"trial-{prefix}",
        "sandbox_id": f"sandbox-{prefix}",
        "session_id": f"session-{prefix}",
    }


def sealed(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def researcher_binding(
    evidence_root: Path,
    *,
    package_path: Path,
    package: str,
    prefix: str,
    mode: str,
    attempt_index: int,
    context_digests: list[str],
) -> dict[str, object]:
    job_config = evidence_root / f"{prefix}-job.json"
    job_config.write_text('{"job":"frozen"}\n')
    request = {
        "request_id": f"request-{prefix}",
        "run_id": "run-q03",
        "question_revision": "r1",
        "attempt_index": attempt_index,
        "mode": mode,
        "package_path": str(package_path),
        "package_sha256": package,
        "job_config_path": str(job_config),
        "job_config_sha256": hashlib.sha256(job_config.read_bytes()).hexdigest(),
        "context_digests": context_digests,
        "researcher_thread_id": f"thread-{prefix}",
        "harness": "codex",
        "model": "matmaster/gpt-5.6-sol",
        "target_solution_time_sec": 1800,
        "scientific_timeout_sec": 3600,
        "schema_version": 1,
    }
    request_path = evidence_root / f"{prefix}-request.json"
    request_path.write_bytes(qfv.canonical_json_bytes(request))
    capability = {
        "schema_version": 1,
        "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
        "researcher_thread_id": request["researcher_thread_id"],
        "token_sha256": hashlib.sha256(f"token-{prefix}".encode()).hexdigest(),
        "status": "CONSUMED",
        "consumed_at": "2026-08-26T00:00:00+00:00",
    }
    capability_path = evidence_root / f"{prefix}-capability.json"
    capability_path.write_bytes(qfv.canonical_json_bytes(capability))
    return {
        "request_id": request["request_id"],
        "context_digests": context_digests,
        "request": sealed(request_path),
        "capability": sealed(capability_path),
        **execution(prefix),
    }


def formal(evidence_root: Path, *, name: str, package: str, level: str, contract: str) -> dict[str, str]:
    extra: dict[str, object] = {
        "verdict": "PASS",
        "reviewer_independent": True,
        "grader_pass": True,
        "security_pass": True,
        "source_pass": True,
        "provenance_pass": True,
        "scientific_contract_same_source": True,
    }
    if level != "high":
        extra["derived_only_from_reviewed_hint"] = True
    return write_evidence(
        evidence_root,
        name=name,
        evidence_type="formal-review",
        package=package,
        level=level,
        contract=contract,
        **extra,
    )


def perfect(evidence_root: Path, *, name: str, kind: str, package: str, level: str, contract: str) -> dict[str, str]:
    independent_key = "oracle_independent" if kind == "oracle" else "solver_independent"
    return write_evidence(
        evidence_root,
        name=name,
        evidence_type=f"{kind}-validation",
        package=package,
        level=level,
        contract=contract,
        verdict="PASS",
        score=1.0,
        **{independent_key: True},
    )


def reviewed_hint(
    evidence_root: Path,
    *,
    name: str,
    package: str,
    package_path: Path,
    contract: str,
    hint_sha: str,
    identity: str,
    hint_index: int,
    parent_hint_sha256: str | None,
) -> dict[str, object]:
    review = write_evidence(
        evidence_root,
        name=f"{name}-review",
        evidence_type="hint-review",
        package=package,
        level="high",
        contract=contract,
        verdict="PASS",
        contains_answer=False,
        hint_sha256=hint_sha,
        hint_index=hint_index,
        parent_hint_sha256=parent_hint_sha256,
    )
    result = write_evidence(
        evidence_root,
        name=f"{name}-result",
        evidence_type="hint-result",
        package=package,
        level="high",
        contract=contract,
        classification="SCIENTIFIC_RESULT",
        mode="hint",
        leakage_free=True,
        score=0.9,
        hint_sha256=hint_sha,
        hint_index=hint_index,
        parent_hint_sha256=parent_hint_sha256,
        **researcher_binding(
            evidence_root,
            package_path=package_path,
            package=package,
            prefix=identity,
            mode="hint",
            attempt_index=hint_index,
            context_digests=[hint_sha],
        ),
    )
    return {"score": 0.9, "review_seal": review, "result_evidence": result}


def runtime_closure_evidence(
    evidence_root: Path,
    *,
    package: str,
    contract: str,
) -> dict[str, str]:
    baseline = ArtifactIdentity(
        "lbg",
        "lbg://production",
        "2309771",
        "baseline",
        "registry.example/base@sha256:" + "1" * 64,
        "sha256:" + "1" * 64,
    )
    trace = evidence_root / "runtime-scientific-trace.json"
    trace.write_bytes(
        qfv.canonical_json_bytes(
            {
                "schema_version": 1,
                "classification": "SCIENTIFIC_RESULT",
                "run_id": "run-q03",
                "question_revision": "r1",
                "package_sha256": package,
                "baseline_identity_sha256": artifact_identity_sha256(baseline),
                "researcher_request_id": "request-high-blind-1",
                "sandbox_id": "sandbox-high-blind-1",
                "runtime_delta_request_ids": [],
            }
        )
    )
    plan = ImageSealPlan(
        run_id="run-q03",
        question_revision="r1",
        package_sha256=package,
        baseline_artifact=baseline,
        baseline_identity_sha256=artifact_identity_sha256(baseline),
        scientific_trace_path=str(trace),
        scientific_trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),
        delta_receipts=(),
        created_at=datetime.now(UTC).isoformat(),
    )
    closure = evidence_root / "runtime-closure-plan.json"
    closure.write_bytes(qfv.canonical_json_bytes(plan.to_dict()))
    lock = evidence_root / "runtime-dependency-lock.json"
    lock.write_bytes(
        qfv.canonical_json_bytes(
            {"schema_version": 1, "packages": [{"name": "python", "version": "3.12.13"}]}
        )
    )
    artifact = ArtifactIdentity(
        "lbg",
        "lbg://production",
        "2309771",
        "stable-record",
        "registry.example/stable@sha256:" + "2" * 64,
        "sha256:" + "2" * 64,
    )
    manifest = evidence_root / "runtime-environment-manifest.json"
    manifest.write_bytes(
        qfv.canonical_json_bytes(
            {
                "status": "ENVIRONMENT_READY",
                "image": {
                    "provider": artifact.provider,
                    "record_id": artifact.record_id,
                    "url": artifact.image_url,
                    "digest": artifact.digest,
                    "immutable": True,
                },
                "runtime_closure": sealed(closure),
                "dependency_lock": sealed(lock),
                "resources": [],
            }
        )
    )
    receipt = EnvironmentReceipt(
        environment_key="3" * 64,
        lifecycle="STABLE",
        artifact=artifact,
        workdir="/app",
        manifest_path=str(manifest),
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        resource_digests=(),
        runtime_closure_path=str(closure),
        runtime_closure_sha256=hashlib.sha256(closure.read_bytes()).hexdigest(),
        schema_version=2,
    )
    receipt_path = evidence_root / "runtime-environment-receipt.json"
    receipt_path.write_bytes(qfv.canonical_json_bytes(receipt.to_dict()))
    exclusions = [
        {"id": check_id, "passed": True, "exit_code": 0, "observed_count": 0}
        for check_id in sorted(qfv.REQUIRED_CLEAN_EXCLUSIONS)
    ]
    clean = evidence_root / "runtime-clean-sandboxes.json"
    clean.write_bytes(
        qfv.canonical_json_bytes(
            {
                "image_record_id": artifact.record_id,
                "image_url": artifact.image_url,
                "clean_sandboxes_verified": 2,
                "install_commands_executed": 0,
                "public_asset_uploads_executed": 0,
                "runs": [
                    {
                        "sandbox_id": f"clean-{index}",
                        "validation": [{"id": "python", "passed": True, "exit_code": 0}],
                        "exclusions": exclusions,
                    }
                    for index in (1, 2)
                ],
            }
        )
    )
    return write_evidence(
        evidence_root,
        name="high-runtime",
        evidence_type="runtime-closure",
        package=package,
        level="high",
        contract=contract,
        verdict="PASS",
        runtime_closure=sealed(closure),
        environment_receipt=sealed(receipt_path),
        environment_manifest=sealed(manifest),
        dependency_lock=sealed(lock),
        clean_sandbox_evidence=sealed(clean),
    )


@pytest.fixture
def complete_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, object]]:
    workbench = tmp_path / "workbench"
    question_root = tmp_path / "questions"
    evidence_root = tmp_path / "evidence"
    for number in range(1, 33):
        (question_root / str(number) / "question-pack").mkdir(parents=True)
    monkeypatch.setattr(qfv, "WORKBENCH_ROOT", workbench)
    monkeypatch.setattr(qfv, "QUESTION_ROOT", question_root)
    monkeypatch.setattr(qfv, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(promoter, "QUESTION_ROOT", question_root)
    monkeypatch.setattr(checker, "QUESTION_ROOT", question_root)
    family = workbench / "q03" / "scientific-family"
    high_package = make_package(family / "high", "high")
    medium_package = make_package(family / "medium", "medium")
    contract = qfv.scientific_contract_sha256(family / "high")
    assert qfv.scientific_contract_sha256(family / "medium") == contract
    hint_sha = "d" * 64
    hint = reviewed_hint(
        evidence_root,
        name="high-hint",
        package=high_package,
        package_path=family / "high",
        contract=contract,
        hint_sha=hint_sha,
        identity="high-hint",
        hint_index=1,
        parent_hint_sha256=None,
    )
    blinds = []
    for index, score in enumerate((0.2, 0.4, 0.6), 1):
        evidence = write_evidence(
            evidence_root,
            name=f"high-blind-{index}",
            evidence_type="fresh-blind",
            package=high_package,
            level="high",
            contract=contract,
            classification="SCIENTIFIC_RESULT",
            mode="blind",
            leakage_free=True,
            score=score,
            **researcher_binding(
                evidence_root,
                package_path=family / "high",
                package=high_package,
                prefix=f"high-blind-{index}",
                mode="blind",
                attempt_index=index,
                context_digests=[],
            ),
        )
        blinds.append({"score": score, "evidence": evidence})
    high = {
        "status": "PASS_FINAL_PROGRESSION",
        "package_sha256": high_package,
        "scientific_contract_sha256": contract,
        "formal_reviewer": formal(evidence_root, name="high-formal", package=high_package, level="high", contract=contract),
        "oracle_validation": perfect(evidence_root, name="high-oracle", kind="oracle", package=high_package, level="high", contract=contract),
        "honest_validation": perfect(evidence_root, name="high-honest", kind="honest", package=high_package, level="high", contract=contract),
        "fresh_blinds": blinds,
        "successful_hint": hint,
        "runtime_closure": runtime_closure_evidence(
            evidence_root,
            package=high_package,
            contract=contract,
        ),
        "post_validation": write_evidence(evidence_root, name="high-post", evidence_type="post-validation", package=high_package, level="high", contract=contract, verdict="PASS_FINAL_PROGRESSION", reviewer_independent=True),
    }
    medium_result = write_evidence(
        evidence_root,
        name="medium-harbor",
        evidence_type="hint-result",
        package=medium_package,
        level="medium",
        contract=contract,
        classification="SCIENTIFIC_RESULT",
        mode="hint",
        leakage_free=True,
        score=0.93,
        hint_sha256=hint_sha,
        hint_index=1,
        parent_hint_sha256=None,
        **researcher_binding(
            evidence_root,
            package_path=family / "medium",
            package=medium_package,
            prefix="medium-harbor",
            mode="hint",
            attempt_index=1,
            context_digests=[hint_sha],
        ),
    )
    medium = {
        "status": "PASS_FINAL_PROGRESSION",
        "package_sha256": medium_package,
        "scientific_contract_sha256": contract,
        "derivation_hint": hint,
        "formal_reviewer": formal(evidence_root, name="medium-formal", package=medium_package, level="medium", contract=contract),
        "oracle_validation": perfect(evidence_root, name="medium-oracle", kind="oracle", package=medium_package, level="medium", contract=contract),
        "honest_validation": perfect(evidence_root, name="medium-honest", kind="honest", package=medium_package, level="medium", contract=contract),
        "fresh_harbor": [medium_result],
    }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "publication_mode": "family-v1",
        "question_id": "Q03",
        "family_slug": "scientific-family",
        "scientific_objective": "Select a transferable scientific method for held-out conditions.",
        "scientific_contract_sha256": contract,
        "levels": {"high": high, "medium": medium},
    }
    (family / "FAMILY_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return family, manifest


def rewrite_manifest(family: Path, manifest: dict[str, object]) -> None:
    (family / "FAMILY_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def rewrite_link(link: dict[str, str], value: dict[str, object]) -> None:
    path = Path(link["path"])
    path.write_bytes(qfv.canonical_json_bytes(value))
    link["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def retarget_medium_package(family: Path, manifest: dict[str, object]) -> str:
    medium = manifest["levels"]["medium"]  # type: ignore[index]
    package = package_sha256(family / "medium")
    medium["package_sha256"] = package
    links = [
        medium["formal_reviewer"],
        medium["oracle_validation"],
        medium["honest_validation"],
        *medium["fresh_harbor"],
    ]
    for link in links:
        value = json.loads(Path(link["path"]).read_text())
        value["package_sha256"] = package
        request_link = value.get("request")
        capability_link = value.get("capability")
        if isinstance(request_link, dict) and isinstance(capability_link, dict):
            request = json.loads(Path(request_link["path"]).read_text())
            request["package_sha256"] = package
            rewrite_link(request_link, request)
            capability = json.loads(Path(capability_link["path"]).read_text())
            capability["request_sha256"] = request_link["sha256"]
            rewrite_link(capability_link, capability)
        rewrite_link(link, value)
    return package


def test_complete_high_medium_family_passes(complete_family: tuple[Path, dict[str, object]]) -> None:
    family, _ = complete_family
    assert qfv.validate_family(3, family)["family_slug"] == "scientific-family"


def test_scientific_contract_is_recomputed_from_each_level(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    contract_path = family / "medium" / qfv.SCIENTIFIC_CONTRACT
    value = json.loads(contract_path.read_text())
    value["weights"] = {"decision": 0.6, "numeric": 0.4}
    contract_path.write_bytes(qfv.canonical_json_bytes(value))
    retarget_medium_package(family, manifest)
    rewrite_manifest(family, manifest)

    with pytest.raises(qfv.FamilyValidationError, match="实算摘要"):
        qfv.validate_family(3, family)


def test_runtime_closure_rejects_schema_v1_environment_receipt(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    runtime_link = manifest["levels"]["high"]["runtime_closure"]  # type: ignore[index]
    runtime = json.loads(Path(runtime_link["path"]).read_text())
    receipt_link = runtime["environment_receipt"]
    receipt = json.loads(Path(receipt_link["path"]).read_text())
    receipt.update(schema_version=1, runtime_closure_path=None, runtime_closure_sha256=None)
    rewrite_link(receipt_link, receipt)
    rewrite_link(runtime_link, runtime)
    rewrite_manifest(family, manifest)

    with pytest.raises(qfv.FamilyValidationError, match="schema-v2 EnvironmentReceipt"):
        qfv.validate_family(3, family)


def test_runtime_closure_rejects_manifest_lock_or_clean_sandbox_drift(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    runtime_link = manifest["levels"]["high"]["runtime_closure"]  # type: ignore[index]
    runtime = json.loads(Path(runtime_link["path"]).read_text())
    lock_link = runtime["dependency_lock"]
    lock_path = Path(lock_link["path"])
    lock_path.write_text('{"packages":[]}\n')
    lock_link["sha256"] = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    rewrite_link(runtime_link, runtime)
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="dependency lock"):
        qfv.validate_family(3, family)


def test_runtime_closure_requires_two_distinct_clean_sandboxes(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    runtime_link = manifest["levels"]["high"]["runtime_closure"]  # type: ignore[index]
    runtime = json.loads(Path(runtime_link["path"]).read_text())
    clean_link = runtime["clean_sandbox_evidence"]
    clean = json.loads(Path(clean_link["path"]).read_text())
    clean["runs"][1]["sandbox_id"] = clean["runs"][0]["sandbox_id"]
    rewrite_link(clean_link, clean)
    rewrite_link(runtime_link, runtime)
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="身份不完整或被复用"):
        qfv.validate_family(3, family)


def test_runtime_closure_requires_manifest_immutable_image_digest(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    runtime_link = manifest["levels"]["high"]["runtime_closure"]  # type: ignore[index]
    runtime = json.loads(Path(runtime_link["path"]).read_text())
    manifest_link = runtime["environment_manifest"]
    environment_manifest = json.loads(Path(manifest_link["path"]).read_text())
    environment_manifest["image"]["immutable"] = False
    rewrite_link(manifest_link, environment_manifest)
    receipt_link = runtime["environment_receipt"]
    receipt = json.loads(Path(receipt_link["path"]).read_text())
    receipt["manifest_sha256"] = manifest_link["sha256"]
    rewrite_link(receipt_link, receipt)
    rewrite_link(runtime_link, runtime)
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="immutable image"):
        qfv.validate_family(3, family)


def test_high_blind_at_threshold_is_too_easy(complete_family: tuple[Path, dict[str, object]]) -> None:
    family, manifest = complete_family
    manifest["levels"]["high"]["fresh_blinds"][0]["score"] = 0.85  # type: ignore[index]
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="TOO_EASY"):
        qfv.validate_family(3, family)


def test_medium_contract_must_match_high(complete_family: tuple[Path, dict[str, object]]) -> None:
    family, manifest = complete_family
    manifest["levels"]["medium"]["scientific_contract_sha256"] = "e" * 64  # type: ignore[index]
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="科学合同绑定"):
        qfv.validate_family(3, family)


def test_medium_must_derive_from_high_hint(complete_family: tuple[Path, dict[str, object]]) -> None:
    family, manifest = complete_family
    manifest["levels"]["medium"]["derivation_hint"]["review_seal"] = manifest["levels"]["high"]["formal_reviewer"]  # type: ignore[index]
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="evidence 身份不匹配"):
        qfv.validate_family(3, family)


def test_medium_fresh_harbor_must_pass(complete_family: tuple[Path, dict[str, object]]) -> None:
    family, manifest = complete_family
    link = manifest["levels"]["medium"]["fresh_harbor"][0]  # type: ignore[index]
    path = Path(link["path"])
    value = json.loads(path.read_text())
    value["score"] = 0.84
    path.write_bytes(qfv.canonical_json_bytes(value))
    link["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="超出"):
        qfv.validate_family(3, family)


def test_optional_low_level_passes(complete_family: tuple[Path, dict[str, object]]) -> None:
    family, manifest = complete_family
    contract = manifest["scientific_contract_sha256"]
    high_package = manifest["levels"]["high"]["package_sha256"]  # type: ignore[index]
    evidence_root = Path(manifest["levels"]["high"]["formal_reviewer"]["path"]).parent  # type: ignore[index]
    low_package = make_package(family / "low", "low")
    low_hint = reviewed_hint(
        evidence_root,
        name="low-hint",
        package=high_package,
        package_path=family / "high",
        contract=contract,
        hint_sha="f" * 64,
        identity="low-hint",
        hint_index=2,
        parent_hint_sha256="d" * 64,
    )
    low_harbor = write_evidence(
        evidence_root,
        name="low-harbor",
        evidence_type="hint-result",
        package=low_package,
        level="low",
        contract=contract,
        classification="SCIENTIFIC_RESULT",
        mode="hint",
        leakage_free=True,
        score=0.95,
        hint_sha256="f" * 64,
        hint_index=2,
        parent_hint_sha256="d" * 64,
        **researcher_binding(
            evidence_root,
            package_path=family / "low",
            package=low_package,
            prefix="low-harbor",
            mode="hint",
            attempt_index=2,
            context_digests=["f" * 64],
        ),
    )
    manifest["levels"]["low"] = {  # type: ignore[index]
        "status": "PASS_FINAL_PROGRESSION",
        "package_sha256": low_package,
        "scientific_contract_sha256": contract,
        "derivation_hint": low_hint,
        "formal_reviewer": formal(evidence_root, name="low-formal", package=low_package, level="low", contract=contract),
        "oracle_validation": perfect(evidence_root, name="low-oracle", kind="oracle", package=low_package, level="low", contract=contract),
        "honest_validation": perfect(evidence_root, name="low-honest", kind="honest", package=low_package, level="low", contract=contract),
        "fresh_harbor": [low_harbor],
    }
    rewrite_manifest(family, manifest)
    assert set(qfv.validate_family(3, family)["levels"]) == {"high", "medium", "low"}


def test_noncanonical_evidence_is_rejected(complete_family: tuple[Path, dict[str, object]]) -> None:
    family, manifest = complete_family
    link = manifest["levels"]["high"]["formal_reviewer"]  # type: ignore[index]
    path = Path(link["path"])
    value = json.loads(path.read_text())
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    link["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="不是 canonical JSON"):
        qfv.validate_family(3, family)


def test_promoter_and_checker_share_validator(complete_family: tuple[Path, dict[str, object]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    family, _ = complete_family
    assert promoter.validate_family is qfv.validate_family
    assert checker.validate_family is qfv.validate_family
    monkeypatch.setattr(promoter, "parse_args", lambda: argparse.Namespace(question=3, family=family, apply=False))
    assert promoter.main() == 0
    assert json.loads(capsys.readouterr().out)["validated"] is True


def test_promoter_applies_atomic_family_and_checker_reads_same_contract(
    complete_family: tuple[Path, dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    family, manifest = complete_family
    publication_root = qfv.QUESTION_ROOT / "3" / "question-pack"
    monkeypatch.setattr(promoter, "PROMOTION_EVIDENCE_ROOT", tmp_path / "promotions")
    monkeypatch.setattr(
        promoter,
        "parse_args",
        lambda: argparse.Namespace(question=3, family=family, apply=True),
    )
    assert promoter.main() == 0
    target = publication_root / manifest["family_slug"]
    assert target.is_dir()
    assert family.is_dir()
    assert {path.name for path in publication_root.iterdir()} == {manifest["family_slug"]}
    records = sorted((tmp_path / "promotions" / "q03").glob("*.json"))
    assert len(records) == 2
    assert json.loads(records[-1].read_text())["status"] == "PUBLISHED"
    capsys.readouterr()

    monkeypatch.setattr(
        checker,
        "parse_args",
        lambda: argparse.Namespace(allow_empty=True),
    )
    assert checker.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"families": 1, "failures": []}


def test_promoter_post_copy_validation_failure_never_exposes_target(
    complete_family: tuple[Path, dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    family, manifest = complete_family
    publication_root = qfv.QUESTION_ROOT / "3" / "question-pack"
    target = publication_root / manifest["family_slug"]
    original_validate = promoter.validate_family

    def reject_staged_copy(
        question: int,
        candidate: Path,
        *,
        staging_required: bool = True,
    ) -> dict[str, object]:
        if not staging_required:
            raise qfv.FamilyValidationError("injected post-copy failure")
        return original_validate(question, candidate, staging_required=staging_required)

    monkeypatch.setattr(promoter, "validate_family", reject_staged_copy)
    monkeypatch.setattr(promoter, "PROMOTION_EVIDENCE_ROOT", tmp_path / "promotions")
    monkeypatch.setattr(
        promoter,
        "parse_args",
        lambda: argparse.Namespace(question=3, family=family, apply=True),
    )
    with pytest.raises(qfv.FamilyValidationError, match="post-copy"):
        promoter.main()
    assert not target.exists()
    assert list(publication_root.iterdir()) == []
    assert family.is_dir()


def test_promoter_detects_source_mutation_during_copy_without_publication(
    complete_family: tuple[Path, dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    family, manifest = complete_family
    publication_root = qfv.QUESTION_ROOT / "3" / "question-pack"
    target = publication_root / manifest["family_slug"]
    original_copy_family = promoter.copy_family

    def copy_then_mutate(source: Path, destination: Path) -> None:
        original_copy_family(source, destination)
        (family / "late-write.txt").write_text("racing writer")

    monkeypatch.setattr(promoter, "copy_family", copy_then_mutate)
    monkeypatch.setattr(promoter, "PROMOTION_EVIDENCE_ROOT", tmp_path / "promotions")
    monkeypatch.setattr(
        promoter,
        "parse_args",
        lambda: argparse.Namespace(question=3, family=family, apply=True),
    )
    with pytest.raises(qfv.FamilyValidationError, match="源题族在晋级期间发生变化"):
        promoter.main()
    assert not target.exists()
    assert list(publication_root.iterdir()) == []


def test_promotion_lock_serializes_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    first_ready = context.Event()
    release_first = context.Event()
    second_acquired = context.Event()
    lock_path = tmp_path / "promotion.lock"
    first = context.Process(
        target=hold_promotion_lock,
        args=(str(lock_path), first_ready, release_first),
    )
    second = context.Process(
        target=acquire_promotion_lock,
        args=(str(lock_path), second_acquired),
    )
    first.start()
    assert first_ready.wait(timeout=5)
    second.start()
    assert not second_acquired.wait(timeout=0.2)
    release_first.set()
    assert second_acquired.wait(timeout=5)
    first.join(timeout=5)
    second.join(timeout=5)
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_schema_declares_high_medium_and_optional_low() -> None:
    schema = json.loads((Path(__file__).parents[1] / "policies/publishing/FAMILY_MANIFEST.schema.json").read_text())
    standard = schema["$defs"]["standardFamily"]
    levels = standard["properties"]["levels"]
    assert standard["properties"]["publication_mode"]["const"] == "family-v1"
    assert levels["required"] == ["high", "medium"]
    assert set(levels["properties"]) == {"high", "medium", "low"}
    assert "re-hashing" in schema["$defs"]["scientificContractSha256"]["description"]
    assert "schema-v2 EnvironmentReceipt" in schema["$defs"]["highLevel"]["properties"]["runtime_closure"]["description"]


def make_grandfathered_family(
    root: Path,
    evidence_root: Path,
    *,
    question: int,
) -> Path:
    slug = "legacy-complete"
    family = root / f"q{question:02d}" / slug
    package = make_package(family / "legacy", "legacy")
    evidence = {
        "schema_version": 1,
        "evidence_type": "grandfathered-completion",
        "question_id": f"Q{question:02d}",
        "package_sha256": package,
        "verdict": "PASS_GRANDFATHERED",
        "owner_attested": True,
        "historical_policy_version": "pre-family-v1",
        "historical_evidence_sha256s": ["9" * 64],
    }
    evidence_path = evidence_root / f"q{question:02d}-grandfathered.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes(qfv.canonical_json_bytes(evidence))
    manifest = {
        "schema_version": 1,
        "publication_mode": "grandfathered-v1",
        "question_id": f"Q{question:02d}",
        "family_slug": slug,
        "scientific_objective": "Preserve the already validated historical scientific question.",
        "grandfathered": {
            "status": "PASS_GRANDFATHERED",
            "package_sha256": package,
            "completion_evidence": {
                "path": str(evidence_path),
                "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            },
        },
    }
    rewrite_manifest(family, manifest)
    return family


def test_q01_q02_narrow_grandfathered_mode_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbench = tmp_path / "workbench"
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(qfv, "WORKBENCH_ROOT", workbench)
    monkeypatch.setattr(qfv, "EVIDENCE_ROOT", evidence_root)
    for question in (1, 2):
        family = make_grandfathered_family(
            workbench,
            evidence_root,
            question=question,
        )
        manifest = qfv.validate_family(question, family)
        assert manifest["publication_mode"] == "grandfathered-v1"
        assert "levels" not in manifest


def test_grandfathered_package_is_not_rejudged_by_current_lint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbench = tmp_path / "workbench"
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(qfv, "WORKBENCH_ROOT", workbench)
    monkeypatch.setattr(qfv, "EVIDENCE_ROOT", evidence_root)
    family = make_grandfathered_family(workbench, evidence_root, question=2)
    legacy = family / "legacy"
    (legacy / "instruction.md").write_text("Historical accepted instructions.\n")
    package = package_sha256(legacy)
    manifest_path = family / "FAMILY_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    evidence_path = Path(manifest["grandfathered"]["completion_evidence"]["path"])
    evidence = json.loads(evidence_path.read_text())
    evidence["package_sha256"] = package
    evidence_path.write_bytes(qfv.canonical_json_bytes(evidence))
    manifest["grandfathered"]["package_sha256"] = package
    manifest["grandfathered"]["completion_evidence"]["sha256"] = hashlib.sha256(
        evidence_path.read_bytes()
    ).hexdigest()
    rewrite_manifest(family, manifest)

    with pytest.raises(qfv.FamilyValidationError, match="lint"):
        qfv.lint_package(legacy)
    assert qfv.validate_family(2, family)["publication_mode"] == "grandfathered-v1"


def test_grandfathered_package_still_requires_exact_historical_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbench = tmp_path / "workbench"
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(qfv, "WORKBENCH_ROOT", workbench)
    monkeypatch.setattr(qfv, "EVIDENCE_ROOT", evidence_root)
    family = make_grandfathered_family(workbench, evidence_root, question=1)
    (family / "legacy" / "instruction.md").write_text("drifted\n")

    with pytest.raises(qfv.FamilyValidationError, match="历史题包 digest"):
        qfv.validate_family(1, family)


def test_q03_cannot_use_grandfathered_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbench = tmp_path / "workbench"
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(qfv, "WORKBENCH_ROOT", workbench)
    monkeypatch.setattr(qfv, "EVIDENCE_ROOT", evidence_root)
    family = make_grandfathered_family(
        workbench,
        evidence_root,
        question=3,
    )
    with pytest.raises(qfv.FamilyValidationError, match="仅限 Q01 和 Q02"):
        qfv.validate_family(3, family)


def test_grandfathered_completion_requires_owner_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbench = tmp_path / "workbench"
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(qfv, "WORKBENCH_ROOT", workbench)
    monkeypatch.setattr(qfv, "EVIDENCE_ROOT", evidence_root)
    family = make_grandfathered_family(
        workbench,
        evidence_root,
        question=1,
    )
    manifest = json.loads((family / "FAMILY_MANIFEST.json").read_text())
    link = manifest["grandfathered"]["completion_evidence"]
    path = Path(link["path"])
    evidence = json.loads(path.read_text())
    evidence["owner_attested"] = False
    path.write_bytes(qfv.canonical_json_bytes(evidence))
    link["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="所有者确认"):
        qfv.validate_family(1, family)


def test_schema_grandfather_exception_is_only_q01_q02() -> None:
    schema = json.loads(
        (Path(__file__).parents[1] / "policies/publishing/FAMILY_MANIFEST.schema.json").read_text()
    )
    grandfathered = schema["$defs"]["grandfatheredFamily"]
    assert grandfathered["properties"]["publication_mode"]["const"] == "grandfathered-v1"
    assert grandfathered["properties"]["question_id"]["enum"] == ["Q01", "Q02"]


def test_cli_argument_parsers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    family = tmp_path / "family"
    monkeypatch.setattr(sys, "argv", ["promote", "3", str(family), "--apply"])
    promote_args = promoter.parse_args()
    assert (promote_args.question, promote_args.family, promote_args.apply) == (3, family, True)
    monkeypatch.setattr(sys, "argv", ["check", "--allow-empty"])
    assert checker.parse_args().allow_empty is True


def test_checker_reports_missing_roots_and_invalid_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    question_root = tmp_path / "questions"
    publication = question_root / "1" / "question-pack"
    (publication / "invalid-a").mkdir(parents=True)
    (publication / "invalid-b").mkdir()
    monkeypatch.setattr(qfv, "QUESTION_ROOT", question_root)
    monkeypatch.setattr(checker, "QUESTION_ROOT", question_root)
    monkeypatch.setattr(checker, "parse_args", lambda: argparse.Namespace(allow_empty=False))
    assert checker.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["families"] == 0
    assert any("应有恰好一个" in failure for failure in report["failures"])
    assert any("缺少 question-pack" in failure for failure in report["failures"])
    assert any("invalid-a" in failure for failure in report["failures"])


def test_promoter_refuses_nonempty_publication(
    complete_family: tuple[Path, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    family, _ = complete_family
    publication = qfv.QUESTION_ROOT / "3" / "question-pack"
    (publication / "occupied").mkdir()
    monkeypatch.setattr(
        promoter,
        "parse_args",
        lambda: argparse.Namespace(question=3, family=family, apply=False),
    )
    with pytest.raises(qfv.FamilyValidationError, match="发布目录非空"):
        promoter.main()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("publication_mode", "unknown-v1", "不支持的 publication_mode"),
        ("scientific_contract_sha256", "bad", "科学合同 digest"),
    ],
)
def test_standard_manifest_identity_fields_fail_closed(
    complete_family: tuple[Path, dict[str, object]],
    field: str,
    value: str,
    message: str,
) -> None:
    family, manifest = complete_family
    manifest[field] = value
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match=message):
        qfv.validate_family(3, family)


def test_standard_family_rejects_unexpected_entry(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, _ = complete_family
    (family / "debug.txt").write_text("not publishable")
    with pytest.raises(qfv.FamilyValidationError, match="题族包含未声明条目"):
        qfv.validate_family(3, family)


def test_standard_family_rejects_duplicate_execution_identity(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    first = manifest["levels"]["high"]["fresh_blinds"][0]["evidence"]  # type: ignore[index]
    second = manifest["levels"]["high"]["fresh_blinds"][1]["evidence"]  # type: ignore[index]
    first_data = json.loads(Path(first["path"]).read_text())
    second_path = Path(second["path"])
    second_data = json.loads(second_path.read_text())
    for key in ("job_id", "trial_id", "sandbox_id", "session_id"):
        second_data[key] = first_data[key]
    second_path.write_bytes(qfv.canonical_json_bytes(second_data))
    second["sha256"] = hashlib.sha256(second_path.read_bytes()).hexdigest()
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="全局 fresh"):
        qfv.validate_family(3, family)


def test_standard_family_rejects_reuse_of_one_execution_identity_field(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    first = manifest["levels"]["high"]["fresh_blinds"][0]["evidence"]  # type: ignore[index]
    second = manifest["levels"]["high"]["fresh_blinds"][1]["evidence"]  # type: ignore[index]
    first_data = json.loads(Path(first["path"]).read_text())
    second_data = json.loads(Path(second["path"]).read_text())
    second_data["job_id"] = first_data["job_id"]
    rewrite_link(second, second_data)
    rewrite_manifest(family, manifest)

    with pytest.raises(qfv.FamilyValidationError, match="job_id.*fresh"):
        qfv.validate_family(3, family)


def test_blind_requires_empty_context_and_consumed_bound_capability(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    link = manifest["levels"]["high"]["fresh_blinds"][0]["evidence"]  # type: ignore[index]
    value = json.loads(Path(link["path"]).read_text())
    value["context_digests"] = ["a" * 64]
    rewrite_link(link, value)
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="request 绑定缺失"):
        qfv.validate_family(3, family)


def test_blind_rejects_capability_bound_to_other_request(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    link = manifest["levels"]["high"]["fresh_blinds"][0]["evidence"]  # type: ignore[index]
    value = json.loads(Path(link["path"]).read_text())
    capability_link = value["capability"]
    capability = json.loads(Path(capability_link["path"]).read_text())
    capability["request_sha256"] = "f" * 64
    rewrite_link(capability_link, capability)
    rewrite_link(link, value)
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="未消费或未绑定"):
        qfv.validate_family(3, family)


def test_derived_result_uses_workflow_hint_mode_and_order(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    link = manifest["levels"]["medium"]["fresh_harbor"][0]  # type: ignore[index]
    value = json.loads(Path(link["path"]).read_text())
    value["mode"] = "derived"
    rewrite_link(link, value)
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="缺少通过的科学结果"):
        qfv.validate_family(3, family)


def test_low_hint_must_be_second_and_parent_high_hint(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    test_optional_low_level_passes((family, manifest))
    manifest = json.loads((family / "FAMILY_MANIFEST.json").read_text())
    link = manifest["levels"]["low"]["derivation_hint"]["review_seal"]
    review = json.loads(Path(link["path"]).read_text())
    review["parent_hint_sha256"] = None
    rewrite_link(link, review)
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="hint_index/parent"):
        qfv.validate_family(3, family)


def test_standard_family_rejects_bool_score(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    manifest["levels"]["high"]["fresh_blinds"][0]["score"] = True  # type: ignore[index]
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="分数必须是数值"):
        qfv.validate_family(3, family)


def test_evidence_hash_mismatch_is_rejected(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    manifest["levels"]["high"]["formal_reviewer"]["sha256"] = "0" * 64  # type: ignore[index]
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="evidence 字节不匹配"):
        qfv.validate_family(3, family)


def test_manifest_duplicate_key_and_nonfinite_are_rejected(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, _ = complete_family
    manifest_path = family / "FAMILY_MANIFEST.json"
    manifest_path.write_text('{"schema_version":1,"schema_version":1}\n')
    with pytest.raises(qfv.FamilyValidationError, match="JSON 键重复"):
        qfv.validate_family(3, family)
    manifest_path.write_text(
        '{"schema_version":1,"publication_mode":"family-v1",'
        '"question_id":"Q03","family_slug":NaN}\n'
    )
    with pytest.raises(qfv.FamilyValidationError, match="JSON 数值不是有限值"):
        qfv.validate_family(3, family)


def test_grandfathered_family_rejects_extra_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbench = tmp_path / "workbench"
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(qfv, "WORKBENCH_ROOT", workbench)
    monkeypatch.setattr(qfv, "EVIDENCE_ROOT", evidence_root)
    family = make_grandfathered_family(workbench, evidence_root, question=2)
    (family / "medium").mkdir()
    with pytest.raises(qfv.FamilyValidationError, match="只能包含"):
        qfv.validate_family(2, family)


def test_publication_tree_rejects_symlink(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, _ = complete_family
    (family / "linked").symlink_to(family / "high", target_is_directory=True)
    with pytest.raises(qfv.FamilyValidationError, match="软链接或特殊节点"):
        qfv.validate_family(3, family)


@pytest.mark.parametrize(
    ("returncode", "stdout", "message"),
    [
        (1, "", "lint 执行失败"),
        (0, "not-json", "非法 JSON"),
        (0, '{"passed":false}', "lint 未通过"),
    ],
)
def test_lint_package_failures_are_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    stdout: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        qfv.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode, stdout, "failure"),
    )
    with pytest.raises(qfv.FamilyValidationError, match=message):
        qfv.lint_package(tmp_path)


def test_evidence_must_stay_inside_authority_root(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    link = manifest["levels"]["high"]["formal_reviewer"]  # type: ignore[index]
    link["path"] = "/tmp/outside-authority.json"
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="evidence 必须位于"):
        qfv.validate_family(3, family)


def test_harbor_execution_identity_must_be_complete(
    complete_family: tuple[Path, dict[str, object]],
) -> None:
    family, manifest = complete_family
    link = manifest["levels"]["high"]["fresh_blinds"][0]["evidence"]  # type: ignore[index]
    path = Path(link["path"])
    evidence = json.loads(path.read_text())
    evidence["session_id"] = ""
    path.write_bytes(qfv.canonical_json_bytes(evidence))
    link["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    rewrite_manifest(family, manifest)
    with pytest.raises(qfv.FamilyValidationError, match="执行身份不完整"):
        qfv.validate_family(3, family)

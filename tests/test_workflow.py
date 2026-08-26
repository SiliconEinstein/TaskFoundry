from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
from pathlib import Path

import pytest

from taskfoundry.labwright import ArtifactIdentity, EnvironmentReceipt
from taskfoundry.labwright_runtime import DeltaReceipt, DeltaState, ImageSealPlan, artifact_identity_sha256
from taskfoundry.health import BoundHealthEvidence, HealthGate
from taskfoundry.model import Actor, RunState
from taskfoundry.store import RunStore
from taskfoundry.package import package_sha256
from taskfoundry.researcher import ResearcherReceipt, ResearcherRequest
from taskfoundry.validation import AttemptEvidence, HealthEvidence
from taskfoundry.workflow import RunWorkflow, WorkflowError


def write_brief(path: Path) -> None:
    path.write_text(json.dumps({
        "brief_id": "brief-1",
        "question_type": "method-selection",
        "title": "选择方法",
        "scientific_goal": "选择可迁移方法",
        "research_object": "隐藏科学情形",
        "source_questions": [
            {
                "source_id": "q1",
                "paper_id": "p1",
                "research_goal": "目标",
                "method": "方法",
                "transferred_role": "候选方法",
            },
            {
                "source_id": "q2",
                "paper_id": "p1",
                "research_goal": "诊断",
                "method": "方法二",
                "transferred_role": "诊断证据",
            },
            {
                "source_id": "q3",
                "paper_id": "p2",
                "research_goal": "评价",
                "method": "方法三",
                "transferred_role": "评价指标",
            },
        ],
        "method_space": ["m1", "m2"],
        "evidence_roles": {"q1": "候选方法", "q2": "诊断证据", "q3": "评价指标"},
        "public_inputs": ["input.csv"],
        "required_outputs": ["output.csv"],
        "hidden_evaluation_axes": ["迁移"],
        "environment_capabilities": ["python"],
        "difficulty_hypothesis": "需要迁移判断",
        "solvability_argument": "公开数据足以完成判断",
        "target_solution_time_sec": 1800,
    }))


def attach_design_evidence(flow: RunWorkflow, tmp_path: Path) -> None:
    """为测试运行生成与当前 brief/revision 精确绑定的设计证据。"""
    brief_path = Path(str(flow.snapshot.brief_path))
    brief = json.loads(brief_path.read_text())
    brief_sha256 = hashlib.sha256(brief_path.read_bytes()).hexdigest()
    source_ids = [item["source_id"] for item in brief["source_questions"]]
    role_map = tmp_path / f"source-role-map-{flow.snapshot.question_revision}.json"
    role_map.write_text(json.dumps({
        "schema_version": 1,
        "evidence_type": "source-role-map",
        "question_revision": flow.snapshot.question_revision,
        "brief_sha256": brief_sha256,
        "source_ids": source_ids,
        "roles": brief["evidence_roles"],
        "immutable_source_sha256s": {
            source_id: hashlib.sha256(source_id.encode()).hexdigest()
            for source_id in source_ids
        },
        "license_review_pass": True,
    }))
    ledger = tmp_path / f"ground-truth-ledger-{flow.snapshot.question_revision}.json"
    ledger.write_text(json.dumps({
        "schema_version": 1,
        "evidence_type": "ground-truth-ledger",
        "question_revision": flow.snapshot.question_revision,
        "brief_sha256": brief_sha256,
        "scored_quantities": ["prediction", "method_selection"],
        "producer_sha256": "1" * 64,
        "independent_crosscheck_sha256": "2" * 64,
        "scoring_contract_sha256": "3" * 64,
        "derived_reference": True,
        "crosscheck_pass": True,
    }))
    flow.attach_design_evidence(
        Actor.TEACHER,
        source_role_map_path=role_map,
        ground_truth_ledger_path=ledger,
        idempotency_key=f"design-evidence-{flow.snapshot.question_revision}",
    )


def package(root):
    root.mkdir()
    (root / "instruction.md").write_text("See resources.yaml.\n")
    (root / "task.toml").write_text(
        'schema_version = "1.3"\n[task]\nname="org/task"\n[environment]\n'
        'docker_image="registry/task:fixed"\nworkdir="/app"\n[agent]\ntimeout_sec=3600\n'
    )
    (root / "environment").mkdir()
    (root / "environment/resources.yaml").write_text("resources: []\n")
    (root / "solution").mkdir()
    (root / "solution/solve.sh").write_text("#!/bin/sh\ntrue\n")
    (root / "tests").mkdir()
    (root / "tests/test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    return root


def receipt(tmp_path, runtime_closure: Path | None = None):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("manifest")
    closure_sha256 = hashlib.sha256(runtime_closure.read_bytes()).hexdigest() if runtime_closure else None
    return EnvironmentReceipt(
        environment_key="a" * 64,
        lifecycle="STABLE",
        artifact=ArtifactIdentity("lbg", "lbg://prod", "42", "1", "registry/task:fixed"),
        workdir="/app",
        manifest_path=str(manifest),
        manifest_sha256="b" * 64,
        resource_digests=(),
        runtime_closure_path=str(runtime_closure.resolve()) if runtime_closure else None,
        runtime_closure_sha256=closure_sha256,
        schema_version=2 if runtime_closure else 1,
    )


def bound_health(flow: RunWorkflow, tmp_path: Path, *, revision: str = "r1") -> BoundHealthEvidence:
    evidence = []
    for gate in (
        HealthGate.PACKAGE,
        HealthGate.ENVIRONMENT,
        HealthGate.ORACLE,
        HealthGate.HONEST,
        HealthGate.ADVERSARIAL,
        HealthGate.LEAKAGE,
    ):
        path = tmp_path / f"final-{gate.value}.json"
        path.write_text(f'{{"gate":"{gate.value}"}}')
        evidence.append((gate, path))
    snapshot = flow.snapshot
    assert snapshot.package_digest is not None
    assert snapshot.environment_key is not None
    return BoundHealthEvidence.create(
        question_revision=revision,
        package_sha256=snapshot.package_digest,
        environment_key=snapshot.environment_key,
        health=HealthEvidence(True, True, True, True, True, True),
        evidence=evidence,
    )


def attempt(index, *, mode="blind", score=0.4):
    return AttemptEvidence(
        request_id=f"r-{mode}-{index}",
        attempt_index=index,
        mode=mode,
        classification="SCIENTIFIC_RESULT",
        score=score,
        frozen_contract_digest="f" * 64,
        job_id=f"j-{mode}-{index}",
        trial_id=f"t-{mode}-{index}",
        sandbox_id=f"s-{mode}-{index}",
        session_id=f"x-{mode}-{index}",
        wall_time_sec=10,
        hint_sha256="h" * 64 if mode == "hint" else None,
        leakage_free=True,
        leakage_evidence_sha256="e" * 64,
    )


def runtime_attempt(
    flow: RunWorkflow,
    *,
    request_id: str,
    sandbox_id: str,
    classification: str,
    score: float | None,
    attempt_index: int = 1,
    mode: str = "blind",
) -> AttemptEvidence:
    return AttemptEvidence(
        request_id=request_id,
        attempt_index=attempt_index,
        mode=mode,
        classification=classification,
        score=score,
        frozen_contract_digest=str(flow.snapshot.package_digest),
        job_id=f"job-{request_id}",
        trial_id=f"trial-{request_id}",
        sandbox_id=sandbox_id,
        session_id=f"session-{request_id}",
        wall_time_sec=5,
        hint_sha256="h" * 64 if mode == "hint" else None,
        leakage_free=True,
        leakage_evidence_sha256="e" * 64,
    )


def runtime_evidence_file(tmp_path: Path, name: str, value: dict | None = None) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value or {"passed": True}, sort_keys=True))
    return path


def baseline_artifact() -> ArtifactIdentity:
    return ArtifactIdentity(
        "lbg", "lbg://prod", "42", "base", "registry/base:fixed", "sha256:" + "1" * 64
    )


def runtime_receipt(flow: RunWorkflow, tmp_path: Path) -> tuple[Path, DeltaReceipt, ArtifactIdentity]:
    baseline = baseline_artifact()
    builder = ArtifactIdentity(
        "lbg", "lbg://prod", "42", "builder", "registry/builder:fixed", "sha256:" + "2" * 64
    )
    source_trace = runtime_evidence_file(tmp_path, "source-failure.json")
    before = runtime_evidence_file(tmp_path, "inventory-before.json")
    after = runtime_evidence_file(tmp_path, "inventory-after.json")
    probes = runtime_evidence_file(tmp_path, "probes.json")
    evidence = runtime_evidence_file(tmp_path, "delta-evidence.json")
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    value = DeltaReceipt(
        request_id="delta-1",
        request_sha256="3" * 64,
        run_id=flow.snapshot.run_id,
        question_revision=flow.snapshot.question_revision,
        package_sha256=str(flow.snapshot.package_digest),
        source_researcher_request_id="source-request",
        source_sandbox_id="source-sandbox",
        baseline_artifact=baseline,
        baseline_identity_sha256=artifact_identity_sha256(baseline),
        builder_runtime_request_id="builder-request",
        builder_sandbox_id="builder-sandbox",
        builder_artifact=builder,
        builder_identity_sha256=artifact_identity_sha256(builder),
        state=DeltaState.READY,
        capability_name="numpy",
        capability_version="2.3.5",
        evidence_path=str(evidence),
        evidence_sha256=digest(evidence),
        inventory_before_path=str(before),
        inventory_before_sha256=digest(before),
        inventory_after_path=str(after),
        inventory_after_sha256=digest(after),
        probe_evidence_path=str(probes),
        probe_evidence_sha256=digest(probes),
        source_trace_path=str(source_trace),
        source_trace_sha256=digest(source_trace),
        recovery_started_at=None,
        recovery_finished_at=None,
        recovery_duration_sec=None,
        fencing_token="fence-1",
    )
    path = tmp_path / "delta-receipt.json"
    path.write_text(json.dumps(value.to_dict(), sort_keys=True))
    return path, value, baseline


def runtime_closure(
    flow: RunWorkflow,
    tmp_path: Path,
    baseline: ArtifactIdentity,
    receipts: tuple[DeltaReceipt, ...] = (),
) -> Path:
    trace = runtime_evidence_file(
        tmp_path,
        "scientific-trace.json",
        {
            "schema_version": 1,
            "classification": "SCIENTIFIC_RESULT",
            "run_id": flow.snapshot.run_id,
            "question_revision": flow.snapshot.question_revision,
            "package_sha256": flow.snapshot.package_digest,
            "baseline_identity_sha256": artifact_identity_sha256(baseline),
            "researcher_request_id": "fresh-request",
            "sandbox_id": "fresh-sandbox",
            "runtime_delta_request_ids": [item.request_id for item in receipts],
        },
    )
    plan = ImageSealPlan(
        run_id=flow.snapshot.run_id,
        question_revision=flow.snapshot.question_revision,
        package_sha256=str(flow.snapshot.package_digest),
        baseline_artifact=baseline,
        baseline_identity_sha256=artifact_identity_sha256(baseline),
        scientific_trace_path=str(trace),
        scientific_trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),
        delta_receipts=tuple(item.to_dict() for item in receipts),
        created_at=datetime.now(UTC).isoformat(),
    )
    path = tmp_path / "runtime-closure.json"
    path.write_text(json.dumps(plan.to_dict(), sort_keys=True))
    return path


def difficulty_revision(flow: RunWorkflow, path: Path, revision: str = "r2") -> Path:
    path.write_text(json.dumps({
        "schema_version": 1,
        "evidence_type": "difficulty-revision",
        "source_package_sha256": flow.snapshot.package_digest,
        "next_revision": revision,
        "scientific_objective_unchanged": True,
        "change_summary": "increase reasoning difficulty without changing the scientific contract",
    }))
    return path


def post_validation(flow: RunWorkflow, path: Path) -> Path:
    path.write_text(json.dumps({
        "schema_version": 1,
        "evidence_type": "post-validation",
        "verdict": "PASS_FINAL_PROGRESSION",
        "reviewer_independent": True,
        "package_sha256": flow.snapshot.package_digest,
        "question_revision": flow.snapshot.question_revision,
    }))
    return path


def ready_workflow(tmp_path):
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief.json"
    write_brief(brief)
    policies = tmp_path / "policies.json"
    policies.write_text("{}")
    flow.attach_brief(Actor.TEACHER, brief, "brief")
    flow.lock_policies(Actor.TEACHER, policies, "policies")
    flow.request_environment(Actor.TEACHER, "env-request")
    flow.environment_ready(Actor.LABWRIGHT, receipt(tmp_path), "env-ready")
    flow.begin_authoring(Actor.TEACHER, "authoring")
    attach_design_evidence(flow, tmp_path)
    flow.freeze_package(Actor.TEACHER, package(tmp_path / "task"), "freeze")
    health = HealthEvidence(True, True, True, True, True, True)
    flow.accept_health(Actor.REVIEWER, health, ("oracle.json",), "health")
    flow.start_blind_validation(Actor.TEACHER, "blind")
    return flow


def provisional_workflow(tmp_path):
    """构造不依赖 Stable 镜像的首次解题前状态。"""
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief.json"
    write_brief(brief)
    policies = tmp_path / "policies.json"
    policies.write_text("{}")
    flow.attach_brief(Actor.TEACHER, brief, "brief")
    flow.lock_policies(Actor.TEACHER, policies, "policies")
    flow.begin_authoring(Actor.TEACHER, "authoring")
    attach_design_evidence(flow, tmp_path)
    flow.freeze_package(Actor.TEACHER, package(tmp_path / "task"), "freeze")
    health = HealthEvidence(True, True, True, False, True, True)
    flow.accept_health(Actor.REVIEWER, health, ("preflight.json",), "preflight")
    return flow


def runtime_final_workflow(tmp_path):
    """构造已有首条科学 trace、closure、Stable 和最终健康证据的运行。"""
    flow = provisional_workflow(tmp_path)
    flow.start_blind_validation(Actor.TEACHER, "blind")
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="fresh-request",
            sandbox_id="fresh-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
        ),
        "runtime-first-scientific-audit",
    )
    closure = runtime_closure(flow, tmp_path, baseline_artifact())
    flow.accept_runtime_closure(Actor.LABWRIGHT, closure, "runtime-closure")
    flow.bind_runtime_environment(
        Actor.LABWRIGHT,
        receipt(tmp_path, closure),
        "runtime-environment",
    )
    flow.accept_bound_health(
        Actor.REVIEWER,
        bound_health(flow, tmp_path),
        "final-health",
    )
    return flow


def audit_bound(
    flow,
    tmp_path,
    index,
    *,
    mode="blind",
    score=0.4,
    leakage_overrides: dict | None = None,
):
    evidence = tmp_path / f"bound-{mode}-{index}"
    evidence.mkdir()
    config = evidence / "job.json"
    package_path = flow.snapshot.package_path
    config.write_text(json.dumps({
        "tasks": [{"path": package_path}],
        "agents": [{"model_name": "deepseek/deepseek-v4-pro"}],
        "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
        "extra_instruction_paths": [],
    }))
    request = ResearcherRequest(
        request_id=f"request-{mode}-{index}", run_id="run-1", question_revision="r1",
        attempt_index=index, mode=mode, package_path=package_path,
        package_sha256=package_sha256(Path(package_path)), job_config_path=str(config),
        job_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        context_digests=("h" * 64,) if mode == "hint" else (), researcher_thread_id="breaker",
        harness="dsh", model="deepseek-v4-pro",
    )
    request_path = evidence / "request.json"
    request_path.write_text(json.dumps(request.to_dict()))
    result = evidence / "result.json"
    result.write_text("{}")
    receipt_value = asdict(ResearcherReceipt(
        request_id=request.request_id, classification="SCIENTIFIC_RESULT", reward=score,
        result_path=str(result), started_at="start", finished_at="finish", exit_code=0,
        result_sha256=hashlib.sha256(result.read_bytes()).hexdigest(), job_id=f"j-{mode}-{index}",
        trial_id=f"t-{mode}-{index}", sandbox_id=f"s-{mode}-{index}",
        session_id=f"x-{mode}-{index}", wall_time_sec=10,
    ))
    receipt_path = evidence / "receipt.json"
    receipt_path.write_text(json.dumps(receipt_value))
    capability = evidence / "capability.json"
    capability.write_text(json.dumps({
        "status": "CONSUMED",
        "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
    }))
    leakage = evidence / "leakage.json"
    leakage_value = {
        "schema_version": 1,
        "evidence_type": "leakage-audit",
        "verdict": "PASS",
        "reviewer_independent": True,
        "leakage_free": True,
        "request_id": request.request_id,
        "package_sha256": request.package_sha256,
        "job_id": receipt_value["job_id"],
        "trial_id": receipt_value["trial_id"],
        "sandbox_id": receipt_value["sandbox_id"],
        "session_id": receipt_value["session_id"],
    }
    leakage_value.update(leakage_overrides or {})
    leakage.write_text(json.dumps(leakage_value))
    hint_review = None
    if mode == "hint":
        hint_review = evidence / "hint-review.json"
        hint_review.write_text(json.dumps({
            "schema_version": 1,
            "evidence_type": "hint-review",
            "verdict": "PASS",
            "contains_answer": False,
            "package_sha256": flow.snapshot.package_digest,
            "question_revision": flow.snapshot.question_revision,
            "approved_context_digests": list(request.context_digests),
            "hint_sha256": request.context_digests[0],
        }))
    return flow.audit_researcher_receipt(
        Actor.REVIEWER, request_path=request_path, capability_path=capability,
        receipt_path=receipt_path, leakage_evidence_path=leakage,
        hint_review_path=hint_review,
        idempotency_key=f"bound-audit-{mode}-{index}",
    )


def test_legacy_health_without_bound_evidence_cannot_complete(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    for index in range(1, 4):
        flow._record_attempt(Actor.REVIEWER, attempt(index), f"audit-{index}")
    assert flow.snapshot.state is RunState.HINT_VALIDATION
    flow._record_attempt(Actor.REVIEWER, attempt(1, mode="hint", score=0.9), "hint-audit")
    assert flow.snapshot.state is RunState.BLOCKED


def test_late_bound_health_recovers_only_health_blocked_completion(tmp_path) -> None:
    flow = provisional_workflow(tmp_path)
    flow.start_blind_validation(Actor.TEACHER, "blind")
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="fresh-request",
            sandbox_id="fresh-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
        ),
        "audit-1",
    )
    for index in range(2, 4):
        flow._record_attempt(
            Actor.REVIEWER,
            runtime_attempt(
                flow,
                request_id=f"fresh-request-{index}",
                sandbox_id=f"fresh-sandbox-{index}",
                classification="SCIENTIFIC_RESULT",
                score=0.4,
                attempt_index=index,
            ),
            f"audit-{index}",
        )
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="hint-request",
            sandbox_id="hint-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.9,
            mode="hint",
        ),
        "hint-audit",
    )
    assert flow.snapshot.state is RunState.BLOCKED

    closure = runtime_closure(flow, tmp_path, baseline_artifact())
    flow.accept_runtime_closure(Actor.LABWRIGHT, closure, "runtime-closure")
    flow.bind_runtime_environment(
        Actor.LABWRIGHT,
        receipt(tmp_path, closure),
        "runtime-environment",
    )

    snapshot = flow.accept_bound_health(
        Actor.REVIEWER,
        bound_health(flow, tmp_path),
        "late-final-health",
    )

    assert snapshot.state is RunState.VALIDATION_PASSED


def test_first_blind_starts_without_stable_environment(tmp_path) -> None:
    flow = provisional_workflow(tmp_path)

    assert flow.snapshot.state is RunState.PREFLIGHT_PASSED
    assert flow.snapshot.environment_key is None

    snapshot = flow.start_blind_validation(Actor.TEACHER, "blind")

    assert snapshot.state is RunState.BLIND_VALIDATION
    assert snapshot.environment_key is None


def test_teacher_cannot_accept_runtime_first_preflight(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief.json"
    write_brief(brief)
    policies = tmp_path / "policies.json"
    policies.write_text("{}")
    flow.attach_brief(Actor.TEACHER, brief, "brief")
    flow.lock_policies(Actor.TEACHER, policies, "policies")
    flow.begin_authoring(Actor.TEACHER, "authoring")
    attach_design_evidence(flow, tmp_path)
    flow.freeze_package(Actor.TEACHER, package(tmp_path / "task"), "freeze")

    with pytest.raises(WorkflowError, match="does not own"):
        flow.accept_health(
            Actor.TEACHER,
            HealthEvidence(True, True, True, False, True, True),
            ("preflight.json",),
            "teacher-preflight",
        )


def test_stable_environment_requires_a_scientific_trace(tmp_path) -> None:
    flow = provisional_workflow(tmp_path)
    flow.start_blind_validation(Actor.TEACHER, "blind")

    with pytest.raises(WorkflowError, match="scientific trace"):
        flow.bind_runtime_environment(
            Actor.LABWRIGHT,
            receipt(tmp_path),
            "runtime-environment",
        )


def test_stable_environment_is_bound_after_first_scientific_trace(tmp_path) -> None:
    flow = provisional_workflow(tmp_path)
    flow.start_blind_validation(Actor.TEACHER, "blind")
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="fresh-request",
            sandbox_id="fresh-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
        ),
        "audit-1",
    )
    closure = runtime_closure(flow, tmp_path, baseline_artifact())
    flow.accept_runtime_closure(Actor.LABWRIGHT, closure, "runtime-closure")

    stable = receipt(tmp_path, closure)
    snapshot = flow.bind_runtime_environment(
        Actor.LABWRIGHT,
        stable,
        "runtime-environment",
    )

    assert snapshot.state is RunState.BLIND_VALIDATION
    assert snapshot.environment_key == stable.environment_key
    assert snapshot.evidence["health"]["environment_stable"] is False
    assert "bound_health" not in snapshot.evidence


def test_bound_health_can_be_accepted_after_runtime_environment(tmp_path) -> None:
    flow = provisional_workflow(tmp_path)
    flow.start_blind_validation(Actor.TEACHER, "blind")
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="fresh-request",
            sandbox_id="fresh-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
        ),
        "audit-1",
    )
    closure = runtime_closure(flow, tmp_path, baseline_artifact())
    flow.accept_runtime_closure(Actor.LABWRIGHT, closure, "runtime-closure")
    flow.bind_runtime_environment(
        Actor.LABWRIGHT,
        receipt(tmp_path, closure),
        "runtime-environment",
    )

    snapshot = flow.accept_bound_health(
        Actor.REVIEWER,
        bound_health(flow, tmp_path),
        "final-health",
    )

    assert snapshot.state is RunState.BLIND_VALIDATION
    assert snapshot.evidence["bound_health"]["question_revision"] == "r1"
    assert snapshot.evidence["health"]["environment_stable"] is True

    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="fresh-request-2",
            sandbox_id="fresh-sandbox-2",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
            attempt_index=2,
        ),
        "audit-2",
    )
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="fresh-request-3",
            sandbox_id="fresh-sandbox-3",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
            attempt_index=3,
        ),
        "audit-3",
    )
    validated = flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="hint-request-1",
            sandbox_id="hint-sandbox-1",
            classification="SCIENTIFIC_RESULT",
            score=0.9,
            mode="hint",
        ),
        "hint-audit",
    )
    assert validated.state is RunState.VALIDATION_PASSED
    completed = flow.accept_post_validation(
        Actor.REVIEWER,
        post_validation(flow, tmp_path / "post-validation.json"),
        "post-validation",
    )
    assert completed.state is RunState.COMPLETED


def test_runtime_delta_binds_failure_builder_and_fresh_retry(tmp_path) -> None:
    flow = provisional_workflow(tmp_path)
    flow.start_blind_validation(Actor.TEACHER, "blind")
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="source-request",
            sandbox_id="source-sandbox",
            classification="ENVIRONMENT_FAILURE",
            score=None,
        ),
        "source-failure",
    )
    delta_path, delta, baseline = runtime_receipt(flow, tmp_path)
    flow.accept_runtime_delta(Actor.LABWRIGHT, delta_path, "runtime-delta")
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="fresh-request",
            sandbox_id="fresh-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
        ),
        "fresh-retry",
    )
    closure = runtime_closure(flow, tmp_path, baseline, (delta,))

    snapshot = flow.accept_runtime_closure(
        Actor.LABWRIGHT,
        closure,
        "runtime-closure",
    )

    assert len(snapshot.attempts) == 2
    assert snapshot.evidence["runtime_closure"]["delta_request_ids"] == ["delta-1"]
    stable = receipt(tmp_path, closure)
    bound = flow.bind_runtime_environment(Actor.LABWRIGHT, stable, "runtime-environment")
    assert bound.environment_key == stable.environment_key


def test_runtime_first_rejects_legacy_stable_receipt_without_closure(tmp_path) -> None:
    flow = provisional_workflow(tmp_path)
    flow.start_blind_validation(Actor.TEACHER, "blind")
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="fresh-request",
            sandbox_id="fresh-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
        ),
        "audit-1",
    )
    closure = runtime_closure(flow, tmp_path, baseline_artifact())
    flow.accept_runtime_closure(Actor.LABWRIGHT, closure, "runtime-closure")

    with pytest.raises(WorkflowError, match="does not bind"):
        flow.bind_runtime_environment(
            Actor.LABWRIGHT,
            receipt(tmp_path),
            "legacy-runtime-environment",
        )


def test_blind_pass_is_terminal_too_easy(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    flow._record_attempt(Actor.REVIEWER, attempt(1, score=1), "audit")
    assert flow.snapshot.state is RunState.TOO_EASY


def test_teacher_cannot_audit_researcher_attempt(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    with pytest.raises(WorkflowError, match="does not own"):
        flow._record_attempt(Actor.TEACHER, attempt(1), "teacher-audit")


def test_too_easy_can_start_evidence_backed_revision(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    flow._record_attempt(Actor.REVIEWER, attempt(1, score=0.9), "audit-1")
    reason = difficulty_revision(flow, tmp_path / "too-easy.json")

    snapshot = flow.begin_revision(Actor.TEACHER, "r2", reason, "revision-r2")

    assert snapshot.state is RunState.AUTHORING
    assert snapshot.attempts == ()
    assert snapshot.evidence["superseded_revision"]["next_revision"] == "r2"


def test_difficulty_revision_clears_all_revision_scoped_runtime_evidence(tmp_path) -> None:
    flow = runtime_final_workflow(tmp_path)
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="too-easy-request",
            sandbox_id="too-easy-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.9,
            attempt_index=2,
        ),
        "too-easy-audit",
    )
    reason = difficulty_revision(flow, tmp_path / "too-easy-runtime.json")

    snapshot = flow.begin_revision(Actor.TEACHER, "r2", reason, "revision-r2-runtime")

    assert snapshot.environment_key is None
    assert snapshot.package_digest is None
    assert snapshot.attempts == ()
    for key in (
        "package_lint",
        "design_evidence",
        "health",
        "bound_health",
        "environment",
        "runtime_closure",
        "runtime_deltas",
        "validation_decision",
    ):
        assert key not in snapshot.evidence


def test_hint_progression_cannot_reset_the_blind_ledger(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    for index in range(1, 4):
        flow._record_attempt(Actor.REVIEWER, attempt(index), f"audit-{index}")
    reason = difficulty_revision(flow, tmp_path / "invalid-hint-revision.json")

    with pytest.raises(WorkflowError, match="invalid from HINT_VALIDATION"):
        flow.begin_revision(Actor.TEACHER, "r2", reason, "invalid-revision-r2")


def test_too_easy_revision_can_refresh_stable_environment_before_freeze(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    flow._record_attempt(Actor.REVIEWER, attempt(1, score=0.9), "audit-1")
    reason = difficulty_revision(flow, tmp_path / "too-easy.json")
    flow.begin_revision(Actor.TEACHER, "r2", reason, "revision-r2")

    revised = receipt(tmp_path)
    snapshot = flow.refresh_revision_environment(
        Actor.LABWRIGHT, revised, "revision-r2-environment"
    )

    assert snapshot.state is RunState.AUTHORING
    assert snapshot.environment_key == revised.environment_key
    assert snapshot.evidence["environment"]["artifact"]["record_id"] == "1"


def test_untried_blind_revision_can_migrate_environment(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    reason = tmp_path / "environment-migration.json"
    reason.write_text('{"reason":"move to the pinned harness-bearing base"}')

    snapshot = flow.begin_environment_revision(
        Actor.TEACHER, "r2", reason, "revision-r2-environment"
    )

    assert snapshot.state is RunState.AUTHORING
    assert snapshot.package_path is None
    assert snapshot.evidence["superseded_revision"]["reason_kind"] == "ENVIRONMENT_MIGRATION"


def test_environment_migration_cannot_discard_audited_attempts(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    flow._record_attempt(Actor.REVIEWER, attempt(1), "audit-1")
    reason = tmp_path / "environment-migration.json"
    reason.write_text('{"reason":"move base"}')

    with pytest.raises(WorkflowError, match="audited attempts"):
        flow.begin_environment_revision(
            Actor.TEACHER, "r2", reason, "revision-r2-environment"
        )


def test_bound_receipts_complete_workflow(tmp_path) -> None:
    flow = runtime_final_workflow(tmp_path)
    for index in range(2, 4):
        audit_bound(flow, tmp_path, index)
    audit_bound(flow, tmp_path, 1, mode="hint", score=0.9)
    assert flow.snapshot.state is RunState.VALIDATION_PASSED
    flow.accept_post_validation(
        Actor.REVIEWER,
        post_validation(flow, tmp_path / "post-validation.json"),
        "post-validation",
    )
    assert flow.snapshot.state is RunState.COMPLETED


def test_bound_receipt_rejects_missing_evidence(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    with pytest.raises(WorkflowError, match="missing"):
        flow.audit_researcher_receipt(
            Actor.REVIEWER,
            request_path=tmp_path / "missing",
            capability_path=tmp_path / "missing",
            receipt_path=tmp_path / "missing",
            leakage_evidence_path=tmp_path / "missing",
            idempotency_key="bad",
        )


def test_leakage_audit_must_bind_the_exact_researcher_execution(tmp_path) -> None:
    flow = ready_workflow(tmp_path)

    with pytest.raises(WorkflowError, match="bound independent leakage PASS"):
        audit_bound(
            flow,
            tmp_path,
            1,
            leakage_overrides={"sandbox_id": "unrelated-sandbox"},
        )


def test_hint_receipt_requires_independent_review(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    for index in range(1, 4):
        audit_bound(flow, tmp_path, index)

    evidence = tmp_path / "unreviewed-hint"
    evidence.mkdir()
    config = evidence / "job.json"
    package_path = flow.snapshot.package_path
    config.write_text(json.dumps({
        "tasks": [{"path": package_path}],
        "agents": [{"model_name": "deepseek/deepseek-v4-pro"}],
        "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
        "extra_instruction_paths": [],
    }))
    request = ResearcherRequest(
        request_id="unreviewed-hint",
        run_id="run-1",
        question_revision="r1",
        attempt_index=1,
        mode="hint",
        package_path=package_path,
        package_sha256=package_sha256(Path(package_path)),
        job_config_path=str(config),
        job_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        context_digests=("h" * 64,),
        researcher_thread_id="fresh-breaker",
        harness="dsh",
        model="deepseek-v4-pro",
    )
    request_path = evidence / "request.json"
    request_path.write_text(json.dumps(request.to_dict()))
    result = evidence / "result.json"
    result.write_text("{}")
    receipt_path = evidence / "receipt.json"
    receipt_path.write_text(json.dumps(asdict(ResearcherReceipt(
        request_id=request.request_id,
        classification="SCIENTIFIC_RESULT",
        reward=0.9,
        result_path=str(result),
        started_at="start",
        finished_at="finish",
        exit_code=0,
        result_sha256=hashlib.sha256(result.read_bytes()).hexdigest(),
        job_id="hint-job",
        trial_id="hint-trial",
        sandbox_id="hint-sandbox",
        session_id="hint-session",
        wall_time_sec=10,
    ))))
    capability = evidence / "capability.json"
    capability.write_text(json.dumps({
        "status": "CONSUMED",
        "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
    }))
    leakage = evidence / "leakage.json"
    leakage.write_text(json.dumps({
        "schema_version": 1,
        "evidence_type": "leakage-audit",
        "verdict": "PASS",
        "reviewer_independent": True,
        "leakage_free": True,
        "request_id": request.request_id,
        "package_sha256": request.package_sha256,
        "job_id": "hint-job",
        "trial_id": "hint-trial",
        "sandbox_id": "hint-sandbox",
        "session_id": "hint-session",
    }))

    with pytest.raises(WorkflowError, match="hint review"):
        flow.audit_researcher_receipt(
            Actor.REVIEWER,
            request_path=request_path,
            capability_path=capability,
            receipt_path=receipt_path,
            leakage_evidence_path=leakage,
            idempotency_key="unreviewed-hint-audit",
        )


def test_post_validation_must_bind_current_revision(tmp_path) -> None:
    flow = runtime_final_workflow(tmp_path)
    for index in range(2, 4):
        flow._record_attempt(
            Actor.REVIEWER,
            runtime_attempt(
                flow,
                request_id=f"fresh-request-{index}",
                sandbox_id=f"fresh-sandbox-{index}",
                classification="SCIENTIFIC_RESULT",
                score=0.4,
                attempt_index=index,
            ),
            f"audit-{index}",
        )
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="hint-request",
            sandbox_id="hint-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.9,
            mode="hint",
        ),
        "hint-audit",
    )
    invalid = post_validation(flow, tmp_path / "post-validation-invalid.json")
    value = json.loads(invalid.read_text())
    value["package_sha256"] = "0" * 64
    invalid.write_text(json.dumps(value))

    with pytest.raises(WorkflowError, match="does not bind"):
        flow.accept_post_validation(Actor.REVIEWER, invalid, "invalid-post-validation")


def test_post_validation_rechecks_runtime_closure_bytes(tmp_path) -> None:
    flow = runtime_final_workflow(tmp_path)
    for index in range(2, 4):
        flow._record_attempt(
            Actor.REVIEWER,
            runtime_attempt(
                flow,
                request_id=f"fresh-request-{index}",
                sandbox_id=f"fresh-sandbox-{index}",
                classification="SCIENTIFIC_RESULT",
                score=0.4,
                attempt_index=index,
            ),
            f"audit-{index}",
        )
    flow._record_attempt(
        Actor.REVIEWER,
        runtime_attempt(
            flow,
            request_id="hint-request",
            sandbox_id="hint-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.9,
            mode="hint",
        ),
        "hint-audit",
    )
    closure_path = Path(flow.snapshot.evidence["runtime_closure"]["path"])
    closure_path.write_text("{}")

    with pytest.raises(WorkflowError, match="current runtime closure"):
        flow.accept_post_validation(
            Actor.REVIEWER,
            post_validation(flow, tmp_path / "post-validation-tampered.json"),
            "tampered-runtime-post-validation",
        )


def test_teacher_cannot_claim_labwright_transition(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief"
    write_brief(brief)
    lock = tmp_path / "lock"
    lock.write_text("x")
    flow.attach_brief(Actor.TEACHER, brief, "brief")
    flow.lock_policies(Actor.TEACHER, lock, "lock")
    flow.request_environment(Actor.TEACHER, "request")
    with pytest.raises(WorkflowError, match="does not own"):
        flow.environment_ready(Actor.TEACHER, receipt(tmp_path), "bad")


def test_untried_environment_discovery_uses_explicit_runtime_first_migration(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief"
    write_brief(brief)
    lock = tmp_path / "lock"
    lock.write_text("x")
    flow.attach_brief(Actor.TEACHER, brief, "brief")
    flow.lock_policies(Actor.TEACHER, lock, "lock")
    flow.request_environment(Actor.TEACHER, "request")

    snapshot = flow.begin_authoring(Actor.TEACHER, "runtime-first-migration")

    assert snapshot.state is RunState.AUTHORING
    assert store.events()[-1].event_type == "authoring.runtime_first.migrated"


def test_environment_discovery_migration_rejects_existing_attempts(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief"
    write_brief(brief)
    lock = tmp_path / "lock"
    lock.write_text("x")
    flow.attach_brief(Actor.TEACHER, brief, "brief")
    flow.lock_policies(Actor.TEACHER, lock, "lock")
    current = flow.request_environment(Actor.TEACHER, "request")
    store.commit(
        actor=Actor.SYSTEM,
        event_type="legacy.attempt.restored",
        idempotency_key="legacy-attempt",
        payload={},
        snapshot=store.advance(current, attempts=(asdict(attempt(1)),)),
    )

    with pytest.raises(WorkflowError, match="without attempts"):
        flow.begin_authoring(Actor.TEACHER, "unsafe-migration")


def test_cannot_freeze_noncompliant_package(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief"
    write_brief(brief)
    lock = tmp_path / "lock"
    lock.write_text("x")
    flow.attach_brief(Actor.TEACHER, brief, "brief")
    flow.lock_policies(Actor.TEACHER, lock, "lock")
    flow.request_environment(Actor.TEACHER, "request")
    flow.environment_ready(Actor.LABWRIGHT, receipt(tmp_path), "ready")
    flow.begin_authoring(Actor.TEACHER, "author")
    attach_design_evidence(flow, tmp_path)
    broken = tmp_path / "broken"
    broken.mkdir()
    with pytest.raises(WorkflowError, match="lint"):
        flow.freeze_package(Actor.TEACHER, broken, "freeze")


def test_cannot_freeze_without_source_role_map_and_ground_truth_ledger(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief"
    write_brief(brief)
    lock = tmp_path / "lock"
    lock.write_text("x")
    flow.attach_brief(Actor.TEACHER, brief, "brief")
    flow.lock_policies(Actor.TEACHER, lock, "lock")
    flow.begin_authoring(Actor.TEACHER, "author")

    with pytest.raises(WorkflowError, match="source role map and Ground Truth ledger"):
        flow.freeze_package(Actor.TEACHER, package(tmp_path / "task"), "freeze")

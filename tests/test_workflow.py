from __future__ import annotations

import json
from datetime import UTC, datetime
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskfoundry.harbor_evidence import (
    VerifiedPersistentRound,
    VerifiedPersistentSession,
)
from taskfoundry.labwright import ArtifactIdentity, EnvironmentReceipt
from taskfoundry.labwright_runtime import (
    DeltaReceipt,
    DeltaState,
    ImageSealPlan,
    artifact_identity_sha256,
)
from taskfoundry.model import Actor, RunSnapshot, RunState
from taskfoundry.store import RunStore
from taskfoundry.researcher import ApprovedHint, CapabilityStore, ResearcherRequest
from taskfoundry.validation import AttemptEvidence, JobClassification
from taskfoundry.workflow import RunWorkflow, WorkflowError


def write_brief(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
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
                "evidence_roles": {
                    "q1": "候选方法",
                    "q2": "诊断证据",
                    "q3": "评价指标",
                },
                "public_inputs": ["input.csv"],
                "required_outputs": ["output.csv"],
                "hidden_evaluation_axes": ["迁移"],
                "environment_capabilities": ["python"],
                "difficulty_hypothesis": "需要迁移判断",
                "solvability_argument": "公开数据足以完成判断",
                "target_solution_time_sec": 1800,
            }
        )
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
    (root / "tests/test.sh").write_text(
        '#!/bin/sh\n[ "$1" = "--probe" ] && exit 0\necho 1 > /logs/verifier/reward.txt\n'
    )
    return root


def receipt(tmp_path, runtime_closure: Path | None = None):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("manifest")
    closure_sha256 = (
        hashlib.sha256(runtime_closure.read_bytes()).hexdigest()
        if runtime_closure
        else None
    )
    return EnvironmentReceipt(
        environment_key="a" * 64,
        lifecycle="STABLE",
        artifact=ArtifactIdentity(
            "lbg", "lbg://prod", "42", "1", "registry/task:fixed"
        ),
        workdir="/app",
        manifest_path=str(manifest),
        manifest_sha256="b" * 64,
        resource_digests=(),
        runtime_closure_path=str(runtime_closure.resolve())
        if runtime_closure
        else None,
        runtime_closure_sha256=closure_sha256,
        schema_version=2 if runtime_closure else 1,
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


def runtime_receipt(
    flow: RunWorkflow,
    tmp_path: Path,
    *,
    source_request_id: str = "source-request",
    source_sandbox_id: str = "source-sandbox",
    source_trace_value: dict | None = None,
) -> tuple[Path, DeltaReceipt, ArtifactIdentity]:
    baseline = baseline_artifact()
    builder = ArtifactIdentity(
        "lbg",
        "lbg://prod",
        "42",
        "builder",
        "registry/builder:fixed",
        "sha256:" + "2" * 64,
    )
    source_trace = runtime_evidence_file(
        tmp_path,
        "source-failure.json",
        source_trace_value,
    )
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
        source_researcher_request_id=source_request_id,
        source_sandbox_id=source_sandbox_id,
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
    request_id: str = "fresh-request",
    sandbox_id: str = "fresh-sandbox",
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
            "researcher_request_id": request_id,
            "sandbox_id": sandbox_id,
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
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_type": "difficulty-revision",
                "source_package_sha256": flow.snapshot.package_digest,
                "next_revision": revision,
                "scientific_objective_unchanged": True,
                "change_summary": "increase reasoning difficulty without changing the scientific contract",
            }
        )
    )
    return path


def ready_workflow(tmp_path):
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "task"),
        validation_session_id="validation-session",
        researcher_thread_id="researcher-thread",
        idempotency_key="direct-start",
    )
    return flow


def provisional_workflow(tmp_path):
    """构造当前首次运行优先的 blind-validation 状态。"""
    return ready_workflow(tmp_path)


def direct_workflow(tmp_path: Path) -> RunWorkflow:
    """构造不含设计、Reviewer 或环境前置门的 AUTHORING。"""
    store = RunStore(tmp_path / "direct-run")
    store.initialize("direct-run-1")
    question_root = tmp_path / "3"
    question_root.mkdir()
    brief = question_root / "QuestionDesignBrief.json"
    write_brief(brief)
    brief_markdown = question_root / "QuestionDesignBrief.md"
    brief_markdown.write_text("# 选择方法\n\nbrief-1\n")

    def skill_validator(path: Path, question: int, stage: str) -> dict:
        assert question == 3
        value = json.loads(path.read_text())
        assert value["stage"] == stage
        return value

    flow = RunWorkflow(
        store,
        skill_validator=skill_validator,
        brief_locator=lambda question: (brief, brief_markdown),
    )
    activations = {}
    for stage in ("outline", "author"):
        prompt = tmp_path / f"{stage}-prompt.txt"
        prompt.write_text(f"{stage} frozen prompt")
        activation = tmp_path / f"{stage}-activation.json"
        activation.write_text(
            json.dumps(
                {
                    "stage": stage,
                    "batch_id": "batch-1",
                    "batch_questions": [3],
                    "stable_generation": 1,
                    "stable_lock_snapshot": {"sha256": "a" * 64},
                    "input_set_sha256": stage[0] * 64,
                    "prompt_bundle": {"path": str(prompt)},
                }
            )
        )
        activations[stage] = activation
    flow.accept_teacher_skill_activation(
        Actor.TEACHER,
        question=3,
        stage="outline",
        activation_path=activations["outline"],
        idempotency_key="direct-outline-skill",
    )
    flow.attach_brief(Actor.TEACHER, brief, "direct-brief")
    flow.accept_teacher_skill_activation(
        Actor.TEACHER,
        question=3,
        stage="author",
        activation_path=activations["author"],
        idempotency_key="direct-author-skill",
    )
    flow.begin_authoring(Actor.TEACHER, "direct-authoring")
    return flow


def test_workflow_rejects_brief_and_authoring_without_skill_activations(
    tmp_path,
) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief.json"
    write_brief(brief)

    with pytest.raises(WorkflowError, match="outline Skill"):
        flow.attach_brief(Actor.TEACHER, brief, "brief")
    with pytest.raises(WorkflowError, match="Skill contract"):
        flow.begin_authoring(Actor.TEACHER, "authoring")


def test_workflow_snapshot_requires_initialized_run(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError, match="not initialized"):
        _ = RunWorkflow(RunStore(tmp_path / "missing-run")).snapshot


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"batch_id": "other"}, "contract drifted"),
        ({"batch_questions": [3, 4]}, "contract drifted"),
        ({"stable_generation": 2}, "contract drifted"),
        ({"batch_questions": None}, "frozen batch roster"),
        ({"stable_lock_snapshot": {}}, "frozen stable version"),
        ({"input_set_sha256": "z" * 64}, "contract drifted"),
    ],
)
def test_teacher_knowledge_revalidation_fails_closed_on_cross_stage_drift(
    tmp_path: Path,
    change: dict[str, object],
    message: str,
) -> None:
    flow = direct_workflow(tmp_path)

    def validator(path: Path, question: int, stage: str) -> dict:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value | (change if stage == "author" else {})

    verifier = RunWorkflow(
        flow.store,
        skill_validator=validator,
        brief_locator=flow.brief_locator,
    )
    with pytest.raises(WorkflowError, match=message):
        verifier._revalidate_teacher_knowledge(flow.snapshot, require_author=True)


def test_direct_start_atomically_freezes_and_opens_linear_session(tmp_path) -> None:
    flow = direct_workflow(tmp_path)
    source = package(tmp_path / "direct-task")

    snapshot = flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=source,
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="direct-start",
    )

    assert snapshot.state is RunState.BLIND_VALIDATION
    assert snapshot.package_digest
    assert Path(str(snapshot.package_path)).is_relative_to(flow.store.run_dir)
    assert Path(str(snapshot.package_path)) != source.resolve()
    assert "design_evidence" not in snapshot.evidence
    assert "health" not in snapshot.evidence
    assert snapshot.environment_key is None
    assert snapshot.evidence["validation_session"] == {
        "schema_version": 2,
        "validation_session_id": "validation-session-1",
        "researcher_thread_id": "researcher-thread-1",
        "execution_owner": "desktop-thread",
        "question_revision": "r1",
        "package_sha256": snapshot.package_digest,
        "status": "ACTIVE",
    }
    sequence = snapshot.sequence
    repeated = flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=source,
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="direct-start",
    )
    assert repeated.sequence == sequence


def test_validation_start_rejects_reused_key_with_changed_identity(tmp_path) -> None:
    flow = direct_workflow(tmp_path)
    source = package(tmp_path / "direct-task")
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=source,
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="direct-start",
    )

    with pytest.raises(WorkflowError, match="idempotency"):
        flow.freeze_and_start_validation_session(
            Actor.TEACHER,
            package=source,
            validation_session_id="changed-session",
            researcher_thread_id="researcher-thread-1",
            idempotency_key="direct-start",
        )


def record_direct_round(
    flow: RunWorkflow,
    tmp_path: Path,
    round_index: int,
    *,
    score: float,
    mode: str = "blind",
) -> RunState:
    """持久化一轮绑定已接纳前序记录链的 schema-v2 Harbor 结果。"""
    request_id = f"direct-request-{round_index}"
    handoff_root = flow.store.run_dir / "researcher-requests"
    directory = handoff_root / request_id
    directory.mkdir(parents=True)
    session = flow.snapshot.evidence["validation_session"]
    prior = tuple(session.get("round_receipt_sha256s", ()))
    prior_history_paths = tuple(session.get("round_history_paths", ()))
    jobs = directory / "harbor-jobs"
    job_name = f"job-{round_index}"
    config = directory / "job.json"
    hint_fields = {}
    contexts = prior
    context_paths = list(prior_history_paths)
    if mode == "hint":
        hint_path = directory / "approved-hint.json"
        hint_path.write_text(
            json.dumps(
                ApprovedHint(
                    validation_session_id=session["validation_session_id"],
                    round_index=round_index,
                    content="检查边界条件。",
                    teacher_declares_non_answer=True,
                ).to_dict()
            )
        )
        hint_digest = hashlib.sha256(hint_path.read_bytes()).hexdigest()
        contexts = prior + (hint_digest,)
        context_paths.append(str(hint_path))
        hint_fields = {
            "approved_hint_path": str(hint_path),
            "approved_hint_sha256": hint_digest,
        }
    config.write_text(
        json.dumps(
            {
                "jobs_dir": str(jobs),
                "job_name": job_name,
                "tasks": [{"path": flow.snapshot.package_path}],
                "agents": [{"model_name": "deepseek/deepseek-v4-pro"}],
                "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
                "extra_instruction_paths": context_paths,
            }
        )
    )
    request = ResearcherRequest(
        request_id=request_id,
        run_id=flow.snapshot.run_id,
        question_revision=flow.snapshot.question_revision,
        attempt_index=round_index,
        mode=mode,
        package_path=str(flow.snapshot.package_path),
        package_sha256=str(flow.snapshot.package_digest),
        job_config_path=str(config),
        job_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        context_digests=contexts,
        researcher_thread_id=session["researcher_thread_id"],
        validation_session_id=session["validation_session_id"],
        round_index=round_index,
        prior_round_receipt_sha256s=prior,
        prior_round_history_paths=prior_history_paths,
        harness="dsh",
        model="deepseek-v4-pro",
        schema_version=2,
        **hint_fields,
    )
    request_path = directory / "request.json"
    request_path.write_text(json.dumps(request.to_dict()))
    capability_path = directory / "capability.json"
    capability_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "CONSUMED",
                "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
                "researcher_thread_id": session["researcher_thread_id"],
                "consumed_by_thread_id": session["researcher_thread_id"],
            }
        )
    )
    job = jobs / job_name
    trial = job / f"trial-{round_index}"
    trial.joinpath("agent").mkdir(parents=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": f"job-id-{round_index}",
                "started_at": "2026-08-27T00:00:00+00:00",
                "finished_at": "2026-08-27T00:00:10+00:00",
                "stats": {
                    "n_errored_trials": 0,
                    "evals": {
                        "solver": {
                            "metrics": [{"reward": score}],
                            "exception_stats": {},
                        }
                    },
                },
            }
        )
    )
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": f"trial-id-{round_index}",
                "agent_result": {"metadata": None},
            }
        )
    )
    (trial / "agent/codex.txt").write_text(
        json.dumps(
            {"type": "thread.started", "thread_id": f"inner-session-{round_index}"}
        )
        + "\n"
    )
    (job / "job.log").write_text(
        f"Sandbox created: provider-agent-{round_index}\n"
        f"Sandbox created: provider-verifier-{round_index}\n"
    )
    (job / "provider-identities.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "harbor-adapter-job-log",
                "job_log_sha256": hashlib.sha256(
                    (job / "job.log").read_bytes()
                ).hexdigest(),
                "agent_sandbox_id": f"provider-agent-{round_index}",
                "verifier_sandbox_id": f"provider-verifier-{round_index}",
            }
        )
    )
    return flow.record_validation_round(
        Actor.TEACHER,
        request_id=request_id,
        idempotency_key=f"direct-round-{round_index}",
    ).state


def test_direct_session_stops_after_first_passing_blind(tmp_path) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "direct-round-task"),
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="direct-round-start",
    )

    assert record_direct_round(flow, tmp_path, 1, score=1.0) is RunState.TOO_EASY
    session = flow.snapshot.evidence["validation_session"]
    assert session["status"] == "CLOSED_TOO_EASY"
    assert len(session["round_receipt_sha256s"]) == 1


def test_workflow_derives_only_legal_researcher_request_from_current_session(
    tmp_path,
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "direct-request-task"),
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="direct-request-start",
    )
    config = tmp_path / "job-config.json"
    config.write_text(
        json.dumps(
            {
                "tasks": [{"path": flow.snapshot.package_path}],
                "agents": [{"model_name": "matmaster/gpt-5.6-sol"}],
                "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
                "extra_instruction_paths": [],
            }
        ),
        encoding="utf-8",
    )

    hint = tmp_path / "premature-hint.json"
    hint.write_text("{}", encoding="utf-8")
    with pytest.raises(WorkflowError, match="blind validation cannot"):
        flow.issue_researcher_request(
            Actor.TEACHER,
            request_id="request-with-hint",
            job_config_path=config,
            approved_hint_path=hint,
        )
    bad_config = tmp_path / "bad-job-config.json"
    bad_config.write_text(
        json.dumps(
            {
                "tasks": [{"path": "/wrong-package"}],
                "agents": [{"model_name": "matmaster/gpt-5.6-sol"}],
                "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
                "extra_instruction_paths": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match="does not match current workflow"):
        flow.issue_researcher_request(
            Actor.TEACHER,
            request_id="bad-request",
            job_config_path=bad_config,
        )
    with pytest.raises(WorkflowError, match="launch artifact is unavailable"):
        flow.issue_researcher_request(
            Actor.TEACHER,
            request_id="missing-config",
            job_config_path=tmp_path / "missing-job-config.json",
        )
    handoff = flow.issue_researcher_request(
        Actor.TEACHER,
        request_id="request-1",
        job_config_path=config,
    )
    request = ResearcherRequest.from_dict(
        json.loads(Path(handoff.request_path).read_text(encoding="utf-8"))
    )
    assert request.run_id == flow.snapshot.run_id
    assert request.researcher_thread_id == "researcher-thread-1"
    assert request.validation_session_id == "validation-session-1"
    assert request.attempt_index == request.round_index == 1
    assert request.mode == "blind"
    assert request.prior_round_history_paths == ()
    with pytest.raises(WorkflowError, match="round already has"):
        flow.issue_researcher_request(
            Actor.TEACHER,
            request_id="request-duplicate-round",
            job_config_path=config,
        )
    not_ready_store = RunStore(tmp_path / "not-ready")
    not_ready_store.initialize("not-ready")
    with pytest.raises(WorkflowError, match="invalid from DESIGNING"):
        RunWorkflow(not_ready_store).issue_researcher_request(
            Actor.TEACHER,
            request_id="request-2",
            job_config_path=config,
        )


def test_workflow_issues_one_schema3_request_for_persistent_harbor_session(
    tmp_path,
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "persistent-task"),
        validation_session_id="persistent-session-1",
        researcher_thread_id="persistent-researcher-1",
        idempotency_key="persistent-start",
    )
    source_controller = tmp_path / "temporary-controller"
    config = tmp_path / "persistent-job-config.json"
    config.write_text(
        json.dumps(
            {
                "tasks": [{"path": flow.snapshot.package_path}],
                "agents": [
                    {
                        "model_name": "matmaster/gpt-5.6-sol",
                        "kwargs": {
                            "persistent_validation": {
                                "schema_version": 1,
                                "validation_session_id": "persistent-session-1",
                                "controller_dir": str(source_controller),
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
        )
    )

    handoff = flow.issue_researcher_request(
        Actor.TEACHER,
        request_id="persistent-request-1",
        job_config_path=config,
    )

    request = ResearcherRequest.from_dict(
        json.loads(Path(handoff.request_path).read_text())
    )
    frozen = json.loads(Path(request.job_config_path).read_text())
    assert request.schema_version == 3
    assert request.mode == "interactive"
    assert request.controller_dir == str(
        (Path(handoff.request_path).parent / "validation-control").resolve()
    )
    assert (
        frozen["agents"][0]["kwargs"]["persistent_validation"]["controller_dir"]
        == request.controller_dir
    )
    assert frozen["extra_instruction_paths"] == []


def test_workflow_uses_runtime_owned_researcher_without_desktop_thread(
    tmp_path,
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "runtime-owned-task"),
        validation_session_id="runtime-session-1",
        researcher_thread_id=None,
        idempotency_key="runtime-owned-start",
    )
    config = tmp_path / "runtime-owned-job-config.json"
    config.write_text(
        json.dumps(
            {
                "tasks": [{"path": flow.snapshot.package_path}],
                "agents": [
                    {
                        "model_name": "matmaster/gpt-5.6-sol",
                        "kwargs": {
                            "persistent_validation": {
                                "schema_version": 1,
                                "validation_session_id": "runtime-session-1",
                                "controller_dir": str(tmp_path / "controller"),
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
        )
    )

    handoff = flow.issue_researcher_request(
        Actor.TEACHER,
        request_id="runtime-owned-request-1",
        job_config_path=config,
    )
    request = ResearcherRequest.from_dict(
        json.loads(Path(handoff.request_path).read_text())
    )

    assert request.schema_version == 4
    assert request.researcher_thread_id == "harbor-runtime:runtime-session-1"
    assert (
        CapabilityStore(flow.store.run_dir / "researcher-requests")
        .redeem_runtime(handoff)
        .request_id
        == "runtime-owned-request-1"
    )


def test_workflow_atomically_imports_persistent_first_blind_pass(
    tmp_path, monkeypatch
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "persistent-result-task"),
        validation_session_id="persistent-session-1",
        researcher_thread_id="persistent-researcher-1",
        idempotency_key="persistent-result-start",
    )
    request = ResearcherRequest(
        request_id="persistent-request-1",
        run_id=flow.snapshot.run_id,
        question_revision=flow.snapshot.question_revision,
        attempt_index=1,
        mode="interactive",
        package_path=str(flow.snapshot.package_path),
        package_sha256=str(flow.snapshot.package_digest),
        job_config_path="/canonical/job-config.json",
        job_config_sha256="a" * 64,
        context_digests=(),
        researcher_thread_id="persistent-researcher-1",
        validation_session_id="persistent-session-1",
        controller_dir="/canonical/validation-control",
        schema_version=3,
    )
    round_evidence = VerifiedPersistentRound(
        round_index=1,
        mode="blind",
        classification=JobClassification.SCIENTIFIC_RESULT,
        score=1.0,
        wall_time_sec=10.0,
        verifier_sandbox_id="verifier-sandbox-1",
        result_path="/canonical/round-01-result.json",
        result_sha256="b" * 64,
        decision_path="/canonical/round-01-decision.json",
        decision_sha256="c" * 64,
        decision_action="STOP_TOO_EASY",
        artifact_manifest_path="/canonical/manifest.json",
        artifact_manifest_sha256="d" * 64,
    )
    verified = VerifiedPersistentSession(
        request=request,
        request_path="/canonical/request.json",
        request_sha256="e" * 64,
        capability_path="/canonical/capability.json",
        capability_sha256="f" * 64,
        job_config_path=request.job_config_path,
        job_config_sha256=request.job_config_sha256,
        job_id="persistent-job",
        trial_id="persistent-trial",
        agent_sandbox_id="persistent-agent-sandbox",
        harness_session_id="persistent-codex-session",
        wall_time_sec=11.0,
        job_result_path="/canonical/job-result.json",
        job_result_sha256="1" * 64,
        trial_result_path="/canonical/trial-result.json",
        trial_result_sha256="2" * 64,
        job_log_path="/canonical/job.log",
        job_log_sha256="3" * 64,
        provider_identity_path="/canonical/provider.json",
        provider_identity_sha256="4" * 64,
        rounds=(round_evidence,),
    )

    class Importer:
        def __init__(self, _root):
            pass

        def import_persistent_session(self, _request_id):
            return verified

    monkeypatch.setattr("taskfoundry.workflow.HarborEvidenceImporter", Importer)

    snapshot = flow.record_persistent_validation_session(
        Actor.TEACHER,
        request_id="persistent-request-1",
        idempotency_key="persistent-import",
    )

    assert snapshot.state is RunState.TOO_EASY
    assert len(snapshot.attempts) == 1
    assert snapshot.attempts[0]["schema_version"] == 3
    assert snapshot.evidence["validation_session"]["status"] == "CLOSED_TOO_EASY"


def test_persistent_hint_is_materialized_from_bound_teacher_decision(
    tmp_path: Path,
) -> None:
    flow = direct_workflow(tmp_path)
    decision = tmp_path / "round-03-decision.json"
    decision.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "validation_session_id": "persistent-session-1",
                "round_index": 3,
                "result_sha256": "a" * 64,
                "action": "CONTINUE_HINT",
                "hint": "先比较每个候选的连续误差。",
                "teacher_declares_non_answer": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    verified = SimpleNamespace(
        request=SimpleNamespace(
            request_id="persistent-request-1",
            validation_session_id="persistent-session-1",
        ),
        rounds=(
            SimpleNamespace(
                round_index=3,
                mode="blind",
                decision_path=str(decision),
            ),
            SimpleNamespace(
                round_index=4,
                mode="hint",
                decision_path=str(tmp_path / "round-04-decision.json"),
            ),
        ),
    )

    bindings = flow._materialize_persistent_hints(verified)

    path, digest = bindings[4]
    hint = ApprovedHint.from_path(path)
    assert hint == ApprovedHint(
        "persistent-session-1",
        1,
        "先比较每个候选的连续误差。",
        True,
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert flow._materialize_persistent_hints(verified) == bindings


def test_persistent_platform_failure_stays_on_same_scientific_attempt(
    tmp_path, monkeypatch
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "persistent-platform-task"),
        validation_session_id="persistent-session-1",
        researcher_thread_id="persistent-researcher-1",
        idempotency_key="persistent-platform-start",
    )
    request = ResearcherRequest(
        request_id="persistent-platform-request-1",
        run_id=flow.snapshot.run_id,
        question_revision=flow.snapshot.question_revision,
        attempt_index=1,
        mode="interactive",
        package_path=str(flow.snapshot.package_path),
        package_sha256=str(flow.snapshot.package_digest),
        job_config_path="/canonical/job-config.json",
        job_config_sha256="a" * 64,
        context_digests=(),
        researcher_thread_id="persistent-researcher-1",
        validation_session_id="persistent-session-1",
        controller_dir="/canonical/validation-control",
        schema_version=3,
    )
    failed_round = VerifiedPersistentRound(
        round_index=1,
        mode="blind",
        classification=JobClassification.PLATFORM_FAILURE,
        score=None,
        wall_time_sec=2.0,
        verifier_sandbox_id=None,
        result_path="/canonical/round-01-result.json",
        result_sha256="b" * 64,
        decision_path=None,
        decision_sha256=None,
        decision_action=None,
        artifact_manifest_path="/canonical/manifest.json",
        artifact_manifest_sha256="d" * 64,
    )
    verified = VerifiedPersistentSession(
        request=request,
        request_path="/canonical/request.json",
        request_sha256="e" * 64,
        capability_path="/canonical/capability.json",
        capability_sha256="f" * 64,
        job_config_path=request.job_config_path,
        job_config_sha256=request.job_config_sha256,
        job_id="failed-job",
        trial_id="failed-trial",
        agent_sandbox_id="failed-agent-sandbox",
        harness_session_id="",
        wall_time_sec=2.0,
        job_result_path="/canonical/job-result.json",
        job_result_sha256="1" * 64,
        trial_result_path="/canonical/trial-result.json",
        trial_result_sha256="2" * 64,
        job_log_path="/canonical/job.log",
        job_log_sha256="3" * 64,
        provider_identity_path="/canonical/provider.json",
        provider_identity_sha256="4" * 64,
        rounds=(failed_round,),
    )

    class Importer:
        def __init__(self, _root):
            pass

        def import_persistent_session(self, _request_id):
            return verified

    monkeypatch.setattr("taskfoundry.workflow.HarborEvidenceImporter", Importer)

    snapshot = flow.record_persistent_validation_session(
        Actor.TEACHER,
        request_id=request.request_id,
        idempotency_key="persistent-platform-import",
    )

    assert snapshot.state is RunState.BLIND_VALIDATION
    assert snapshot.attempts[0]["classification"] == "PLATFORM_FAILURE"
    assert snapshot.evidence["validation_decision"]["action"] == "RETRY_SAME_ATTEMPT"
    assert snapshot.evidence["validation_session"]["status"] == "ACTIVE"
    assert set(snapshot.evidence["persistent_harbor_sessions"]) == {request.request_id}

    config = tmp_path / "persistent-platform-retry.json"
    config.write_text(
        json.dumps(
            {
                "tasks": [{"path": snapshot.package_path}],
                "agents": [
                    {
                        "model_name": "matmaster/gpt-5.6-sol",
                        "kwargs": {
                            "persistent_validation": {
                                "schema_version": 1,
                                "validation_session_id": "persistent-session-1",
                                "controller_dir": str(tmp_path / "next-controller"),
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
        ),
        encoding="utf-8",
    )
    retry = flow.issue_researcher_request(
        Actor.TEACHER,
        request_id="persistent-platform-request-2",
        job_config_path=config,
    )
    retry_request = ResearcherRequest.from_dict(
        json.loads(Path(retry.request_path).read_text())
    )
    assert retry_request.attempt_index == 1
    assert retry_request.validation_session_id == "persistent-session-1"


def test_hint_request_requires_teacher_declaration(tmp_path) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "hint-request-task"),
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="hint-request-start",
    )
    for index in range(1, 4):
        record_direct_round(flow, tmp_path, index, score=0.4)
    config = tmp_path / "hint-job-config.json"
    session = flow.snapshot.evidence["validation_session"]
    config.write_text(
        json.dumps(
            {
                "tasks": [{"path": flow.snapshot.package_path}],
                "agents": [{"model_name": "matmaster/gpt-5.6-sol"}],
                "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
                "extra_instruction_paths": session["round_history_paths"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowError, match="Teacher-approved hint"):
        flow.issue_researcher_request(
            Actor.TEACHER,
            request_id="hint-request",
            job_config_path=config,
        )


def test_researcher_issue_rejects_malformed_canonical_round_ledger(
    tmp_path: Path,
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "ledger-task"),
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="ledger-start",
    )
    current = flow.snapshot
    corrupted = flow.store.advance(
        current,
        evidence=current.evidence | {"harbor_rounds": []},
    )
    flow.store.commit(
        actor=Actor.TEACHER,
        event_type="test.corrupt.ledger",
        idempotency_key="test-corrupt-ledger",
        payload={},
        snapshot=corrupted,
    )
    config = tmp_path / "ledger-job.json"
    config.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkflowError, match="round ledger is invalid"):
        flow.issue_researcher_request(
            Actor.TEACHER,
            request_id="ledger-request",
            job_config_path=config,
        )


def test_researcher_issue_rejects_inconsistent_linear_history_ledger(
    tmp_path: Path,
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "history-ledger-task"),
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="history-ledger-start",
    )
    current = flow.snapshot
    session = dict(current.evidence["validation_session"])
    session["round_receipt_sha256s"] = ["a" * 64]
    corrupted = flow.store.advance(
        current,
        evidence=current.evidence | {"validation_session": session},
    )
    flow.store.commit(
        actor=Actor.TEACHER,
        event_type="test.corrupt.history",
        idempotency_key="test-corrupt-history",
        payload={},
        snapshot=corrupted,
    )

    with pytest.raises(WorkflowError, match="history ledger is inconsistent"):
        flow.issue_researcher_request(
            Actor.TEACHER,
            request_id="history-request",
            job_config_path=tmp_path / "not-needed.json",
        )


def test_record_round_is_idempotent_after_state_transition(tmp_path) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "direct-round-task"),
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="direct-round-start",
    )
    assert record_direct_round(flow, tmp_path, 1, score=1.0) is RunState.TOO_EASY
    sequence = flow.snapshot.sequence

    repeated = flow.record_validation_round(
        Actor.TEACHER,
        request_id="direct-request-1",
        idempotency_key="direct-round-1",
    )

    assert repeated.sequence == sequence
    assert len(repeated.attempts) == 1


def test_record_round_replay_repairs_registry_after_commit_close_crash(
    tmp_path, monkeypatch
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "direct-round-task"),
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="direct-round-start",
    )
    registry = flow._validation_session_registry()
    original_close = registry.close
    monkeypatch.setattr(flow, "_validation_session_registry", lambda: registry)
    monkeypatch.setattr(
        registry,
        "close",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("crash after commit")
        ),
    )

    with pytest.raises(RuntimeError, match="crash after commit"):
        record_direct_round(flow, tmp_path, 1, score=1.0)
    assert flow.snapshot.state is RunState.TOO_EASY
    assert registry.records()[0].status == "ACTIVE"

    monkeypatch.setattr(registry, "close", original_close)
    repeated = flow.record_validation_round(
        Actor.TEACHER,
        request_id="direct-request-1",
        idempotency_key="direct-round-1",
    )

    assert repeated.state is RunState.TOO_EASY
    assert registry.records()[0].status == "CLOSED_TOO_EASY"


def test_revision_transition_repairs_registry_without_round_replay(
    tmp_path, monkeypatch
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "direct-round-task"),
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="direct-round-start",
    )
    registry = flow._validation_session_registry()
    original_close = registry.close
    monkeypatch.setattr(flow, "_validation_session_registry", lambda: registry)
    monkeypatch.setattr(
        registry,
        "close",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("crash after commit")
        ),
    )
    with pytest.raises(RuntimeError, match="crash after commit"):
        record_direct_round(flow, tmp_path, 1, score=1.0)
    monkeypatch.setattr(registry, "close", original_close)
    reason = difficulty_revision(flow, tmp_path / "too-easy.json")

    flow.begin_revision(Actor.TEACHER, "r2", reason, "revision-r2")

    assert registry.records()[0].status == "CLOSED_TOO_EASY"


def test_runtime_transition_repairs_registry_without_round_replay(
    tmp_path, monkeypatch
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "direct-round-task"),
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        idempotency_key="direct-round-start",
    )
    for index in range(1, 4):
        record_direct_round(flow, tmp_path, index, score=0.4)
    registry = flow._validation_session_registry()
    original_close = registry.close
    monkeypatch.setattr(flow, "_validation_session_registry", lambda: registry)
    monkeypatch.setattr(
        registry,
        "close",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("crash after commit")
        ),
    )
    with pytest.raises(RuntimeError, match="crash after commit"):
        record_direct_round(flow, tmp_path, 4, score=0.9, mode="hint")
    monkeypatch.setattr(registry, "close", original_close)

    flow.begin_runtime_finalization(Actor.TEACHER, "runtime-finalization")

    assert registry.records()[0].status == "CLOSED_VALIDATION_PASSED"


def test_runtime_closure_helpers_fail_closed_on_unbound_evidence(tmp_path) -> None:
    current = RunSnapshot(
        run_id="run",
        state=RunState.RUNTIME_FINALIZATION,
        sequence=1,
        package_digest="a" * 64,
        attempts=(
            {
                "request_id": "scientific",
                "sandbox_id": "sandbox",
                "classification": "SCIENTIFIC_RESULT",
                "frozen_contract_digest": "a" * 64,
                "attempt_index": 1,
            },
        ),
    )
    trace = {"researcher_request_id": "scientific", "sandbox_id": "sandbox"}
    scientific = RunWorkflow._runtime_scientific_attempt(current, trace)
    assert scientific["request_id"] == "scientific"
    with pytest.raises(WorkflowError, match="lacks"):
        RunWorkflow._runtime_scientific_attempt(
            current, {"researcher_request_id": "missing"}
        )
    wrong = RunSnapshot(**(current.__dict__ | {"package_digest": "b" * 64}))
    with pytest.raises(WorkflowError, match="another package"):
        RunWorkflow._runtime_scientific_attempt(wrong, trace)

    plan = SimpleNamespace(delta_receipts=({"request_id": "delta"},))
    with pytest.raises(WorkflowError, match="ledger"):
        RunWorkflow._validate_accepted_deltas(
            RunSnapshot(**(current.__dict__ | {"evidence": {"runtime_deltas": []}})),
            plan,
            scientific,
        )
    with pytest.raises(WorkflowError, match="unaccepted"):
        RunWorkflow._validate_accepted_deltas(current, plan, scientific)
    bad_attempt = RunSnapshot(
        **(
            current.__dict__
            | {
                "evidence": {
                    "runtime_deltas": {
                        "delta": {
                            "receipt": {"request_id": "delta"},
                            "source_attempt_index": 2,
                        }
                    }
                }
            }
        )
    )
    with pytest.raises(WorkflowError, match="attempt identity"):
        RunWorkflow._validate_accepted_deltas(bad_attempt, plan, scientific)


def test_workflow_strict_json_and_runtime_schema_errors(tmp_path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(WorkflowError, match="missing"):
        RunWorkflow._json_object(missing)
    for name, content, message in (
        ("duplicate.json", '{"a":1,"a":2}', "duplicate"),
        ("nan.json", '{"a":NaN}', "non-finite"),
        ("list.json", "[]", "must be an object"),
    ):
        path = tmp_path / name
        path.write_text(content)
        with pytest.raises(WorkflowError, match=message):
            RunWorkflow._json_object(path)
    invalid = tmp_path / "invalid-runtime.json"
    invalid.write_text("{}")
    with pytest.raises(WorkflowError, match="delta receipt schema"):
        RunWorkflow._delta_receipt(invalid)
    with pytest.raises(WorkflowError, match="closure schema"):
        RunWorkflow._image_seal_plan(invalid)


def test_deprecated_policy_lock_and_invalid_skill_stage_are_closed(tmp_path) -> None:
    flow = direct_workflow(tmp_path)
    with pytest.raises(WorkflowError, match="superseded"):
        flow.lock_policies(Actor.TEACHER, tmp_path / "policy", "policy")
    with pytest.raises(WorkflowError, match="outline or author"):
        flow.accept_teacher_skill_activation(
            Actor.TEACHER,
            question=3,
            stage="invalid",
            activation_path=tmp_path / "missing",
            idempotency_key="invalid-stage",
        )


def test_incomplete_legacy_session_migrates_to_new_revision(tmp_path) -> None:
    flow = ready_workflow(tmp_path)

    snapshot = flow.migrate_incomplete_validation_session(
        Actor.TEACHER,
        "r2",
        "migrate-r2",
    )

    assert snapshot.state is RunState.AUTHORING
    assert snapshot.question_revision == "r2"
    assert snapshot.attempts == ()
    assert snapshot.package_digest is None
    assert (
        snapshot.evidence["migrated_validation_session"]["status"] == "CLOSED_MIGRATED"
    )
    assert (
        flow.migrate_incomplete_validation_session(
            Actor.TEACHER,
            "r2",
            "migrate-r2",
        ).sequence
        == snapshot.sequence
    )


def test_completed_linear_session_can_be_revalidated_with_persistent_protocol(
    tmp_path,
) -> None:
    flow = ready_workflow(tmp_path)
    current = flow.snapshot
    completed = flow.store.advance(current, state=RunState.COMPLETED)
    flow.store.commit(
        actor=Actor.TEACHER,
        event_type="legacy.completed",
        idempotency_key="legacy-completed",
        payload={},
        snapshot=completed,
    )

    snapshot = flow.migrate_incomplete_validation_session(
        Actor.TEACHER,
        "r2-persistent-validation",
        "migrate-completed-r2",
    )

    assert snapshot.state is RunState.AUTHORING
    assert snapshot.question_revision == "r2-persistent-validation"
    assert snapshot.evidence["migrated_validation_session"]["source_state"] == (
        "COMPLETED"
    )


def test_direct_session_hint_pass_completes_without_post_review_gate(tmp_path) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "direct-hint-task"),
        validation_session_id="validation-session-hint",
        researcher_thread_id="researcher-thread-hint",
        idempotency_key="direct-hint-start",
    )
    for index in range(1, 4):
        record_direct_round(flow, tmp_path, index, score=0.4)
    assert flow.snapshot.state is RunState.HINT_VALIDATION

    assert (
        record_direct_round(flow, tmp_path, 4, score=0.9, mode="hint")
        is RunState.VALIDATION_PASSED
    )
    assert flow.snapshot.evidence["validation_session"]["status"] == "VALIDATION_PASSED"


def test_direct_validation_requires_runtime_finalization_before_completion(
    tmp_path,
) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "direct-final-task"),
        validation_session_id="validation-session-final",
        researcher_thread_id="researcher-thread-final",
        idempotency_key="direct-final-start",
    )
    for index in range(1, 4):
        record_direct_round(flow, tmp_path, index, score=0.4)
    record_direct_round(flow, tmp_path, 4, score=0.9, mode="hint")

    snapshot = flow.begin_runtime_finalization(Actor.TEACHER, "runtime-finalization")
    assert snapshot.state is RunState.RUNTIME_FINALIZATION
    latest = snapshot.attempts[-1]
    closure = runtime_closure(
        flow,
        tmp_path,
        baseline_artifact(),
        request_id=latest["request_id"],
        sandbox_id=latest["sandbox_id"],
    )
    flow.accept_runtime_closure(Actor.LABWRIGHT, closure, "direct-runtime-closure")

    completed = flow.bind_runtime_environment(
        Actor.LABWRIGHT,
        receipt(tmp_path, closure),
        "direct-runtime-environment",
    )
    assert completed.state is RunState.COMPLETED


def test_direct_start_rejects_missing_session_identity_or_bad_package(tmp_path) -> None:
    flow = direct_workflow(tmp_path)
    with pytest.raises(WorkflowError, match="session"):
        flow.freeze_and_start_validation_session(
            Actor.TEACHER,
            package=package(tmp_path / "identity-task"),
            validation_session_id="",
            researcher_thread_id="thread",
            idempotency_key="bad-identity",
        )
    broken = tmp_path / "broken-task"
    broken.mkdir()
    with pytest.raises(WorkflowError, match="launch probe"):
        flow.freeze_and_start_validation_session(
            Actor.TEACHER,
            package=broken,
            validation_session_id="session",
            researcher_thread_id="thread",
            idempotency_key="bad-package",
        )


def test_stable_environment_cannot_be_bound_before_validation_finalization(
    tmp_path,
) -> None:
    flow = provisional_workflow(tmp_path)

    with pytest.raises(WorkflowError, match="invalid from BLIND_VALIDATION"):
        flow.bind_runtime_environment(
            Actor.LABWRIGHT,
            receipt(tmp_path),
            "runtime-environment",
        )


def test_first_scientific_trace_does_not_open_environment_binding(tmp_path) -> None:
    flow = provisional_workflow(tmp_path)
    flow._record_attempt(
        Actor.TEACHER,
        runtime_attempt(
            flow,
            request_id="fresh-request",
            sandbox_id="fresh-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
        ),
        "audit-1",
    )
    with pytest.raises(WorkflowError, match="invalid from BLIND_VALIDATION"):
        flow.accept_runtime_closure(
            Actor.LABWRIGHT,
            runtime_closure(flow, tmp_path, baseline_artifact()),
            "runtime-closure",
        )


def test_runtime_delta_binds_failure_builder_and_fresh_retry(tmp_path) -> None:
    flow = provisional_workflow(tmp_path)
    flow._record_attempt(
        Actor.TEACHER,
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
        Actor.TEACHER,
        runtime_attempt(
            flow,
            request_id="fresh-request",
            sandbox_id="fresh-sandbox",
            classification="SCIENTIFIC_RESULT",
            score=0.4,
        ),
        "fresh-retry",
    )
    for index in (2, 3):
        flow._record_attempt(
            Actor.TEACHER,
            runtime_attempt(
                flow,
                request_id=f"blind-{index}",
                sandbox_id=f"blind-sandbox-{index}",
                classification="SCIENTIFIC_RESULT",
                score=0.4,
                attempt_index=index,
            ),
            f"blind-{index}",
        )
    flow._record_attempt(
        Actor.TEACHER,
        runtime_attempt(
            flow,
            request_id="hint-1",
            sandbox_id="hint-sandbox-1",
            classification="SCIENTIFIC_RESULT",
            score=0.9,
            mode="hint",
        ),
        "hint-1",
    )
    flow.begin_runtime_finalization(Actor.TEACHER, "runtime-finalization")
    closure = runtime_closure(flow, tmp_path, baseline, (delta,))

    snapshot = flow.accept_runtime_closure(
        Actor.LABWRIGHT,
        closure,
        "runtime-closure",
    )

    assert len(snapshot.attempts) == 5
    assert snapshot.evidence["runtime_closure"]["delta_request_ids"] == ["delta-1"]
    stable = receipt(tmp_path, closure)
    bound = flow.bind_runtime_environment(
        Actor.LABWRIGHT, stable, "runtime-environment"
    )
    assert bound.environment_key == stable.environment_key


def test_runtime_delta_can_supersede_zero_delta_closure_from_same_scientific_trace(
    tmp_path,
) -> None:
    flow = provisional_workflow(tmp_path)
    for index in range(1, 4):
        flow._record_attempt(
            Actor.TEACHER,
            runtime_attempt(
                flow,
                request_id=f"blind-{index}",
                sandbox_id=f"blind-sandbox-{index}",
                classification="SCIENTIFIC_RESULT",
                score=0.4,
                attempt_index=index,
            ),
            f"blind-{index}",
        )
    flow._record_attempt(
        Actor.TEACHER,
        runtime_attempt(
            flow,
            request_id="hint-1",
            sandbox_id="hint-sandbox-1",
            classification="SCIENTIFIC_RESULT",
            score=0.9,
            attempt_index=1,
            mode="hint",
        ),
        "hint-1",
    )
    flow.begin_runtime_finalization(Actor.TEACHER, "runtime-finalization")
    zero_closure = runtime_closure(
        flow,
        tmp_path,
        baseline_artifact(),
        request_id="hint-1",
        sandbox_id="hint-sandbox-1",
    )
    zero_sha256 = hashlib.sha256(zero_closure.read_bytes()).hexdigest()
    flow.accept_runtime_closure(Actor.LABWRIGHT, zero_closure, "zero-closure")

    delta_path, delta, baseline = runtime_receipt(
        flow,
        tmp_path,
        source_request_id="hint-1",
        source_sandbox_id="hint-sandbox-1",
        source_trace_value={
            "schema_version": 1,
            "classification": "ENVIRONMENT_FAILURE",
            "run_id": flow.snapshot.run_id,
            "question_revision": flow.snapshot.question_revision,
            "package_sha256": flow.snapshot.package_digest,
            "researcher_request_id": "hint-1",
            "sandbox_id": "hint-sandbox-1",
            "failure_stage": "public_scientific_capability_probe",
            "scientific_round_classification_unchanged": "SCIENTIFIC_RESULT",
            "scientific_round_count_increment": False,
        },
    )
    accepted = flow.accept_runtime_delta(
        Actor.LABWRIGHT,
        delta_path,
        "late-runtime-delta",
    )
    assert "runtime_closure" not in accepted.evidence
    assert accepted.evidence["superseded_runtime_closures"] == [
        {
            "path": str(zero_closure.resolve()),
            "sha256": zero_sha256,
            "reason": "TRACE_BACKED_DELTA_DISCOVERED_DURING_FINALIZATION",
        }
    ]

    closure = runtime_closure(
        flow,
        tmp_path,
        baseline,
        (delta,),
        request_id="hint-1",
        sandbox_id="hint-sandbox-1",
    )
    snapshot = flow.accept_runtime_closure(
        Actor.LABWRIGHT,
        closure,
        "replacement-closure",
    )
    assert snapshot.evidence["runtime_closure"]["delta_request_ids"] == ["delta-1"]


def test_runtime_first_rejects_legacy_stable_receipt_without_closure(tmp_path) -> None:
    flow = direct_workflow(tmp_path)
    flow.freeze_and_start_validation_session(
        Actor.TEACHER,
        package=package(tmp_path / "legacy-task"),
        validation_session_id="legacy-session",
        researcher_thread_id="legacy-thread",
        idempotency_key="legacy-start",
    )
    for index in range(1, 4):
        record_direct_round(flow, tmp_path, index, score=0.4)
    record_direct_round(flow, tmp_path, 4, score=0.9, mode="hint")
    flow.begin_runtime_finalization(Actor.TEACHER, "legacy-finalization")
    latest = flow.snapshot.attempts[-1]
    closure = runtime_closure(
        flow,
        tmp_path,
        baseline_artifact(),
        request_id=latest["request_id"],
        sandbox_id=latest["sandbox_id"],
    )
    flow.accept_runtime_closure(Actor.LABWRIGHT, closure, "runtime-closure")

    with pytest.raises(WorkflowError, match="does not bind"):
        flow.bind_runtime_environment(
            Actor.LABWRIGHT,
            receipt(tmp_path),
            "legacy-runtime-environment",
        )


def test_blind_pass_is_terminal_too_easy(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    flow._record_attempt(Actor.TEACHER, attempt(1, score=1), "audit")
    assert flow.snapshot.state is RunState.TOO_EASY


def test_too_easy_can_start_evidence_backed_revision(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    flow._record_attempt(Actor.TEACHER, attempt(1, score=0.9), "audit-1")
    reason = difficulty_revision(flow, tmp_path / "too-easy.json")

    snapshot = flow.begin_revision(Actor.TEACHER, "r2", reason, "revision-r2")

    assert snapshot.state is RunState.AUTHORING
    assert snapshot.attempts == ()
    assert snapshot.evidence["superseded_revision"]["next_revision"] == "r2"
    assert snapshot.environment_key is None
    assert snapshot.package_digest is None
    for key in (
        "package_lint",
        "design_evidence",
        "health",
        "bound_health",
        "environment",
        "runtime_closure",
        "runtime_deltas",
        "validation_decision",
        "validation_session",
        "harbor_rounds",
    ):
        assert key not in snapshot.evidence


def test_scientific_stop_blocked_can_start_evidence_backed_revision(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    current = flow.snapshot
    blocked = flow.store.advance(
        current,
        state=RunState.BLOCKED,
        evidence=current.evidence
        | {"validation_decision": {"action": "STOP_BLOCKED"}},
    )
    flow.store.commit(
        actor=Actor.TEACHER,
        event_type="validation.blocked",
        idempotency_key="scientific-stop-blocked",
        payload={},
        snapshot=blocked,
    )
    reason = difficulty_revision(flow, tmp_path / "blocked-revision.json")

    snapshot = flow.begin_revision(Actor.TEACHER, "r2", reason, "revision-r2")

    assert snapshot.state is RunState.AUTHORING
    assert snapshot.question_revision == "r2"
    assert snapshot.evidence["superseded_revision"]["next_revision"] == "r2"


def test_formal_review_blocker_can_start_sibling_from_runtime_finalization(
    tmp_path,
) -> None:
    flow = ready_workflow(tmp_path)
    for index in range(1, 4):
        flow._record_attempt(Actor.TEACHER, attempt(index), f"audit-{index}")
    flow._record_attempt(
        Actor.TEACHER,
        attempt(1, mode="hint", score=0.9),
        "hint-pass",
    )
    flow.begin_runtime_finalization(Actor.TEACHER, "runtime-finalization")
    reason = tmp_path / "formal-repair.json"
    reason.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_type": "formal-repair-revision",
                "source_package_sha256": flow.snapshot.package_digest,
                "next_revision": "r2",
                "scientific_objective_unchanged": True,
                "formal_review_verdict": "BLOCKED",
                "required_revalidation": True,
                "change_summary": "isolate untrusted predictor execution from private tests",
            }
        )
    )

    snapshot = flow.begin_revision(
        Actor.TEACHER,
        "r2",
        reason,
        "formal-repair-r2",
    )

    assert snapshot.state is RunState.AUTHORING
    assert snapshot.question_revision == "r2"
    assert snapshot.evidence["superseded_revision"]["reason_evidence"] == str(
        reason.resolve()
    )
    assert snapshot.attempts == ()


def test_hint_progression_cannot_reset_the_blind_ledger(tmp_path) -> None:
    flow = ready_workflow(tmp_path)
    for index in range(1, 4):
        flow._record_attempt(Actor.TEACHER, attempt(index), f"audit-{index}")
    reason = difficulty_revision(flow, tmp_path / "invalid-hint-revision.json")

    with pytest.raises(WorkflowError, match="invalid from HINT_VALIDATION"):
        flow.begin_revision(Actor.TEACHER, "r2", reason, "invalid-revision-r2")

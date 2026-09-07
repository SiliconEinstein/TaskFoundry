"""TaskFoundry 适配器、证据操作和多题调度的命令行入口。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Sequence

from .codex_agent import CodexDispatcher
from .harbor import (
    HarborJobSpec,
    PersistentValidationSpec,
    build_job_config,
    runtime_from_dict,
    write_job_config,
)
from .harbor_queue import HarborJobQueue, HarborQueueState
from .labwright import (
    ArtifactIdentity,
    EnvironmentReceipt,
    FileLabwrightRegistry,
    RuntimeSnapshot,
)
from .labwright_runtime import FileLabwrightRuntimeService
from .model import Actor
from .package import lint_package
from .policy import file_sha256
from .researcher import (
    CapabilityStore,
    IssuedHandoff,
    execute_harbor,
    write_json,
)
from .scheduler import QuestionScheduler
from .scheduler_worker import SchedulerWorker
from .skillbank import (
    close_skill_batch,
    evaluate_skill_candidate,
    reconcile_skill_batch,
    record_skill_attribution,
    record_revision_attribution,
    resolve_teacher_activation,
    validate_latest_brief,
)
from .store import RunStore
from .supervisor import CampaignSupervisor, register_supervisor_plan
from .supervisor_state import SupervisorPlan
from .validation_control import TeacherDecision
from .workflow import RunWorkflow


def parser() -> argparse.ArgumentParser:
    """构造公开命令行解析器。"""
    root = argparse.ArgumentParser(prog="taskfoundry")
    commands = root.add_subparsers(dest="command", required=True)

    lint = commands.add_parser("lint-package")
    lint.add_argument("package", type=Path)

    status = commands.add_parser("status")
    status.add_argument("run_dir", type=Path)

    authoring = commands.add_parser("begin-authoring")
    authoring.add_argument("run_dir", type=Path)

    bind_skill = commands.add_parser("bind-teacher-skill")
    bind_skill.add_argument("run_dir", type=Path)
    bind_skill.add_argument("question", type=int, choices=range(3, 33))
    bind_skill.add_argument("stage", choices=["outline", "author"])
    bind_skill.add_argument("activation", type=Path)

    attach_brief = commands.add_parser("attach-question-brief")
    attach_brief.add_argument("run_dir", type=Path)
    attach_brief.add_argument("brief", type=Path)

    resolve_skill = commands.add_parser("resolve-teacher-skill")
    resolve_skill.add_argument("question", type=int, choices=range(3, 33))
    resolve_skill.add_argument("stage", choices=["outline", "author"])
    resolve_skill.add_argument("attempt_id")
    resolve_skill.add_argument("--batch-id", required=True)
    resolve_skill.add_argument(
        "--batch-question",
        type=int,
        choices=range(3, 33),
        action="append",
        required=True,
    )
    resolve_skill.add_argument("--task-input", type=Path, action="append", default=[])

    validate_brief = commands.add_parser("validate-question-brief")
    validate_brief.add_argument("question", type=int, choices=range(3, 33))

    attribution = commands.add_parser("record-skill-attribution")
    attribution.add_argument("question", type=int, choices=range(3, 33))
    attribution.add_argument("evidence", type=Path)

    revision_attribution = commands.add_parser("record-revision-attribution")
    revision_attribution.add_argument("question", type=int, choices=range(3, 33))
    revision_attribution.add_argument("evidence", type=Path)

    reconcile = commands.add_parser("reconcile-skill-bank")
    reconcile.add_argument("batch_id")
    reconcile.add_argument(
        "--question", type=int, choices=range(3, 33), action="append", required=True
    )

    close_batch = commands.add_parser("close-skill-batch")
    close_batch.add_argument("batch_id")
    close_batch.add_argument(
        "--question", type=int, choices=range(3, 33), action="append", required=True
    )

    evaluate = commands.add_parser("evaluate-skill-candidate")
    evaluate.add_argument("plan", type=Path)
    evaluate.add_argument("verdict", type=Path)

    direct = commands.add_parser("freeze-and-start-validation")
    direct.add_argument("run_dir", type=Path)
    direct.add_argument("package", type=Path)
    direct.add_argument("--validation-session-id", required=True)
    direct.add_argument("--researcher-thread-id")

    recover_session = commands.add_parser("recover-validation-session")
    recover_session.add_argument("run_dir", type=Path)
    recover_session.add_argument("request", type=Path)

    record_round = commands.add_parser("record-validation-round")
    record_round.add_argument("run_dir", type=Path)
    record_round.add_argument("request_id")

    decide_round = commands.add_parser("decide-persistent-validation-round")
    decide_round.add_argument("run_dir", type=Path)
    decide_round.add_argument("request_id")
    decide_round.add_argument("round_index", type=int)
    decide_round.add_argument(
        "action",
        choices=[
            "CONTINUE_BLIND",
            "CONTINUE_HINT",
            "STOP_TOO_EASY",
            "STOP_PASSED",
            "STOP_BLOCKED",
        ],
    )
    decide_round.add_argument("--hint")
    decide_round.add_argument("--teacher-declares-non-answer", action="store_true")

    direct_decide = commands.add_parser("decide-validation-round")
    direct_decide.add_argument("run_dir", type=Path)
    direct_decide.add_argument("request_id")
    direct_decide.add_argument("action", choices=["CONTINUE_BLIND", "CONTINUE_HINT", "STOP_TOO_EASY", "STOP_PASSED", "STOP_BLOCKED"])
    direct_decide.add_argument("--hint", type=Path)

    record_session = commands.add_parser("record-persistent-validation-session")
    record_session.add_argument("run_dir", type=Path)
    record_session.add_argument("request_id")

    repair_history = commands.add_parser("repair-validation-history-delivery")
    repair_history.add_argument("run_dir", type=Path)

    revision = commands.add_parser("begin-difficulty-revision")
    revision.add_argument("run_dir", type=Path)
    revision.add_argument("revision")
    revision.add_argument("reason_evidence", type=Path)

    migration = commands.add_parser("migrate-incomplete-validation")
    migration.add_argument("run_dir", type=Path)
    migration.add_argument("revision")

    accept_delta = commands.add_parser("accept-runtime-delta")
    accept_delta.add_argument("run_dir", type=Path)
    accept_delta.add_argument("receipt", type=Path)

    accept_closure = commands.add_parser("accept-runtime-closure")
    accept_closure.add_argument("run_dir", type=Path)
    accept_closure.add_argument("closure", type=Path)

    runtime_finalization = commands.add_parser("begin-runtime-finalization")
    runtime_finalization.add_argument("run_dir", type=Path)

    complete_optional_runtime = commands.add_parser("complete-without-runtime-environment")
    complete_optional_runtime.add_argument("run_dir", type=Path)
    complete_optional_runtime.add_argument("evidence", type=Path)

    bind_environment = commands.add_parser("bind-runtime-environment")
    bind_environment.add_argument("run_dir", type=Path)
    bind_environment.add_argument("receipt", type=Path)

    environment = commands.add_parser("import-environment")
    environment.add_argument("state_root", type=Path)
    environment.add_argument("manifest", type=Path)
    environment.add_argument("--clean-evidence", type=Path, required=True)
    environment.add_argument("--fencing-token", required=True)
    environment.add_argument("--endpoint", required=True)
    environment.add_argument("--project-id", required=True)
    environment.add_argument("--workdir", default="/app")

    harbor = commands.add_parser("build-harbor-config")
    harbor.add_argument("spec", type=Path)
    harbor.add_argument("runtime", type=Path)
    harbor.add_argument("output", type=Path)

    issue = commands.add_parser("issue-researcher")
    issue.add_argument("run_dir", type=Path)
    issue.add_argument("request_id")
    issue.add_argument("job_config", type=Path)
    issue.add_argument("--approved-hint", type=Path)
    issue.add_argument("--closed-request-id", action="append", default=[])

    revoke = commands.add_parser("revoke-researcher")
    revoke.add_argument("handoff", type=Path)
    revoke.add_argument("--reason", required=True)

    dispatch = commands.add_parser("dispatch")
    dispatch.add_argument(
        "--role", choices=["teacher", "researcher", "reviewer"], required=True
    )
    dispatch.add_argument("--thread-id", required=True)
    dispatch.add_argument("--prompt", type=Path, required=True)

    run = commands.add_parser("researcher-run")
    run.add_argument("handoff", type=Path)
    run.add_argument("runtime", type=Path)
    run.add_argument("--env-file", type=Path, required=True)
    run.add_argument("--receipt", type=Path, required=True)
    run.add_argument("--overall-timeout-sec", type=int, default=7200)
    run.add_argument("--queue-root", type=Path, required=True)
    run.add_argument("--claim-id")
    run.add_argument("--recovery-evidence", type=Path)

    runtime_run = commands.add_parser("runtime-researcher-run")
    runtime_run.add_argument("handoff", type=Path)
    runtime_run.add_argument("runtime", type=Path)
    runtime_run.add_argument("--env-file", type=Path, required=True)
    runtime_run.add_argument("--receipt", type=Path, required=True)
    runtime_run.add_argument("--overall-timeout-sec", type=int, default=7200)
    runtime_run.add_argument("--queue-root", type=Path, required=True)
    runtime_run.add_argument("--claim-id")

    supervisor_register = commands.add_parser("supervisor-register")
    supervisor_register.add_argument("plan", type=Path)

    supervise = commands.add_parser("supervise")
    supervise.add_argument("runs_root", type=Path)
    supervise.add_argument("--once", action="store_true")
    supervise.add_argument("--poll-interval-sec", type=float, default=1.0)
    supervise.add_argument("--stop-file", type=Path)

    supervisor_resume = commands.add_parser("supervisor-resume")
    supervisor_resume.add_argument("runs_root", type=Path)
    supervisor_resume.add_argument("question_id")

    harbor_queue_status = commands.add_parser("harbor-queue-status")
    harbor_queue_status.add_argument("queue_root", type=Path)

    harbor_queue_submit = commands.add_parser("harbor-queue-submit")
    harbor_queue_submit.add_argument("queue_root", type=Path)
    harbor_queue_submit.add_argument("request_id")
    harbor_queue_submit.add_argument("job_config", type=Path)

    harbor_queue_claim = commands.add_parser("harbor-queue-claim")
    harbor_queue_claim.add_argument("queue_root", type=Path)
    harbor_queue_claim.add_argument("--worker-id", required=True)
    harbor_queue_claim.add_argument("--limit", type=int, default=200)

    harbor_queue_complete = commands.add_parser("harbor-queue-complete")
    harbor_queue_complete.add_argument("queue_root", type=Path)
    harbor_queue_complete.add_argument("request_id")
    harbor_queue_complete.add_argument("--claim-id", required=True)
    harbor_queue_complete.add_argument(
        "--state",
        choices=["COMPLETED", "PLATFORM_FAILED", "RECOVERY_REQUIRED"],
        required=True,
    )

    harbor_queue_reclaim = commands.add_parser("harbor-queue-reclaim-stale")
    harbor_queue_reclaim.add_argument("queue_root", type=Path)
    harbor_queue_reclaim.add_argument("request_id")
    harbor_queue_reclaim.add_argument("--claim-id", required=True)
    harbor_queue_reclaim.add_argument("--stale-after-sec", type=int, required=True)
    harbor_queue_reclaim.add_argument("--evidence", type=Path, required=True)

    _add_scheduler_parsers(commands)

    delta_claim = commands.add_parser("labwright-claim-delta")
    delta_claim.add_argument("state_root", type=Path)
    delta_claim.add_argument("request_id")
    delta_claim.add_argument("--worker-id", required=True)
    delta_claim.add_argument("--fencing-token", required=True)

    delta_complete = commands.add_parser("labwright-complete-delta")
    delta_complete.add_argument("state_root", type=Path)
    delta_complete.add_argument("request_id")
    delta_complete.add_argument("runtime", type=Path)
    delta_complete.add_argument("evidence", type=Path)
    delta_complete.add_argument("--capability-version", required=True)
    delta_complete.add_argument("--fencing-token", required=True)

    seal = commands.add_parser("labwright-seal-plan")
    seal.add_argument("state_root", type=Path)
    seal.add_argument("run_id")
    seal.add_argument("question_revision")
    seal.add_argument("package_sha256")
    seal.add_argument("baseline_runtime", type=Path)
    seal.add_argument("scientific_trace", type=Path)
    seal.add_argument("output", type=Path)
    seal.add_argument("--request-id", action="append", default=[])
    return root


def _add_scheduler_parsers(commands: Any) -> None:
    """集中声明 scheduler v2 控制面命令。"""
    status = commands.add_parser("scheduler-status")
    status.add_argument("scheduler_root", type=Path)
    enqueue = commands.add_parser("scheduler-enqueue")
    enqueue.add_argument("scheduler_root", type=Path)
    enqueue.add_argument("question_id")
    enqueue.add_argument("run_dir", type=Path)
    enqueue.add_argument("--teacher-thread-id")
    enqueue.add_argument("--teacher-prompt", type=Path)
    tick = commands.add_parser("scheduler-tick")
    tick.add_argument("scheduler_root", type=Path)
    heartbeat = commands.add_parser("scheduler-heartbeat")
    heartbeat.add_argument("scheduler_root", type=Path)
    heartbeat.add_argument("question_id")
    heartbeat.add_argument("--owner-id", required=True)
    heartbeat.add_argument("--lease-id", required=True)
    heartbeat.add_argument("--generation", type=int, required=True)
    wait = commands.add_parser("scheduler-wait-external")
    wait.add_argument("scheduler_root", type=Path)
    wait.add_argument("question_id")
    wait.add_argument("--owner-id", required=True)
    wait.add_argument("--lease-id", required=True)
    wait.add_argument("--generation", type=int, required=True)
    wait.add_argument("--kind", required=True)
    wait.add_argument("--external-id", required=True)
    wait.add_argument("--phase", required=True)
    resume = commands.add_parser("scheduler-resume-external")
    resume.add_argument("scheduler_root", type=Path)
    resume.add_argument("question_id")
    resume.add_argument("--external-id", required=True)
    requeue = commands.add_parser("scheduler-requeue")
    requeue.add_argument("scheduler_root", type=Path)
    requeue.add_argument("question_id")
    publish = commands.add_parser("scheduler-bind-published-family")
    publish.add_argument("scheduler_root", type=Path)
    publish.add_argument("question_id")
    publish.add_argument("--owner-id", required=True)
    publish.add_argument("--lease-id", required=True)
    publish.add_argument("--generation", type=int, required=True)
    limit = commands.add_parser("scheduler-set-limit")
    limit.add_argument("scheduler_root", type=Path)
    limit.add_argument("max_active", type=int)
    work = commands.add_parser("scheduler-work-once")
    work.add_argument("scheduler_root", type=Path)
    work.add_argument("--worker-id")


def main(argv: Sequence[str] | None = None) -> int:
    """执行一个命令并输出结构化 JSON。"""
    args = parser().parse_args(argv)
    handlers = {
        "lint-package": _lint,
        "status": _status,
        "begin-authoring": _begin_authoring,
        "bind-teacher-skill": _bind_teacher_skill,
        "attach-question-brief": _attach_question_brief,
        "resolve-teacher-skill": _resolve_teacher_skill,
        "validate-question-brief": _validate_question_brief,
        "record-skill-attribution": _record_skill_attribution,
        "record-revision-attribution": _record_revision_attribution,
        "reconcile-skill-bank": _reconcile_skill_bank,
        "close-skill-batch": _close_skill_batch,
        "evaluate-skill-candidate": _evaluate_skill_candidate,
        "freeze-and-start-validation": _freeze_and_start_validation,
        "recover-validation-session": _recover_validation_session,
        "record-validation-round": _record_validation_round,
        "decide-validation-round": _decide_validation_round,
        "decide-persistent-validation-round": _decide_persistent_validation_round,
        "record-persistent-validation-session": _record_persistent_validation_session,
        "repair-validation-history-delivery": _repair_validation_history_delivery,
        "begin-difficulty-revision": _begin_difficulty_revision,
        "migrate-incomplete-validation": _migrate_incomplete_validation,
        "accept-runtime-delta": _accept_runtime_delta,
        "accept-runtime-closure": _accept_runtime_closure,
        "begin-runtime-finalization": _begin_runtime_finalization,
        "complete-without-runtime-environment": _complete_without_runtime_environment,
        "bind-runtime-environment": _bind_runtime_environment,
        "import-environment": _import_environment,
        "build-harbor-config": _build_harbor,
        "issue-researcher": _issue_researcher,
        "revoke-researcher": _revoke_researcher,
        "dispatch": _dispatch,
        "researcher-run": _researcher_run,
        "runtime-researcher-run": _runtime_researcher_run,
        "supervisor-register": _supervisor_register,
        "supervise": _supervise,
        "supervisor-resume": _supervisor_resume,
        "harbor-queue-status": _harbor_queue_status,
        "harbor-queue-submit": _harbor_queue_submit,
        "harbor-queue-claim": _harbor_queue_claim,
        "harbor-queue-complete": _harbor_queue_complete,
        "harbor-queue-reclaim-stale": _harbor_queue_reclaim_stale,
        "scheduler-status": _scheduler_status,
        "scheduler-enqueue": _scheduler_enqueue,
        "scheduler-tick": _scheduler_tick,
        "scheduler-heartbeat": _scheduler_heartbeat,
        "scheduler-wait-external": _scheduler_wait_external,
        "scheduler-resume-external": _scheduler_resume_external,
        "scheduler-requeue": _scheduler_requeue,
        "scheduler-bind-published-family": _scheduler_bind_published_family,
        "scheduler-set-limit": _scheduler_set_limit,
        "scheduler-work-once": _scheduler_work_once,
        "labwright-claim-delta": _labwright_claim_delta,
        "labwright-complete-delta": _labwright_complete_delta,
        "labwright-seal-plan": _labwright_seal_plan,
    }
    result = handlers[args.command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _lint(args: argparse.Namespace) -> dict[str, Any]:
    report = lint_package(args.package)
    if not report.passed:
        raise SystemExit(2)
    return report.to_dict()


def _status(args: argparse.Namespace) -> dict[str, Any]:
    store = RunStore(args.run_dir)
    snapshot = store.read_snapshot()
    if snapshot is None:
        raise SystemExit("run is not initialized")
    return snapshot.to_dict() | {"event_count": len(store.events())}


def _begin_authoring(args: argparse.Namespace) -> dict[str, Any]:
    """从已绑定 outline、Brief 与 author Skill 的状态进入出题。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).begin_authoring(
        Actor.TEACHER,
        "authoring:runtime-labwright",
    )
    return snapshot.to_dict()


def _bind_teacher_skill(args: argparse.Namespace) -> dict[str, Any]:
    """把 SkillFoundry resolve 产物绑定到正式 run。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).accept_teacher_skill_activation(
        Actor.TEACHER,
        question=args.question,
        stage=args.stage,
        activation_path=args.activation,
        idempotency_key=f"teacher-skill:{args.question}:{args.stage}:{file_sha256(args.activation)}",
    )
    return snapshot.to_dict()


def _attach_question_brief(args: argparse.Namespace) -> dict[str, Any]:
    """在 outline Skill 后绑定题号根目录中的最新版 JSON/Markdown Brief。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).attach_brief(
        Actor.TEACHER,
        args.brief,
        f"brief:{file_sha256(args.brief)}",
    )
    return snapshot.to_dict()


def _freeze_and_start_validation(args: argparse.Namespace) -> dict[str, Any]:
    """冻结可启动题包并原子打开同会话线性 Harbor 验证。"""
    store = RunStore(args.run_dir)
    current = store.read_snapshot()
    if current is None:
        raise SystemExit("run is not initialized")
    snapshot = RunWorkflow(store).freeze_and_start_validation_session(
        Actor.TEACHER,
        package=args.package,
        validation_session_id=args.validation_session_id,
        researcher_thread_id=args.researcher_thread_id,
        idempotency_key=(
            f"validation:session:{current.question_revision}:"
            f"{args.validation_session_id}"
        ),
    )
    return snapshot.to_dict()


def _recover_validation_session(args: argparse.Namespace) -> dict[str, Any]:
    workflow = RunWorkflow(RunStore(args.run_dir))
    snapshot = workflow.recover_validation_session_from_request(
        Actor.TEACHER, args.request, f"validation:recover:{file_sha256(args.request)}"
    )
    return snapshot.to_dict()


def _resolve_teacher_skill(args: argparse.Namespace) -> dict[str, Any]:
    path = resolve_teacher_activation(
        args.question,
        args.stage,
        args.attempt_id,
        args.batch_id,
        tuple(args.task_input),
        batch_questions=tuple(args.batch_question),
    )
    activation = _read_json(path)
    return {
        "activation_path": str(path),
        "prompt_path": activation["prompt_bundle"]["path"],
        "activation": activation,
    }


def _validate_question_brief(args: argparse.Namespace) -> dict[str, Any]:
    json_path, markdown_path = validate_latest_brief(args.question)
    return {
        "question": f"q{args.question}",
        "json": str(json_path),
        "markdown": str(markdown_path),
    }


def _record_skill_attribution(args: argparse.Namespace) -> dict[str, Any]:
    path = record_skill_attribution(args.question, args.evidence)
    return {"question": f"q{args.question}", "attribution_path": str(path)}


def _record_revision_attribution(args: argparse.Namespace) -> dict[str, Any]:
    return record_revision_attribution(args.question, args.evidence)


def _reconcile_skill_bank(args: argparse.Namespace) -> dict[str, Any]:
    return reconcile_skill_batch(args.batch_id, tuple(args.question))


def _close_skill_batch(args: argparse.Namespace) -> dict[str, Any]:
    return close_skill_batch(args.batch_id, tuple(args.question))


def _evaluate_skill_candidate(args: argparse.Namespace) -> dict[str, Any]:
    return evaluate_skill_candidate(args.plan, args.verdict)


def _record_validation_round(args: argparse.Namespace) -> dict[str, Any]:
    """从 canonical Harbor 原始产物导入线性会话的一轮结果。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).record_validation_round(
        Actor.TEACHER,
        request_id=args.request_id,
        idempotency_key=f"validation:round:{args.request_id}",
    )
    return snapshot.to_dict()


def _decide_validation_round(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = RunWorkflow(RunStore(args.run_dir)).decide_validation_round(
        Actor.TEACHER, request_id=args.request_id, action=args.action,
        hint_path=args.hint,
    )
    return snapshot.to_dict()


def _decide_persistent_validation_round(args: argparse.Namespace) -> dict[str, Any]:
    """Let Teacher advance or stop a live persistent Harbor validation trial."""
    path = RunWorkflow(RunStore(args.run_dir)).decide_persistent_validation_round(
        Actor.TEACHER,
        request_id=args.request_id,
        round_index=args.round_index,
        decision=TeacherDecision(
            action=args.action,
            hint=args.hint,
            teacher_declares_non_answer=(
                True if args.teacher_declares_non_answer else None
            ),
        ),
    )
    return {"decision_path": str(path.resolve()), "sha256": file_sha256(path)}


def _record_persistent_validation_session(args: argparse.Namespace) -> dict[str, Any]:
    """Atomically import all rounds after the persistent Harbor trial stops."""
    snapshot = RunWorkflow(RunStore(args.run_dir)).record_persistent_validation_session(
        Actor.TEACHER,
        request_id=args.request_id,
        idempotency_key=f"validation:persistent-session:{args.request_id}",
    )
    return snapshot.to_dict()


def _repair_validation_history_delivery(args: argparse.Namespace) -> dict[str, Any]:
    """对不计科学轮次的 argv 溢出执行有来源绑定的历史交付修复。"""
    store = RunStore(args.run_dir)
    current = store.read_snapshot()
    if current is None:
        raise SystemExit("run is not initialized")
    session = current.evidence.get("validation_session", {})
    source_digests = session.get("round_receipt_sha256s", ())
    identity = hashlib.sha256(
        json.dumps(
            {
                "question_revision": current.question_revision,
                "source_digests": source_digests,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    snapshot = RunWorkflow(store).repair_validation_history_delivery(
        Actor.TEACHER,
        idempotency_key=f"validation:history-delivery:compact-v1:{identity}",
    )
    return snapshot.to_dict()


def _begin_difficulty_revision(args: argparse.Namespace) -> dict[str, Any]:
    """从 TOO_EASY 进入绑定证据的同题难度修订。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).begin_revision(
        Actor.TEACHER,
        args.revision,
        args.reason_evidence,
        f"revision:difficulty:{args.revision}:{file_sha256(args.reason_evidence)}",
    )
    return snapshot.to_dict()


def _accept_runtime_delta(args: argparse.Namespace) -> dict[str, Any]:
    """接纳一份 source 与 builder 身份分离的 runtime delta。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).accept_runtime_delta(
        Actor.LABWRIGHT,
        args.receipt,
        f"runtime:delta:{file_sha256(args.receipt)}",
    )
    return snapshot.to_dict()


def _migrate_incomplete_validation(args: argparse.Namespace) -> dict[str, Any]:
    """关闭旧验证会话并创建采用当前线性合同的新修订。"""
    snapshot = RunWorkflow(
        RunStore(args.run_dir)
    ).migrate_incomplete_validation_session(
        Actor.TEACHER,
        args.revision,
        f"validation:migrate:{args.revision}",
    )
    return snapshot.to_dict()


def _accept_runtime_closure(args: argparse.Namespace) -> dict[str, Any]:
    """接纳 fresh 科学 trace 对应的 Stable 构建闭合。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).accept_runtime_closure(
        Actor.LABWRIGHT,
        args.closure,
        f"runtime:closure:{file_sha256(args.closure)}",
    )
    return snapshot.to_dict()


def _bind_runtime_environment(args: argparse.Namespace) -> dict[str, Any]:
    """把与当前 closure 精确绑定的 Stable 环境写入运行状态。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).bind_runtime_environment(
        Actor.LABWRIGHT,
        _environment_receipt(args.receipt),
        f"runtime:environment:{file_sha256(args.receipt)}",
    )
    return snapshot.to_dict()


def _begin_runtime_finalization(args: argparse.Namespace) -> dict[str, Any]:
    """验证通过后进入环境固化，不重复科学解题。"""
    workflow = RunWorkflow(RunStore(args.run_dir))
    current = workflow.snapshot
    snapshot = workflow.begin_runtime_finalization(
        Actor.TEACHER,
        (
            "runtime:finalization:start:"
            f"{current.question_revision}:{current.package_digest}"
        ),
    )
    return snapshot.to_dict()


def _complete_without_runtime_environment(args: argparse.Namespace) -> dict[str, Any]:
    workflow = RunWorkflow(RunStore(args.run_dir))
    snapshot = workflow.complete_without_runtime_environment(
        Actor.TEACHER,
        args.evidence,
        f"publication:optional-runtime:{file_sha256(args.evidence)}",
    )
    return snapshot.to_dict()


def _import_environment(args: argparse.Namespace) -> dict[str, Any]:
    receipt = FileLabwrightRegistry(args.state_root).import_stable(
        args.manifest,
        clean_evidence_path=args.clean_evidence,
        fencing_token=args.fencing_token,
        endpoint_identity=args.endpoint,
        project_id=args.project_id,
        workdir=args.workdir,
    )
    return receipt.to_dict()


def _build_harbor(args: argparse.Namespace) -> dict[str, Any]:
    spec_value = _read_json(args.spec)
    spec_value["context_paths"] = tuple(spec_value.get("context_paths", ()))
    persistent_value = spec_value.pop("persistent_validation", None)
    persistent = (
        PersistentValidationSpec(**persistent_value)
        if isinstance(persistent_value, dict)
        else None
    )
    runtime = runtime_from_dict(_read_json(args.runtime))
    config = build_job_config(
        HarborJobSpec(**spec_value),
        runtime,
        persistent_validation=persistent,
    )
    write_job_config(args.output, config)
    return {"config_path": str(args.output.resolve()), "job_name": config["job_name"]}


def _issue_researcher(args: argparse.Namespace) -> dict[str, Any]:
    handoff = RunWorkflow(RunStore(args.run_dir)).issue_researcher_request(
        Actor.TEACHER,
        request_id=args.request_id,
        job_config_path=args.job_config,
        approved_hint_path=args.approved_hint,
        closed_request_ids=tuple(args.closed_request_id),
    )
    output = asdict(handoff)
    handoff_path = Path(handoff.request_path).parent / "handoff.json"
    write_json(handoff_path, output)
    return output | {"handoff_path": str(handoff_path)}


def _revoke_researcher(args: argparse.Namespace) -> dict[str, Any]:
    """撤销尚未兑换且未进入 Harbor 的 Researcher capability。"""
    handoff = IssuedHandoff(**_read_json(args.handoff))
    CapabilityStore(Path(handoff.capability_path).parents[1]).revoke(
        handoff,
        reason=args.reason,
    )
    return {"request_path": handoff.request_path, "status": "REVOKED"}


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    receipt = CodexDispatcher().queue(
        role=Actor(args.role),
        thread_id=args.thread_id,
        prompt_path=args.prompt,
    )
    return asdict(receipt) | {"role": receipt.role.value}


def _researcher_run(args: argparse.Namespace) -> dict[str, Any]:
    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if not thread_id:
        raise SystemExit("CODEX_THREAD_ID is required for Researcher attestation")
    return _execute_researcher_handoff(args, thread_id=thread_id, runtime_owned=False)


def _runtime_researcher_run(args: argparse.Namespace) -> dict[str, Any]:
    """由 CampaignSupervisor 直接运行 schema-v4 Harbor request。"""
    handoff = IssuedHandoff(**_read_json(args.handoff))
    request = _read_json(Path(handoff.request_path))
    validation_session_id = request.get("validation_session_id")
    if not isinstance(validation_session_id, str):
        raise SystemExit("runtime-owned handoff lacks validation session identity")
    thread_id = f"harbor-runtime:{validation_session_id}"
    return _execute_researcher_handoff(args, thread_id=thread_id, runtime_owned=True)


def _supervisor_register(args: argparse.Namespace) -> dict[str, Any]:
    """Bind one immutable question plan to its durable Supervisor state."""
    plan = SupervisorPlan.from_path(args.plan)
    snapshot = register_supervisor_plan(plan)
    return snapshot.to_dict()


def _supervise(args: argparse.Namespace) -> dict[str, Any]:
    """Advance once for automation, or retain ownership until stopped."""
    supervisor = CampaignSupervisor(args.runs_root)
    if args.once:
        return {"results": [asdict(item) for item in supervisor.run_once()]}
    supervisor.run_forever(
        poll_interval_sec=args.poll_interval_sec,
        stop_path=args.stop_file,
    )
    return {"status": "STOPPED"}


def _supervisor_resume(args: argparse.Namespace) -> dict[str, Any]:
    """Resume one recovered wait or apply its Teacher action file."""
    return asdict(CampaignSupervisor(args.runs_root).resume(args.question_id))


def _execute_researcher_handoff(
    args: argparse.Namespace, *, thread_id: str, runtime_owned: bool
) -> dict[str, Any]:
    """在一个受信 owner 下兑换、排队并执行一次不可变 Harbor request。"""
    if args.receipt.exists():
        raise SystemExit("Researcher receipt already exists and is immutable")
    # Validate the host-controlled runtime before consuming the single-use
    # Researcher capability or occupying a global Harbor queue slot.  A local
    # argument/configuration error must remain recoverable without fabricating
    # a scientific attempt.
    runtime = runtime_from_dict(_read_json(args.runtime))
    handoff = IssuedHandoff(**_read_json(args.handoff))
    queued_request = _read_json(Path(handoff.request_path))
    request_id = queued_request.get("request_id")
    job_config_path = queued_request.get("job_config_path")
    if not isinstance(request_id, str) or not isinstance(job_config_path, str):
        raise SystemExit("Researcher handoff lacks queue identity")
    queue = HarborJobQueue(args.queue_root)
    if args.claim_id:
        claim_id = args.claim_id
        queue.authorize(
            request_id, claim_id=claim_id, job_config_path=Path(job_config_path)
        )
    else:
        queue.submit(request_id, Path(job_config_path))
        claim = queue.claim_request(request_id, worker_id=thread_id)
        if claim is None:
            return {
                "request_id": request_id,
                "classification": "QUEUED",
                "reward": None,
            }
        claim_id = claim.claim_id
        assert claim_id is not None
    store = CapabilityStore(Path(handoff.capability_path).parents[1])
    if args.recovery_evidence is not None:
        request = store.redeem_platform_recovery(handoff, thread_id, args.recovery_evidence)
    else:
        request = store.redeem_runtime(handoff) if runtime_owned else store.redeem(handoff, thread_id)
    queue.authorize(
        request.request_id,
        claim_id=claim_id,
        job_config_path=Path(request.job_config_path),
    )
    receipt = execute_harbor(
        request=request,
        runtime=runtime,
        env_file=args.env_file,
        overall_timeout_sec=args.overall_timeout_sec,
    )
    write_json(args.receipt, asdict(receipt))
    terminal_state = (
        HarborQueueState.COMPLETED
        if receipt.classification in {"SCIENTIFIC_RESULT", "DEFERRED_TIMEOUT"}
        else HarborQueueState.PLATFORM_FAILED
    )
    queue.complete(request.request_id, claim_id=claim_id, terminal_state=terminal_state)
    return asdict(receipt)


def _harbor_queue_status(args: argparse.Namespace) -> dict[str, Any]:
    """返回全局 Harbor Job 队列状态。"""
    return HarborJobQueue(args.queue_root).snapshot().to_dict()


def _harbor_queue_submit(args: argparse.Namespace) -> dict[str, Any]:
    """提交一个单任务、单 trial 的独立 Harbor Job。"""
    return (
        HarborJobQueue(args.queue_root)
        .submit(args.request_id, args.job_config)
        .to_dict()
    )


def _harbor_queue_claim(args: argparse.Namespace) -> dict[str, Any]:
    """在 200 个全局槽内认领等待 Job。"""
    queue = HarborJobQueue(args.queue_root)
    claimed = queue.claim(worker_id=args.worker_id, limit=args.limit)
    return {
        "max_active": queue.snapshot().max_active,
        "claimed": [item.to_dict() for item in claimed],
    }


def _harbor_queue_complete(args: argparse.Namespace) -> dict[str, Any]:
    """以科学完成、平台失败或待恢复状态结束认领。"""
    return (
        HarborJobQueue(args.queue_root)
        .complete(
            args.request_id,
            claim_id=args.claim_id,
            terminal_state=HarborQueueState(args.state),
        )
        .to_dict()
    )


def _harbor_queue_reclaim_stale(args: argparse.Namespace) -> dict[str, Any]:
    """回收有中断证据且超过心跳期限的 ACTIVE claim。"""
    return HarborJobQueue(args.queue_root).reclaim_stale(
        args.request_id,
        claim_id=args.claim_id,
        stale_after_sec=args.stale_after_sec,
        evidence_path=args.evidence,
    ).to_dict()


def _scheduler_status(args: argparse.Namespace) -> dict[str, Any]:
    return QuestionScheduler(args.scheduler_root).snapshot().to_dict()


def _scheduler_enqueue(args: argparse.Namespace) -> dict[str, Any]:
    result = QuestionScheduler(args.scheduler_root).enqueue(
        args.question_id,
        args.run_dir,
        teacher_thread_id=args.teacher_thread_id,
        teacher_prompt_path=args.teacher_prompt,
    )
    return {
        "activated": result.activated,
        "released": result.released,
        "snapshot": result.snapshot.to_dict(),
    }


def _scheduler_tick(args: argparse.Namespace) -> dict[str, Any]:
    result = QuestionScheduler(args.scheduler_root).tick()
    return {
        "activated": result.activated,
        "released": result.released,
        "snapshot": result.snapshot.to_dict(),
    }


def _scheduler_heartbeat(args: argparse.Namespace) -> dict[str, Any]:
    """由当前唯一 owner 刷新 Teacher 租约。"""
    return (
        QuestionScheduler(args.scheduler_root)
        .heartbeat(
            args.question_id,
            owner_id=args.owner_id,
            lease_id=args.lease_id,
            generation=args.generation,
        )
        .to_dict()
    )


def _scheduler_wait_external(args: argparse.Namespace) -> dict[str, Any]:
    """释放外部等待题的 Teacher 槽并立即补位。"""
    result = QuestionScheduler(args.scheduler_root).set_waiting_external(
        args.question_id,
        owner_id=args.owner_id,
        lease_id=args.lease_id,
        generation=args.generation,
        kind=args.kind,
        external_id=args.external_id,
        phase=args.phase,
    )
    return {
        "activated": result.activated,
        "released": result.released,
        "snapshot": result.snapshot.to_dict(),
    }


def _scheduler_resume_external(args: argparse.Namespace) -> dict[str, Any]:
    """接收匹配外部结果并让题目重新参与三槽调度。"""
    result = QuestionScheduler(args.scheduler_root).resume_external(
        args.question_id,
        external_id=args.external_id,
    )
    return {
        "activated": result.activated,
        "released": result.released,
        "snapshot": result.snapshot.to_dict(),
    }


def _scheduler_requeue(args: argparse.Namespace) -> dict[str, Any]:
    """经人工确认后重新排入恢复项。"""
    result = QuestionScheduler(args.scheduler_root).requeue(args.question_id)
    return {
        "activated": result.activated,
        "released": result.released,
        "snapshot": result.snapshot.to_dict(),
    }


def _scheduler_bind_published_family(args: argparse.Namespace) -> dict[str, Any]:
    """验证已发布题族，并在同一事务完成对应 campaign 槽位。"""
    result = QuestionScheduler(args.scheduler_root).bind_published_family(
        args.question_id,
        owner_id=args.owner_id,
        lease_id=args.lease_id,
        generation=args.generation,
    )
    return {
        "activated": result.activated,
        "released": result.released,
        "snapshot": result.snapshot.to_dict(),
    }


def _scheduler_set_limit(args: argparse.Namespace) -> dict[str, Any]:
    """确认固定三槽约束；其他数值由 scheduler 拒绝。"""
    result = QuestionScheduler(args.scheduler_root).configure(
        max_active=args.max_active
    )
    return {
        "activated": result.activated,
        "released": result.released,
        "snapshot": result.snapshot.to_dict(),
    }


def _scheduler_work_once(args: argparse.Namespace) -> dict[str, Any]:
    outcomes = SchedulerWorker(
        QuestionScheduler(args.scheduler_root),
        worker_id=args.worker_id,
    ).run_once()
    return {"outcomes": [asdict(outcome) for outcome in outcomes]}


def _labwright_claim_delta(args: argparse.Namespace) -> dict[str, Any]:
    claim = FileLabwrightRuntimeService(args.state_root).claim(
        Actor.LABWRIGHT,
        args.request_id,
        worker_id=args.worker_id,
        fencing_token=args.fencing_token,
    )
    return asdict(claim)


def _labwright_complete_delta(args: argparse.Namespace) -> dict[str, Any]:
    value = _read_json(args.runtime)
    value["artifact"] = ArtifactIdentity(**value["artifact"])
    receipt = FileLabwrightRuntimeService(args.state_root).complete(
        Actor.LABWRIGHT,
        args.request_id,
        fencing_token=args.fencing_token,
        runtime=RuntimeSnapshot(**value),
        capability_version=args.capability_version,
        evidence_path=args.evidence,
    )
    return receipt.to_dict()


def _labwright_seal_plan(args: argparse.Namespace) -> dict[str, Any]:
    runtime = _read_json(args.baseline_runtime)
    artifact = ArtifactIdentity(**runtime["artifact"])
    plan = FileLabwrightRuntimeService(args.state_root).create_seal_plan(
        Actor.LABWRIGHT,
        run_id=args.run_id,
        question_revision=args.question_revision,
        package_sha256=args.package_sha256,
        baseline_artifact=artifact,
        scientific_trace_path=args.scientific_trace,
        request_ids=tuple(args.request_id),
        output_path=args.output,
    )
    return plan.to_dict()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"JSON input is missing: {path}")

    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """把单层 JSON 对象转换成拒绝重复键的字典。"""
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise SystemExit(f"JSON input contains duplicate key {key!r}: {path}")
            value[key] = item
        return value

    def reject_nonfinite(token: str) -> None:
        """拒绝 Python JSON 解码器默认放行的非有限常量。"""
        raise SystemExit(f"JSON input contains non-finite number {token}: {path}")

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=strict_object,
        parse_constant=reject_nonfinite,
    )
    if not isinstance(value, dict):
        raise SystemExit(f"JSON input must be an object: {path}")
    return value


def _environment_receipt(path: Path) -> EnvironmentReceipt:
    """从严格 JSON 恢复 Stable 环境回执。"""
    value = _read_json(path)
    try:
        converted = dict(value)
        converted["artifact"] = ArtifactIdentity(**converted["artifact"])
        converted["resource_digests"] = tuple(converted.get("resource_digests", ()))
        receipt = EnvironmentReceipt(**converted)
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"environment receipt schema is invalid: {path}") from error
    receipt.validate()
    return receipt


if __name__ == "__main__":
    raise SystemExit(main())

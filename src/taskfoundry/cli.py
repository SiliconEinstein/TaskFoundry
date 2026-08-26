"""TaskFoundry 适配器、证据操作和多题调度的命令行入口。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any, Sequence

from .codex_agent import CodexDispatcher
from .harbor import HarborJobSpec, build_job_config, runtime_from_dict, write_job_config
from .harbor_queue import HarborJobQueue, HarborQueueState
from .health import BoundHealthEvidence, EvidenceReference, HealthGate
from .labwright import ArtifactIdentity, EnvironmentReceipt, FileLabwrightRegistry, RuntimeSnapshot
from .labwright_runtime import FileLabwrightRuntimeService
from .model import Actor, RunState
from .package import lint_package, migrate_legacy_task
from .policy import file_sha256
from .researcher import (
    CapabilityStore,
    IssuedHandoff,
    ResearcherRequest,
    execute_harbor,
    write_json,
)
from .scheduler import QuestionScheduler
from .scheduler_worker import SchedulerWorker
from .store import RunStore
from .validation import HealthEvidence
from .workflow import RunWorkflow


def parser() -> argparse.ArgumentParser:
    """构造公开命令行解析器。"""
    root = argparse.ArgumentParser(prog="taskfoundry")
    commands = root.add_subparsers(dest="command", required=True)

    lint = commands.add_parser("lint-package")
    lint.add_argument("package", type=Path)

    migrate = commands.add_parser("migrate-task")
    migrate.add_argument("source", type=Path)
    migrate.add_argument("target", type=Path)
    migrate.add_argument("--manifest", type=Path, required=True)

    status = commands.add_parser("status")
    status.add_argument("run_dir", type=Path)

    authoring = commands.add_parser("begin-authoring")
    authoring.add_argument("run_dir", type=Path)

    design_evidence = commands.add_parser("attach-design-evidence")
    design_evidence.add_argument("run_dir", type=Path)
    design_evidence.add_argument("source_role_map", type=Path)
    design_evidence.add_argument("ground_truth_ledger", type=Path)

    freeze = commands.add_parser("freeze-package")
    freeze.add_argument("run_dir", type=Path)
    freeze.add_argument("package", type=Path)

    health = commands.add_parser("accept-health")
    health.add_argument("run_dir", type=Path)
    health.add_argument("health", type=Path)
    health.add_argument("--evidence-ref", action="append", required=True)

    bound_health = commands.add_parser("accept-bound-health")
    bound_health.add_argument("run_dir", type=Path)
    bound_health.add_argument("bundle", type=Path)

    blind = commands.add_parser("start-blind")
    blind.add_argument("run_dir", type=Path)

    audit = commands.add_parser("audit-researcher")
    audit.add_argument("run_dir", type=Path)
    audit.add_argument("request", type=Path)
    audit.add_argument("capability", type=Path)
    audit.add_argument("receipt", type=Path)
    audit.add_argument("leakage_evidence", type=Path)
    audit.add_argument("--hint-review", type=Path)

    revision = commands.add_parser("begin-difficulty-revision")
    revision.add_argument("run_dir", type=Path)
    revision.add_argument("revision")
    revision.add_argument("reason_evidence", type=Path)

    accept_delta = commands.add_parser("accept-runtime-delta")
    accept_delta.add_argument("run_dir", type=Path)
    accept_delta.add_argument("receipt", type=Path)

    accept_closure = commands.add_parser("accept-runtime-closure")
    accept_closure.add_argument("run_dir", type=Path)
    accept_closure.add_argument("closure", type=Path)

    bind_environment = commands.add_parser("bind-runtime-environment")
    bind_environment.add_argument("run_dir", type=Path)
    bind_environment.add_argument("receipt", type=Path)

    post_validation = commands.add_parser("accept-post-validation")
    post_validation.add_argument("run_dir", type=Path)
    post_validation.add_argument("evidence", type=Path)

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
    issue.add_argument("request", type=Path)
    issue.add_argument("handoff_root", type=Path)

    dispatch = commands.add_parser("dispatch")
    dispatch.add_argument("--role", choices=["teacher", "researcher", "reviewer"], required=True)
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
    publish.add_argument("family", type=Path)
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
        "migrate-task": _migrate,
        "status": _status,
        "begin-authoring": _begin_authoring,
        "attach-design-evidence": _attach_design_evidence,
        "freeze-package": _freeze_package,
        "accept-health": _accept_health,
        "accept-bound-health": _accept_bound_health,
        "start-blind": _start_blind,
        "audit-researcher": _audit_researcher,
        "begin-difficulty-revision": _begin_difficulty_revision,
        "accept-runtime-delta": _accept_runtime_delta,
        "accept-runtime-closure": _accept_runtime_closure,
        "bind-runtime-environment": _bind_runtime_environment,
        "accept-post-validation": _accept_post_validation,
        "import-environment": _import_environment,
        "build-harbor-config": _build_harbor,
        "issue-researcher": _issue_researcher,
        "dispatch": _dispatch,
        "researcher-run": _researcher_run,
        "harbor-queue-status": _harbor_queue_status,
        "harbor-queue-submit": _harbor_queue_submit,
        "harbor-queue-claim": _harbor_queue_claim,
        "harbor-queue-complete": _harbor_queue_complete,
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


def _migrate(args: argparse.Namespace) -> dict[str, Any]:
    return migrate_legacy_task(args.source, args.target, args.manifest).to_dict()


def _status(args: argparse.Namespace) -> dict[str, Any]:
    store = RunStore(args.run_dir)
    snapshot = store.read_snapshot()
    if snapshot is None:
        raise SystemExit("run is not initialized")
    return snapshot.to_dict() | {"event_count": len(store.events())}


def _begin_authoring(args: argparse.Namespace) -> dict[str, Any]:
    """从规范锁定或旧环境前置状态进入 Teacher 出题阶段。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).begin_authoring(
        Actor.TEACHER,
        "authoring:runtime-labwright",
    )
    return snapshot.to_dict()


def _freeze_package(args: argparse.Namespace) -> dict[str, Any]:
    """按当前题目修订和题包摘要幂等冻结可供 Harbor 解题的题包。"""
    store = RunStore(args.run_dir)
    current = store.read_snapshot()
    if current is None:
        raise SystemExit("run is not initialized")
    package_digest = lint_package(args.package).sha256
    if current.state is RunState.PACKAGE_FROZEN and current.package_digest == package_digest:
        return current.to_dict()
    snapshot = RunWorkflow(store).freeze_package(
        Actor.TEACHER,
        args.package,
        f"package:freeze:{current.question_revision}:{package_digest}",
    )
    return snapshot.to_dict()


def _attach_design_evidence(args: argparse.Namespace) -> dict[str, Any]:
    """绑定当前大纲的来源角色图与 Ground Truth 账本。"""
    store = RunStore(args.run_dir)
    current = store.read_snapshot()
    if current is None:
        raise SystemExit("run is not initialized")
    digest = file_sha256(args.source_role_map) + file_sha256(args.ground_truth_ledger)
    snapshot = RunWorkflow(store).attach_design_evidence(
        Actor.TEACHER,
        source_role_map_path=args.source_role_map,
        ground_truth_ledger_path=args.ground_truth_ledger,
        idempotency_key=f"design:evidence:{current.question_revision}:{digest}",
    )
    return snapshot.to_dict()


def _accept_health(args: argparse.Namespace) -> dict[str, Any]:
    """按当前修订幂等接受同一份 Reviewer 快速健康回执。"""
    health = HealthEvidence(**_read_json(args.health))
    store = RunStore(args.run_dir)
    current = store.read_snapshot()
    if current is None:
        raise SystemExit("run is not initialized")
    refs = tuple(args.evidence_ref)
    accepted_state = RunState.ORACLE_PASSED if health.passed else RunState.PREFLIGHT_PASSED
    if (
        current.state is accepted_state
        and current.evidence.get("health") == asdict(health)
        and tuple(current.evidence.get("health_evidence_refs", ())) == refs
    ):
        return current.to_dict()
    snapshot = RunWorkflow(store).accept_health(
        Actor.REVIEWER,
        health,
        refs,
        f"health:{current.question_revision}:{args.health.name}",
    )
    return snapshot.to_dict()


def _accept_bound_health(args: argparse.Namespace) -> dict[str, Any]:
    value = _read_json(args.bundle)
    health = HealthEvidence(**value["health"])
    references = tuple(
        EvidenceReference(
            gate=HealthGate(item["gate"]),
            path=item["path"],
            sha256=item["sha256"],
        )
        for item in value["references"]
    )
    bundle = BoundHealthEvidence(
        question_revision=value["question_revision"],
        package_sha256=value["package_sha256"],
        environment_key=value["environment_key"],
        health=health,
        references=references,
        created_at=value["created_at"],
        schema_version=value.get("schema_version", 1),
    )
    snapshot = RunWorkflow(RunStore(args.run_dir)).accept_bound_health(
        Actor.REVIEWER,
        bundle,
        "bound-health:" + args.bundle.name,
    )
    return snapshot.to_dict()


def _start_blind(args: argparse.Namespace) -> dict[str, Any]:
    """按当前修订幂等开启首次 blind 验证。"""
    store = RunStore(args.run_dir)
    current = store.read_snapshot()
    if current is None:
        raise SystemExit("run is not initialized")
    if current.state is RunState.BLIND_VALIDATION:
        return current.to_dict()
    snapshot = RunWorkflow(store).start_blind_validation(
        Actor.TEACHER,
        f"validation:blind:{current.question_revision}:start",
    )
    return snapshot.to_dict()


def _audit_researcher(args: argparse.Namespace) -> dict[str, Any]:
    """由 Reviewer 接纳一次 capability、回执和泄漏审计闭合的 Harbor 尝试。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).audit_researcher_receipt(
        Actor.REVIEWER,
        request_path=args.request,
        capability_path=args.capability,
        receipt_path=args.receipt,
        leakage_evidence_path=args.leakage_evidence,
        hint_review_path=args.hint_review,
        idempotency_key=f"attempt:audit:{file_sha256(args.receipt)}",
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


def _accept_post_validation(args: argparse.Namespace) -> dict[str, Any]:
    """接纳独立最终复审并结束单题 progression。"""
    snapshot = RunWorkflow(RunStore(args.run_dir)).accept_post_validation(
        Actor.REVIEWER,
        args.evidence,
        f"validation:post:{file_sha256(args.evidence)}",
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
    runtime = runtime_from_dict(_read_json(args.runtime))
    config = build_job_config(HarborJobSpec(**spec_value), runtime)
    write_job_config(args.output, config)
    return {"config_path": str(args.output.resolve()), "job_name": config["job_name"]}


def _issue_researcher(args: argparse.Namespace) -> dict[str, Any]:
    value = _read_json(args.request)
    handoff = CapabilityStore(args.handoff_root).issue(Actor.TEACHER, ResearcherRequest.from_dict(value))
    output = asdict(handoff)
    handoff_path = Path(handoff.request_path).parent / "handoff.json"
    write_json(handoff_path, output)
    return output | {"handoff_path": str(handoff_path)}


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
    if args.receipt.exists():
        raise SystemExit("Researcher receipt already exists and is immutable")
    handoff = IssuedHandoff(**_read_json(args.handoff))
    queued_request = _read_json(Path(handoff.request_path))
    request_id = queued_request.get("request_id")
    job_config_path = queued_request.get("job_config_path")
    if not isinstance(request_id, str) or not isinstance(job_config_path, str):
        raise SystemExit("Researcher handoff lacks queue identity")
    queue = HarborJobQueue(args.queue_root)
    if args.claim_id:
        claim_id = args.claim_id
        queue.authorize(request_id, claim_id=claim_id, job_config_path=Path(job_config_path))
    else:
        queue.submit(request_id, Path(job_config_path))
        claim = queue.claim_request(request_id, worker_id=thread_id)
        if claim is None:
            return {"request_id": request_id, "classification": "QUEUED", "reward": None}
        claim_id = claim.claim_id
        assert claim_id is not None
    store = CapabilityStore(Path(handoff.capability_path).parents[1])
    request = store.redeem(handoff, thread_id)
    queue.authorize(
        request.request_id,
        claim_id=claim_id,
        job_config_path=Path(request.job_config_path),
    )
    receipt = execute_harbor(
        request=request,
        runtime=runtime_from_dict(_read_json(args.runtime)),
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
    return HarborJobQueue(args.queue_root).submit(args.request_id, args.job_config).to_dict()


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
    return HarborJobQueue(args.queue_root).complete(
        args.request_id,
        claim_id=args.claim_id,
        terminal_state=HarborQueueState(args.state),
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
    return QuestionScheduler(args.scheduler_root).heartbeat(
        args.question_id,
        owner_id=args.owner_id,
        lease_id=args.lease_id,
        generation=args.generation,
    ).to_dict()


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
        args.family,
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
    result = QuestionScheduler(args.scheduler_root).configure(max_active=args.max_active)
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

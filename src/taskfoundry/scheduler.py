"""隐藏三槽租约、心跳、迁移和自动补位的出题调度器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import runpy
import tempfile
import uuid

from .model import ContractError, RunState
from .scheduler_state import (
    CAMPAIGN_SLOT_LIMIT,
    DEFAULT_LEASE_TTL_SEC,
    DispatchState,
    ExternalWait,
    QueueState,
    ScheduledQuestion,
    SchedulerSnapshot,
    TeacherLease,
    campaign_snapshot,
    parse_timestamp,
    question_number,
    snapshot_from_dict,
)
from .store import RunStore


# 旧调用方仍可导入这个名称，但 Q1--Q32 campaign 已固定为三槽。
DEFAULT_TEACHER_MAX_ACTIVE = CAMPAIGN_SLOT_LIMIT
MAX_TEACHER_MAX_ACTIVE = CAMPAIGN_SLOT_LIMIT
PublicationValidator = Callable[[int, Path], None]


def _default_validate_published_family(question: int, family: Path) -> None:
    """调用发布与全仓检查共用的题族验证器。"""
    validator_path = Path(__file__).resolve().parents[2] / "scripts" / "question_family_validation.py"
    if not validator_path.is_file():
        raise ContractError("question family publication validator is unavailable")
    try:
        validator = runpy.run_path(str(validator_path))["validate_family"]
        validator(question, family, staging_required=False)
    except Exception as error:
        raise ContractError(f"published question family validation failed: {error}") from error


@dataclass(frozen=True)
class ScheduleResult:
    """一次原子调度后新占用和已释放的题目。"""

    snapshot: SchedulerSnapshot
    activated: tuple[str, ...]
    released: tuple[str, ...]


class QuestionScheduler:
    """Q1--Q32 单一看板的租约调度门面。"""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        lease_ttl_sec: int = DEFAULT_LEASE_TTL_SEC,
        publication_validator: PublicationValidator | None = None,
    ) -> None:
        if not 30 <= lease_ttl_sec <= 86400:
            raise ContractError("lease TTL must be between 30 and 86400 seconds")
        self.root = root
        self.state_path = root / "scheduler.json"
        self.v1_backup_path = root / "scheduler.v1.json"
        self.lock_path = root / "scheduler.lock"
        self.clock = clock or (lambda: datetime.now(UTC))
        self.token_factory = token_factory or (lambda: uuid.uuid4().hex)
        self.lease_ttl_sec = lease_ttl_sec
        self.publication_validator = publication_validator or _default_validate_published_family

    def snapshot(self) -> SchedulerSnapshot:
        """读取 v2；缺失时返回全局看板，v1 时只做内存迁移。"""
        now = self._now()
        if not self.state_path.exists():
            return campaign_snapshot(now, lease_ttl_sec=self.lease_ttl_sec)
        if not self.state_path.is_file() or self.state_path.is_symlink():
            raise ContractError("scheduler state must be a regular file")
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractError("scheduler state is invalid JSON") from error
        if not isinstance(value, dict):
            raise ContractError("scheduler state root must be an object")
        return snapshot_from_dict(value, now=now, lease_ttl_sec=self.lease_ttl_sec)

    def enqueue(
        self,
        question_id: str,
        run_dir: Path,
        *,
        teacher_thread_id: str | None = None,
        teacher_prompt_path: Path | None = None,
    ) -> ScheduleResult:
        """配置一道 backlog 题并按三槽约束立即尝试激活。"""
        number = question_number(question_id)
        if number <= 2:
            raise ContractError("q1 and q2 are grandfathered and cannot be enqueued")
        if bool(teacher_thread_id) != bool(teacher_prompt_path):
            raise ContractError("Teacher thread and prompt must be configured together")
        with self._locked():
            current = self.snapshot()
            item = current.questions[number - 1]
            resolved_run = str(run_dir.resolve())
            if item.run_dir and item.run_dir != resolved_run:
                raise ContractError("question_id already targets another run")
            if item.state not in {QueueState.BACKLOG, QueueState.READY}:
                return ScheduleResult(current, (), ())
            now = self._now()
            questions = list(current.questions)
            questions[number - 1] = replace(
                item,
                run_dir=resolved_run,
                state=QueueState.READY,
                phase="AUTHORING" if item.phase == "BACKLOG" else item.phase,
                teacher_thread_id=teacher_thread_id or item.teacher_thread_id,
                teacher_prompt_path=(
                    str(teacher_prompt_path.resolve()) if teacher_prompt_path else item.teacher_prompt_path
                ),
                updated_at=now.isoformat(),
            )
            result = self._sync_and_fill(replace(current, questions=tuple(questions)), now)
            self._write(result.snapshot)
            return result

    def tick(self) -> ScheduleResult:
        """过期陈旧租约、同步终态并自动补足三个槽。"""
        with self._locked():
            result = self._sync_and_fill(self.snapshot(), self._now())
            self._write(result.snapshot)
            return result

    def configure(self, *, max_active: int) -> ScheduleResult:
        """兼容旧 CLI；campaign 并发只能确认成三槽。"""
        if max_active != CAMPAIGN_SLOT_LIMIT:
            raise ContractError("Q1-Q32 campaign Teacher limit is fixed at 3")
        return ScheduleResult(self.snapshot(), (), ())

    def heartbeat(
        self,
        question_id: str,
        *,
        owner_id: str,
        lease_id: str,
        generation: int,
    ) -> SchedulerSnapshot:
        """由真实 lease owner 刷新心跳；过期租约不能复活。"""
        with self._locked():
            current = self.snapshot()
            item = self._item(current, question_id)
            now = self._now()
            lease = self._require_lease(item, owner_id, lease_id, generation, now=now)
            refreshed = replace(
                lease,
                heartbeat_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=current.lease_ttl_sec)).isoformat(),
            )
            next_snapshot = self._replace_item(current, replace(item, lease=refreshed, updated_at=now.isoformat()))
            self._write(next_snapshot)
            return next_snapshot

    def claim_dispatch(self, question_id: str, *, worker_id: str) -> ScheduledQuestion | None:
        """原子认领一次待派发租约，防止并发 worker 重复发消息。"""
        if not worker_id.strip():
            raise ContractError("dispatch worker identity is required")
        with self._locked():
            current = self.snapshot()
            item = self._item(current, question_id)
            if item.state is not QueueState.LEASED or not item.lease:
                return None
            if parse_timestamp(item.lease.expires_at, "expires_at") <= self._now():
                return None
            if item.lease.dispatch_state is not DispatchState.PENDING:
                return None
            claimed = replace(
                item,
                lease=replace(
                    item.lease,
                    dispatch_state=DispatchState.SENDING,
                    dispatch_worker_id=worker_id,
                ),
                updated_at=self._now().isoformat(),
            )
            next_snapshot = self._replace_item(current, claimed)
            self._write(next_snapshot)
            return claimed

    def record_dispatch(
        self,
        question_id: str,
        message_id: str,
        *,
        worker_id: str,
        owner_id: str,
        lease_id: str,
        generation: int,
    ) -> SchedulerSnapshot:
        """以 worker claim 和 Teacher fencing 共同确认控制面回执。"""
        if not message_id.strip():
            raise ContractError("dispatch message identity is required")
        with self._locked():
            current = self.snapshot()
            item = self._item(current, question_id)
            lease = self._require_lease(item, owner_id, lease_id, generation, now=self._now())
            if lease.dispatch_state is not DispatchState.SENDING or lease.dispatch_worker_id != worker_id:
                raise ContractError("dispatch worker claim is stale")
            updated = replace(
                item,
                lease=replace(lease, dispatch_state=DispatchState.SENT, dispatch_message_id=message_id),
                updated_at=self._now().isoformat(),
            )
            next_snapshot = self._replace_item(current, updated)
            self._write(next_snapshot)
            return next_snapshot

    def require_recovery(
        self,
        question_id: str,
        *,
        worker_id: str,
        owner_id: str,
        lease_id: str,
        generation: int,
    ) -> SchedulerSnapshot:
        """派发失败时由已认领 worker 释放租约并保留恢复现场。"""
        with self._locked():
            current = self.snapshot()
            item = self._item(current, question_id)
            lease = self._require_lease(item, owner_id, lease_id, generation, now=self._now())
            if lease.dispatch_worker_id != worker_id:
                raise ContractError("dispatch worker claim is stale")
            updated = replace(
                item,
                state=QueueState.RECOVERY_REQUIRED,
                lease=None,
                updated_at=self._now().isoformat(),
            )
            intermediate = self._replace_item(current, updated, increment=False)
            result = self._sync_and_fill(intermediate, self._now())
            self._write(result.snapshot)
            return result.snapshot

    def set_waiting_external(
        self,
        question_id: str,
        *,
        owner_id: str,
        lease_id: str,
        generation: int,
        kind: str,
        external_id: str,
        phase: str,
    ) -> ScheduleResult:
        """把外部等待从三槽中移除，并在同一事务自动补位。"""
        with self._locked():
            current = self.snapshot()
            item = self._item(current, question_id)
            now = self._now()
            self._require_lease(item, owner_id, lease_id, generation, now=now)
            waiting = replace(
                item,
                state=QueueState.WAITING_EXTERNAL,
                phase=phase,
                lease=None,
                wait=ExternalWait(kind=kind, external_id=external_id, since=now.isoformat()),
                updated_at=now.isoformat(),
            )
            intermediate = self._replace_item(current, waiting, increment=False)
            result = self._sync_and_fill(intermediate, now)
            result = replace(result, released=(question_id, *result.released))
            self._write(result.snapshot)
            return result

    def resume_external(self, question_id: str, *, external_id: str) -> ScheduleResult:
        """以匹配外部回执恢复等待题，并按三槽规则重入队列。"""
        with self._locked():
            current = self.snapshot()
            item = self._item(current, question_id)
            if item.state is not QueueState.WAITING_EXTERNAL or not item.wait:
                raise ContractError("question is not waiting for an external result")
            if item.wait.external_id != external_id:
                raise ContractError("external wait identity is stale")
            now = self._now()
            ready = replace(item, state=QueueState.READY, wait=None, updated_at=now.isoformat())
            result = self._sync_and_fill(self._replace_item(current, ready, increment=False), now)
            self._write(result.snapshot)
            return result

    def requeue(self, question_id: str) -> ScheduleResult:
        """经人工确认后把恢复项重新置为 READY。"""
        with self._locked():
            current = self.snapshot()
            item = self._item(current, question_id)
            if item.state not in {QueueState.RECOVERY_REQUIRED, QueueState.BLOCKED}:
                raise ContractError("only recovery or blocked questions can be requeued")
            now = self._now()
            ready = replace(item, state=QueueState.READY, lease=None, wait=None, updated_at=now.isoformat())
            result = self._sync_and_fill(self._replace_item(current, ready, increment=False), now)
            self._write(result.snapshot)
            return result

    def bind_published_family(
        self,
        question_id: str,
        family: Path,
        *,
        owner_id: str,
        lease_id: str,
        generation: int,
    ) -> ScheduleResult:
        """验证最终题族并把完成运行与发布槽位原子绑定。"""
        number = question_number(question_id)
        if number <= 2:
            raise ContractError("q1 and q2 publication is grandfathered")
        resolved_family = family.resolve()
        with self._locked():
            current = self.snapshot()
            item = self._item(current, question_id)
            now = self._now()
            self._require_lease(item, owner_id, lease_id, generation, now=now)
            if not item.run_dir:
                raise ContractError("scheduled question has no run directory")
            run = RunStore(Path(item.run_dir)).read_snapshot()
            if run is None or run.state is not RunState.COMPLETED:
                raise ContractError("published family can only bind to a completed run")
            self.publication_validator(number, resolved_family)
            completed = replace(
                item,
                state=QueueState.COMPLETED,
                phase="COMPLETED",
                lease=None,
                wait=None,
                last_run_state=RunState.COMPLETED.value,
                published_family=str(resolved_family),
                completion_basis="RUN_COMPLETED_AND_FAMILY_PUBLISHED",
                updated_at=now.isoformat(),
            )
            intermediate = self._replace_item(current, completed, increment=False)
            result = self._sync_and_fill(intermediate, now)
            result = replace(result, released=(question_id, *result.released))
            self._write(result.snapshot)
            return result

    def _sync_and_fill(self, current: SchedulerSnapshot, now: datetime) -> ScheduleResult:
        released: list[str] = []
        questions = list(current.questions)
        for index, item in enumerate(questions):
            if item.state is not QueueState.LEASED or not item.lease:
                continue
            if parse_timestamp(item.lease.expires_at, "expires_at") <= now:
                questions[index] = replace(
                    item, state=QueueState.RECOVERY_REQUIRED, lease=None, updated_at=now.isoformat()
                )
                released.append(item.question_id)
                continue
            synchronized = self._sync_run(item, now)
            questions[index] = synchronized
            if synchronized.state is not QueueState.LEASED:
                released.append(item.question_id)
        occupied = sum(item.state is QueueState.LEASED for item in questions)
        owners = {item.lease.owner_id for item in questions if item.lease}
        activated: list[str] = []
        for index, item in enumerate(questions):
            if occupied >= CAMPAIGN_SLOT_LIMIT:
                break
            if not self._eligible(item) or item.teacher_thread_id in owners:
                continue
            lease = TeacherLease(
                lease_id=self.token_factory(),
                owner_id=item.teacher_thread_id,
                heartbeat_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=current.lease_ttl_sec)).isoformat(),
            )
            questions[index] = replace(
                item,
                state=QueueState.LEASED,
                generation=item.generation + 1,
                lease=lease,
                updated_at=now.isoformat(),
            )
            owners.add(lease.owner_id)
            activated.append(item.question_id)
            occupied += 1
        snapshot = replace(current, sequence=current.sequence + 1, questions=tuple(questions))
        return ScheduleResult(snapshot, tuple(activated), tuple(released))

    @staticmethod
    def _eligible(item: ScheduledQuestion) -> bool:
        return bool(
            item.state is QueueState.READY
            and item.run_dir
            and item.teacher_thread_id
            and item.teacher_prompt_path
        )

    @staticmethod
    def _sync_run(item: ScheduledQuestion, now: datetime) -> ScheduledQuestion:
        assert item.run_dir is not None
        run = RunStore(Path(item.run_dir)).read_snapshot()
        if run is None:
            return replace(item, state=QueueState.RECOVERY_REQUIRED, lease=None, updated_at=now.isoformat())
        if run.state is RunState.COMPLETED:
            return replace(
                item,
                phase="RUN_COMPLETED_AWAITING_PUBLICATION",
                last_run_state=run.state.value,
                updated_at=now.isoformat(),
            )
        if run.state in {RunState.DEFERRED_TIMEOUT, RunState.HUMAN_REVIEW}:
            return replace(
                item,
                state=QueueState.WAITING_EXTERNAL,
                lease=None,
                wait=ExternalWait(
                    kind=run.state.value,
                    external_id=f"run:{run.run_id}:{run.sequence}",
                    since=now.isoformat(),
                ),
                last_run_state=run.state.value,
                updated_at=now.isoformat(),
            )
        if run.state is RunState.BLOCKED:
            return replace(
                item,
                state=QueueState.BLOCKED,
                lease=None,
                last_run_state=run.state.value,
                updated_at=now.isoformat(),
            )
        if item.last_run_state != run.state.value:
            return replace(item, phase=run.state.value, last_run_state=run.state.value, updated_at=now.isoformat())
        return item

    def _require_lease(
        self,
        item: ScheduledQuestion,
        owner_id: str,
        lease_id: str,
        generation: int,
        *,
        now: datetime,
    ) -> TeacherLease:
        lease = item.lease
        if (
            item.state is not QueueState.LEASED
            or not lease
            or lease.owner_id != owner_id
            or lease.lease_id != lease_id
            or item.generation != generation
            or parse_timestamp(lease.expires_at, "expires_at") <= now
        ):
            raise ContractError("lease fencing identity is stale")
        return lease

    @staticmethod
    def _item(snapshot: SchedulerSnapshot, question_id: str) -> ScheduledQuestion:
        return snapshot.questions[question_number(question_id) - 1]

    @staticmethod
    def _replace_item(
        snapshot: SchedulerSnapshot,
        item: ScheduledQuestion,
        *,
        increment: bool = True,
    ) -> SchedulerSnapshot:
        questions = list(snapshot.questions)
        questions[item.position - 1] = item
        return replace(
            snapshot,
            sequence=snapshot.sequence + int(increment),
            questions=tuple(questions),
        )

    def _write(self, snapshot: SchedulerSnapshot) -> None:
        snapshot.validate()
        self.root.mkdir(parents=True, exist_ok=True)
        self._backup_v1_if_needed()
        descriptor, temporary = tempfile.mkstemp(prefix="scheduler-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(snapshot.to_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            self._fsync_root()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _backup_v1_if_needed(self) -> None:
        if not self.state_path.is_file() or self.state_path.is_symlink():
            return
        original = self.state_path.read_bytes()
        try:
            value = json.loads(original)
        except json.JSONDecodeError as error:
            raise ContractError("scheduler state is invalid JSON") from error
        if value.get("schema_version", 1) != 1:
            return
        if self.v1_backup_path.exists():
            if self.v1_backup_path.read_bytes() != original:
                raise ContractError("scheduler v1 backup does not match migration source")
            return
        descriptor = os.open(self.v1_backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, original)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_root(self) -> None:
        """确保备份和原子替换的目录项落盘。"""
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ContractError("scheduler clock must be timezone-aware")
        return now.astimezone(UTC)

    def _locked(self) -> _FileLock:
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        return _FileLock(self.lock_path)


class _FileLock:
    """scheduler.json 事务的进程级排他锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = None

    def __enter__(self) -> None:
        self.stream = self.path.open("r+")
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)

    def __exit__(self, exc_type, exc, traceback) -> None:
        assert self.stream is not None
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()

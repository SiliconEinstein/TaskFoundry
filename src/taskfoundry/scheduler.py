"""面向多题连续出题的持久调度模块。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .model import ContractError, RunState
from .store import RunStore


DEFAULT_TEACHER_MAX_ACTIVE = 5
MAX_TEACHER_MAX_ACTIVE = 200


class QueueState(StrEnum):
    """一道题在多题调度队列中的状态。"""

    WAITING = "WAITING"
    ACTIVE = "ACTIVE"
    DEFERRED = "DEFERRED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ScheduledQuestion:
    """一条只用于正式出题的调度记录。"""

    question_id: str
    run_dir: str
    state: QueueState
    position: int
    updated_at: str
    last_run_state: str | None = None
    teacher_thread_id: str | None = None
    teacher_prompt_path: str | None = None
    dispatch_message_id: str | None = None

    def validate(self) -> None:
        """拒绝实验条目、相对路径和无效顺序。"""
        if not self.question_id.strip() or self.position < 1:
            raise ContractError("question_id and positive position are required")
        if not Path(self.run_dir).is_absolute():
            raise ContractError("scheduled run_dir must be absolute")
        if bool(self.teacher_thread_id) != bool(self.teacher_prompt_path):
            raise ContractError("Teacher thread and prompt must be configured together")
        if self.teacher_prompt_path and not Path(self.teacher_prompt_path).is_absolute():
            raise ContractError("Teacher prompt path must be absolute")

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的表示。"""
        self.validate()
        value = asdict(self)
        value["state"] = self.state.value
        return value


@dataclass(frozen=True)
class SchedulerSnapshot:
    """带动态 Teacher 并发上限的可恢复调度快照。"""

    sequence: int
    questions: tuple[ScheduledQuestion, ...]
    max_active: int = DEFAULT_TEACHER_MAX_ACTIVE
    schema_version: int = 1

    def validate(self) -> None:
        """校验队列唯一性、顺序和并发上限。"""
        if (
            self.schema_version != 1
            or self.sequence < 0
            or not 1 <= self.max_active <= MAX_TEACHER_MAX_ACTIVE
        ):
            raise ContractError("unsupported scheduler snapshot")
        identifiers = [item.question_id for item in self.questions]
        positions = [item.position for item in self.questions]
        if len(identifiers) != len(set(identifiers)) or len(positions) != len(set(positions)):
            raise ContractError("scheduled questions and positions must be unique")
        for item in self.questions:
            item.validate()

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的表示。"""
        self.validate()
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "max_active": self.max_active,
            "questions": [item.to_dict() for item in self.questions],
        }


@dataclass(frozen=True)
class ScheduleResult:
    """一次调度后需要启动和停止关注的题目。"""

    snapshot: SchedulerSnapshot
    activated: tuple[str, ...]
    released: tuple[str, ...]


class QuestionScheduler:
    """隐藏文件锁、状态同步和自动补位细节的多题调度模块。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / "scheduler.json"
        self.lock_path = root / "scheduler.lock"

    def snapshot(self) -> SchedulerSnapshot:
        """读取当前快照；尚未初始化时返回空队列。"""
        if not self.state_path.is_file():
            return SchedulerSnapshot(sequence=0, questions=())
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        questions = tuple(
            ScheduledQuestion(**item | {"state": QueueState(item["state"])})
            for item in value.get("questions", ())
        )
        snapshot = SchedulerSnapshot(
            sequence=value["sequence"],
            questions=questions,
            max_active=value.get("max_active", DEFAULT_TEACHER_MAX_ACTIVE),
            schema_version=value.get("schema_version", 1),
        )
        snapshot.validate()
        return snapshot

    def enqueue(
        self,
        question_id: str,
        run_dir: Path,
        *,
        teacher_thread_id: str | None = None,
        teacher_prompt_path: Path | None = None,
    ) -> ScheduleResult:
        """幂等加入正式出题队列，并立即补足空闲槽位。"""
        with self._locked():
            current = self.snapshot()
            resolved = str(run_dir.resolve())
            existing = next((item for item in current.questions if item.question_id == question_id), None)
            if existing:
                if existing.run_dir != resolved:
                    raise ContractError("question_id already targets another run")
                return ScheduleResult(current, (), ())
            now = datetime.now(UTC).isoformat()
            item = ScheduledQuestion(
                question_id=question_id,
                run_dir=resolved,
                state=QueueState.WAITING,
                position=max((value.position for value in current.questions), default=0) + 1,
                updated_at=now,
                teacher_thread_id=teacher_thread_id,
                teacher_prompt_path=str(teacher_prompt_path.resolve()) if teacher_prompt_path else None,
            )
            return self._sync_and_fill(replace(current, questions=(*current.questions, item)))

    def tick(self) -> ScheduleResult:
        """同步所有活动题的运行状态，并自动激活后续待处理题。"""
        with self._locked():
            return self._sync_and_fill(self.snapshot())

    def configure(self, *, max_active: int) -> ScheduleResult:
        """持久修改 Teacher 上限；降容时不终止已经活动的题。"""
        if not 1 <= max_active <= MAX_TEACHER_MAX_ACTIVE:
            raise ContractError("Teacher max_active must be between 1 and 200")
        with self._locked():
            current = self.snapshot()
            if current.max_active == max_active:
                return ScheduleResult(current, (), ())
            return self._sync_and_fill(replace(current, max_active=max_active))

    def record_dispatch(self, question_id: str, message_id: str) -> SchedulerSnapshot:
        """记录控制面已接收 Teacher 工作，避免重复派发。"""
        if not message_id.strip():
            raise ContractError("dispatch message identity is required")
        with self._locked():
            current = self.snapshot()
            questions = tuple(
                replace(item, dispatch_message_id=message_id, updated_at=datetime.now(UTC).isoformat())
                if item.question_id == question_id and item.state is QueueState.ACTIVE
                else item
                for item in current.questions
            )
            if questions == current.questions:
                raise ContractError("active scheduled question is unavailable")
            next_snapshot = replace(current, sequence=current.sequence + 1, questions=questions)
            self._write(next_snapshot)
            return next_snapshot

    def require_recovery(self, question_id: str) -> SchedulerSnapshot:
        """派发失败时标记人工恢复，禁止静默重复启动任务。"""
        with self._locked():
            current = self.snapshot()
            now = datetime.now(UTC).isoformat()
            questions = tuple(
                replace(item, state=QueueState.RECOVERY_REQUIRED, updated_at=now)
                if item.question_id == question_id and item.state is QueueState.ACTIVE
                else item
                for item in current.questions
            )
            if questions == current.questions:
                raise ContractError("active scheduled question is unavailable")
            next_snapshot = replace(current, sequence=current.sequence + 1, questions=questions)
            self._write(next_snapshot)
            return next_snapshot

    def _sync_and_fill(self, current: SchedulerSnapshot) -> ScheduleResult:
        now = datetime.now(UTC).isoformat()
        released: list[str] = []
        synchronized: list[ScheduledQuestion] = []
        for item in current.questions:
            updated = self._sync_item(item, now)
            if item.state is QueueState.ACTIVE and updated.state is not QueueState.ACTIVE:
                released.append(item.question_id)
            synchronized.append(updated)
        free_slots = current.max_active - sum(item.state is QueueState.ACTIVE for item in synchronized)
        activated: list[str] = []
        if free_slots > 0:
            for index, item in enumerate(synchronized):
                if item.state is not QueueState.WAITING:
                    continue
                synchronized[index] = replace(item, state=QueueState.ACTIVE, updated_at=now)
                activated.append(item.question_id)
                free_slots -= 1
                if free_slots == 0:
                    break
        next_snapshot = SchedulerSnapshot(
            sequence=current.sequence + 1,
            questions=tuple(synchronized),
            max_active=current.max_active,
        )
        self._write(next_snapshot)
        return ScheduleResult(next_snapshot, tuple(activated), tuple(released))

    @staticmethod
    def _sync_item(item: ScheduledQuestion, now: str) -> ScheduledQuestion:
        if item.state is not QueueState.ACTIVE:
            return item
        run = RunStore(Path(item.run_dir)).read_snapshot()
        if run is None:
            return replace(item, state=QueueState.RECOVERY_REQUIRED, updated_at=now)
        mapping = {
            RunState.COMPLETED: QueueState.COMPLETED,
            RunState.DEFERRED_TIMEOUT: QueueState.DEFERRED,
            RunState.HUMAN_REVIEW: QueueState.HUMAN_REVIEW,
            RunState.BLOCKED: QueueState.BLOCKED,
        }
        next_state = mapping.get(run.state, QueueState.ACTIVE)
        if next_state is item.state and item.last_run_state == run.state.value:
            return item
        return replace(item, state=next_state, last_run_state=run.state.value, updated_at=now)

    def _write(self, snapshot: SchedulerSnapshot) -> None:
        snapshot.validate()
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="scheduler-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(snapshot.to_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _locked(self):
        """返回进程级排他锁上下文，避免多个调度者同时补位。"""
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        return _FileLock(self.lock_path)


class _FileLock:
    """调度模块内部使用的文件锁适配器。"""

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

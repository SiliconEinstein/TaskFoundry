"""Q1--Q32 出题活动的持久调度状态契约。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
import re
from typing import Any

from .model import ContractError


CAMPAIGN_QUESTION_COUNT = 32
CAMPAIGN_SLOT_LIMIT = 3
DEFAULT_LEASE_TTL_SEC = 600
CAMPAIGN_ID = "q01-q32-runtime-first"
_QUESTION_ID = re.compile(r"q([1-9]|[12][0-9]|3[0-2])$")


class QueueState(StrEnum):
    """一道题是否占用 Teacher 执行槽。"""

    BACKLOG = "BACKLOG"
    READY = "READY"
    LEASED = "LEASED"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"

    # 兼容旧调用方的符号名；v2 持久值始终使用上面的新名称。
    WAITING = "READY"
    ACTIVE = "LEASED"
    DEFERRED = "WAITING_EXTERNAL"
    HUMAN_REVIEW = "WAITING_EXTERNAL"


class DispatchState(StrEnum):
    """一次租约的控制面派发进度。"""

    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"


def parse_timestamp(value: str, label: str) -> datetime:
    """解析带时区 ISO 时间，拒绝本地模糊时间。"""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ContractError(f"{label} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def question_number(question_id: str) -> int:
    """返回规范 q1--q32 编号。"""
    match = _QUESTION_ID.fullmatch(question_id)
    if not match:
        raise ContractError("campaign question_id must be q1 through q32")
    return int(match.group(1))


@dataclass(frozen=True)
class TeacherLease:
    """唯一 Teacher owner 的可过期写租约。"""

    lease_id: str
    owner_id: str
    heartbeat_at: str
    expires_at: str
    dispatch_state: DispatchState = DispatchState.PENDING
    dispatch_worker_id: str | None = None
    dispatch_message_id: str | None = None

    def validate(self) -> None:
        """校验期限和派发状态的一致性。"""
        if not self.lease_id or not self.owner_id:
            raise ContractError("lease identity and owner are required")
        if parse_timestamp(self.expires_at, "expires_at") <= parse_timestamp(
            self.heartbeat_at, "heartbeat_at"
        ):
            raise ContractError("lease expiry must follow heartbeat")
        if self.dispatch_state is DispatchState.PENDING:
            if self.dispatch_worker_id or self.dispatch_message_id:
                raise ContractError("pending dispatch cannot have a worker or receipt")
        elif self.dispatch_state is DispatchState.SENDING:
            if not self.dispatch_worker_id or self.dispatch_message_id:
                raise ContractError("sending dispatch requires one worker and no receipt")
        elif not self.dispatch_worker_id or not self.dispatch_message_id:
            raise ContractError("sent dispatch requires worker and receipt identities")

    def to_dict(self) -> dict[str, Any]:
        """返回稳定 JSON 表示。"""
        self.validate()
        return {
            "lease_id": self.lease_id,
            "owner_id": self.owner_id,
            "heartbeat_at": self.heartbeat_at,
            "expires_at": self.expires_at,
            "dispatch_state": self.dispatch_state.value,
            "dispatch_worker_id": self.dispatch_worker_id,
            "dispatch_message_id": self.dispatch_message_id,
        }


@dataclass(frozen=True)
class ExternalWait:
    """不占 Teacher 槽的外部等待凭据。"""

    kind: str
    external_id: str
    since: str

    def validate(self) -> None:
        """校验等待类别、外部标识和时间。"""
        if not self.kind or not self.external_id:
            raise ContractError("external wait kind and identity are required")
        parse_timestamp(self.since, "wait.since")

    def to_dict(self) -> dict[str, str]:
        """返回稳定 JSON 表示。"""
        self.validate()
        return {"kind": self.kind, "external_id": self.external_id, "since": self.since}


@dataclass(frozen=True)
class ScheduledQuestion:
    """全局看板中的一道题及其当前写租约。"""

    question_id: str
    state: QueueState
    position: int
    updated_at: str
    phase: str = "BACKLOG"
    generation: int = 0
    run_dir: str | None = None
    last_run_state: str | None = None
    teacher_thread_id: str | None = None
    teacher_prompt_path: str | None = None
    lease: TeacherLease | None = None
    wait: ExternalWait | None = None
    published_family: str | None = None
    completion_basis: str | None = None

    def validate(self) -> None:
        """拒绝不完整路由、双重所有权和不一致状态。"""
        if question_number(self.question_id) != self.position or self.generation < 0 or not self.phase:
            raise ContractError("question position, generation, and phase are invalid")
        parse_timestamp(self.updated_at, "question.updated_at")
        if self.run_dir and not Path(self.run_dir).is_absolute():
            raise ContractError("scheduled run_dir must be absolute")
        if bool(self.teacher_thread_id) != bool(self.teacher_prompt_path):
            raise ContractError("Teacher thread and prompt must be configured together")
        if self.teacher_prompt_path and not Path(self.teacher_prompt_path).is_absolute():
            raise ContractError("Teacher prompt path must be absolute")
        if self.published_family and not Path(self.published_family).is_absolute():
            raise ContractError("published family path must be absolute")
        if self.state is QueueState.LEASED:
            if not self.lease or self.wait or self.lease.owner_id != self.teacher_thread_id:
                raise ContractError("leased question requires its unique routed owner")
            self.lease.validate()
        elif self.lease is not None:
            raise ContractError("only a leased question may carry a lease")
        if self.state is QueueState.WAITING_EXTERNAL:
            if not self.wait:
                raise ContractError("external waiting state requires wait identity")
            self.wait.validate()
        elif self.wait is not None:
            raise ContractError("only external waiting state may carry wait identity")
        if self.state is QueueState.COMPLETED:
            if not self.completion_basis:
                raise ContractError("completed question requires its completion basis")
            if self.position > 2 and (
                self.completion_basis != "RUN_COMPLETED_AND_FAMILY_PUBLISHED"
                or not self.published_family
                or self.last_run_state != "COMPLETED"
            ):
                raise ContractError(
                    "completed campaign question requires published family and exact completion basis"
                )
        elif self.completion_basis is not None:
            raise ContractError("only a completed question may carry a completion basis")

    def to_dict(self) -> dict[str, Any]:
        """返回稳定 JSON 表示。"""
        self.validate()
        return {
            "question_id": self.question_id,
            "state": self.state.value,
            "position": self.position,
            "updated_at": self.updated_at,
            "phase": self.phase,
            "generation": self.generation,
            "run_dir": self.run_dir,
            "last_run_state": self.last_run_state,
            "teacher_thread_id": self.teacher_thread_id,
            "teacher_prompt_path": self.teacher_prompt_path,
            "lease": self.lease.to_dict() if self.lease else None,
            "wait": self.wait.to_dict() if self.wait else None,
            "published_family": self.published_family,
            "completion_basis": self.completion_basis,
        }


@dataclass(frozen=True)
class SchedulerSnapshot:
    """Q1--Q32 单一看板和固定三槽调度快照。"""

    sequence: int
    questions: tuple[ScheduledQuestion, ...]
    lease_ttl_sec: int = DEFAULT_LEASE_TTL_SEC
    max_active: int = CAMPAIGN_SLOT_LIMIT
    campaign_id: str = CAMPAIGN_ID
    schema_version: int = 2

    def validate(self) -> None:
        """校验全局题集、三槽和唯一 owner/fencing。"""
        if self.schema_version != 2 or self.sequence < 0 or self.max_active != CAMPAIGN_SLOT_LIMIT:
            raise ContractError("unsupported scheduler snapshot")
        if self.campaign_id != CAMPAIGN_ID or not 30 <= self.lease_ttl_sec <= 86400:
            raise ContractError("unsupported campaign or lease TTL")
        expected = [f"q{index}" for index in range(1, CAMPAIGN_QUESTION_COUNT + 1)]
        if [item.question_id for item in self.questions] != expected:
            raise ContractError("scheduler must contain the ordered q1-q32 campaign")
        for item in self.questions:
            item.validate()
        leased = [item for item in self.questions if item.state is QueueState.LEASED]
        lease_ids = [item.lease.lease_id for item in leased if item.lease]
        owners = [item.lease.owner_id for item in leased if item.lease]
        if len(leased) > CAMPAIGN_SLOT_LIMIT or len(lease_ids) != len(set(lease_ids)):
            raise ContractError("campaign lease limit or lease uniqueness is violated")
        if len(owners) != len(set(owners)):
            raise ContractError("one Teacher owner cannot hold multiple campaign leases")
        for question_id in ("q1", "q2"):
            item = self.questions[question_number(question_id) - 1]
            if item.state is not QueueState.COMPLETED or item.completion_basis != "GRANDFATHERED":
                raise ContractError("q1 and q2 must remain grandfathered completed")

    def to_dict(self) -> dict[str, Any]:
        """返回公开看板 JSON。"""
        self.validate()
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "campaign_id": self.campaign_id,
            "max_active": self.max_active,
            "lease_ttl_sec": self.lease_ttl_sec,
            "questions": [item.to_dict() for item in self.questions],
        }


def campaign_snapshot(now: datetime, *, lease_ttl_sec: int, sequence: int = 0) -> SchedulerSnapshot:
    """构造含 grandfather 与全局 backlog 的初始看板。"""
    timestamp = now.astimezone(UTC).isoformat()
    questions = tuple(
        ScheduledQuestion(
            question_id=f"q{index}",
            state=QueueState.COMPLETED if index <= 2 else QueueState.BACKLOG,
            position=index,
            updated_at=timestamp,
            phase="GRANDFATHERED" if index <= 2 else "BACKLOG",
            completion_basis="GRANDFATHERED" if index <= 2 else None,
        )
        for index in range(1, CAMPAIGN_QUESTION_COUNT + 1)
    )
    return SchedulerSnapshot(sequence=sequence, questions=questions, lease_ttl_sec=lease_ttl_sec)


def snapshot_from_dict(value: dict[str, Any], *, now: datetime, lease_ttl_sec: int) -> SchedulerSnapshot:
    """读取 v2，或把 v1 安全提升为无伪心跳的 v2 内存快照。"""
    if value.get("schema_version", 1) == 1:
        return _migrate_v1(value, now=now, lease_ttl_sec=lease_ttl_sec)
    if value.get("schema_version") != 2:
        raise ContractError("unsupported scheduler schema_version")
    questions = tuple(_question_from_dict(item) for item in value.get("questions", ()))
    snapshot = SchedulerSnapshot(
        sequence=value["sequence"],
        questions=questions,
        lease_ttl_sec=value.get("lease_ttl_sec", lease_ttl_sec),
        max_active=value.get("max_active", CAMPAIGN_SLOT_LIMIT),
        campaign_id=value.get("campaign_id", CAMPAIGN_ID),
        schema_version=2,
    )
    snapshot.validate()
    return snapshot


def _question_from_dict(value: dict[str, Any]) -> ScheduledQuestion:
    lease_value = value.get("lease")
    wait_value = value.get("wait")
    lease = None
    if lease_value is not None:
        lease = TeacherLease(**lease_value | {"dispatch_state": DispatchState(lease_value["dispatch_state"])})
    wait = ExternalWait(**wait_value) if wait_value is not None else None
    item = ScheduledQuestion(
        **value | {"state": QueueState(value["state"]), "lease": lease, "wait": wait}
    )
    if item.position > 2 and item.state is QueueState.COMPLETED and (
        item.completion_basis != "RUN_COMPLETED_AND_FAMILY_PUBLISHED"
        or not item.published_family
        or item.last_run_state != "COMPLETED"
    ):
        return replace(
            item,
            state=QueueState.RECOVERY_REQUIRED,
            phase="PUBLICATION_BINDING_REQUIRED",
            lease=None,
            wait=None,
            published_family=None,
            completion_basis=None,
        )
    return item


def _migrate_v1(value: dict[str, Any], *, now: datetime, lease_ttl_sec: int) -> SchedulerSnapshot:
    board = campaign_snapshot(now, lease_ttl_sec=lease_ttl_sec, sequence=value.get("sequence", 0))
    questions = list(board.questions)
    state_map = {
        "WAITING": QueueState.READY,
        "ACTIVE": QueueState.RECOVERY_REQUIRED,
        "DEFERRED": QueueState.WAITING_EXTERNAL,
        "HUMAN_REVIEW": QueueState.WAITING_EXTERNAL,
        "RECOVERY_REQUIRED": QueueState.RECOVERY_REQUIRED,
        # 只有 Q1/Q2 获得 grandfather；其余历史完成记录必须重新核验。
        "COMPLETED": QueueState.RECOVERY_REQUIRED,
        "BLOCKED": QueueState.BLOCKED,
    }
    for legacy in value.get("questions", ()):
        number = question_number(legacy["question_id"])
        if number <= 2:
            continue
        state = state_map.get(legacy["state"])
        if state is None:
            raise ContractError(f"unsupported v1 queue state: {legacy['state']}")
        updated_at = legacy.get("updated_at", now.astimezone(UTC).isoformat())
        wait = None
        if state is QueueState.WAITING_EXTERNAL:
            wait = ExternalWait(
                kind=f"LEGACY_{legacy['state']}",
                external_id=f"legacy:{legacy['question_id']}:{value.get('sequence', 0)}",
                since=updated_at,
            )
        phase = (
            "LEGACY_COMPLETION_REVIEW_REQUIRED"
            if legacy["state"] == "COMPLETED"
            else legacy.get("last_run_state") or "BACKLOG"
        )
        questions[number - 1] = replace(
            questions[number - 1],
            state=state,
            updated_at=updated_at,
            phase=phase,
            run_dir=legacy.get("run_dir"),
            last_run_state=legacy.get("last_run_state"),
            teacher_thread_id=legacy.get("teacher_thread_id"),
            teacher_prompt_path=legacy.get("teacher_prompt_path"),
            wait=wait,
            completion_basis=None,
        )
    migrated = replace(board, questions=tuple(questions))
    migrated.validate()
    return migrated

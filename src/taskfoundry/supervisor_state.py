"""CampaignSupervisor 的版本化计划和单题运行快照。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator

from .model import ContractError


class SupervisorPhase(StrEnum):
    """Supervisor 对一道题拥有的持久推进阶段。"""

    READY = "READY"
    RUNNING = "RUNNING"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    TEACHER_ACTION = "TEACHER_ACTION"
    FINISHED = "FINISHED"


@dataclass(frozen=True)
class SupervisorPlan:
    """一道题启动持久 Harbor 验证所需的非秘密配置。"""

    question_id: str
    run_dir: str
    package_path: str
    job_config_path: str
    runtime_path: str
    env_file: str
    queue_root: str
    validation_session_id: str
    pass_threshold: float = 0.85
    overall_timeout_sec: int = 7200
    schema_version: int = 1

    def validate(self) -> None:
        """拒绝相对路径、秘密内联和非法验证边界。"""
        if self.schema_version != 1 or not self.question_id.startswith("q"):
            raise ContractError("unsupported supervisor plan")
        for name in (
            "run_dir",
            "package_path",
            "job_config_path",
            "runtime_path",
            "env_file",
            "queue_root",
        ):
            value = Path(str(getattr(self, name)))
            if not value.is_absolute():
                raise ContractError(f"supervisor plan {name} must be absolute")
        if not self.validation_session_id.strip():
            raise ContractError("supervisor validation session is required")
        if not 0 < self.pass_threshold <= 1:
            raise ContractError("supervisor pass threshold must be in (0, 1]")
        if not 3600 <= self.overall_timeout_sec <= 14400:
            raise ContractError("supervisor overall timeout must be 3600..14400 seconds")

    def to_dict(self) -> dict[str, Any]:
        """返回稳定 JSON 表示。"""
        self.validate()
        return asdict(self)

    @classmethod
    def from_path(cls, path: Path) -> "SupervisorPlan":
        """严格读取一份 Supervisor 计划。"""
        plan = cls(**_read_json(path))
        plan.validate()
        return plan


@dataclass(frozen=True)
class SupervisorSnapshot:
    """单题 Supervisor 可从进程崩溃恢复的最小状态。"""

    question_id: str
    phase: SupervisorPhase
    sequence: int
    runtime_attempt: int
    scientific_round: int
    updated_at: str
    request_id: str | None = None
    handoff_path: str | None = None
    receipt_path: str | None = None
    process_id: int | None = None
    process_started_at: str | None = None
    last_decided_round: int = 0
    next_action_at: str | None = None
    last_failure_stage: str | None = None
    last_evidence_sha256: str | None = None
    recovery_condition: str | None = None
    schema_version: int = 1

    def validate(self) -> None:
        """校验阶段和执行身份的一致性。"""
        if (
            self.schema_version != 1
            or not self.question_id.startswith("q")
            or self.sequence < 0
            or self.runtime_attempt < 0
            or self.scientific_round < 1
            or self.last_decided_round < 0
            or not self.updated_at
        ):
            raise ContractError("unsupported supervisor snapshot")
        if self.phase is SupervisorPhase.RUNNING and (
            not self.request_id
            or not self.handoff_path
            or not self.receipt_path
            or not self.process_id
            or not self.process_started_at
        ):
            raise ContractError("running supervisor state requires process identity")
        if self.phase is SupervisorPhase.RETRY_SCHEDULED and not self.next_action_at:
            raise ContractError("scheduled retry requires next action time")
        if self.phase is SupervisorPhase.WAITING_EXTERNAL and not self.recovery_condition:
            raise ContractError("external wait requires recovery condition")
        for value in (self.handoff_path, self.receipt_path):
            if value is not None and not Path(value).is_absolute():
                raise ContractError("supervisor evidence paths must be absolute")
        if self.last_evidence_sha256 is not None and not _full_sha256(
            self.last_evidence_sha256
        ):
            raise ContractError("supervisor evidence digest is invalid")

    def to_dict(self) -> dict[str, Any]:
        """返回稳定 JSON 表示。"""
        self.validate()
        value = asdict(self)
        value["phase"] = self.phase.value
        return value


class SupervisorStateStore:
    """以一个锁原子维护单题 Supervisor 计划和快照。"""

    def __init__(self, run_dir: Path) -> None:
        self.root = run_dir / "supervisor"
        self.plan_path = self.root / "plan.json"
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "state.lock"

    def register(self, plan: SupervisorPlan, *, now: str) -> SupervisorSnapshot:
        """幂等注册计划；既有计划不得漂移。"""
        plan.validate()
        with self.locked():
            if self.plan_path.exists():
                if _read_json(self.plan_path) != plan.to_dict():
                    raise ContractError("supervisor plan is already bound differently")
            else:
                _write_json(self.plan_path, plan.to_dict())
            if self.state_path.exists():
                return self.read_unlocked()
            snapshot = SupervisorSnapshot(
                question_id=plan.question_id,
                phase=SupervisorPhase.READY,
                sequence=0,
                runtime_attempt=0,
                scientific_round=1,
                updated_at=now,
            )
            self.write_unlocked(snapshot)
            return snapshot

    def read(self) -> SupervisorSnapshot:
        """在锁内读取当前快照。"""
        with self.locked():
            return self.read_unlocked()

    def read_unlocked(self) -> SupervisorSnapshot:
        """供已持锁事务读取快照。"""
        value = _read_json(self.state_path)
        snapshot = SupervisorSnapshot(
            **value | {"phase": SupervisorPhase(value["phase"])}
        )
        snapshot.validate()
        return snapshot

    def write_unlocked(self, snapshot: SupervisorSnapshot) -> None:
        """供已持锁事务原子写入快照。"""
        snapshot.validate()
        _write_json(self.state_path, snapshot.to_dict())

    def locked(self) -> Iterator[None]:
        """返回可用于跨读写事务的文件锁。"""
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        return _FileLock(self.lock_path)


class _FileLock:
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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"supervisor state is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise ContractError("supervisor state root must be an object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _full_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

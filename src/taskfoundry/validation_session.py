"""跨运行持久化线性验证会话的唯一身份。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
from typing import Any

from .model import ContractError


@dataclass(frozen=True)
class ValidationSessionRecord:
    """一份不可复用的验证会话身份及其当前状态。"""

    run_id: str
    question_revision: str
    validation_session_id: str
    researcher_thread_id: str
    package_sha256: str
    status: str
    created_at: str
    closed_at: str | None = None


class ValidationSessionRegistry:
    """在进程锁内追加会话事件，并拒绝跨运行身份复用。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    def reserve(
        self,
        *,
        run_id: str,
        question_revision: str,
        validation_session_id: str,
        researcher_thread_id: str,
        package_sha256: str,
    ) -> ValidationSessionRecord:
        """原子保留 revision、session 和外层 Researcher thread。"""
        values = (
            run_id,
            question_revision,
            validation_session_id,
            researcher_thread_id,
            package_sha256,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ContractError("validation session identity is incomplete")
        if not _full_sha256(package_sha256):
            raise ContractError("validation session package digest is invalid")
        with self._locked():
            current = self._records_unlocked()
            exact = next(
                (
                    record
                    for record in current
                    if (
                        record.run_id,
                        record.question_revision,
                        record.validation_session_id,
                        record.researcher_thread_id,
                        record.package_sha256,
                    )
                    == values
                ),
                None,
            )
            if exact is not None:
                if exact.status != "ACTIVE":
                    raise ContractError("closed validation session identity cannot be reserved again")
                return exact
            if any(
                record.run_id == run_id and record.question_revision == question_revision
                for record in current
            ):
                raise ContractError("question revision already has a validation session")
            if any(record.validation_session_id == validation_session_id for record in current):
                raise ContractError("validation session identity was already used")
            thread_records = [
                record for record in current if record.researcher_thread_id == researcher_thread_id
            ]
            if thread_records and not all(
                record.run_id == run_id and record.status == "CLOSED_MIGRATED"
                for record in thread_records
            ):
                raise ContractError("Researcher thread identity was already used")
            record = ValidationSessionRecord(
                run_id=run_id,
                question_revision=question_revision,
                validation_session_id=validation_session_id,
                researcher_thread_id=researcher_thread_id,
                package_sha256=package_sha256,
                status="ACTIVE",
                created_at=datetime.now(UTC).isoformat(),
            )
            self._append({"event": "RESERVED", "record": asdict(record)})
            return record

    def close(self, validation_session_id: str, *, status: str) -> ValidationSessionRecord:
        """关闭会话但永久保留其身份，防止日后复用。"""
        if not status.startswith("CLOSED_"):
            raise ContractError("closed validation session requires a CLOSED_* status")
        with self._locked():
            current = self._records_unlocked()
            record = next(
                (item for item in current if item.validation_session_id == validation_session_id),
                None,
            )
            if record is None:
                raise ContractError("validation session is not registered")
            if record.status == status:
                return record
            if record.status != "ACTIVE":
                raise ContractError("validation session is already closed differently")
            closed = ValidationSessionRecord(
                **(
                    asdict(record)
                    | {
                        "status": status,
                        "closed_at": datetime.now(UTC).isoformat(),
                    }
                )
            )
            self._append({"event": "CLOSED", "record": asdict(closed)})
            return closed

    def records(self) -> tuple[ValidationSessionRecord, ...]:
        """返回每个会话的最新记录。"""
        with self._locked():
            return tuple(self._records_unlocked())

    def reconcile(self, validation_session_id: str, *, status: str) -> ValidationSessionRecord:
        """把 run journal 的终态投影回 registry；重复修复保持幂等。"""
        return self.close(validation_session_id, status=status)

    def _records_unlocked(self) -> list[ValidationSessionRecord]:
        if not self.path.exists():
            return []
        latest: dict[str, ValidationSessionRecord] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                value = json.loads(line, object_pairs_hook=_unique_object)
                if not isinstance(value, dict) or value.get("event") not in {"RESERVED", "CLOSED"}:
                    raise ValueError("unknown registry event")
                record = ValidationSessionRecord(**value["record"])
                latest[record.validation_session_id] = record
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ContractError(f"validation session registry is invalid: {error}") from error
        return list(latest.values())

    def _append(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
        descriptor = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        return _FileLock(self.lock_path)


class _FileLock:
    """registry 专用排他文件锁。"""

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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate registry key: {key}")
        value[key] = item
    return value


def _full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

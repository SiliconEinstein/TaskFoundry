"""支持崩溃恢复和进程并发的文件运行日志。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import fcntl
import os
from pathlib import Path
import tempfile
from typing import Any

from .model import Actor, ContractError, RunEvent, RunSnapshot, RunState


class SequenceConflict(RuntimeError):
    """调用者尝试覆盖更新快照时抛出。"""


class RunStore:
    """先追加事件，再原子替换可重建快照。"""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.events_path = run_dir / "events.jsonl"
        self.snapshot_path = run_dir / "state.json"
        self.lock_path = run_dir / "run.lock"

    def initialize(self, run_id: str) -> RunSnapshot:
        """创建新运行，或返回已经存在的初始快照。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        current = self.read_snapshot()
        if current is not None:
            if current.run_id != run_id:
                raise ContractError("run directory belongs to a different run")
            return current
        snapshot = RunSnapshot(run_id=run_id, state=RunState.DESIGNING, sequence=0)
        self._write_snapshot(snapshot, expected_sequence=None)
        return snapshot

    def read_snapshot(self) -> RunSnapshot | None:
        """读取快照，并重放崩溃前领先写入的一条日志。"""
        snapshot = self._read_snapshot_file()
        if not self.events_path.exists():
            return snapshot
        events = self.events()
        if not events or (snapshot is not None and snapshot.sequence == events[-1].sequence):
            return snapshot
        expected = 0 if snapshot is None else snapshot.sequence
        if events[-1].sequence != expected + 1:
            raise ContractError("journal cannot be reconciled with snapshot")
        encoded = events[-1].payload.get("_next_snapshot")
        if not isinstance(encoded, dict):
            raise ContractError("journal entry lacks recoverable next snapshot")
        recovered = self._snapshot_from_dict(encoded)
        self._write_snapshot(recovered, expected_sequence=expected, recover=False)
        return recovered

    def _read_snapshot_file(self) -> RunSnapshot | None:
        """只读取物化快照，不执行日志协调。"""
        if not self.snapshot_path.exists():
            return None
        value = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        return self._snapshot_from_dict(value)

    @staticmethod
    def _snapshot_from_dict(value: dict[str, Any]) -> RunSnapshot:
        """从稳定 JSON 表示恢复快照对象。"""
        value["state"] = RunState(value["state"])
        value["attempts"] = tuple(value.get("attempts", ()))
        return RunSnapshot(**value)

    def events(self) -> list[RunEvent]:
        """加载日志并拒绝序列缺口或重复幂等键。"""
        if not self.events_path.exists():
            return []
        result: list[RunEvent] = []
        keys: set[str] = set()
        for expected, line in enumerate(self.events_path.read_text(encoding="utf-8").splitlines(), 1):
            value = json.loads(line)
            value["actor"] = Actor(value["actor"])
            event = RunEvent(**value)
            event.validate()
            if event.sequence != expected:
                raise ContractError(f"event sequence gap at {expected}")
            if event.idempotency_key in keys:
                raise ContractError("duplicate idempotency key in journal")
            keys.add(event.idempotency_key)
            result.append(event)
        return result

    def commit(
        self,
        *,
        actor: Actor,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        snapshot: RunSnapshot,
    ) -> tuple[RunEvent, RunSnapshot]:
        """持久追加事件，并以 CAS 方式写入下一快照。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with _RunLock(self.lock_path):
            return self._commit_locked(
                actor=actor,
                event_type=event_type,
                idempotency_key=idempotency_key,
                payload=payload,
                snapshot=snapshot,
            )

    def _commit_locked(
        self,
        *,
        actor: Actor,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        snapshot: RunSnapshot,
    ) -> tuple[RunEvent, RunSnapshot]:
        """在进程级排他锁内完成事件追加和快照 CAS。"""
        current = self.read_snapshot()
        if current is None:
            raise ContractError("run must be initialized before commit")
        existing = {item.idempotency_key: item for item in self.events()}
        if idempotency_key in existing:
            event = existing[idempotency_key]
            recorded = {key: value for key, value in event.payload.items() if key != "_next_snapshot"}
            if event.event_type != event_type or recorded != payload:
                raise ContractError("idempotency key identifies another operation")
            return event, current
        if snapshot.sequence != current.sequence + 1:
            raise SequenceConflict("next snapshot sequence is not current + 1")
        journal_payload = dict(payload)
        journal_payload["_next_snapshot"] = snapshot.to_dict()
        event = RunEvent(
            sequence=snapshot.sequence,
            timestamp=datetime.now(UTC).isoformat(),
            actor=actor,
            event_type=event_type,
            idempotency_key=idempotency_key,
            payload=journal_payload,
        )
        self._append_event(event)
        event = self.events()[-1]
        self._write_snapshot(snapshot, expected_sequence=current.sequence, recover=False)
        return event, snapshot

    def advance(
        self,
        current: RunSnapshot,
        *,
        state: RunState | None = None,
        **changes: Any,
    ) -> RunSnapshot:
        """构造但不持久化下一快照。"""
        return replace(
            current,
            sequence=current.sequence + 1,
            state=state or current.state,
            **changes,
        )

    def _append_event(self, event: RunEvent) -> None:
        encoded = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        descriptor = os.open(self.events_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_snapshot(
        self,
        snapshot: RunSnapshot,
        expected_sequence: int | None,
        *,
        recover: bool = True,
    ) -> None:
        current = self.read_snapshot() if recover else self._read_snapshot_file()
        actual = None if current is None else current.sequence
        if actual != expected_sequence:
            raise SequenceConflict(f"expected sequence {expected_sequence}, found {actual}")
        descriptor, temporary = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.run_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(snapshot.to_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.snapshot_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class _RunLock:
    """单个运行日志的进程级排他锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = None

    def __enter__(self) -> None:
        self.path.touch(mode=0o600, exist_ok=True)
        self.stream = self.path.open("r+")
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)

    def __exit__(self, exc_type, exc, traceback) -> None:
        assert self.stream is not None
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()

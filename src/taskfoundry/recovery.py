"""Harbor 非科学失败的持久重试、外部等待与共享熔断策略。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterator

from .model import ContractError


MODEL_TRANSPORT_ROUTE = "codex-strong-model-transport"
TRANSIENT_DELAYS_SEC = (30, 120, 300)
MODEL_CIRCUIT_COOLDOWN_SEC = 900


class RecoveryAction(StrEnum):
    """一次非科学结果之后允许 Supervisor 执行的动作。"""

    RETRY_RUNTIME = "RETRY_RUNTIME"
    WAIT_EXTERNAL = "WAIT_EXTERNAL"
    TEACHER_AUDIT = "TEACHER_AUDIT"
    SCIENTIFIC_DECISION = "SCIENTIFIC_DECISION"


class CircuitState(StrEnum):
    """共享模型传输线路的熔断状态。"""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True)
class RecoveryDecision:
    """Supervisor 可直接执行且可持久化的一项恢复决定。"""

    action: RecoveryAction
    reason: str
    failure_count: int
    retry_after_sec: int | None = None
    recovery_condition: str | None = None

    def validate(self) -> None:
        """拒绝缺少时间或恢复条件的决定。"""
        if self.failure_count < 1 or not self.reason:
            raise ContractError("recovery decision identity is invalid")
        if self.action is RecoveryAction.RETRY_RUNTIME:
            if self.retry_after_sec is None or self.retry_after_sec < 0:
                raise ContractError("runtime retry requires a non-negative delay")
        elif self.retry_after_sec is not None:
            raise ContractError("only runtime retry may carry a retry delay")
        if self.action is RecoveryAction.WAIT_EXTERNAL:
            if not self.recovery_condition:
                raise ContractError("external wait requires a recovery condition")
        elif self.recovery_condition is not None:
            raise ContractError("only external wait may carry a recovery condition")

    def to_dict(self) -> dict[str, Any]:
        """返回稳定 JSON 表示。"""
        self.validate()
        value = asdict(self)
        value["action"] = self.action.value
        return value


@dataclass(frozen=True)
class CircuitSnapshot:
    """一条外部线路的持久熔断快照。"""

    route: str
    state: CircuitState
    failure_count: int
    updated_at: str
    next_probe_at: str | None = None
    probe_owner: str | None = None
    last_evidence_sha256: str | None = None
    schema_version: int = 1

    def validate(self) -> None:
        """校验状态、时间和半开 owner 的一致性。"""
        if self.schema_version != 1 or not self.route or self.failure_count < 0:
            raise ContractError("unsupported circuit snapshot")
        _timestamp(self.updated_at, "circuit.updated_at")
        if self.state is CircuitState.CLOSED:
            if self.next_probe_at is not None or self.probe_owner is not None:
                raise ContractError("closed circuit cannot carry probe state")
        else:
            if self.next_probe_at is None:
                raise ContractError("open circuit requires next probe time")
            _timestamp(self.next_probe_at, "circuit.next_probe_at")
        if self.state is CircuitState.HALF_OPEN and not self.probe_owner:
            raise ContractError("half-open circuit requires one probe owner")
        if self.state is not CircuitState.HALF_OPEN and self.probe_owner is not None:
            raise ContractError("only half-open circuit may have a probe owner")
        if self.last_evidence_sha256 is not None and not _full_sha256(
            self.last_evidence_sha256
        ):
            raise ContractError("circuit evidence digest is invalid")

    def to_dict(self) -> dict[str, Any]:
        """返回稳定 JSON 表示。"""
        self.validate()
        value = asdict(self)
        value["state"] = self.state.value
        return value


@dataclass(frozen=True)
class RuntimeFailureRecord:
    """同一 Scientific Round 的一个阶段失败累计。"""

    question_id: str
    revision: str
    scientific_round: int
    failure_stage: str
    failure_count: int
    updated_at: str
    evidence_sha256: str
    decision: dict[str, Any]

    @property
    def identity(self) -> str:
        """返回不依赖 Runtime Attempt 的稳定累计键。"""
        return "|".join(
            (
                self.question_id,
                self.revision,
                str(self.scientific_round),
                self.failure_stage,
            )
        )


class RecoveryPolicy:
    """把失败阶段和累计次数收敛为一项确定性动作。"""

    transient_stages = frozenset({"IMAGE", "SANDBOX", "PLATFORM", "PROVIDER"})
    audit_stages = frozenset(
        {"HARNESS_BOOTSTRAP", "VERIFIER", "EXECUTION_CONTRACT_FAILURE"}
    )

    def decide(self, failure_stage: str, failure_count: int) -> RecoveryDecision:
        """应用固定重试预算；未知阶段交给 Teacher 审计。"""
        if failure_count < 1:
            raise ContractError("failure count must be positive")
        if failure_stage in self.transient_stages:
            if failure_count <= len(TRANSIENT_DELAYS_SEC):
                return RecoveryDecision(
                    RecoveryAction.RETRY_RUNTIME,
                    "transient Harbor infrastructure failure",
                    failure_count,
                    retry_after_sec=TRANSIENT_DELAYS_SEC[failure_count - 1],
                )
            return RecoveryDecision(
                RecoveryAction.WAIT_EXTERNAL,
                "transient retry budget exhausted",
                failure_count,
                recovery_condition="provider health probe succeeds",
            )
        if failure_stage == "MODEL_CONNECTION":
            return RecoveryDecision(
                RecoveryAction.WAIT_EXTERNAL,
                "shared strong-model transport circuit is open",
                failure_count,
                recovery_condition="strong model transport probe succeeds",
            )
        if failure_stage == "SCIENTIFIC_EXECUTION":
            return RecoveryDecision(
                RecoveryAction.SCIENTIFIC_DECISION,
                "scientific timeout or result requires validation policy",
                failure_count,
            )
        return RecoveryDecision(
            RecoveryAction.TEACHER_AUDIT,
            (
                "deterministic execution contract failure"
                if failure_stage in self.audit_stages
                else "unclassified failure requires Teacher audit"
            ),
            failure_count,
        )


class RecoveryLedger:
    """原子记录同一 Scientific Round 的失败，并维护共享模型熔断器。"""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        policy: RecoveryPolicy | None = None,
    ) -> None:
        self.root = root
        self.state_path = root / "runtime-failures.json"
        self.circuit_path = root / "model-transport-circuit.json"
        self.lock_path = root / "recovery.lock"
        self.clock = clock or (lambda: datetime.now(UTC))
        self.policy = policy or RecoveryPolicy()

    def record_failure(
        self,
        *,
        question_id: str,
        revision: str,
        scientific_round: int,
        failure_stage: str,
        evidence_sha256: str,
    ) -> RecoveryDecision:
        """累计一次 Runtime Attempt 失败并返回下一动作。"""
        _validate_failure_identity(
            question_id, revision, scientific_round, failure_stage, evidence_sha256
        )
        with self._locked():
            records = self._records()
            identity = _failure_identity(
                question_id, revision, scientific_round, failure_stage
            )
            count = int(records.get(identity, {}).get("failure_count", 0)) + 1
            decision = self.policy.decide(failure_stage, count)
            now = self._now()
            record = RuntimeFailureRecord(
                question_id=question_id,
                revision=revision,
                scientific_round=scientific_round,
                failure_stage=failure_stage,
                failure_count=count,
                updated_at=now.isoformat(),
                evidence_sha256=evidence_sha256,
                decision=decision.to_dict(),
            )
            records[identity] = asdict(record)
            _write_json(self.state_path, {"schema_version": 1, "records": records})
            if failure_stage == "MODEL_CONNECTION":
                self._open_circuit(now, count, evidence_sha256)
            return decision

    def clear_round(self, question_id: str, revision: str, scientific_round: int) -> None:
        """成功导入 Scientific Round 后删除其 Runtime Attempt 累计。"""
        with self._locked():
            records = {
                key: value
                for key, value in self._records().items()
                if not key.startswith(f"{question_id}|{revision}|{scientific_round}|")
            }
            _write_json(self.state_path, {"schema_version": 1, "records": records})

    def circuit(self) -> CircuitSnapshot:
        """读取共享 strong 模型传输熔断状态。"""
        with self._locked():
            return self._circuit()

    def claim_model_probe(self, worker_id: str) -> CircuitSnapshot | None:
        """到期后原子授予唯一模型传输探针。"""
        if not worker_id.strip():
            raise ContractError("probe worker identity is required")
        with self._locked():
            current = self._circuit()
            now = self._now()
            if current.state is not CircuitState.OPEN or _timestamp(
                current.next_probe_at, "circuit.next_probe_at"
            ) > now:
                return None
            claimed = CircuitSnapshot(
                route=current.route,
                state=CircuitState.HALF_OPEN,
                failure_count=current.failure_count,
                updated_at=now.isoformat(),
                next_probe_at=current.next_probe_at,
                probe_owner=worker_id,
                last_evidence_sha256=current.last_evidence_sha256,
            )
            _write_json(self.circuit_path, claimed.to_dict())
            return claimed

    def finish_model_probe(
        self, *, worker_id: str, succeeded: bool, evidence_sha256: str
    ) -> CircuitSnapshot:
        """关闭线路或重新打开一个完整冷却窗口。"""
        if not _full_sha256(evidence_sha256):
            raise ContractError("probe evidence digest is invalid")
        with self._locked():
            current = self._circuit()
            if (
                current.state is not CircuitState.HALF_OPEN
                or current.probe_owner != worker_id
            ):
                raise ContractError("model probe owner does not hold the circuit")
            now = self._now()
            next_snapshot = (
                CircuitSnapshot(
                    route=current.route,
                    state=CircuitState.CLOSED,
                    failure_count=0,
                    updated_at=now.isoformat(),
                    last_evidence_sha256=evidence_sha256,
                )
                if succeeded
                else CircuitSnapshot(
                    route=current.route,
                    state=CircuitState.OPEN,
                    failure_count=current.failure_count + 1,
                    updated_at=now.isoformat(),
                    next_probe_at=(
                        now + timedelta(seconds=MODEL_CIRCUIT_COOLDOWN_SEC)
                    ).isoformat(),
                    last_evidence_sha256=evidence_sha256,
                )
            )
            _write_json(self.circuit_path, next_snapshot.to_dict())
            return next_snapshot

    def _open_circuit(
        self, now: datetime, failure_count: int, evidence_sha256: str
    ) -> None:
        snapshot = CircuitSnapshot(
            route=MODEL_TRANSPORT_ROUTE,
            state=CircuitState.OPEN,
            failure_count=failure_count,
            updated_at=now.isoformat(),
            next_probe_at=(
                now + timedelta(seconds=MODEL_CIRCUIT_COOLDOWN_SEC)
            ).isoformat(),
            last_evidence_sha256=evidence_sha256,
        )
        _write_json(self.circuit_path, snapshot.to_dict())

    def _records(self) -> dict[str, dict[str, Any]]:
        if not self.state_path.exists():
            return {}
        value = _read_json(self.state_path)
        if value.get("schema_version") != 1 or not isinstance(
            value.get("records"), dict
        ):
            raise ContractError("recovery ledger is invalid")
        return dict(value["records"])

    def _circuit(self) -> CircuitSnapshot:
        if not self.circuit_path.exists():
            return CircuitSnapshot(
                route=MODEL_TRANSPORT_ROUTE,
                state=CircuitState.CLOSED,
                failure_count=0,
                updated_at=self._now().isoformat(),
            )
        value = _read_json(self.circuit_path)
        snapshot = CircuitSnapshot(
            **value | {"state": CircuitState(value["state"])}
        )
        snapshot.validate()
        return snapshot

    def _now(self) -> datetime:
        now = self.clock().astimezone(UTC)
        return now

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        with self.lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield


def _validate_failure_identity(
    question_id: str,
    revision: str,
    scientific_round: int,
    failure_stage: str,
    evidence_sha256: str,
) -> None:
    if (
        not question_id.strip()
        or not revision.strip()
        or scientific_round < 1
        or not failure_stage.strip()
        or not _full_sha256(evidence_sha256)
    ):
        raise ContractError("runtime failure identity is invalid")


def _failure_identity(
    question_id: str, revision: str, scientific_round: int, failure_stage: str
) -> str:
    return "|".join((question_id, revision, str(scientific_round), failure_stage))


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ContractError(f"{label} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _full_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"recovery state is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise ContractError("recovery state root must be an object")
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

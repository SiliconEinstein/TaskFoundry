"""Researcher 解题期间可调用的 Labwright 增量环境模块。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import fcntl
import json
from pathlib import Path
from typing import Any

from .labwright import EnvironmentDeltaRequest, RuntimeSnapshot, atomic_json, sha256_file
from .model import Actor, ContractError


class DeltaState(StrEnum):
    """一次运行时环境增量的生命周期。"""

    PENDING = "PENDING_LABWRIGHT"
    APPLYING = "APPLYING"
    READY = "READY"
    FAILED = "FAILED"


@dataclass(frozen=True)
class DeltaClaim:
    """Labwright 对一项增量工作的排他认领。"""

    request_id: str
    worker_id: str
    fencing_token: str
    claimed_at: str
    request_sha256: str


@dataclass(frozen=True)
class DeltaReceipt:
    """可供同一 Researcher 沙盒继续计算的增量回执。"""

    request_id: str
    researcher_request_id: str
    sandbox_id: str
    state: DeltaState
    capability_name: str
    capability_version: str
    evidence_path: str
    evidence_sha256: str
    scientific_clock_paused_at: str
    scientific_clock_resumed_at: str
    pause_duration_sec: float
    fencing_token: str
    schema_version: int = 1

    def validate(self) -> None:
        """检查运行身份、证据字节和暂停计时。"""
        if self.state is not DeltaState.READY:
            raise ContractError("only READY delta receipts may resume Researcher execution")
        if not all((self.request_id, self.researcher_request_id, self.sandbox_id, self.capability_name)):
            raise ContractError("delta receipt identity is incomplete")
        evidence = Path(self.evidence_path)
        if not evidence.is_file() or sha256_file(evidence) != self.evidence_sha256:
            raise ContractError("delta evidence changed after completion")
        if self.pause_duration_sec < 0:
            raise ContractError("delta pause duration cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的表示。"""
        self.validate()
        return asdict(self) | {"state": self.state.value}


@dataclass(frozen=True)
class ImageSealPlan:
    """解题完成后把已验证增量固化进题目镜像的构建输入。"""

    run_id: str
    question_revision: str
    base_environment_key: str
    delta_receipts: tuple[dict[str, Any], ...]
    created_at: str
    schema_version: int = 1

    def validate(self) -> None:
        """确保构建计划可追溯且不接受空增量。"""
        if not all((self.run_id.strip(), self.question_revision.strip())):
            raise ContractError("image seal plan identity is incomplete")
        if len(self.base_environment_key) != 64:
            raise ContractError("image seal plan requires a Stable base environment key")
        if not self.delta_receipts:
            raise ContractError("image seal plan requires at least one completed delta")

    def to_dict(self) -> dict[str, Any]:
        """返回交给镜像构建适配器的确定性输入。"""
        self.validate()
        return asdict(self)


class FileLabwrightRuntimeService:
    """隐藏认领、围栏、证据绑定和恢复细节的文件服务适配器。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests = root / "requests"
        self.receipts = root / "delta-receipts"
        self.locks = root / "delta-locks"

    def claim(
        self,
        actor: Actor,
        request_id: str,
        *,
        worker_id: str,
        fencing_token: str,
    ) -> DeltaClaim:
        """由 Labwright 排他认领一个待处理增量。"""
        if actor is not Actor.LABWRIGHT:
            raise ContractError("only Labwright may claim environment deltas")
        if not all((worker_id.strip(), fencing_token.strip())):
            raise ContractError("worker and fencing token are required")
        with self._lock(request_id):
            path, value = self._request(request_id)
            state = value.get("status")
            if state == DeltaState.APPLYING.value:
                if value.get("fencing_token") != fencing_token:
                    raise ContractError("delta is claimed by another fence")
            elif state != DeltaState.PENDING.value:
                raise ContractError(f"delta cannot be claimed from {state}")
            now = datetime.now(UTC).isoformat()
            value.update(
                status=DeltaState.APPLYING.value,
                worker_id=worker_id,
                fencing_token=fencing_token,
                scientific_clock_paused_at=now,
            )
            atomic_json(path, value)
            return DeltaClaim(request_id, worker_id, fencing_token, now, sha256_file(path))

    def complete(
        self,
        actor: Actor,
        request_id: str,
        *,
        fencing_token: str,
        runtime: RuntimeSnapshot,
        capability_version: str,
        evidence_path: Path,
    ) -> DeltaReceipt:
        """完成增量并返回恢复同一沙盒所需的回执。"""
        if actor is not Actor.LABWRIGHT:
            raise ContractError("only Labwright may complete environment deltas")
        runtime.validate()
        if not capability_version.strip() or not evidence_path.is_file():
            raise ContractError("delta completion requires versioned evidence")
        with self._lock(request_id):
            path, value = self._request(request_id)
            if value.get("status") != DeltaState.APPLYING.value:
                raise ContractError("delta is not being applied")
            if value.get("fencing_token") != fencing_token:
                raise ContractError("delta completion fence does not match")
            request = self._hydrate_request(value)
            if runtime.sandbox_id != request.sandbox_id:
                raise ContractError("Labwright runtime targets another Researcher sandbox")
            resumed = datetime.now(UTC)
            paused = datetime.fromisoformat(value["scientific_clock_paused_at"])
            receipt = DeltaReceipt(
                request_id=request.request_id,
                researcher_request_id=str(request.researcher_request_id),
                sandbox_id=str(request.sandbox_id),
                state=DeltaState.READY,
                capability_name=request.name,
                capability_version=capability_version,
                evidence_path=str(evidence_path.resolve()),
                evidence_sha256=sha256_file(evidence_path),
                scientific_clock_paused_at=paused.isoformat(),
                scientific_clock_resumed_at=resumed.isoformat(),
                pause_duration_sec=(resumed - paused).total_seconds(),
                fencing_token=fencing_token,
            )
            receipt.validate()
            atomic_json(self.receipts / f"{request_id}.json", receipt.to_dict())
            value.update(status=DeltaState.READY.value, receipt_path=str((self.receipts / f"{request_id}.json").resolve()))
            atomic_json(path, value)
            return receipt

    def resolve(self, request_id: str) -> DeltaReceipt:
        """读取一份已经完成的增量回执。"""
        path = self.receipts / f"{request_id}.json"
        if not path.is_file():
            raise ContractError("Labwright delta receipt is not ready")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["state"] = DeltaState(value["state"])
        receipt = DeltaReceipt(**value)
        receipt.validate()
        return receipt

    def create_seal_plan(
        self,
        actor: Actor,
        *,
        run_id: str,
        question_revision: str,
        base_environment_key: str,
        request_ids: tuple[str, ...],
        output_path: Path,
    ) -> ImageSealPlan:
        """汇总完成的运行时增量，供构建并发布最终题目镜像。"""
        if actor is not Actor.LABWRIGHT:
            raise ContractError("only Labwright may create image seal plans")
        receipts = tuple(self.resolve(request_id).to_dict() for request_id in request_ids)
        if len(request_ids) != len(set(request_ids)):
            raise ContractError("image seal plan cannot repeat a delta")
        plan = ImageSealPlan(
            run_id=run_id,
            question_revision=question_revision,
            base_environment_key=base_environment_key,
            delta_receipts=receipts,
            created_at=datetime.now(UTC).isoformat(),
        )
        atomic_json(output_path, plan.to_dict())
        return plan

    def _request(self, request_id: str) -> tuple[Path, dict[str, Any]]:
        path = self.requests / f"{request_id}.json"
        if not path.is_file():
            raise ContractError("unknown Labwright delta request")
        value = json.loads(path.read_text(encoding="utf-8"))
        return path, value

    @staticmethod
    def _hydrate_request(value: dict[str, Any]) -> EnvironmentDeltaRequest:
        fields = EnvironmentDeltaRequest.__dataclass_fields__
        request = EnvironmentDeltaRequest(**{key: item for key, item in value.items() if key in fields})
        request.validate()
        return request

    def _lock(self, request_id: str):
        self.locks.mkdir(parents=True, exist_ok=True)
        return _RequestLock(self.locks / f"{request_id}.lock")


class _RequestLock:
    """单个增量请求的内部文件锁。"""

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

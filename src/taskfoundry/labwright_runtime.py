"""Researcher 解题期间可调用的 Labwright 增量环境模块。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any

from .labwright import ArtifactIdentity, EnvironmentDeltaRequest, RuntimeSnapshot, atomic_json, sha256_file
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
    """绑定失败来源、独立 builder runtime 与冻结科学字节的增量回执。"""

    request_id: str
    request_sha256: str
    run_id: str
    question_revision: str
    package_sha256: str
    source_researcher_request_id: str
    source_sandbox_id: str
    baseline_artifact: ArtifactIdentity
    baseline_identity_sha256: str
    builder_runtime_request_id: str
    builder_sandbox_id: str
    builder_artifact: ArtifactIdentity
    builder_identity_sha256: str
    state: DeltaState
    capability_name: str
    capability_version: str
    evidence_path: str
    evidence_sha256: str
    inventory_before_path: str
    inventory_before_sha256: str
    inventory_after_path: str
    inventory_after_sha256: str
    probe_evidence_path: str
    probe_evidence_sha256: str
    source_trace_path: str
    source_trace_sha256: str
    recovery_started_at: str | None
    recovery_finished_at: str | None
    recovery_duration_sec: float | None
    fencing_token: str
    schema_version: int = 2

    def validate(self) -> None:
        """检查来源、builder、不可变证据和可选恢复计时。"""
        if self.schema_version != 2 or self.state is not DeltaState.READY:
            raise ContractError("only READY schema-v2 delta receipts may close runtime recovery")
        identity = (
            self.request_id,
            self.run_id,
            self.question_revision,
            self.source_researcher_request_id,
            self.source_sandbox_id,
            self.builder_runtime_request_id,
            self.builder_sandbox_id,
            self.capability_name,
            self.capability_version,
            self.fencing_token,
        )
        if not all(value.strip() for value in identity):
            raise ContractError("delta receipt identity is incomplete")
        for label, value in (
            ("request SHA-256", self.request_sha256),
            ("package SHA-256", self.package_sha256),
            ("baseline identity", self.baseline_identity_sha256),
            ("builder identity", self.builder_identity_sha256),
        ):
            _require_sha256(value, label)
        self.baseline_artifact.validate()
        if self.baseline_artifact.digest is None:
            raise ContractError("runtime baseline artifact requires an immutable digest")
        if artifact_identity_sha256(self.baseline_artifact) != self.baseline_identity_sha256:
            raise ContractError("delta receipt baseline identity does not match its artifact")
        self.builder_artifact.validate()
        if self.builder_artifact.digest is None:
            raise ContractError("Labwright builder artifact requires an immutable digest")
        if artifact_identity_sha256(self.builder_artifact) != self.builder_identity_sha256:
            raise ContractError("delta receipt builder identity does not match its artifact")
        for label, path, digest in (
            ("delta", self.evidence_path, self.evidence_sha256),
            ("inventory_before", self.inventory_before_path, self.inventory_before_sha256),
            ("inventory_after", self.inventory_after_path, self.inventory_after_sha256),
            ("probe", self.probe_evidence_path, self.probe_evidence_sha256),
            ("source trace", self.source_trace_path, self.source_trace_sha256),
        ):
            _validate_file_reference(label, path, digest)
        timing = (self.recovery_started_at, self.recovery_finished_at, self.recovery_duration_sec)
        if any(value is not None for value in timing):
            if not all(value is not None for value in timing):
                raise ContractError("recovery timing must be wholly present or absent")
            started = _parse_timestamp(str(self.recovery_started_at), "recovery start")
            finished = _parse_timestamp(str(self.recovery_finished_at), "recovery finish")
            elapsed = (finished - started).total_seconds()
            duration = float(self.recovery_duration_sec)
            if elapsed < 0 or duration < 0 or abs(elapsed - duration) > 0.01:
                raise ContractError("recovery duration does not match its timestamps")

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的完整回执。"""
        self.validate()
        return asdict(self) | {
            "state": self.state.value,
            "baseline_artifact": asdict(self.baseline_artifact),
            "builder_artifact": asdict(self.builder_artifact),
        }


@dataclass(frozen=True)
class ImageSealPlan:
    """从真实科学 trace 与可选增量生成 Stable 镜像的闭合输入。"""

    run_id: str
    question_revision: str
    package_sha256: str
    baseline_artifact: ArtifactIdentity
    baseline_identity_sha256: str
    scientific_trace_path: str
    scientific_trace_sha256: str
    delta_receipts: tuple[dict[str, Any], ...]
    created_at: str
    schema_version: int = 2

    def validate(self) -> None:
        """拒绝脱离题包、基线或有效科学 trace 的构建计划。"""
        if self.schema_version != 2 or not all((self.run_id.strip(), self.question_revision.strip())):
            raise ContractError("image seal plan identity is incomplete")
        _require_sha256(self.package_sha256, "package SHA-256")
        _require_sha256(self.baseline_identity_sha256, "baseline identity")
        self.baseline_artifact.validate()
        if self.baseline_artifact.digest is None:
            raise ContractError("image seal plan baseline requires an immutable digest")
        if artifact_identity_sha256(self.baseline_artifact) != self.baseline_identity_sha256:
            raise ContractError("image seal plan baseline identity does not match its artifact")
        _validate_file_reference(
            "scientific trace", self.scientific_trace_path, self.scientific_trace_sha256
        )
        _, _, runtime_delta_request_ids = _validate_scientific_trace(
            Path(self.scientific_trace_path),
            run_id=self.run_id,
            question_revision=self.question_revision,
            package_sha256=self.package_sha256,
            baseline_identity_sha256=self.baseline_identity_sha256,
        )
        _parse_timestamp(self.created_at, "image seal plan creation")
        for value in self.delta_receipts:
            receipt = _hydrate_receipt(value)
            receipt.validate()
            if (
                receipt.run_id != self.run_id
                or receipt.question_revision != self.question_revision
                or receipt.package_sha256 != self.package_sha256
                or receipt.baseline_identity_sha256 != self.baseline_identity_sha256
            ):
                raise ContractError("delta receipt targets another runtime closure")
        if tuple(value["request_id"] for value in self.delta_receipts) != runtime_delta_request_ids:
            raise ContractError("runtime closure delta list does not match its scientific trace")

    def to_dict(self) -> dict[str, Any]:
        """返回交给镜像构建适配器的确定性输入。"""
        self.validate()
        return asdict(self) | {"baseline_artifact": asdict(self.baseline_artifact)}


class FileLabwrightRuntimeService:
    """隐藏认领、围栏、证据绑定和恢复细节的文件适配器。"""

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
        """由 Labwright 幂等、排他地认领一个待处理增量。"""
        if actor is not Actor.LABWRIGHT:
            raise ContractError("only Labwright may claim environment deltas")
        if not all((worker_id.strip(), fencing_token.strip())):
            raise ContractError("worker and fencing token are required")
        with self._lock(request_id):
            path, value = self._request(request_id)
            request = self._hydrate_request(value)
            request_sha256 = _request_contract_sha256(request)
            state = value.get("status")
            if state == DeltaState.APPLYING.value:
                if value.get("fencing_token") != fencing_token or value.get("worker_id") != worker_id:
                    raise ContractError("delta is claimed by another fence")
                return DeltaClaim(
                    request_id,
                    worker_id,
                    fencing_token,
                    str(value["claimed_at"]),
                    request_sha256,
                )
            if state != DeltaState.PENDING.value:
                raise ContractError(f"delta cannot be claimed from {state}")
            now = datetime.now(UTC).isoformat()
            value.update(
                status=DeltaState.APPLYING.value,
                worker_id=worker_id,
                fencing_token=fencing_token,
                claimed_at=now,
                recovery_started_at=now,
            )
            atomic_json(path, value)
            return DeltaClaim(request_id, worker_id, fencing_token, now, request_sha256)

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
        """在独立 builder runtime 完成增量并返回 fresh retry 所需回执。"""
        if actor is not Actor.LABWRIGHT:
            raise ContractError("only Labwright may complete environment deltas")
        runtime.validate()
        if runtime.status != "READY":
            raise ContractError("delta completion requires a READY runtime")
        if not capability_version.strip() or not evidence_path.is_file():
            raise ContractError("delta completion requires versioned evidence")
        with self._lock(request_id):
            path, value = self._request(request_id)
            if value.get("status") != DeltaState.APPLYING.value:
                raise ContractError("delta is not being applied")
            if value.get("fencing_token") != fencing_token:
                raise ContractError("delta completion fence does not match")
            request = self._hydrate_request(value)
            evidence = _strict_json_object(evidence_path)
            refs = self._validate_completion_evidence(
                request,
                runtime,
                capability_version,
                evidence,
            )
            finished = datetime.now(UTC)
            started = _parse_timestamp(value["recovery_started_at"], "recovery start")
            receipt = self._build_receipt(
                request=request,
                runtime=runtime,
                capability_version=capability_version,
                evidence_path=evidence_path,
                evidence=evidence,
                refs=refs,
                started=started,
                finished=finished,
                fencing_token=fencing_token,
            )
            receipt.validate()
            receipt_path = self.receipts / f"{request_id}.json"
            atomic_json(receipt_path, receipt.to_dict())
            value.update(status=DeltaState.READY.value, receipt_path=str(receipt_path.resolve()))
            atomic_json(path, value)
            return receipt

    @staticmethod
    def _build_receipt(
        *,
        request: EnvironmentDeltaRequest,
        runtime: RuntimeSnapshot,
        capability_version: str,
        evidence_path: Path,
        evidence: dict[str, Any],
        refs: dict[str, tuple[str, str]],
        started: datetime,
        finished: datetime,
        fencing_token: str,
    ) -> DeltaReceipt:
        """集中构造一份已绑定且可立即自校验的增量回执。"""
        return DeltaReceipt(
            request_id=request.request_id,
            request_sha256=_request_contract_sha256(request),
            run_id=request.run_id,
            question_revision=request.question_revision,
            package_sha256=evidence["package_sha256"],
            source_researcher_request_id=str(request.researcher_request_id),
            source_sandbox_id=str(request.sandbox_id),
            baseline_artifact=ArtifactIdentity(**evidence["baseline_artifact"]),
            baseline_identity_sha256=evidence["baseline_identity_sha256"],
            builder_runtime_request_id=runtime.request_id,
            builder_sandbox_id=runtime.sandbox_id,
            builder_artifact=runtime.artifact,
            builder_identity_sha256=artifact_identity_sha256(runtime.artifact),
            state=DeltaState.READY,
            capability_name=request.name,
            capability_version=capability_version,
            evidence_path=str(evidence_path.resolve()),
            evidence_sha256=sha256_file(evidence_path),
            inventory_before_path=refs["inventory_before"][0],
            inventory_before_sha256=refs["inventory_before"][1],
            inventory_after_path=refs["inventory_after"][0],
            inventory_after_sha256=refs["inventory_after"][1],
            probe_evidence_path=refs["probes"][0],
            probe_evidence_sha256=refs["probes"][1],
            source_trace_path=refs["source_trace"][0],
            source_trace_sha256=refs["source_trace"][1],
            recovery_started_at=started.isoformat(),
            recovery_finished_at=finished.isoformat(),
            recovery_duration_sec=(finished - started).total_seconds(),
            fencing_token=fencing_token,
        )

    def resolve(self, request_id: str) -> DeltaReceipt:
        """读取一份已经完成的增量回执。"""
        path = self.receipts / f"{request_id}.json"
        if not path.is_file():
            raise ContractError("Labwright delta receipt is not ready")
        receipt = _hydrate_receipt(_strict_json_object(path))
        receipt.validate()
        return receipt

    def create_seal_plan(
        self,
        actor: Actor,
        *,
        run_id: str,
        question_revision: str,
        package_sha256: str,
        baseline_artifact: ArtifactIdentity,
        scientific_trace_path: Path,
        request_ids: tuple[str, ...],
        output_path: Path,
    ) -> ImageSealPlan:
        """把首个科学 trace 与零个或多个增量闭合为 Stable 构建计划。"""
        if actor is not Actor.LABWRIGHT:
            raise ContractError("only Labwright may create image seal plans")
        if len(request_ids) != len(set(request_ids)):
            raise ContractError("image seal plan cannot repeat a delta")
        baseline_sha256 = artifact_identity_sha256(baseline_artifact)
        _, _, runtime_delta_request_ids = _validate_scientific_trace(
            scientific_trace_path,
            run_id=run_id,
            question_revision=question_revision,
            package_sha256=package_sha256,
            baseline_identity_sha256=baseline_sha256,
        )
        receipts = tuple(self.resolve(item) for item in request_ids)
        if tuple(receipt.request_id for receipt in receipts) != runtime_delta_request_ids:
            raise ContractError("runtime closure delta list does not match its scientific trace")
        plan = ImageSealPlan(
            run_id=run_id,
            question_revision=question_revision,
            package_sha256=package_sha256,
            baseline_artifact=baseline_artifact,
            baseline_identity_sha256=baseline_sha256,
            scientific_trace_path=str(scientific_trace_path.resolve()),
            scientific_trace_sha256=sha256_file(scientific_trace_path),
            delta_receipts=tuple(receipt.to_dict() for receipt in receipts),
            created_at=datetime.now(UTC).isoformat(),
        )
        plan.validate()
        atomic_json(output_path, plan.to_dict())
        return plan

    @staticmethod
    def _validate_completion_evidence(
        request: EnvironmentDeltaRequest,
        runtime: RuntimeSnapshot,
        capability_version: str,
        evidence: dict[str, Any],
    ) -> dict[str, tuple[str, str]]:
        required = {
            "schema_version",
            "run_id",
            "question_revision",
            "package_sha256",
            "researcher_request_id",
            "sandbox_id",
            "request_sha256",
            "baseline_artifact",
            "baseline_identity_sha256",
            "builder_runtime_request_id",
            "builder_sandbox_id",
            "builder_identity_sha256",
            "capability_name",
            "capability_version",
            "source_trace",
            "inventory_before",
            "inventory_after",
            "probes",
        }
        if set(evidence) != required or evidence.get("schema_version") != 2:
            raise ContractError("delta evidence schema is incomplete or ambiguous")
        if evidence["run_id"] != request.run_id or evidence["question_revision"] != request.question_revision:
            raise ContractError("delta evidence targets another run or revision")
        _require_sha256(evidence["package_sha256"], "package SHA-256")
        if (
            evidence["researcher_request_id"] != request.researcher_request_id
            or evidence["sandbox_id"] != request.sandbox_id
        ):
            raise ContractError("delta evidence targets another Researcher runtime")
        if evidence["request_sha256"] != _request_contract_sha256(request):
            raise ContractError("delta evidence request SHA-256 does not match")
        try:
            baseline_artifact = ArtifactIdentity(**evidence["baseline_artifact"])
        except (TypeError, KeyError) as error:
            raise ContractError("delta evidence baseline artifact is invalid") from error
        if evidence["baseline_identity_sha256"] != artifact_identity_sha256(baseline_artifact):
            raise ContractError("delta evidence baseline identity does not match")
        if (
            evidence["builder_runtime_request_id"] != runtime.request_id
            or evidence["builder_sandbox_id"] != runtime.sandbox_id
        ):
            raise ContractError("delta evidence targets another Labwright builder runtime")
        if evidence["builder_identity_sha256"] != artifact_identity_sha256(runtime.artifact):
            raise ContractError("delta evidence builder identity does not match")
        if evidence["capability_name"] != request.name:
            raise ContractError("delta evidence capability name does not match")
        if evidence["capability_version"] != capability_version:
            raise ContractError("delta evidence capability version does not match")
        refs = {
            label: _evidence_reference(evidence[label], label)
            for label in ("inventory_before", "inventory_after", "probes", "source_trace")
        }
        _validate_source_failure_trace(
            Path(refs["source_trace"][0]),
            request=request,
            package_sha256=evidence["package_sha256"],
            baseline_identity_sha256=evidence["baseline_identity_sha256"],
        )
        return refs

    def _request(self, request_id: str) -> tuple[Path, dict[str, Any]]:
        path = self.requests / f"{request_id}.json"
        if not path.is_file():
            raise ContractError("unknown Labwright delta request")
        return path, _strict_json_object(path)

    @staticmethod
    def _hydrate_request(value: dict[str, Any]) -> EnvironmentDeltaRequest:
        fields = EnvironmentDeltaRequest.__dataclass_fields__
        request = EnvironmentDeltaRequest(**{key: item for key, item in value.items() if key in fields})
        request.validate()
        return request

    def _lock(self, request_id: str):
        self.locks.mkdir(parents=True, exist_ok=True)
        return _RequestLock(self.locks / f"{request_id}.lock")


def artifact_identity_sha256(artifact: ArtifactIdentity) -> str:
    """返回完整 baseline artifact 身份的规范 SHA-256。"""
    artifact.validate()
    material = json.dumps(asdict(artifact), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(material).hexdigest()


def _request_contract_sha256(request: EnvironmentDeltaRequest) -> str:
    material = json.dumps(asdict(request), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(material).hexdigest()


def _strict_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"JSON evidence is missing: {path}")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """拒绝重复键并返回本层对象。"""
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ContractError(f"duplicate JSON evidence key: {key}")
            value[key] = item
        return value

    def reject_nonfinite(token: str) -> None:
        """拒绝 JSON 的 NaN 与无穷常量。"""
        raise ContractError(f"non-finite JSON evidence number: {token}")

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=object_pairs,
        parse_constant=reject_nonfinite,
    )
    if not isinstance(value, dict):
        raise ContractError("JSON evidence must be an object")
    return value


def _evidence_reference(value: Any, label: str) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ContractError(f"{label} evidence reference is invalid")
    path, digest = value.get("path"), value.get("sha256")
    if not isinstance(path, str) or not isinstance(digest, str):
        raise ContractError(f"{label} evidence reference is invalid")
    _validate_file_reference(label, path, digest)
    return str(Path(path).resolve()), digest


def _validate_file_reference(label: str, path_value: str, digest: str) -> None:
    _require_sha256(digest, f"{label} SHA-256")
    path = Path(path_value)
    if not path.is_file() or sha256_file(path) != digest:
        raise ContractError(f"{label} evidence changed after completion")


def _require_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ContractError(f"{label} must be a full SHA-256")


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ContractError(f"invalid {label} timestamp") from error
    if timestamp.tzinfo is None:
        raise ContractError(f"invalid {label} timestamp")
    return timestamp


def _hydrate_receipt(value: dict[str, Any]) -> DeltaReceipt:
    converted = dict(value)
    converted["state"] = DeltaState(converted["state"])
    converted["baseline_artifact"] = ArtifactIdentity(**converted["baseline_artifact"])
    converted["builder_artifact"] = ArtifactIdentity(**converted["builder_artifact"])
    return DeltaReceipt(**converted)


def _validate_scientific_trace(
    path: Path,
    *,
    run_id: str,
    question_revision: str,
    package_sha256: str,
    baseline_identity_sha256: str,
) -> tuple[str, str, tuple[str, ...]]:
    trace = _strict_json_object(path)
    expected = {
        "schema_version": 1,
        "classification": "SCIENTIFIC_RESULT",
        "run_id": run_id,
        "question_revision": question_revision,
        "package_sha256": package_sha256,
        "baseline_identity_sha256": baseline_identity_sha256,
    }
    if any(trace.get(key) != value for key, value in expected.items()):
        raise ContractError("scientific trace does not bind the runtime closure")
    researcher_request_id = trace.get("researcher_request_id")
    sandbox_id = trace.get("sandbox_id")
    if not isinstance(researcher_request_id, str) or not researcher_request_id.strip():
        raise ContractError("scientific trace lacks Researcher execution identity")
    if not isinstance(sandbox_id, str) or not sandbox_id.strip():
        raise ContractError("scientific trace lacks Researcher execution identity")
    delta_ids = trace.get("runtime_delta_request_ids")
    if (
        not isinstance(delta_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in delta_ids)
        or len(delta_ids) != len(set(delta_ids))
    ):
        raise ContractError("scientific trace has an invalid runtime delta list")
    return researcher_request_id, sandbox_id, tuple(delta_ids)


def _validate_source_failure_trace(
    path: Path,
    *,
    request: EnvironmentDeltaRequest,
    package_sha256: str,
    baseline_identity_sha256: str,
) -> None:
    trace = _strict_json_object(path)
    expected = {
        "schema_version": 1,
        "run_id": request.run_id,
        "question_revision": request.question_revision,
        "package_sha256": package_sha256,
        "researcher_request_id": request.researcher_request_id,
        "sandbox_id": request.sandbox_id,
        "baseline_identity_sha256": baseline_identity_sha256,
    }
    if trace.get("classification") not in {
        "ENVIRONMENT_FAILURE",
        "HARNESS_FAILURE",
        "PLATFORM_FAILURE",
    } or any(trace.get(key) != value for key, value in expected.items()):
        raise ContractError("source failure trace does not bind the runtime recovery")


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

"""可复用科学环境的 Labwright 模块接口。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlparse

from .model import Actor, ContractError


_CAPABILITY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_REQUIRED_CLEAN_EXCLUSIONS = frozenset(
    {
        "exclusion:app-allowlist",
        "exclusion:task-privileged-material",
        "exclusion:harness-executables",
        "exclusion:credential-material",
        "exclusion:researcher-residue",
    }
)
DEFAULT_CPU_BASE_IMAGE = (
    "dp-harbor-registry.cn-zhangjiakou.cr.aliyuncs.com/"
    "public/paper2arm-env:v1.0-20260708"
)
DEFAULT_CPU_BASE_DIGEST = "sha256:e56af8bcfc37be6ee859a7867ad2a3a941f9b576445a56378c6a4a5a017b57d2"


class LabwrightError(RuntimeError):
    """环境证据无法满足模块契约时抛出。"""


@dataclass(frozen=True)
class BaseImagePolicy:
    """校验根 EnvironmentSpec 能表达的基础镜像边界。"""

    image: str = DEFAULT_CPU_BASE_IMAGE
    digest: str = DEFAULT_CPU_BASE_DIGEST
    platform: str = "linux/amd64"

    def validate_spec(self, spec: dict[str, Any]) -> None:
        """拒绝可变或与当前运行 ABI 不兼容的基础镜像声明。"""
        image = str(spec.get("base_image", "")).strip()
        if not image:
            raise ContractError("base_image must be non-empty")
        if image.endswith(":latest"):
            raise ContractError("latest base images are not allowed")
        if spec.get("platform") != self.platform:
            raise ContractError("question environments require linux/amd64")


@dataclass(frozen=True)
class ArtifactIdentity:
    """运行时与镜像生命周期共享的规范产物身份。"""

    provider: str
    endpoint_identity: str
    project_id: str
    record_id: str
    image_url: str
    digest: str | None = None

    def validate(self) -> None:
        """拒绝可变、不完整或有歧义的提供方身份。"""
        if not all((self.provider, self.endpoint_identity, self.project_id, self.record_id, self.image_url)):
            raise ContractError("artifact identity fields must be non-empty")
        if self.image_url.endswith(":latest"):
            raise ContractError("latest image references are not immutable")
        parsed = urlparse(self.endpoint_identity)
        if parsed.scheme not in {"https", "lbg"}:
            raise ContractError("endpoint identity must use https or lbg scheme")
        if self.digest is not None and not re_full_sha256(self.digest):
            raise ContractError("artifact digest must be sha256:<64 hex>")


@dataclass(frozen=True)
class EnvironmentReceipt:
    """返回给编排器的 Stable 环境回执。"""

    environment_key: str
    lifecycle: str
    artifact: ArtifactIdentity
    workdir: str
    manifest_path: str
    manifest_sha256: str
    resource_digests: tuple[str, ...]
    runtime_closure_path: str | None = None
    runtime_closure_sha256: str | None = None
    schema_version: int = 1

    def validate(self) -> None:
        """执行 Stable 环境的最小发布门。"""
        self.artifact.validate()
        if self.lifecycle != "STABLE":
            raise ContractError("only Stable environments may be resolved")
        if not self.workdir.startswith("/"):
            raise ContractError("environment workdir must be absolute")
        if not re_full_hex(self.environment_key) or not re_full_hex(self.manifest_sha256):
            raise ContractError("environment and manifest keys must be SHA-256")
        closure = (self.runtime_closure_path, self.runtime_closure_sha256)
        if self.schema_version == 1 and any(value is not None for value in closure):
            raise ContractError("legacy environment receipts cannot bind runtime closure")
        if self.schema_version == 2:
            if not all(isinstance(value, str) and value for value in closure):
                raise ContractError("runtime-first Stable receipt requires runtime closure")
            assert self.runtime_closure_path is not None
            assert self.runtime_closure_sha256 is not None
            if not re_full_hex(self.runtime_closure_sha256):
                raise ContractError("runtime closure key must be SHA-256")
            closure_path = Path(self.runtime_closure_path)
            if not closure_path.is_file() or sha256_file(closure_path) != self.runtime_closure_sha256:
                raise ContractError("runtime closure changed after Stable import")
        elif self.schema_version != 1:
            raise ContractError("unsupported environment receipt schema")

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的回执。"""
        self.validate()
        return asdict(self)


@dataclass(frozen=True)
class RuntimeSnapshot:
    """与镜像发布逻辑分离的临时 Labwright 运行时。"""

    request_id: str
    sandbox_id: str
    artifact: ArtifactIdentity
    started_at: str
    status: str

    def validate(self) -> None:
        """只接受身份明确的模块运行时。"""
        self.artifact.validate()
        if self.status not in {"PROVISIONING", "READY", "CLOSED", "FAILED"}:
            raise ContractError("invalid runtime snapshot status")
        if not all((self.request_id, self.sandbox_id, self.started_at)):
            raise ContractError("runtime snapshot identity is incomplete")


@dataclass(frozen=True)
class EnvironmentDeltaRequest:
    """Researcher 对额外公开能力的类型化请求。"""

    request_id: str
    run_id: str
    question_revision: str
    kind: str
    name: str
    version_constraint: str
    reason: str
    researcher_request_id: str | None = None
    sandbox_id: str | None = None
    source_uri: str | None = None
    source_sha256: str | None = None
    schema_version: int = 2

    def validate(self) -> None:
        """拒绝任意 shell、宿主机本地路径和未固定资源。"""
        if self.kind not in {"package", "tool", "data", "model", "document"}:
            raise ContractError("unsupported environment delta kind")
        if not all((self.request_id, self.run_id, self.question_revision, self.name, self.reason)):
            raise ContractError("environment delta fields must be non-empty")
        if not _CAPABILITY_NAME.fullmatch(self.name):
            raise ContractError("delta name must be a capability, not a command or path")
        if self.source_uri and urlparse(self.source_uri).scheme != "https":
            raise ContractError("external resources must use HTTPS")
        if self.source_uri and not self.source_sha256:
            raise ContractError("external resources require a SHA-256")
        if self.schema_version == 2 and not all((self.researcher_request_id, self.sandbox_id)):
            raise ContractError("runtime delta must bind a Researcher request and sandbox")
        if self.schema_version not in {1, 2}:
            raise ContractError("unsupported environment delta schema")


class FileLabwrightRegistry:
    """导入外部 Stable 清单的可审计热缓存适配器。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.environments = root / "environments"
        self.requests = root / "requests"

    def import_stable(
        self,
        manifest_path: Path,
        *,
        clean_evidence_path: Path,
        fencing_token: str,
        endpoint_identity: str,
        project_id: str,
        workdir: str = "/app",
    ) -> EnvironmentReceipt:
        """使用干净沙盒证据把 Candidate 经 VERIFYING 发布为 Stable。"""
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        image = manifest.get("image", {})
        if manifest.get("status") != "ENVIRONMENT_READY" or not image.get("immutable"):
            raise LabwrightError("manifest is not an immutable ready environment")
        manifest_digest = sha256_file(manifest_path)
        artifact = ArtifactIdentity(
            provider=str(image.get("provider", "")),
            endpoint_identity=endpoint_identity,
            project_id=str(project_id),
            record_id=str(image.get("record_id", "")),
            image_url=str(image.get("url", "")),
            digest=image.get("digest"),
        )
        artifact.validate()
        key = self._environment_key(artifact, manifest_digest)
        self._verify_clean_evidence(clean_evidence_path, artifact)
        self._promote(key, manifest_digest, clean_evidence_path, fencing_token)
        closure_path, closure_sha256 = self._runtime_closure(manifest)
        receipt = EnvironmentReceipt(
            environment_key=key,
            lifecycle="STABLE",
            artifact=artifact,
            workdir=workdir,
            manifest_path=str(manifest_path.resolve()),
            manifest_sha256=manifest_digest,
            resource_digests=tuple(sorted(item["sha256"] for item in manifest.get("resources", []))),
            runtime_closure_path=closure_path,
            runtime_closure_sha256=closure_sha256,
            schema_version=2 if closure_path is not None else 1,
        )
        receipt.validate()
        target = self.environments / f"{receipt.environment_key}.json"
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            comparable = json.loads(json.dumps(receipt.to_dict()))
            if existing != comparable:
                raise LabwrightError("environment key collision")
            return receipt
        atomic_json(target, receipt.to_dict())
        return receipt

    @staticmethod
    def _environment_key(artifact: ArtifactIdentity, manifest_digest: str) -> str:
        material = json.dumps(
            asdict(artifact) | {"manifest_sha256": manifest_digest},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(material).hexdigest()

    def _promote(self, key: str, manifest_digest: str, evidence: Path, fence: str) -> None:
        """发布幂等且带围栏的生命周期转换。"""
        if not fence.strip():
            raise LabwrightError("image promotion requires a fencing token")
        path = self.root / "lifecycles" / f"{key}.json"
        lifecycle = {
            "schema_version": 1,
            "environment_key": key,
            "state": "STABLE",
            "fencing_token": fence,
            "manifest_sha256": manifest_digest,
            "clean_evidence_sha256": sha256_file(evidence),
        }
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != lifecycle:
            raise LabwrightError("environment promotion conflicts with an existing fence")
        if not path.exists():
            for state in ("CANDIDATE", "VERIFYING", "STABLE"):
                atomic_json(path, lifecycle | {"state": state})

    @staticmethod
    def _verify_clean_evidence(path: Path, artifact: ArtifactIdentity) -> None:
        """要求不同干净沙盒中的正向探针和 harness-neutral 负向证明。"""
        if not path.is_file():
            raise LabwrightError("clean sandbox evidence is missing")
        evidence = json.loads(path.read_text(encoding="utf-8"))
        runs = evidence.get("runs", [])
        sandbox_ids = {run.get("sandbox_id") for run in runs}
        if evidence.get("clean_sandboxes_verified", 0) < 2 or len(runs) < 2 or len(sandbox_ids) != len(runs):
            raise LabwrightError("two distinct clean sandboxes are required")
        if evidence.get("image_url") != artifact.image_url or str(evidence.get("image_record_id")) != artifact.record_id:
            raise LabwrightError("clean evidence targets a different image")
        if evidence.get("install_commands_executed") or evidence.get("public_asset_uploads_executed"):
            raise LabwrightError("clean verification mutated the image environment")
        checks = [check for run in runs for group in ("validation", "exclusions") for check in run.get(group, [])]
        if not checks or any(not check.get("passed") or check.get("exit_code") != 0 for check in checks):
            raise LabwrightError("clean sandbox verification contains a failed probe")
        for run in runs:
            exclusions = {check.get("id"): check for check in run.get("exclusions", [])}
            if not _REQUIRED_CLEAN_EXCLUSIONS <= exclusions.keys():
                raise LabwrightError("clean sandbox is missing required harness-neutral negative scans")
            if any(exclusions[check_id].get("observed_count") != 0 for check_id in _REQUIRED_CLEAN_EXCLUSIONS):
                raise LabwrightError("clean sandbox negative scans found forbidden material")

    def resolve(self, environment_key: str) -> EnvironmentReceipt:
        """只解析本地登记的 Stable 身份。"""
        path = self.environments / f"{environment_key}.json"
        if not path.is_file():
            raise LabwrightError("Stable environment is not registered")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["artifact"] = ArtifactIdentity(**value["artifact"])
        value["resource_digests"] = tuple(value["resource_digests"])
        receipt = EnvironmentReceipt(**value)
        receipt.validate()
        if sha256_file(Path(receipt.manifest_path)) != receipt.manifest_sha256:
            raise LabwrightError("source manifest changed after registration")
        return receipt

    @staticmethod
    def _runtime_closure(manifest: dict[str, Any]) -> tuple[str | None, str | None]:
        """从构建清单读取可选的 runtime-first closure 绑定。"""
        value = manifest.get("runtime_closure")
        if value is None:
            return None, None
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            raise LabwrightError("runtime closure reference is invalid")
        path_value, digest = value.get("path"), value.get("sha256")
        if not isinstance(path_value, str) or not isinstance(digest, str) or not re_full_hex(digest):
            raise LabwrightError("runtime closure reference is invalid")
        path = Path(path_value)
        if not path.is_file() or sha256_file(path) != digest:
            raise LabwrightError("runtime closure changed before Stable import")
        return str(path.resolve()), digest

    def request_delta(self, actor: Actor, request: EnvironmentDeltaRequest) -> Path:
        """持久化交给 Labwright 处理的类型化增量请求。"""
        if actor is not Actor.RESEARCHER:
            raise LabwrightError("only Researcher may request a runtime environment delta")
        request.validate()
        value = asdict(request) | {"status": "PENDING_LABWRIGHT"}
        path = self.requests / f"{request.request_id}.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
            raise LabwrightError("request_id collision")
        if not path.exists():
            atomic_json(path, value)
        return path


def re_full_hex(value: str) -> bool:
    """返回字符串是否为完整小写 SHA-256 十六进制值。"""
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def re_full_sha256(value: str) -> bool:
    """返回字符串是否为带 sha256 前缀的摘要。"""
    return value.startswith("sha256:") and re_full_hex(value.removeprefix("sha256:"))


def sha256_file(path: Path) -> str:
    """计算一个普通文件的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    """原子写入 JSON，并在发布前同步文件字节。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

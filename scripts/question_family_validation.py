#!/usr/bin/env python3
"""暂存题族与已发布题族共用的语义校验器。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any


REPO_ROOT = Path("/personal/TaskFoundry")
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from taskfoundry.labwright import ArtifactIdentity, EnvironmentReceipt  # noqa: E402
from taskfoundry.labwright_runtime import ImageSealPlan  # noqa: E402
from taskfoundry.model import ContractError  # noqa: E402
from taskfoundry.researcher import ResearcherRequest  # noqa: E402


WORKBENCH_ROOT = REPO_ROOT / "workbench"
QUESTION_ROOT = Path("/personal/codex-workspace/question-from-questions")
EVIDENCE_ROOT = REPO_ROOT
LEVELS = ("high", "medium", "low")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TASKFOUNDRY_PYTHON = Path("/opt/mamba/bin/python")
SCIENTIFIC_CONTRACT = "SCIENTIFIC_CONTRACT.json"
REQUIRED_CLEAN_EXCLUSIONS = frozenset(
    {
        "exclusion:app-allowlist",
        "exclusion:task-privileged-material",
        "exclusion:harness-executables",
        "exclusion:credential-material",
        "exclusion:researcher-residue",
    }
)


class FamilyValidationError(RuntimeError):
    """题族、题包或绑定 evidence 违反发布合同。"""


def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """解析 JSON 对象并拒绝重复键。"""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FamilyValidationError(f"JSON 键重复：{key}")
        value[key] = item
    return value


def reject_nonfinite(token: str) -> None:
    """拒绝非标准 NaN 与无穷大 JSON 常量。"""
    raise FamilyValidationError(f"JSON 数值不是有限值：{token}")


def file_sha256(path: Path) -> str:
    """流式计算常规文件的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def taskfoundry_directory_sha256(root: Path) -> str:
    """按 TaskFoundry 历史题包算法计算目录身份，但不执行当前 lint。"""
    if not root.is_dir():
        raise FamilyValidationError(f"题包目录不存在：{root}")
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(f"{path.stat().st_mode & 0o777:o}".encode() + b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """把 evidence 编码为唯一允许的 canonical JSON 字节。"""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def require_plain_tree(root: Path) -> None:
    """拒绝可发布题族树中的软链接与特殊节点。"""
    for path in (root, *root.rglob("*")):
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise FamilyValidationError(f"发布树包含软链接或特殊节点：{path}")


def lint_package(package: Path) -> dict[str, Any]:
    """运行权威 TaskFoundry 题包 lint。"""
    if not TASKFOUNDRY_PYTHON.is_file():
        raise FamilyValidationError(f"缺少 TaskFoundry Python：{TASKFOUNDRY_PYTHON}")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [str(TASKFOUNDRY_PYTHON), "-m", "taskfoundry.cli", "lint-package", str(package)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise FamilyValidationError(
            f"TaskFoundry lint 执行失败：{package}；stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise FamilyValidationError(f"TaskFoundry lint 返回非法 JSON：{error}") from error
    if report.get("passed") is not True:
        raise FamilyValidationError(f"TaskFoundry lint 未通过：{report}")
    return report


def _load_strict_json(path: Path, label: str, *, canonical: bool) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        data = json.loads(raw, object_pairs_hook=strict_object, parse_constant=reject_nonfinite)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, FamilyValidationError) as error:
        raise FamilyValidationError(f"{label}：非法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise FamilyValidationError(f"{label}：JSON 根必须是对象")
    if canonical and raw != canonical_json_bytes(data):
        raise FamilyValidationError(f"{label}：evidence 不是 canonical JSON")
    return data


def _require_exact_fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise FamilyValidationError(f"{label}：字段不符合发布合同")
    return value


def _score(value: Any, label: str, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FamilyValidationError(f"{label}：分数必须是数值")
    score = float(value)
    if not minimum <= score <= maximum:
        raise FamilyValidationError(f"{label}：分数超出 [{minimum}, {maximum}] 范围")
    return score


def _package_file(package: Path, value: Any, label: str) -> dict[str, str]:
    """复算一个题包内部科学合同文件的字节身份。"""
    reference = _require_exact_fields(value, {"path", "sha256"}, label)
    relative, expected = reference["path"], reference["sha256"]
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise FamilyValidationError(f"{label}：路径必须是题包内相对路径")
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise FamilyValidationError(f"{label}：SHA-256 非法")
    path = package / relative
    try:
        path.resolve().relative_to(package.resolve())
    except (OSError, ValueError) as error:
        raise FamilyValidationError(f"{label}：路径越出题包") from error
    if not path.is_file() or path.is_symlink() or file_sha256(path) != expected:
        raise FamilyValidationError(f"{label}：科学合同文件字节不匹配")
    return {"path": Path(relative).as_posix(), "sha256": file_sha256(path)}


def scientific_contract_sha256(package: Path) -> str:
    """从实际 GT、grader、容差、权重和输出合同复算科学合同。"""
    path = package / SCIENTIFIC_CONTRACT
    contract = _load_strict_json(path, SCIENTIFIC_CONTRACT, canonical=True)
    fields = {
        "schema_version",
        "ground_truth",
        "grader",
        "tolerance",
        "weights",
        "output_contract",
    }
    _require_exact_fields(contract, fields, SCIENTIFIC_CONTRACT)
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise FamilyValidationError(f"{SCIENTIFIC_CONTRACT}：schema_version 必须为 1")
    normalized: dict[str, Any] = {"schema_version": 1}
    for field in ("ground_truth", "grader", "output_contract"):
        values = contract[field]
        if not isinstance(values, list) or not values:
            raise FamilyValidationError(f"{SCIENTIFIC_CONTRACT}.{field}：至少绑定一个文件")
        normalized[field] = [
            _package_file(package, item, f"{SCIENTIFIC_CONTRACT}.{field}[{index}]")
            for index, item in enumerate(values)
        ]
    for field in ("tolerance", "weights"):
        value = contract[field]
        if not isinstance(value, dict) or not value:
            raise FamilyValidationError(f"{SCIENTIFIC_CONTRACT}.{field}：必须是非空对象")
        normalized[field] = value
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def _read_bound_file(link: Any, label: str, *, json_object: bool = False) -> tuple[Path, dict[str, Any] | None]:
    """读取 evidence 根中的封签文件；JSON 文件额外拒绝重复键与非有限值。"""
    value = _require_exact_fields(link, {"path", "sha256"}, label)
    path_text, digest = value["path"], value["sha256"]
    if not isinstance(path_text, str) or not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise FamilyValidationError(f"{label}：封签文件身份非法")
    path = Path(path_text)
    try:
        path.resolve().relative_to(EVIDENCE_ROOT.resolve())
    except (OSError, ValueError) as error:
        raise FamilyValidationError(f"{label}：封签文件必须位于 {EVIDENCE_ROOT} 内") from error
    if not path.is_file() or path.is_symlink() or file_sha256(path) != digest:
        raise FamilyValidationError(f"{label}：封签文件字节不匹配")
    if not json_object:
        return path, None
    return path, _load_strict_json(path, label, canonical=False)


def _artifact(value: Any, label: str) -> ArtifactIdentity:
    try:
        artifact = ArtifactIdentity(**value)
        artifact.validate()
    except (ContractError, TypeError, KeyError) as error:
        raise FamilyValidationError(f"{label}：镜像身份非法：{error}") from error
    if artifact.digest is None:
        raise FamilyValidationError(f"{label}：镜像必须绑定不可变 digest")
    return artifact


class EvidenceReader:
    """验证 evidence 字节及其共享题族身份。"""

    def __init__(self, *, question_id: str, contract_sha256: str) -> None:
        self.question_id = question_id
        self.contract_sha256 = contract_sha256

    def read(self, link: Any, *, label: str, evidence_type: str, package_sha256: str, level: str) -> dict[str, Any]:
        """读取一个 evidence 链接并验证共享题族身份。"""
        data = _read_evidence_link(link, label)
        identity = {
            "schema_version": 1,
            "evidence_type": evidence_type,
            "question_id": self.question_id,
            "package_sha256": package_sha256,
            "scientific_contract_sha256": self.contract_sha256,
            "level": level,
        }
        if any(data.get(key) != item for key, item in identity.items()):
            raise FamilyValidationError(f"{label}：evidence 身份不匹配")
        return data


def _read_evidence_link(link: Any, label: str) -> dict[str, Any]:
    value = _require_exact_fields(link, {"path", "sha256"}, label)
    path_text = value["path"]
    digest = value["sha256"]
    if not isinstance(path_text, str):
        raise FamilyValidationError(f"{label}：evidence 路径必须是字符串")
    path = Path(path_text)
    try:
        path.resolve().relative_to(EVIDENCE_ROOT.resolve())
    except (OSError, ValueError) as error:
        raise FamilyValidationError(f"{label}：evidence 必须位于 {EVIDENCE_ROOT} 内") from error
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise FamilyValidationError(f"{label}：evidence SHA-256 非法")
    if not path.is_file() or path.is_symlink() or file_sha256(path) != digest:
        raise FamilyValidationError(f"{label}：evidence 字节不匹配：{path}")
    return _load_strict_json(path, label, canonical=True)


def _execution_identity(data: dict[str, Any], label: str) -> tuple[str, str, str, str]:
    identity = tuple(data.get(key) for key in ("job_id", "trial_id", "sandbox_id", "session_id"))
    if any(not isinstance(item, str) or not item for item in identity):
        raise FamilyValidationError(f"{label}：执行身份不完整")
    return identity  # type: ignore[return-value]


def _bound_researcher_execution(
    data: dict[str, Any],
    label: str,
    *,
    package_sha256: str,
    mode: str,
    attempt_index: int,
    context_digests: list[str],
) -> tuple[str, str, str, str]:
    """复验 workflow 可生成的 request/capability 绑定，而非相信结果摘要。"""
    if data.get("request_id") is None or data.get("context_digests") != context_digests:
        raise FamilyValidationError(f"{label}：Researcher request 绑定缺失")
    request_path, request_value = _read_bound_file(data.get("request"), f"{label}.request", json_object=True)
    capability_path, capability = _read_bound_file(
        data.get("capability"), f"{label}.capability", json_object=True
    )
    assert request_value is not None and capability is not None
    try:
        request = ResearcherRequest.from_dict(request_value)
    except (ContractError, TypeError, KeyError, OSError, ValueError) as error:
        raise FamilyValidationError(f"{label}：Researcher request 非法：{error}") from error
    if (
        request.request_id != data["request_id"]
        or request.package_sha256 != package_sha256
        or request.mode != mode
        or request.attempt_index != attempt_index
        or list(request.context_digests) != context_digests
    ):
        raise FamilyValidationError(f"{label}：Researcher request 与科学结果不一致")
    expected_capability_fields = {
        "schema_version",
        "request_sha256",
        "researcher_thread_id",
        "token_sha256",
        "status",
        "consumed_at",
    }
    if set(capability) != expected_capability_fields:
        raise FamilyValidationError(f"{label}：capability 字段不完整或含额外字段")
    if (
        capability.get("schema_version") != 1
        or capability.get("status") != "CONSUMED"
        or capability.get("request_sha256") != file_sha256(request_path)
        or capability.get("researcher_thread_id") != request.researcher_thread_id
        or not isinstance(capability.get("consumed_at"), str)
        or not capability["consumed_at"]
        or not isinstance(capability.get("token_sha256"), str)
        or not SHA256.fullmatch(capability["token_sha256"])
    ):
        raise FamilyValidationError(f"{label}：capability 未消费或未绑定该 request")
    if capability_path == request_path:
        raise FamilyValidationError(f"{label}：request 与 capability 不能复用同一文件")
    return _execution_identity(data, label)


def _validate_hint_order(
    review: dict[str, Any],
    result: dict[str, Any],
    label: str,
    *,
    expected_index: int,
    expected_parent: str | None,
) -> None:
    """确保 hint 证据遵循正式 workflow 的连续索引和父提示链。"""
    for value, suffix in ((review, "review_seal"), (result, "result_evidence")):
        if (
            type(value.get("hint_index")) is not int
            or value["hint_index"] != expected_index
            or value.get("parent_hint_sha256") != expected_parent
        ):
            raise FamilyValidationError(f"{label}.{suffix}：hint_index/parent 顺序非法")


def _formal_pass(reader: EvidenceReader, link: Any, *, package: str, level: str) -> None:
    data = reader.read(link, label=f"{level}.formal_reviewer", evidence_type="formal-review", package_sha256=package, level=level)
    required_true = (
        "reviewer_independent",
        "grader_pass",
        "security_pass",
        "source_pass",
        "provenance_pass",
        "scientific_contract_same_source",
    )
    if data.get("verdict") != "PASS" or not all(data.get(key) is True for key in required_true):
        raise FamilyValidationError(f"{level}.formal_reviewer：缺少完整独立 PASS")
    if level != "high" and data.get("derived_only_from_reviewed_hint") is not True:
        raise FamilyValidationError(f"{level}.formal_reviewer：缺少仅由 reviewed hint 派生的声明")


def _perfect_validation(reader: EvidenceReader, link: Any, *, kind: str, package: str, level: str) -> None:
    data = reader.read(link, label=f"{level}.{kind}", evidence_type=f"{kind}-validation", package_sha256=package, level=level)
    independent_key = "oracle_independent" if kind == "oracle" else "solver_independent"
    if data.get("verdict") != "PASS" or data.get(independent_key) is not True or _score(data.get("score"), f"{level}.{kind}.score") != 1.0:
        raise FamilyValidationError(f"{level}.{kind}：缺少独立满分 PASS")


def _hint(
    reader: EvidenceReader,
    value: Any,
    *,
    label: str,
    package: str,
    level: str,
    expected_index: int,
    expected_parent: str | None,
) -> tuple[str, tuple[str, str, str, str]]:
    hint = _require_exact_fields(value, {"score", "review_seal", "result_evidence"}, label)
    score = _score(hint["score"], f"{label}.score", minimum=0.85)
    review = reader.read(hint["review_seal"], label=f"{label}.review_seal", evidence_type="hint-review", package_sha256=package, level=level)
    hint_sha = review.get("hint_sha256")
    if review.get("verdict") != "PASS" or review.get("contains_answer") is not False or not isinstance(hint_sha, str) or not SHA256.fullmatch(hint_sha):
        raise FamilyValidationError(f"{label}：缺少经审核的非答案 hint PASS")
    result = reader.read(hint["result_evidence"], label=f"{label}.result_evidence", evidence_type="hint-result", package_sha256=package, level=level)
    if (
        result.get("classification") != "SCIENTIFIC_RESULT"
        or result.get("mode") != "hint"
        or result.get("leakage_free") is not True
        or result.get("hint_sha256") != hint_sha
        or _score(result.get("score"), f"{label}.result_score") != score
    ):
        raise FamilyValidationError(f"{label}：hint 科学结果语义不匹配")
    _validate_hint_order(
        review,
        result,
        label,
        expected_index=expected_index,
        expected_parent=expected_parent,
    )
    identity = _bound_researcher_execution(
        result,
        f"{label}.result_evidence",
        package_sha256=package,
        mode="hint",
        attempt_index=expected_index,
        context_digests=[hint_sha],
    )
    return hint_sha, identity


def _validate_package(family: Path, level: str, package_sha256: str) -> None:
    package = family / level
    if not package.is_dir() or package.is_symlink():
        raise FamilyValidationError(f"{level}：题包目录缺失")
    require_plain_tree(package)
    if lint_package(package).get("sha256") != package_sha256:
        raise FamilyValidationError(f"{level}：题包 digest 不匹配")


def _hydrate_environment_receipt(value: dict[str, Any], label: str) -> EnvironmentReceipt:
    try:
        converted = dict(value)
        converted["artifact"] = ArtifactIdentity(**converted["artifact"])
        converted["resource_digests"] = tuple(converted["resource_digests"])
        receipt = EnvironmentReceipt(**converted)
        receipt.validate()
    except (ContractError, TypeError, KeyError) as error:
        raise FamilyValidationError(f"{label}：EnvironmentReceipt 非法：{error}") from error
    if receipt.schema_version != 2:
        raise FamilyValidationError(f"{label}：必须使用 schema-v2 EnvironmentReceipt")
    _artifact(value.get("artifact"), f"{label}.artifact")
    return receipt


def _hydrate_image_seal_plan(value: dict[str, Any], label: str) -> ImageSealPlan:
    try:
        converted = dict(value)
        converted["baseline_artifact"] = ArtifactIdentity(**converted["baseline_artifact"])
        converted["delta_receipts"] = tuple(converted["delta_receipts"])
        plan = ImageSealPlan(**converted)
        plan.validate()
    except (ContractError, TypeError, KeyError, ValueError) as error:
        raise FamilyValidationError(f"{label}：runtime closure 非法：{error}") from error
    return plan


def _validate_clean_sandboxes(value: dict[str, Any], artifact: ArtifactIdentity, label: str) -> None:
    runs = value.get("runs")
    if not isinstance(runs, list) or len(runs) < 2:
        raise FamilyValidationError(f"{label}：至少需要两个 clean sandbox")
    sandbox_ids = [run.get("sandbox_id") for run in runs if isinstance(run, dict)]
    if (
        type(value.get("clean_sandboxes_verified")) is not int
        or value["clean_sandboxes_verified"] < 2
        or len(sandbox_ids) != len(runs)
        or any(not isinstance(item, str) or not item for item in sandbox_ids)
        or len(sandbox_ids) != len(set(sandbox_ids))
    ):
        raise FamilyValidationError(f"{label}：clean sandbox 身份不完整或被复用")
    if (
        value.get("image_url") != artifact.image_url
        or str(value.get("image_record_id")) != artifact.record_id
        or value.get("install_commands_executed") != 0
        or value.get("public_asset_uploads_executed") != 0
    ):
        raise FamilyValidationError(f"{label}：clean sandbox 未绑定不可变环境或发生变更")
    for index, run in enumerate(runs, 1):
        validation = run.get("validation")
        exclusions = run.get("exclusions")
        if not isinstance(validation, list) or not validation or not isinstance(exclusions, list):
            raise FamilyValidationError(f"{label}.runs[{index}]：探针不完整")
        checks = [*validation, *exclusions]
        if any(
            not isinstance(check, dict)
            or check.get("passed") is not True
            or check.get("exit_code") != 0
            for check in checks
        ):
            raise FamilyValidationError(f"{label}.runs[{index}]：存在失败探针")
        exclusion_by_id = {check.get("id"): check for check in exclusions}
        if not REQUIRED_CLEAN_EXCLUSIONS <= exclusion_by_id.keys() or any(
            exclusion_by_id[check_id].get("observed_count") != 0
            for check_id in REQUIRED_CLEAN_EXCLUSIONS
        ):
            raise FamilyValidationError(f"{label}.runs[{index}]：harness-neutral 负向扫描未闭合")


def _runtime_files(
    data: dict[str, Any], label: str
) -> tuple[Path, dict[str, Any], Path, dict[str, Any], Path, dict[str, Any], Path, Path, dict[str, Any]]:
    """打开 runtime closure 五类封签文件并返回其严格 JSON。"""
    closure_path, closure = _read_bound_file(
        data["runtime_closure"], f"{label}.runtime_closure", json_object=True
    )
    receipt_path, receipt = _read_bound_file(
        data["environment_receipt"], f"{label}.environment_receipt", json_object=True
    )
    manifest_path, manifest = _read_bound_file(
        data["environment_manifest"], f"{label}.environment_manifest", json_object=True
    )
    lock_path, _ = _read_bound_file(data["dependency_lock"], f"{label}.dependency_lock")
    clean_path, clean = _read_bound_file(
        data["clean_sandbox_evidence"], f"{label}.clean_sandbox_evidence", json_object=True
    )
    assert closure is not None and receipt is not None and manifest is not None and clean is not None
    return (
        closure_path,
        closure,
        receipt_path,
        receipt,
        manifest_path,
        manifest,
        lock_path,
        clean_path,
        clean,
    )


def _validate_environment_manifest(
    manifest: dict[str, Any], receipt: EnvironmentReceipt, label: str
) -> None:
    """复验不可变 image 与 manifest resource 清单。"""
    image = manifest.get("image")
    if not isinstance(image, dict) or image.get("immutable") is not True:
        raise FamilyValidationError(f"{label}：environment manifest 缺少 immutable image")
    artifact = _artifact(
        {
            "provider": image.get("provider"),
            "endpoint_identity": receipt.artifact.endpoint_identity,
            "project_id": receipt.artifact.project_id,
            "record_id": str(image.get("record_id", "")),
            "image_url": image.get("url"),
            "digest": image.get("digest"),
        },
        f"{label}.environment_manifest.image",
    )
    if artifact != receipt.artifact:
        raise FamilyValidationError(f"{label}：manifest image 与 EnvironmentReceipt 不一致")
    resources = manifest.get("resources")
    if (
        not isinstance(resources, list)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("sha256"), str)
            or not SHA256.fullmatch(item["sha256"])
            for item in resources
        )
        or tuple(sorted(item["sha256"] for item in resources)) != receipt.resource_digests
    ):
        raise FamilyValidationError(f"{label}：manifest resources 与 EnvironmentReceipt 不一致")


def _validate_runtime_closure(data: dict[str, Any], *, package: str, label: str) -> None:
    expected = {
        "schema_version",
        "evidence_type",
        "question_id",
        "package_sha256",
        "scientific_contract_sha256",
        "level",
        "verdict",
        "runtime_closure",
        "environment_receipt",
        "environment_manifest",
        "dependency_lock",
        "clean_sandbox_evidence",
    }
    _require_exact_fields(data, expected, label)
    if data.get("verdict") != "PASS":
        raise FamilyValidationError(f"{label}：runtime closure 未通过")
    (
        closure_path,
        closure_value,
        receipt_path,
        receipt_value,
        manifest_path,
        manifest,
        lock_path,
        clean_path,
        clean,
    ) = _runtime_files(data, label)
    plan = _hydrate_image_seal_plan(closure_value, f"{label}.runtime_closure")
    receipt = _hydrate_environment_receipt(receipt_value, f"{label}.environment_receipt")
    if plan.package_sha256 != package:
        raise FamilyValidationError(f"{label}：runtime closure 绑定了其他题包")
    if (
        Path(receipt.runtime_closure_path or "").resolve() != closure_path.resolve()
        or receipt.runtime_closure_sha256 != file_sha256(closure_path)
        or Path(receipt.manifest_path).resolve() != manifest_path.resolve()
        or receipt.manifest_sha256 != file_sha256(manifest_path)
    ):
        raise FamilyValidationError(f"{label}：EnvironmentReceipt 未绑定 closure/manifest 字节")
    _validate_environment_manifest(manifest, receipt, label)
    closure_ref = manifest.get("runtime_closure")
    lock_ref = manifest.get("dependency_lock")
    if (
        closure_ref != data["runtime_closure"]
        or lock_ref != data["dependency_lock"]
        or Path(lock_ref["path"]).resolve() != lock_path.resolve()
    ):
        raise FamilyValidationError(f"{label}：manifest 未精确绑定 runtime closure/dependency lock")
    _validate_clean_sandboxes(clean, receipt.artifact, f"{label}.clean_sandbox_evidence")
    if receipt_path == manifest_path or clean_path in {receipt_path, manifest_path}:
        raise FamilyValidationError(f"{label}：环境回执、manifest 与 clean evidence 必须独立封签")


def _validate_high(family: Path, value: Any, reader: EvidenceReader) -> tuple[str, str, list[tuple[str, str, str, str]]]:
    fields = {
        "status", "package_sha256", "scientific_contract_sha256", "formal_reviewer",
        "oracle_validation", "honest_validation", "fresh_blinds", "successful_hint",
        "runtime_closure", "post_validation",
    }
    level = _require_exact_fields(value, fields, "high")
    package = level["package_sha256"]
    if level["status"] != "PASS_FINAL_PROGRESSION" or level["scientific_contract_sha256"] != reader.contract_sha256 or not isinstance(package, str) or not SHA256.fullmatch(package):
        raise FamilyValidationError("high：最终状态、题包或科学合同绑定非法")
    _validate_package(family, "high", package)
    if scientific_contract_sha256(family / "high") != reader.contract_sha256:
        raise FamilyValidationError("high：scientific_contract_sha256 不是题包科学字节的实算摘要")
    _formal_pass(reader, level["formal_reviewer"], package=package, level="high")
    _perfect_validation(reader, level["oracle_validation"], kind="oracle", package=package, level="high")
    _perfect_validation(reader, level["honest_validation"], kind="honest", package=package, level="high")
    blinds = level["fresh_blinds"]
    if not isinstance(blinds, list) or len(blinds) != 3:
        raise FamilyValidationError("high：必须恰好包含三次 fresh blind")
    identities: list[tuple[str, str, str, str]] = []
    for index, item in enumerate(blinds, 1):
        blind = _require_exact_fields(item, {"score", "evidence"}, f"high.blind{index}")
        score = _score(blind["score"], f"high.blind{index}.score")
        if score >= 0.85:
            raise FamilyValidationError(f"high.blind{index}：分数达到 TOO_EASY 阈值")
        data = reader.read(blind["evidence"], label=f"high.blind{index}.evidence", evidence_type="fresh-blind", package_sha256=package, level="high")
        if data.get("classification") != "SCIENTIFIC_RESULT" or data.get("mode") != "blind" or data.get("leakage_free") is not True or _score(data.get("score"), f"high.blind{index}.evidence.score") != score:
            raise FamilyValidationError(f"high.blind{index}：科学 blind 语义不匹配")
        identities.append(
            _bound_researcher_execution(
                data,
                f"high.blind{index}.evidence",
                package_sha256=package,
                mode="blind",
                attempt_index=index,
                context_digests=[],
            )
        )
    hint_sha, hint_identity = _hint(
        reader,
        level["successful_hint"],
        label="high.successful_hint",
        package=package,
        level="high",
        expected_index=1,
        expected_parent=None,
    )
    identities.append(hint_identity)
    runtime = reader.read(level["runtime_closure"], label="high.runtime_closure", evidence_type="runtime-closure", package_sha256=package, level="high")
    _validate_runtime_closure(runtime, package=package, label="high.runtime_closure")
    post = reader.read(level["post_validation"], label="high.post_validation", evidence_type="post-validation", package_sha256=package, level="high")
    if post.get("verdict") != "PASS_FINAL_PROGRESSION" or post.get("reviewer_independent") is not True:
        raise FamilyValidationError("high.post_validation：缺少独立最终 PASS")
    return package, hint_sha, identities


def _validate_derived_results(
    reader: EvidenceReader,
    results: Any,
    *,
    name: str,
    package: str,
    hint_sha: str,
    hint_index: int,
    parent_hint_sha256: str | None,
) -> list[tuple[str, str, str, str]]:
    """验证派生题层使用正式 hint 模式完成 fresh Harbor。"""
    if not isinstance(results, list) or not results:
        raise FamilyValidationError(f"{name}：至少需要一条 fresh Harbor 结果")
    identities = []
    for index, link in enumerate(results, 1):
        label = f"{name}.fresh_harbor{index}"
        data = reader.read(
            link,
            label=label,
            evidence_type="hint-result",
            package_sha256=package,
            level=name,
        )
        score = _score(data.get("score"), f"{label}.score", minimum=0.85)
        if (
            data.get("classification") != "SCIENTIFIC_RESULT"
            or data.get("mode") != "hint"
            or data.get("leakage_free") is not True
            or data.get("hint_sha256") != hint_sha
            or data.get("hint_index") != hint_index
            or data.get("parent_hint_sha256") != parent_hint_sha256
            or score < 0.85
        ):
            raise FamilyValidationError(f"{label}：缺少通过的科学结果")
        identities.append(
            _bound_researcher_execution(
                data,
                label,
                package_sha256=package,
                mode="hint",
                attempt_index=hint_index,
                context_digests=[hint_sha],
            )
        )
    return identities


def _validate_derived(
    family: Path,
    name: str,
    value: Any,
    reader: EvidenceReader,
    *,
    high_package: str,
    expected_hint: str | None,
    expected_hint_index: int,
    expected_parent_hint: str | None,
) -> tuple[str, list[tuple[str, str, str, str]]]:
    fields = {
        "status", "package_sha256", "scientific_contract_sha256", "derivation_hint",
        "formal_reviewer", "oracle_validation", "honest_validation", "fresh_harbor",
    }
    level = _require_exact_fields(value, fields, name)
    package = level["package_sha256"]
    if level["status"] != "PASS_FINAL_PROGRESSION" or level["scientific_contract_sha256"] != reader.contract_sha256 or not isinstance(package, str) or not SHA256.fullmatch(package):
        raise FamilyValidationError(f"{name}：最终状态、题包或科学合同绑定非法")
    _validate_package(family, name, package)
    if scientific_contract_sha256(family / name) != reader.contract_sha256:
        raise FamilyValidationError(
            f"{name}：scientific_contract_sha256 不是题包科学字节的实算摘要"
        )
    _formal_pass(reader, level["formal_reviewer"], package=package, level=name)
    _perfect_validation(reader, level["oracle_validation"], kind="oracle", package=package, level=name)
    _perfect_validation(reader, level["honest_validation"], kind="honest", package=package, level=name)
    hint_sha, hint_identity = _hint(
        reader,
        level["derivation_hint"],
        label=f"{name}.derivation_hint",
        package=high_package,
        level="high",
        expected_index=expected_hint_index,
        expected_parent=expected_parent_hint,
    )
    if expected_hint is not None and hint_sha != expected_hint:
        raise FamilyValidationError(f"{name}：题包不是由 high 成功 hint 派生")
    identities = [] if expected_hint is not None else [hint_identity]
    identities.extend(
        _validate_derived_results(
            reader,
            level["fresh_harbor"],
            name=name,
            package=package,
            hint_sha=hint_sha,
            hint_index=expected_hint_index,
            parent_hint_sha256=expected_parent_hint,
        )
    )
    return hint_sha, identities


def _validate_grandfathered(question: int, family: Path, manifest: dict[str, Any]) -> None:
    if question not in {1, 2}:
        raise FamilyValidationError("grandfathered-v1 仅限 Q01 和 Q02")
    _require_exact_fields(
        manifest,
        {"schema_version", "publication_mode", "question_id", "family_slug", "scientific_objective", "grandfathered"},
        "grandfathered family manifest",
    )
    value = _require_exact_fields(
        manifest["grandfathered"],
        {"status", "package_sha256", "completion_evidence"},
        "grandfathered",
    )
    package = value["package_sha256"]
    if value["status"] != "PASS_GRANDFATHERED" or not isinstance(package, str) or not SHA256.fullmatch(package):
        raise FamilyValidationError("grandfathered：状态或题包 digest 非法")
    if {path.name for path in family.iterdir()} != {"FAMILY_MANIFEST.json", "legacy"}:
        raise FamilyValidationError("grandfathered：题族只能包含 FAMILY_MANIFEST.json 和 legacy")
    legacy = family / "legacy"
    if not legacy.is_dir() or legacy.is_symlink():
        raise FamilyValidationError("grandfathered：legacy 题包目录缺失")
    require_plain_tree(legacy)
    if taskfoundry_directory_sha256(legacy) != package:
        raise FamilyValidationError("grandfathered：历史题包 digest 不匹配")
    evidence = _read_evidence_link(value["completion_evidence"], "grandfathered.completion_evidence")
    identity = {
        "schema_version": 1,
        "evidence_type": "grandfathered-completion",
        "question_id": f"Q{question:02d}",
        "package_sha256": package,
    }
    if any(evidence.get(key) != item for key, item in identity.items()):
        raise FamilyValidationError("grandfathered：completion evidence 身份不匹配")
    refs = evidence.get("historical_evidence_sha256s")
    if (
        evidence.get("verdict") != "PASS_GRANDFATHERED"
        or evidence.get("owner_attested") is not True
        or not isinstance(evidence.get("historical_policy_version"), str)
        or not evidence["historical_policy_version"]
        or not isinstance(refs, list)
        or not refs
        or any(not isinstance(item, str) or not SHA256.fullmatch(item) for item in refs)
    ):
        raise FamilyValidationError("grandfathered：缺少窄范围所有者确认的完成 PASS")


def _validate_standard(family: Path, manifest: dict[str, Any], question_id: str) -> None:
    fields = {
        "schema_version", "publication_mode", "question_id", "family_slug",
        "scientific_objective", "scientific_contract_sha256", "levels",
    }
    _require_exact_fields(manifest, fields, "family manifest")
    if manifest["publication_mode"] != "family-v1":
        raise FamilyValidationError("不支持的 publication_mode")
    contract = manifest["scientific_contract_sha256"]
    if not isinstance(contract, str) or not SHA256.fullmatch(contract):
        raise FamilyValidationError("科学合同 digest 非法")
    levels = manifest["levels"]
    if not isinstance(levels, dict) or not {"high", "medium"} <= set(levels) or set(levels) - set(LEVELS):
        raise FamilyValidationError("levels 必须包含 high 和 medium，low 可选")
    expected_children = {"FAMILY_MANIFEST.json", *levels}
    actual_children = {path.name for path in family.iterdir()}
    if actual_children != expected_children:
        raise FamilyValidationError(f"题族包含未声明条目：{sorted(actual_children ^ expected_children)}")
    reader = EvidenceReader(question_id=question_id, contract_sha256=contract)
    high_package, high_hint, identities = _validate_high(family, levels["high"], reader)
    _, medium_identities = _validate_derived(
        family,
        "medium",
        levels["medium"],
        reader,
        high_package=high_package,
        expected_hint=high_hint,
        expected_hint_index=1,
        expected_parent_hint=None,
    )
    identities.extend(medium_identities)
    if "low" in levels:
        low_hint, low_identities = _validate_derived(
            family,
            "low",
            levels["low"],
            reader,
            high_package=high_package,
            expected_hint=None,
            expected_hint_index=2,
            expected_parent_hint=high_hint,
        )
        if low_hint == high_hint:
            raise FamilyValidationError("low：必须绑定一个更晚且不同的 reviewed hint")
        identities.extend(low_identities)
    for offset, field in enumerate(("job_id", "trial_id", "sandbox_id", "session_id")):
        values = [identity[offset] for identity in identities]
        if len(values) != len(set(values)):
            raise FamilyValidationError(f"题族 Harbor {field} 不是逐字段全局 fresh")


def validate_family(question: int, family: Path, *, staging_required: bool = True) -> dict[str, Any]:
    """校验标准题族或 Q01/Q02 窄范围历史兼容题族。"""
    numbered_root = QUESTION_ROOT / str(question)
    legacy_publication = numbered_root / "new-question"
    publication = (
        legacy_publication
        if question <= 2 and legacy_publication.is_dir()
        else numbered_root / "question-pack"
    )
    expected_root = (
        (WORKBENCH_ROOT / f"q{question:02d}").resolve()
        if staging_required
        else publication.resolve()
    )
    try:
        family.resolve().relative_to(expected_root)
    except (OSError, ValueError) as error:
        boundary = "暂存" if staging_required else "已发布"
        raise FamilyValidationError(f"题族必须位于 {expected_root} 下的{boundary}边界内") from error
    if family.is_symlink() or not family.is_dir():
        raise FamilyValidationError("题族不是常规目录")
    require_plain_tree(family)
    manifest = _load_strict_json(family / "FAMILY_MANIFEST.json", "FAMILY_MANIFEST.json", canonical=False)
    question_id = f"Q{question:02d}"
    if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1 or manifest.get("question_id") != question_id:
        raise FamilyValidationError("题族 manifest 的题号身份不匹配")
    slug = manifest.get("family_slug")
    if not isinstance(slug, str) or not SLUG.fullmatch(slug) or family.name != slug:
        raise FamilyValidationError("题族 slug 非法或与目录名不一致")
    objective = manifest.get("scientific_objective")
    if not isinstance(objective, str) or len(objective) < 20:
        raise FamilyValidationError("科学目标非法")
    mode = manifest.get("publication_mode")
    if mode == "grandfathered-v1":
        _validate_grandfathered(question, family, manifest)
        return manifest
    _validate_standard(family, manifest, question_id)
    return manifest

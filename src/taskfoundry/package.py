"""Paper2Task 题包可见性检查和内容身份。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import tomllib
from typing import Any

from .model import ContractError


_REQUIRED = (
    "instruction.md",
    "task.toml",
    "environment/resources.yaml",
    "solution/solve.sh",
    "tests/test.sh",
)
_FORBIDDEN_VISIBLE_NAMES = {
    ".env",
    "dockerfile",
    "docker-compose.yaml",
    "docker-compose.yml",
    "checker.py",
    "ground_truth.json",
    "hint.md",
    "solution",
    "tests",
}
_SECRET_CONTENT = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_RESOURCE_SECTION = (
    "\n## Available resources\n\n"
    "Task resources and their runtime locations are listed in `resources.yaml`.\n"
)


@dataclass(frozen=True)
class PackageIssue:
    """一条确定性题包检查发现。"""

    severity: str
    rule_id: str
    path: str
    message: str


@dataclass(frozen=True)
class PackageReport:
    """完整检查结果和不可变题包摘要。"""

    package_path: str
    sha256: str
    issues: tuple[PackageIssue, ...]

    @property
    def passed(self) -> bool:
        """没有错误级发现时返回真。"""
        return not any(item.severity == "error" for item in self.issues)

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的报告。"""
        return asdict(self) | {"passed": self.passed}


def package_sha256(root: Path) -> str:
    """按稳定字典序计算相对路径、权限模式和文件字节摘要。"""
    if not root.is_dir():
        raise ContractError(f"package directory does not exist: {root}")
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if (
            "__pycache__" in path.parts
            or path.suffix == ".pyc"
            or path.name == "SCIENTIFIC_CONTRACT.json"
        ):
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(f"{path.stat().st_mode & 0o777:o}".encode() + b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def scientific_contract_sha256(root: Path, manifest_path: Path | None = None) -> str:
    """跨目录迁移计算有效 Agent 输入和评价器摘要。"""
    digest = hashlib.sha256()
    roots = [
        ("instruction.md", root / "instruction.md"),
        ("task.toml", root / "task.toml"),
    ]
    public = root / "public_data"
    if not public.is_dir():
        public = root / "environment" / "public_data"
    if manifest_path is not None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in sorted(manifest.get("resources", []), key=lambda value: value["source"]):
            relative = item["source"]
            local = public / Path(relative).name
            if local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() != item["sha256"]:
                raise ContractError(f"manifest resource digest mismatch: {relative}")
            roots.append((f"public_data-digest/{relative}", _DigestPath(item["sha256"])))
    directories = (("tests", root / "tests"),) if manifest_path is not None else (("public_data", public), ("tests", root / "tests"))
    for prefix, directory in directories:
        if directory.is_dir():
            for path in sorted(item for item in directory.rglob("*") if item.is_file()):
                if "__pycache__" in path.parts or path.suffix == ".pyc" or path.name == "local_gate.sh":
                    continue
                roots.append((f"{prefix}/{path.relative_to(directory).as_posix()}", path))
    for logical_path, path in roots:
        if isinstance(path, _DigestPath):
            content = path.digest.encode("ascii")
        elif not path.is_file():
            raise ContractError(f"scientific contract file is missing: {logical_path}")
        else:
            content = path.read_bytes()
        if logical_path == "instruction.md":
            text = content.decode("utf-8")
            content = (text if "resources.yaml" in text else text.rstrip() + "\n" + _RESOURCE_SECTION).encode("utf-8")
        digest.update(logical_path.encode("utf-8") + b"\0" + content + b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class _DigestPath:
    digest: str


def lint_package(root: Path) -> PackageReport:
    """校验固定 Harbor 题包契约和可见性边界。"""
    issues: list[PackageIssue] = []
    for relative in _REQUIRED:
        path = root / relative
        if not path.is_file():
            issues.append(PackageIssue("error", "P2T-STRUCT-001", relative, "required file is missing"))
    environment = root / "environment"
    if environment.is_dir():
        for path in environment.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(environment)
            lowered = {part.lower() for part in relative.parts}
            forbidden = lowered & _FORBIDDEN_VISIBLE_NAMES
            if forbidden:
                issues.append(
                    PackageIssue("error", "P2T-VIS-001", str(relative), f"Agent-visible forbidden name: {sorted(forbidden)[0]}")
                )
            if _SECRET_CONTENT.search(path.read_text(encoding="utf-8", errors="ignore")):
                issues.append(PackageIssue("error", "P2T-SEC-001", str(relative), "private key material is visible"))
    task_path = root / "task.toml"
    if task_path.is_file():
        _lint_task_toml(task_path, issues)
    instruction = root / "instruction.md"
    if instruction.is_file() and "resources.yaml" not in instruction.read_text(encoding="utf-8"):
        issues.append(PackageIssue("error", "P2T-INSTR-001", "instruction.md", "resource entrypoint is not declared"))
    verifier = root / "tests/test.sh"
    if verifier.is_file():
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (root / "tests").rglob("*")
            if path.is_file() and path.suffix in {"", ".sh", ".py"}
        )
        direct_reward = "/logs/verifier/reward.json" in text or "/logs/verifier/reward.txt" in text
        delegated_reward = "/logs/verifier" in text and ("reward.json" in text or "reward.txt" in text)
        if not direct_reward and not delegated_reward:
            issues.append(PackageIssue("error", "P2T-VERIFY-001", "tests/test.sh", "verifier does not write reward"))
    digest = package_sha256(root) if root.is_dir() else ""
    return PackageReport(str(root.resolve()), digest, tuple(issues))


def _lint_task_toml(path: Path, issues: list[PackageIssue]) -> None:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        issues.append(PackageIssue("error", "P2T-TOML-001", "task.toml", f"invalid TOML: {exc}"))
        return
    if value.get("schema_version", value.get("version")) != "1.3":
        issues.append(PackageIssue("error", "P2T-TOML-002", "task.toml", "schema version must be 1.3"))
    task = value.get("task", {})
    if "/" not in task.get("name", ""):
        issues.append(PackageIssue("error", "P2T-TOML-003", "task.toml", "task name must use organization/name"))
    environment = value.get("environment", {})
    image = environment.get("docker_image", "")
    if not image or image.endswith(":latest"):
        issues.append(PackageIssue("error", "P2T-ENV-001", "task.toml", "image must use an immutable non-latest reference"))
    workdir = environment.get("workdir", "")
    if not workdir.startswith("/"):
        issues.append(PackageIssue("error", "P2T-ENV-002", "task.toml", "workdir must be absolute"))
    agent_timeout = value.get("agent", {}).get("timeout_sec")
    if agent_timeout != 3600:
        issues.append(
            PackageIssue(
                "error",
                "P2T-TIME-001",
                "task.toml",
                "Agent hard timeout must be exactly 3600 seconds",
            )
        )
    target = value.get("metadata", {}).get("target_solution_time_sec")
    if target is None:
        issues.append(
            PackageIssue(
                "warning",
                "P2T-TIME-002",
                "task.toml",
                "new tasks should declare a target solution time of at most 1800 seconds",
            )
        )
    elif not isinstance(target, int) or not 1 <= target <= 1800:
        issues.append(
            PackageIssue(
                "error",
                "P2T-TIME-002",
                "task.toml",
                "target solution time must be within 1800 seconds",
            )
        )

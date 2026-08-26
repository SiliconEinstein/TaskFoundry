"""Paper2Task 题包迁移、可见性检查和内容身份。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
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
        if "__pycache__" in path.parts or path.suffix == ".pyc":
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


def migrate_legacy_task(source: Path, target: Path, manifest_path: Path) -> PackageReport:
    """在不修改历史证据的前提下创建标准新题包。"""
    if target.exists():
        raise ContractError(f"migration target already exists: {target}")
    legacy_reference = source / "reference"
    nested_reference = source / "solution" / "reference"
    if legacy_reference.is_dir() and nested_reference.is_dir():
        for source_path in (
            path
            for path in legacy_reference.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        ):
            nested_path = nested_reference / source_path.relative_to(legacy_reference)
            if nested_path.exists() and (
                not nested_path.is_file() or source_path.read_bytes() != nested_path.read_bytes()
            ):
                raise ContractError("source has conflicting root and solution reference trees")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image = manifest.get("image", {})
    if manifest.get("status") != "ENVIRONMENT_READY" or not image.get("immutable"):
        raise ContractError("migration requires an immutable ready environment")
    target.mkdir(parents=True)
    for name in ("instruction.md", "task.toml"):
        shutil.copy2(source / name, target / name)
    instruction = target / "instruction.md"
    instruction_text = instruction.read_text(encoding="utf-8")
    if "resources.yaml" not in instruction_text:
        instruction.write_text(instruction_text.rstrip() + "\n" + _RESOURCE_SECTION, encoding="utf-8")
    for name in ("solution", "tests"):
        shutil.copytree(source / name, target / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if legacy_reference.is_dir():
        destination = target / "solution" / "reference"
        shutil.copytree(
            legacy_reference,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        solve = target / "solution" / "solve.sh"
        text = solve.read_text(encoding="utf-8")
        export = 'export PYTHONPATH="$(dirname "$0")/reference${PYTHONPATH:+:$PYTHONPATH}"\n'
        if export not in text:
            solve.write_text(text.replace("set -eu\n", "set -eu\n" + export, 1), encoding="utf-8")
    environment = target / "environment"
    environment.mkdir()
    public_data = source / "public_data"
    if public_data.is_dir() and not _manifest_covers_public_data(public_data, manifest):
        shutil.copytree(public_data, environment / "public_data")
    legacy_environment = source / "environment"
    if legacy_environment.is_dir():
        for path in legacy_environment.iterdir():
            if path.name.lower() in _FORBIDDEN_VISIBLE_NAMES or path.name == "resources.yaml":
                continue
            destination = environment / path.name
            if path.is_dir():
                shutil.copytree(path, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(path, destination)
    (environment / "resources.yaml").write_text(_resources_yaml(environment, manifest), encoding="utf-8")
    report = lint_package(target)
    if not report.passed:
        raise ContractError("migrated package failed lint: " + "; ".join(item.message for item in report.issues))
    return report


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


def _resources_yaml(environment: Path, manifest: dict[str, Any]) -> str:
    lines = ["resources:"]
    for path in sorted(item for item in environment.rglob("*") if item.is_file() and item.name != "resources.yaml"):
        relative = path.relative_to(environment).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.extend(
            [
                f"  - name: {json.dumps(path.stem)}",
                "    kind: data",
                "    source: bundled",
                f"    path: {json.dumps(relative)}",
                f"    checksum: {json.dumps('sha256:' + digest)}",
            ]
        )
    for item in manifest.get("inventory", []):
        if item.get("kind") != "tool" or not item.get("command"):
            continue
        lines.extend(
            [
                f"  - name: {json.dumps(item.get('name', item['command']))}",
                "    kind: tool",
                "    source: preinstalled",
                f"    command: {json.dumps(item['command'])}",
                f"    description: {json.dumps('Preinstalled scientific environment capability')}",
            ]
        )
    for item in manifest.get("resources", []):
        lines.extend(
            [
                f"  - name: {json.dumps(Path(item['path']).name)}",
                "    kind: data",
                "    source: preinstalled",
                f"    path: {json.dumps(item['path'])}",
                f"    checksum: {json.dumps('sha256:' + item['sha256'])}",
            ]
        )
    if len(lines) == 1:
        return "resources: []\n"
    return "\n".join(lines) + "\n"


def _manifest_covers_public_data(public_data: Path, manifest: dict[str, Any]) -> bool:
    expected = {Path(item["path"]).name: item["sha256"] for item in manifest.get("resources", [])}
    files = [item for item in public_data.rglob("*") if item.is_file()]
    return bool(files) and all(
        expected.get(path.name) == hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    )

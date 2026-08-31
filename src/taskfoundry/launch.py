"""首次 Harbor 前只验证题包能被启动，不提前判断科学质量。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import tomllib
from typing import Any

from .package import package_sha256


class LaunchContractError(RuntimeError):
    """题包无法安全冻结为普通文件快照。"""


_REQUIRED_REGULAR_FILES = (
    "instruction.md",
    "task.toml",
    "environment/resources.yaml",
    "solution/solve.sh",
    "tests/test.sh",
)


@dataclass(frozen=True)
class LaunchIssue:
    """一项阻断真实 Harbor 启动的最小问题。"""

    rule_id: str
    path: str
    message: str


@dataclass(frozen=True)
class LaunchReport:
    """题包最小结构和 probe 的确定性结果。"""

    package_path: str
    package_sha256: str
    probe_exit_code: int | None
    probe_timed_out: bool
    issues: tuple[LaunchIssue, ...]

    @property
    def passed(self) -> bool:
        """没有启动阻断时返回真。"""
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        """返回可写入运行证据的结构。"""
        return asdict(self) | {"passed": self.passed}


def check_launchability(root: Path, *, timeout_sec: int = 10) -> LaunchReport:
    """检查普通文件、最小 task TOML，并执行 `tests/test.sh --probe`。"""
    issues: list[LaunchIssue] = []
    if not root.is_dir():
        return LaunchReport(str(root.resolve()), "", None, False, (
            LaunchIssue("LAUNCH-ROOT", ".", "package root is not a directory"),
        ))
    for relative in _REQUIRED_REGULAR_FILES:
        path = root / relative
        try:
            regular = stat.S_ISREG(path.lstat().st_mode)
        except OSError:
            regular = False
        if not regular:
            issues.append(LaunchIssue("LAUNCH-NODE", relative, "required path is not a regular file"))
    task_path = root / "task.toml"
    if stat.S_ISREG(task_path.lstat().st_mode) if task_path.exists() else False:
        try:
            task = tomllib.loads(task_path.read_text(encoding="utf-8"))
            if task.get("schema_version", task.get("version")) != "1.3":
                raise ValueError("schema version must be 1.3")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as error:
            issues.append(LaunchIssue("LAUNCH-TOML", "task.toml", str(error)))
    probe_exit: int | None = None
    timed_out = False
    probe = root / "tests/test.sh"
    if probe.is_file() and not probe.is_symlink():
        try:
            with tempfile.TemporaryDirectory(prefix="taskfoundry-launch-probe-") as probe_root:
                probe_runtime = Path(probe_root)
                output_dir = probe_runtime / "outputs"
                output_dir.mkdir()
                completed = subprocess.run(
                    ["/bin/sh", str(probe), "--probe"],
                    cwd=root,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "LANG": "C.UTF-8",
                        "TASK_OUTPUT_DIR": str(output_dir),
                        "TASK_REFERENCE_PATH": str((root / "tests/reference.json").resolve()),
                        "TASK_REWARD_PATH": str(probe_runtime / "reward.txt"),
                        "TASK_TESTS_DIR": str((root / "tests").resolve()),
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
            probe_exit = completed.returncode
            if completed.returncode != 0:
                issues.append(
                    LaunchIssue("LAUNCH-PROBE", "tests/test.sh", "--probe returned non-zero")
                )
        except subprocess.TimeoutExpired:
            timed_out = True
            issues.append(LaunchIssue("LAUNCH-PROBE", "tests/test.sh", "--probe timed out"))
    digest = package_sha256(root)
    return LaunchReport(str(root.resolve()), digest, probe_exit, timed_out, tuple(issues))


def freeze_package_snapshot(source: Path, destination: Path) -> LaunchReport:
    """原子复制普通文件树；后续 authoring 目录变化不影响冻结字节。"""
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        return check_launchability(source)
    if source in destination.parents:
        raise LaunchContractError("冻结目标不能位于源题包内部")
    if not source.is_dir():
        raise LaunchContractError("冻结题包源必须是目录")
    for path in source.rglob("*"):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            raise LaunchContractError(f"冻结题包不得包含摘要排除缓存: {path}")
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise LaunchContractError(f"冻结题包只允许目录和普通文件: {path}")
    source_report = check_launchability(source)
    if not source_report.passed:
        raise LaunchContractError("源题包不满足最小启动契约")
    if destination.exists():
        destination_report = check_launchability(destination)
        if (
            destination_report.passed
            and destination_report.package_sha256 == source_report.package_sha256
        ):
            return destination_report
        raise LaunchContractError("冻结目标已存在且字节身份不同")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="task-snapshot-", dir=destination.parent))
    try:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            target = temporary / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    report = check_launchability(destination)
    if not report.passed or report.package_sha256 != source_report.package_sha256:
        shutil.rmtree(destination)
        raise LaunchContractError("冻结题包复制后未闭合")
    return report

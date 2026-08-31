"""首次 Harbor 前最小启动检查契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taskfoundry.launch import LaunchContractError, check_launchability, freeze_package_snapshot


def launch_package(root: Path, probe: str = "exit 0") -> Path:
    for relative in (
        "instruction.md",
        "task.toml",
        "environment/resources.yaml",
        "solution/solve.sh",
        "tests/test.sh",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    (root / "task.toml").write_text(
        'schema_version="1.3"\n[task]\nname="org/task"\n', encoding="utf-8"
    )
    (root / "tests/test.sh").write_text(
        f'#!/bin/sh\ntest "$1" = "--probe" || exit 9\n{probe}\n', encoding="utf-8"
    )
    return root


def test_launch_checker_runs_only_declared_probe(tmp_path: Path) -> None:
    report = check_launchability(launch_package(tmp_path / "task"))

    assert report.passed is True
    assert report.probe_exit_code == 0
    assert report.probe_timed_out is False


def test_launch_checker_gives_probe_isolated_writable_runtime_paths(tmp_path: Path) -> None:
    package = launch_package(
        tmp_path / "task",
        (
            'test -d "$TASK_OUTPUT_DIR"\n'
            'test -f "$TASK_REFERENCE_PATH"\n'
            'printf "0.0\\n" > "$TASK_REWARD_PATH"'
        ),
    )
    (package / "tests/reference.json").write_text("{}\n", encoding="utf-8")

    report = check_launchability(package)

    assert report.passed is True
    assert report.probe_exit_code == 0


def test_launch_checker_rejects_symlink_and_failed_probe(tmp_path: Path) -> None:
    package = launch_package(tmp_path / "task", "exit 7")
    (package / "instruction.md").unlink()
    (package / "instruction.md").symlink_to(package / "task.toml")

    report = check_launchability(package)

    assert report.passed is False
    assert {issue.rule_id for issue in report.issues} == {"LAUNCH-NODE", "LAUNCH-PROBE"}


def test_launch_checker_rejects_missing_root_invalid_toml_and_timeout(tmp_path: Path) -> None:
    assert check_launchability(tmp_path / "missing").issues[0].rule_id == "LAUNCH-ROOT"
    package = launch_package(tmp_path / "task", "sleep 1")
    (package / "task.toml").write_text("not = [toml", encoding="utf-8")

    report = check_launchability(package, timeout_sec=0.01)

    assert report.probe_timed_out is True
    assert {issue.rule_id for issue in report.issues} == {"LAUNCH-TOML", "LAUNCH-PROBE"}


def test_package_snapshot_is_immutable_copy_and_rejects_symlink(tmp_path: Path) -> None:
    source = launch_package(tmp_path / "source")
    target = tmp_path / "revisions/r1/task"

    report = freeze_package_snapshot(source, target)
    (source / "instruction.md").write_text("changed", encoding="utf-8")

    assert Path(report.package_path) == target.resolve()
    assert (target / "instruction.md").read_text(encoding="utf-8") == "x"
    assert freeze_package_snapshot(target, target).package_sha256 == report.package_sha256
    symlinked = launch_package(tmp_path / "symlinked")
    (symlinked / "instruction.md").unlink()
    (symlinked / "instruction.md").symlink_to(symlinked / "task.toml")
    with pytest.raises(LaunchContractError, match="普通文件"):
        freeze_package_snapshot(symlinked, tmp_path / "revisions/r2/task")


def test_package_snapshot_rejects_runtime_cache(tmp_path: Path) -> None:
    source = launch_package(tmp_path / "source")
    cache = source / "solution/__pycache__"
    cache.mkdir()
    (cache / "solve.cpython-312.pyc").write_bytes(b"cache")

    with pytest.raises(LaunchContractError, match="缓存"):
        freeze_package_snapshot(source, tmp_path / "revisions/r1/task")

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskfoundry.model import ContractError
from taskfoundry.package import (
    lint_package,
    migrate_legacy_task,
    package_sha256,
    scientific_contract_sha256,
)


TASK_TOML = '''schema_version = "1.3"
[task]
name = "paper2task/example"
[environment]
docker_image = "registry.example/task:sha-123"
workdir = "/app"
[agent]
timeout_sec = 3600
'''


def make_task(root: Path, *, legacy: bool = False) -> Path:
    root.mkdir()
    (root / "instruction.md").write_text("Use resources.yaml and write output.\n")
    (root / "task.toml").write_text(TASK_TOML)
    (root / "solution").mkdir()
    (root / "solution/solve.sh").write_text("#!/bin/sh\ntrue\n")
    (root / "tests").mkdir()
    (root / "tests/test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    if legacy:
        (root / "public_data").mkdir()
        (root / "public_data/input.csv").write_text("x\n1\n")
        (root / "environment").mkdir()
        (root / "environment/Dockerfile").write_text("FROM example\n")
    else:
        (root / "environment").mkdir()
        (root / "environment/resources.yaml").write_text("resources: []\n")
    return root


def test_lint_accepts_minimal_package(tmp_path) -> None:
    task = make_task(tmp_path / "task")
    report = lint_package(task)
    assert report.passed
    assert len(report.sha256) == 64


def test_digest_changes_with_mode_or_content(tmp_path) -> None:
    task = make_task(tmp_path / "task")
    first = package_sha256(task)
    (task / "solution/solve.sh").chmod(0o755)
    assert package_sha256(task) != first
    second = package_sha256(task)
    (task / "instruction.md").write_text("Use resources.yaml differently.\n")
    assert package_sha256(task) != second


def test_visibility_and_reward_failures_are_reported(tmp_path) -> None:
    task = make_task(tmp_path / "task")
    (task / "environment/checker.py").write_text("secret\n")
    (task / "tests/test.sh").write_text("exit 0\n")
    rules = {item.rule_id for item in lint_package(task).issues}
    assert {"P2T-VIS-001", "P2T-VERIFY-001"} <= rules


def test_lint_reports_missing_structure_secret_and_instruction_entrypoint(tmp_path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "instruction.md").write_text("No resource pointer.\n")
    (task / "task.toml").write_text("not = [valid")
    (task / "environment").mkdir()
    (task / "environment/key.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    rules = {item.rule_id for item in lint_package(task).issues}
    assert {"P2T-STRUCT-001", "P2T-SEC-001", "P2T-TOML-001", "P2T-INSTR-001"} <= rules


def test_task_toml_requires_schema_version_and_image(tmp_path) -> None:
    task = make_task(tmp_path / "task")
    (task / "task.toml").write_text('[task]\nname="org/task"\n[environment]\nworkdir="/app"\n')
    rules = {item.rule_id for item in lint_package(task).issues}
    assert {"P2T-TOML-002", "P2T-ENV-001"} <= rules


def test_task_toml_requires_one_hour_hard_limit(tmp_path) -> None:
    task = make_task(tmp_path / "task")
    (task / "task.toml").write_text(TASK_TOML.replace("3600", "3599"))
    assert "P2T-TIME-001" in {item.rule_id for item in lint_package(task).issues}


def test_task_toml_rejects_target_over_half_hour(tmp_path) -> None:
    task = make_task(tmp_path / "task")
    value = TASK_TOML.replace(
        "[environment]",
        "[metadata]\ntarget_solution_time_sec = 1801\n[environment]",
    )
    (task / "task.toml").write_text(value)
    assert "P2T-TIME-002" in {item.rule_id for item in lint_package(task).issues}


@pytest.mark.parametrize(
    "replacement,rule",
    [
        (TASK_TOML.replace('paper2task/example', 'example'), "P2T-TOML-003"),
        (TASK_TOML.replace('sha-123', 'latest'), "P2T-ENV-001"),
        (TASK_TOML.replace('/app', 'app'), "P2T-ENV-002"),
    ],
)
def test_task_toml_contract(tmp_path, replacement, rule) -> None:
    task = make_task(tmp_path / "task")
    (task / "task.toml").write_text(replacement)
    assert rule in {item.rule_id for item in lint_package(task).issues}


def test_migration_creates_standard_visible_environment(tmp_path) -> None:
    source = make_task(tmp_path / "legacy", legacy=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "status": "ENVIRONMENT_READY",
                "image": {"immutable": True},
                "inventory": [{"kind": "tool", "name": "python", "command": "python3"}],
            }
        )
    )
    target = tmp_path / "standard"
    report = migrate_legacy_task(source, target, manifest)
    assert report.passed
    assert (target / "environment/public_data/input.csv").is_file()
    assert not (target / "environment/Dockerfile").exists()
    resources = (target / "environment/resources.yaml").read_text()
    assert "public_data/input.csv" in resources and "python3" in resources
    assert scientific_contract_sha256(source) == scientific_contract_sha256(target)


def test_migration_requires_ready_immutable_environment(tmp_path) -> None:
    source = make_task(tmp_path / "legacy", legacy=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"status": "BUILDING", "image": {"immutable": False}}))
    with pytest.raises(ContractError, match="immutable ready"):
        migrate_legacy_task(source, tmp_path / "target", manifest)


def test_migration_accepts_equivalent_nested_reference_tree(tmp_path) -> None:
    source = make_task(tmp_path / "source")
    (source / "reference").mkdir()
    (source / "reference/model.py").write_text("VALUE = 1\n")
    (source / "reference/generate.py").write_text("print('generate')\n")
    (source / "solution/reference").mkdir()
    (source / "solution/reference/model.py").write_text("VALUE = 1\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"status": "ENVIRONMENT_READY", "image": {"immutable": True}}))

    report = migrate_legacy_task(source, tmp_path / "target", manifest)

    assert report.passed
    assert (tmp_path / "target/solution/reference/model.py").read_text() == "VALUE = 1\n"
    assert (tmp_path / "target/solution/reference/generate.py").is_file()


def test_migration_rejects_conflicting_reference_trees_before_writing(tmp_path) -> None:
    source = make_task(tmp_path / "source")
    (source / "reference").mkdir()
    (source / "reference/model.py").write_text("VALUE = 1\n")
    (source / "solution/reference").mkdir()
    (source / "solution/reference/model.py").write_text("VALUE = 2\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"status": "ENVIRONMENT_READY", "image": {"immutable": True}}))

    with pytest.raises(ContractError, match="conflicting root and solution reference"):
        migrate_legacy_task(source, tmp_path / "target", manifest)
    assert not (tmp_path / "target").exists()

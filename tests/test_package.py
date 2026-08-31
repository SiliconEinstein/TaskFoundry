from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from taskfoundry.model import ContractError
from taskfoundry.package import lint_package, package_sha256, scientific_contract_sha256


TASK_TOML = '''schema_version = "1.3"
[task]
name = "paper2task/example"
[environment]
docker_image = "registry.example/task:sha-123"
workdir = "/app"
[agent]
timeout_sec = 3600
'''


def make_task(root: Path) -> Path:
    root.mkdir()
    (root / "instruction.md").write_text("Use resources.yaml and write output.\n")
    (root / "task.toml").write_text(TASK_TOML)
    (root / "solution").mkdir()
    (root / "solution/solve.sh").write_text("#!/bin/sh\ntrue\n")
    (root / "tests").mkdir()
    (root / "tests/test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
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


def test_scientific_contract_normalizes_public_resource_layout(tmp_path) -> None:
    legacy = make_task(tmp_path / "legacy")
    (legacy / "public_data").mkdir()
    (legacy / "public_data/input.csv").write_text("x\n1\n")
    visible = make_task(tmp_path / "visible")
    (visible / "environment/public_data").mkdir()
    (visible / "environment/public_data/input.csv").write_text("x\n1\n")

    assert scientific_contract_sha256(legacy) == scientific_contract_sha256(visible)


def test_scientific_contract_uses_manifest_digests(tmp_path) -> None:
    task = make_task(tmp_path / "task")
    public = task / "environment/public_data"
    public.mkdir()
    resource = public / "input.csv"
    resource.write_text("x\n1\n")
    digest = hashlib.sha256(resource.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"resources": [{"source": "assets/input.csv", "sha256": digest}]}))

    first = scientific_contract_sha256(task, manifest)
    resource.unlink()
    assert scientific_contract_sha256(task, manifest) == first


def test_scientific_contract_rejects_mismatch_and_missing_required_file(tmp_path) -> None:
    task = make_task(tmp_path / "task")
    public = task / "environment/public_data"
    public.mkdir()
    (public / "input.csv").write_text("x\n1\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"resources": [{"source": "input.csv", "sha256": "0" * 64}]}))

    with pytest.raises(ContractError, match="digest mismatch"):
        scientific_contract_sha256(task, manifest)
    (task / "instruction.md").unlink()
    with pytest.raises(ContractError, match="file is missing"):
        scientific_contract_sha256(task)

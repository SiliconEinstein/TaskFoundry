from pathlib import Path

import pytest

from taskfoundry.health import (
    BoundHealthEvidence,
    HealthGate,
    RevisionChange,
    invalidated_gates,
)
from taskfoundry.model import ContractError
from taskfoundry.validation import HealthEvidence


def _bundle(tmp_path: Path) -> BoundHealthEvidence:
    evidence = []
    for gate in (
        HealthGate.PACKAGE,
        HealthGate.ENVIRONMENT,
        HealthGate.ORACLE,
        HealthGate.HONEST,
        HealthGate.ADVERSARIAL,
        HealthGate.LEAKAGE,
    ):
        path = tmp_path / f"{gate.value}.json"
        path.write_text(f'{{"gate":"{gate.value}"}}')
        evidence.append((gate, path))
    return BoundHealthEvidence.create(
        question_revision="r1",
        package_sha256="a" * 64,
        environment_key="b" * 64,
        health=HealthEvidence(True, True, True, True, True, True),
        evidence=evidence,
    )


def test_bound_health_detects_changed_evidence(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    Path(bundle.references[0].path).write_text("changed")
    with pytest.raises(ContractError, match="changed"):
        bundle.validate()


def test_bound_health_rejects_other_package(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(ContractError, match="another package"):
        bundle.assert_current("c" * 64, "b" * 64, "r1")


def test_bound_health_rejects_other_revision(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(ContractError, match="another revision"):
        bundle.assert_current("a" * 64, "b" * 64, "r2")


def test_environment_change_invalidates_runtime_science() -> None:
    gates = invalidated_gates([RevisionChange.ENVIRONMENT])
    assert HealthGate.ENVIRONMENT in gates
    assert HealthGate.ORACLE in gates
    assert HealthGate.BLIND_VALIDATION in gates
    assert HealthGate.ADVERSARIAL not in gates


def test_instruction_change_invalidates_formal_validation() -> None:
    gates = invalidated_gates([RevisionChange.INSTRUCTION])
    assert HealthGate.PACKAGE in gates
    assert HealthGate.BLIND_VALIDATION in gates
    assert HealthGate.HINT_VALIDATION in gates

from __future__ import annotations

import json

import pytest

from taskfoundry.model import ContractError
from taskfoundry.validation_control import TeacherDecision, TeacherValidationController


def _result(path, *, reward=0.2, phase="BLIND", round_index=1):
    value = {
        "schema_version": 1,
        "validation_session_id": "session-1",
        "trial_id": "trial-1",
        "round_index": round_index,
        "phase": phase,
        "classification": "SCIENTIFIC_RESULT",
        "reward": reward,
        "verifier_result": {"reward": reward},
        "artifact_manifest_sha256": "a" * 64,
    }
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return value


def test_teacher_binds_decision_to_published_result(tmp_path) -> None:
    result_path = tmp_path / "round-01-result.json"
    _result(result_path)
    controller = TeacherValidationController(tmp_path, "session-1")

    decision_path = controller.decide(
        result_path,
        TeacherDecision(action="CONTINUE_BLIND"),
    )

    value = json.loads(decision_path.read_text())
    assert value["validation_session_id"] == "session-1"
    assert len(value["result_sha256"]) == 64


def test_teacher_hint_requires_explicit_non_answer_declaration(tmp_path) -> None:
    result_path = tmp_path / "round-03-result.json"
    _result(result_path, round_index=3)
    controller = TeacherValidationController(tmp_path, "session-1")

    with pytest.raises(ContractError, match="non-answer"):
        controller.decide(
            result_path,
            TeacherDecision(
                action="CONTINUE_HINT",
                hint="inspect convergence",
                teacher_declares_non_answer=False,
            ),
        )


def test_teacher_must_stop_immediately_when_first_blind_passes(tmp_path) -> None:
    result_path = tmp_path / "round-01-result.json"
    _result(result_path, reward=1.0)
    controller = TeacherValidationController(tmp_path, "session-1")

    with pytest.raises(ContractError, match="progression"):
        controller.decide(result_path, TeacherDecision(action="CONTINUE_BLIND"))

    decision_path = controller.decide(
        result_path,
        TeacherDecision(action="STOP_TOO_EASY"),
    )
    assert json.loads(decision_path.read_text())["action"] == "STOP_TOO_EASY"

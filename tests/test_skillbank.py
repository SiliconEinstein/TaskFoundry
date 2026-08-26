from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskfoundry.model import ContractError
from taskfoundry import skillbank


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_latest_brief_requires_exactly_two_current_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    root = tmp_path / "3"
    root.mkdir()
    (root / "QuestionDesignBrief.json").write_text("{}", encoding="utf-8")
    (root / "QuestionDesignBrief.md").write_text("# Brief", encoding="utf-8")
    assert skillbank.validate_latest_brief(3) == (
        root / "QuestionDesignBrief.json",
        root / "QuestionDesignBrief.md",
    )
    (root / "QuestionDesignBrief-r2.md").write_text("old", encoding="utf-8")
    with pytest.raises(ContractError, match="latest two"):
        skillbank.validate_latest_brief(3)


def test_layout_has_two_trace_layers_and_publication_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    root = skillbank.ensure_question_layout(7)
    assert (root / "trace/authoring/skill-activations").is_dir()
    assert (root / "trace/final").is_dir()
    assert (root / "question-pack").is_dir()


def test_activation_requires_exact_question_type_skill_and_bank(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    skill = tmp_path / ".skillbank/execution-skills/method-selection-teacher/revisions/v1"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# complete", encoding="utf-8")
    source = tmp_path / "RULE.md"
    source.write_text("rule", encoding="utf-8")
    import hashlib
    write_json(skill / "RULE_SOURCES.json", {
        "schema_version": 1,
        "sources": [{"path": "RULE.md", "sha256": hashlib.sha256(b"rule").hexdigest()}],
    })
    activation = tmp_path / "activation.json"
    write_json(activation, {
        "schema_version": 1,
        "activation_id": "sha256:test",
        "project_id": "question-from-questions",
        "task_id": "q3",
        "attempt_id": "q3-outline-r1",
        "stable_generation": 1,
        "execution_skills": [{
            "artifact_id": "method-selection-teacher",
            "path": ".skillbank/execution-skills/method-selection-teacher/revisions/v1",
        }],
        "experience_banks": [{"artifact_id": "method-selection"}],
        "retrieved_cards": [],
    })
    assert skillbank.validate_activation(activation, question=3, stage="outline")["task_id"] == "q3"


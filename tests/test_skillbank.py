from __future__ import annotations

import hashlib
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
    assert (root / "trace/authoring/prompts").is_dir()
    assert (root / "trace/final").is_dir()
    assert (root / "question-pack").is_dir()


def test_activation_requires_exact_question_type_skill_and_bank(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    skill = tmp_path / ".skillbank/execution-skills/method-selection-teacher/revisions/v1"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# complete", encoding="utf-8")
    source = tmp_path / "RULE.md"
    source.write_text("rule", encoding="utf-8")
    write_json(skill / "RULE_SOURCES.json", {
        "schema_version": 1,
        "sources": [{"path": "RULE.md", "sha256": hashlib.sha256(b"rule").hexdigest()}],
    })
    skill_text = "# complete"
    rule_text = "rule"
    snapshot_text = "{}"
    card_text = '{"card_id":"method-selection-contract"}'
    manifest_text = (skill / "RULE_SOURCES.json").read_text(encoding="utf-8")
    (tmp_path / "snapshot.json").write_text(snapshot_text, encoding="utf-8")
    (tmp_path / "card.json").write_text(card_text, encoding="utf-8")
    source_input = tmp_path / "sources.json"
    source_input.write_text('{"sources":[]}', encoding="utf-8")
    stable_snapshot = tmp_path / "3/trace/authoring/skill-activations/outline-stable.lock.json"
    write_json(stable_snapshot, {"schema_version": 1, "generation": 1})
    prompt = tmp_path / "3/trace/authoring/prompts/outline-prompt.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text(
        "\n".join((
            skill_text,
            manifest_text,
            rule_text,
            snapshot_text,
            card_text,
            source_input.read_text(encoding="utf-8"),
        )),
        encoding="utf-8",
    )
    activation = tmp_path / "activation.json"
    write_json(activation, {
        "schema_version": 2,
        "activation_id": "sha256:test",
        "project_id": "question-from-questions",
        "task_id": "q3",
        "attempt_id": "q3-outline-r1",
        "batch_id": "batch-1",
        "batch_questions": [3],
        "input_set_sha256": "a" * 64,
        "role": "teacher",
        "task_type": "method-selection",
        "stage": "outline",
        "profile": "multi-question-input",
        "stable_generation": 1,
        "stable_lock_snapshot": {
            "path": str(stable_snapshot),
            "sha256": hashlib.sha256(stable_snapshot.read_bytes()).hexdigest(),
            "generation": 1,
        },
        "execution_skills": [{
            "artifact_id": "method-selection-teacher",
            "path": ".skillbank/execution-skills/method-selection-teacher/revisions/v1",
        }],
        "experience_banks": [{"artifact_id": "method-selection"}],
        "retrieved_cards": [{"card_id": "method-selection-contract"}],
        "knowledge_documents": [
            {"kind": "execution_skill", "path": ".skillbank/execution-skills/method-selection-teacher/revisions/v1/SKILL.md", "sha256": hashlib.sha256(skill_text.encode()).hexdigest(), "content": skill_text},
            {"kind": "rule_source_manifest", "path": ".skillbank/execution-skills/method-selection-teacher/revisions/v1/RULE_SOURCES.json", "sha256": hashlib.sha256(manifest_text.encode()).hexdigest(), "content": manifest_text},
            {"kind": "rule_source", "path": "RULE.md", "sha256": hashlib.sha256(rule_text.encode()).hexdigest(), "content": rule_text},
            {"kind": "experience_bank_snapshot", "path": "snapshot.json", "sha256": hashlib.sha256(snapshot_text.encode()).hexdigest(), "content": snapshot_text},
            {"kind": "experience_card", "path": "card.json", "sha256": hashlib.sha256(card_text.encode()).hexdigest(), "content": card_text},
        ],
        "prompt_bundle": {
            "path": str(prompt),
            "sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
            "task_inputs": [{
                "kind": "task_input",
                "path": str(source_input),
                "sha256": hashlib.sha256(source_input.read_bytes()).hexdigest(),
                "content": source_input.read_text(encoding="utf-8"),
            }],
        },
    })
    assert skillbank.validate_activation(activation, question=3, stage="outline")["task_id"] == "q3"

    prompt.write_text("tampered", encoding="utf-8")
    with pytest.raises(ContractError, match="prompt digest"):
        skillbank.validate_activation(activation, question=3, stage="outline")


def test_activation_rejects_metadata_only_legacy_receipt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    activation = tmp_path / "activation.json"
    write_json(activation, {
        "schema_version": 1,
        "project_id": "question-from-questions",
        "task_id": "q3",
        "stable_generation": 1,
        "execution_skills": [],
        "experience_banks": [],
        "retrieved_cards": [],
    })
    with pytest.raises(ContractError, match="self-contained"):
        skillbank.validate_activation(activation, question=3, stage="outline")


def install_fake_resolver(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / ".skillbank/stable.lock", {"schema_version": 1, "generation": 4})
    skill = tmp_path / ".skillbank/execution-skills/method-selection-teacher/revisions/v3"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Skill v3", encoding="utf-8")
    (tmp_path / "RULE.md").write_text("frozen rule", encoding="utf-8")
    write_json(skill / "RULE_SOURCES.json", {
        "schema_version": 1,
        "sources": [{"path": "RULE.md", "sha256": hashlib.sha256(b"frozen rule").hexdigest()}],
    })

    def fake_resolve(command: list[str]) -> dict[str, object]:
        context = json.loads(Path(command[1]).read_text(encoding="utf-8"))
        output = Path(command[command.index("--output") + 1])
        prompt_path = Path(command[command.index("--prompt-output") + 1])
        inputs = [Path(command[index + 1]) for index, item in enumerate(command) if item == "--task-input"]
        card_ids = (
            ["method-selection-contract"]
            if context["stage"] == "outline"
            else ["public-family-fingerprint", "grader-fail-closed"]
        )
        manifest_content = (skill / "RULE_SOURCES.json").read_text(encoding="utf-8")
        snapshot_path = tmp_path / ".skillbank/fake/snapshot.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text("{}", encoding="utf-8")
        card_paths = []
        for card_id in card_ids:
            card_path = tmp_path / f".skillbank/fake/{card_id}.json"
            card_path.write_text(card_id, encoding="utf-8")
            card_paths.append(card_path)
        contents = ["# Skill v3", manifest_content, "frozen rule", "{}", *card_ids]
        document_paths = [
            skill / "SKILL.md",
            skill / "RULE_SOURCES.json",
            tmp_path / "RULE.md",
            snapshot_path,
            *card_paths,
        ]
        documents = [
            {
                "kind": kind,
                "path": str(path.relative_to(tmp_path)),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content": content,
            }
            for path, (kind, content) in zip(document_paths, zip((
                "execution_skill",
                "rule_source_manifest",
                "rule_source",
                "experience_bank_snapshot",
                *("experience_card" for _ in card_ids),
            ), contents, strict=True), strict=True)
        ]
        task_inputs = [
            {
                "kind": "task_input",
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content": path.read_text(encoding="utf-8"),
            }
            for path in inputs
        ]
        prompt_text = "\n".join(
            [document["content"] for document in documents + task_inputs]
        )
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt_text, encoding="utf-8")
        activation = context | {
            "schema_version": 2,
            "activation_id": "sha256:activation",
            "stable_generation": 4,
            "execution_skills": [{
                "artifact_id": "method-selection-teacher",
                "path": ".skillbank/execution-skills/method-selection-teacher/revisions/v3",
            }],
            "experience_banks": [{"artifact_id": "method-selection"}],
            "retrieved_cards": [{"card_id": card_id} for card_id in card_ids],
            "knowledge_documents": documents,
            "prompt_bundle": {
                "path": str(prompt_path),
                "sha256": hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
                "task_inputs": task_inputs,
            },
        }
        write_json(output, activation)
        return activation

    monkeypatch.setattr(skillbank, "_skillfoundry", fake_resolve)


def test_resolve_teacher_activation_binds_inputs_and_freezes_each_stage(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    install_fake_resolver(tmp_path, monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("input source", encoding="utf-8")
    outline = skillbank.resolve_teacher_activation(
        3, "outline", "outline-a1", "batch-1", (source,), batch_questions=(3, 4)
    )
    assert skillbank.validate_activation(outline, question=3, stage="outline")["attempt_id"] == "outline-a1"
    assert (
        skillbank.resolve_teacher_activation(
            3, "outline", "outline-a1", "batch-1", (source,), batch_questions=(3, 4)
        )
        == outline
    )
    with pytest.raises(ContractError, match="another frozen"):
        skillbank.resolve_teacher_activation(
            3, "outline", "outline-a2", "batch-1", (source,), batch_questions=(3, 4)
        )

    root = tmp_path / "3"
    (root / "QuestionDesignBrief.json").write_text("{}", encoding="utf-8")
    (root / "QuestionDesignBrief.md").write_text("# Brief", encoding="utf-8")
    author = skillbank.resolve_teacher_activation(
        3, "author", "author-a1", "batch-1", batch_questions=(3, 4)
    )
    value = skillbank.validate_activation(author, question=3, stage="author")
    paths = {item["path"] for item in value["prompt_bundle"]["task_inputs"]}
    assert str((root / "QuestionDesignBrief.json").resolve()) in paths
    assert str((root / "QuestionDesignBrief.md").resolve()) in paths


def test_resolve_archives_legacy_activation_and_rejects_source_drift(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    install_fake_resolver(tmp_path, monkeypatch)
    root = skillbank.ensure_question_layout(3)
    output = root / "trace/authoring/skill-activations/outline.json"
    write_json(output, {"schema_version": 1, "task_id": "q3"})
    prompt = root / "trace/authoring/prompts/outline-prompt.txt"
    prompt.write_text("legacy", encoding="utf-8")
    source = tmp_path / "source.md"
    source.write_text("first bytes", encoding="utf-8")

    resolved = skillbank.resolve_teacher_activation(
        3, "outline", "outline-a1", "batch-1", (source,), batch_questions=(3,)
    )

    assert skillbank.validate_activation(resolved, question=3, stage="outline")
    assert len(list((output.parent / "archive").glob("*/outline.json"))) == 1
    source.write_text("changed bytes", encoding="utf-8")
    with pytest.raises(ContractError, match="source bytes"):
        skillbank.resolve_teacher_activation(
            3, "outline", "outline-a1", "batch-1", (source,), batch_questions=(3,)
        )


def test_resolve_archives_previous_schema_two_batch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    install_fake_resolver(tmp_path, monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("input source", encoding="utf-8")
    first = skillbank.resolve_teacher_activation(
        3, "outline", "outline-a1", "batch-1", (source,), batch_questions=(3,)
    )
    first_sha = hashlib.sha256(first.read_bytes()).hexdigest()
    source.write_text("revised input source", encoding="utf-8")

    second = skillbank.resolve_teacher_activation(
        3, "outline", "outline-a2", "batch-2", (source,), batch_questions=(3,)
    )

    assert skillbank.validate_activation(second, question=3, stage="outline")["batch_id"] == "batch-2"
    archive = first.parent / "archive" / first_sha
    assert (archive / first.name).is_file()
    manifest = json.loads((archive / "ARCHIVE_MANIFEST.json").read_text())
    assert manifest["original_activation_sha256"] == first_sha
    assert {item["name"] for item in manifest["files"]} == {
        "outline.json",
        "outline-prompt.txt",
        "outline-stable.lock.json",
    }


def test_resolve_failure_never_publishes_partial_activation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    install_fake_resolver(tmp_path, monkeypatch)
    fake_resolve = skillbank._skillfoundry

    def fail_after_staging(command: list[str]) -> dict[str, object]:
        fake_resolve(command)
        raise ContractError("simulated resolver failure")

    monkeypatch.setattr(skillbank, "_skillfoundry", fail_after_staging)
    source = tmp_path / "source.md"
    source.write_text("input source", encoding="utf-8")
    root = skillbank.ensure_question_layout(3)

    with pytest.raises(ContractError, match="simulated resolver failure"):
        skillbank.resolve_teacher_activation(
            3, "outline", "outline-a1", "batch-1", (source,), batch_questions=(3,)
        )

    assert not (root / "trace/authoring/skill-activations/outline.json").exists()
    assert not (root / "trace/authoring/prompts/outline-prompt.txt").exists()


def test_resolve_teacher_activation_rejects_missing_inputs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    with pytest.raises(ContractError, match="explicit task inputs"):
        skillbank.resolve_teacher_activation(
            3, "outline", "outline-a1", "batch-1", batch_questions=(3,)
        )


def test_background_evidence_requirement_is_opt_in_by_stable_skill(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    skill = tmp_path / ".skillbank/execution-skills/method-selection-teacher/revisions/v5"
    skill.mkdir(parents=True)
    write_json(skill / "CONTRACT_REQUIREMENTS.json", skillbank.BACKGROUND_EVIDENCE_REQUIREMENT)
    stable = {
        "schema_version": 1,
        "generation": 6,
        "artifacts": [
            {
                "artifact_kind": "execution_skill",
                "artifact_id": "method-selection-teacher",
                "path": ".skillbank/execution-skills/method-selection-teacher/revisions/v5",
            }
        ],
    }

    assert skillbank._requires_background_evidence(stable)
    (skill / "CONTRACT_REQUIREMENTS.json").unlink()
    assert not skillbank._requires_background_evidence(stable)


def test_resolve_freezes_batch_roster_before_any_question_runs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    install_fake_resolver(tmp_path, monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("input source", encoding="utf-8")
    skillbank.resolve_teacher_activation(
        3, "outline", "outline-a1", "batch-frozen", (source,), batch_questions=(3, 4)
    )
    q4 = skillbank.resolve_teacher_activation(
        4, "outline", "outline-a1", "batch-frozen", (source,), batch_questions=(3, 4)
    )
    assert skillbank.validate_activation(q4, question=4, stage="outline")["batch_questions"] == [3, 4]

    with pytest.raises(ContractError, match="already frozen"):
        skillbank.resolve_teacher_activation(
            4, "outline", "outline-a1", "batch-frozen", (source,), batch_questions=(3, 4, 5)
        )
    write_json(tmp_path / ".skillbank/stable.lock", {"schema_version": 1, "generation": 5})
    with pytest.raises(ContractError, match="another contract"):
        skillbank.resolve_teacher_activation(
            4, "outline", "outline-a1", "batch-frozen", (source,), batch_questions=(3, 4)
        )


def attributed_failure(question: int, evidence_id: str) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "project_id": "question-from-questions",
        "task_id": f"q{question}",
        "run_id": f"run-q{question}",
        "role": "teacher",
        "task_type": "method-selection",
        "stage": "author",
        "profile": "multi-question-input",
        "outcome_class": "SCIENTIFIC_FAILURE",
        "attribution": "skill_knowledge_gap",
        "frontier": "boundary",
        "signature": {
            "stage": "author",
            "missing_responsibility": "linear_validation_session",
            "skill_failure_mechanism": "independent_blind_context",
            "proposed_target": "execution_skill",
        },
        "suggested_guidance": ["keep three Harbor rounds in one Researcher conversation"],
    }


def test_reconcile_skill_batch_clusters_two_normalized_attributions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    for question in (3, 4):
        source = tmp_path / f"q{question}-evidence.json"
        write_json(source, attributed_failure(question, f"evidence-q{question}"))
        recorded = skillbank.record_skill_attribution(question, source)
        assert recorded.is_file()

    result = skillbank.reconcile_skill_batch("batch-1", (3, 4))

    assert len(result["patterns"]) == 1
    assert len(result["lessons"]) == 1
    assert result["lessons"][0]["proposed_target"] == "execution_skill"
    assert (tmp_path / ".skillbank/evolution/batches/batch-1/evidence-manifest.json").is_file()
    assert skillbank.reconcile_skill_batch("batch-1", (3, 4)) == result

    extra = tmp_path / "q3-extra.json"
    write_json(extra, attributed_failure(3, "new-evidence-q3"))
    skillbank.record_skill_attribution(3, extra)
    with pytest.raises(ContractError, match="different evidence"):
        skillbank.reconcile_skill_batch("batch-1", (3, 4))


def test_batch_completion_records_reconcile_even_without_eligible_failures(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)

    result = skillbank.reconcile_skill_batch_if_ready("batch-clean", (3, 4))

    assert result["status"] == "NO_ELIGIBLE_EVIDENCE"
    assert result["evidence_count"] == 0
    assert (
        tmp_path / ".skillbank/evolution/batches/batch-clean/reconcile-result.json"
    ).is_file()

    source = tmp_path / "q3-only.json"
    write_json(source, attributed_failure(3, "q3-only"))
    skillbank.record_skill_attribution(3, source)
    insufficient = skillbank.reconcile_skill_batch_if_ready("batch-single", (3, 4))
    assert insufficient["status"] == "INSUFFICIENT_FAILURE_PATTERN"
    assert skillbank.reconcile_skill_batch_if_ready("batch-single", (3, 4)) == insufficient

def test_activation_keeps_its_frozen_stable_generation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    install_fake_resolver(tmp_path, monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("input source", encoding="utf-8")
    activation = skillbank.resolve_teacher_activation(
        3, "outline", "outline-a1", "batch-1", (source,), batch_questions=(3,)
    )
    write_json(tmp_path / ".skillbank/stable.lock", {"schema_version": 1, "generation": 5})

    value = skillbank.validate_activation(activation, question=3, stage="outline")

    assert value["stable_generation"] == 4
    assert value["stable_lock_snapshot"]["generation"] == 4


def test_activation_keeps_frozen_knowledge_after_rule_source_changes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    install_fake_resolver(tmp_path, monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("input source", encoding="utf-8")
    activation = skillbank.resolve_teacher_activation(
        3, "outline", "outline-a1", "batch-1", (source,), batch_questions=(3,)
    )

    (tmp_path / "RULE.md").write_text("next stable rule revision", encoding="utf-8")

    value = skillbank.validate_activation(activation, question=3, stage="outline")
    assert value["stable_generation"] == 4


def test_attribution_rejects_platform_failure_as_skill_evolution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    source = tmp_path / "platform.json"
    value = attributed_failure(3, "platform")
    value["attribution"] = "platform_failure"
    write_json(source, value)
    with pytest.raises(ContractError, match="Skill-responsible"):
        skillbank.record_skill_attribution(3, source)


def test_attribution_is_idempotent_and_rejects_wrong_frontier_or_signature(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    source = tmp_path / "evidence.json"
    value = attributed_failure(3, "evidence")
    write_json(source, value)
    first = skillbank.record_skill_attribution(3, source)
    assert skillbank.record_skill_attribution(3, source) == first

    value["frontier"] = "too_easy"
    write_json(source, value)
    with pytest.raises(ContractError, match="informative or boundary"):
        skillbank.record_skill_attribution(3, source)
    value["frontier"] = "boundary"
    value["signature"] = {}
    write_json(source, value)
    with pytest.raises(ContractError, match="diagnostic signature"):
        skillbank.record_skill_attribution(3, source)


def test_reconcile_rejects_bad_batch_identity_or_empty_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    with pytest.raises(ContractError, match="batch_id"):
        skillbank.reconcile_skill_batch("bad batch", (3,))
    with pytest.raises(ContractError, match="attributed evidence"):
        skillbank.reconcile_skill_batch("batch", (3,))


def test_reconcile_does_not_count_one_question_twice(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skillbank, "QUESTION_ROOT", tmp_path)
    source = tmp_path / "evidence.json"
    write_json(source, attributed_failure(3, "evidence-q3"))
    skillbank.record_skill_attribution(3, source)

    with pytest.raises(ContractError, match="two distinct question"):
        skillbank.reconcile_skill_batch("batch-duplicate", (3, 3))


@pytest.mark.parametrize(
    "returncode,stdout,stderr,message",
    [
        (2, "", "broken", "failed"),
        (0, "not-json", "", "invalid JSON"),
        (0, "[]", "", "must be an object"),
    ],
)
def test_skillfoundry_adapter_fail_closed(monkeypatch, returncode, stdout, stderr, message) -> None:
    result = type("Result", (), {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    })()
    monkeypatch.setattr(skillbank.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(ContractError, match=message):
        skillbank._skillfoundry(["resolve"])

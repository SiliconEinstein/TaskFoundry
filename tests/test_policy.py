from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskfoundry.model import ContractError
from taskfoundry.policy import PolicyRepository, RuleObservation


def repository(tmp_path: Path) -> PolicyRepository:
    root = tmp_path / "policies"
    typed = root / "question-types" / "method-selection"
    typed.mkdir(parents=True)
    (root / "artifact-contract-v1.md").write_text("artifact\n", encoding="utf-8")
    (typed / "v1.md").write_text("typed\n", encoding="utf-8")
    return PolicyRepository(root)


def test_lock_resolves_exact_hashes(tmp_path) -> None:
    repo = repository(tmp_path)
    lock = repo.lock("method-selection")
    assert len(lock.artifact_contract.sha256) == 64
    assert lock.question_type.policy_id == "method-selection"
    output = tmp_path / "lock.json"
    repo.write_lock(lock, output)
    assert json.loads(output.read_text())["schema_version"] == 1


def test_missing_policy_is_rejected(tmp_path) -> None:
    with pytest.raises(ContractError, match="unavailable"):
        repository(tmp_path).lock("unknown")


def test_amendment_requires_evidence(tmp_path) -> None:
    repo = repository(tmp_path)
    with pytest.raises(ContractError, match="evidence"):
        repo.propose(
            amendment_id="missing-evidence",
            question_type="method-selection",
            parent_policy_sha256="a" * 64,
            problem="problem",
            rule_change="change",
            evidence_refs=(),
        )


def test_amendment_is_immutable_and_review_is_additive(tmp_path) -> None:
    repo = repository(tmp_path)
    parent = repo.lock("method-selection").question_type.sha256
    kwargs = {
        "amendment_id": "better-holdout",
        "question_type": "method-selection",
        "parent_policy_sha256": parent,
        "problem": "random split leaked families",
        "rule_change": "require grouped holdout",
        "evidence_refs": ("run-1/attempt-2",),
    }
    proposed = repo.propose(**kwargs)
    assert repo.propose(**kwargs) == proposed
    accepted = repo.decide("better-holdout", "accepted")
    assert proposed.exists() and accepted.exists()
    lock = repo.lock("method-selection")
    assert [item.policy_id for item in lock.amendments] == ["better-holdout.accepted"]


def test_amendment_id_cannot_be_reused(tmp_path) -> None:
    repo = repository(tmp_path)
    base = {
        "amendment_id": "same-id",
        "question_type": "method-selection",
        "parent_policy_sha256": repo.lock("method-selection").question_type.sha256,
        "problem": "problem",
        "rule_change": "change",
        "evidence_refs": ("evidence",),
    }
    repo.propose(**base)
    with pytest.raises(ContractError, match="different content"):
        repo.propose(**(base | {"problem": "different"}))


@pytest.mark.parametrize("status", ["proposed", "invalid"])
def test_decide_rejects_invalid_status(tmp_path, status) -> None:
    with pytest.raises(ContractError, match="accepted or rejected"):
        repository(tmp_path).decide("unknown", status)


def test_amendment_cannot_have_conflicting_decisions(tmp_path) -> None:
    repo = repository(tmp_path)
    repo.propose(
        amendment_id="one-truth",
        question_type="method-selection",
        parent_policy_sha256=repo.lock("method-selection").question_type.sha256,
        problem="problem",
        rule_change="change",
        evidence_refs=("evidence",),
    )
    repo.decide("one-truth", "accepted")
    with pytest.raises(ContractError, match="opposite"):
        repo.decide("one-truth", "rejected")


def test_rule_learning_requires_regression_case(tmp_path) -> None:
    repo = repository(tmp_path)
    observation = RuleObservation(
        observation_id="grouped-holdout",
        run_id="q17-r2",
        question_type="method-selection",
        problem="随机划分泄漏了同族样例",
        proposed_rule="必须按科学家族划分隐藏集",
        evidence_refs=("runs/q17/r2.json",),
        regression_refs=("tests/regressions/grouped-holdout.json",),
    )
    path = repo.learn_from_observation(
        observation,
        parent_policy_sha256=repo.lock("method-selection").question_type.sha256,
    )
    value = json.loads(path.read_text())
    assert value["regression_refs"] == ["tests/regressions/grouped-holdout.json"]
    assert value["problem"].startswith("run=q17-r2")


def test_rule_learning_rejects_observation_without_regression(tmp_path) -> None:
    observation = RuleObservation(
        observation_id="missing-regression",
        run_id="q17",
        question_type="method-selection",
        problem="问题",
        proposed_rule="规则",
        evidence_refs=("evidence",),
        regression_refs=(),
    )
    with pytest.raises(ContractError, match="regression"):
        observation.validate()

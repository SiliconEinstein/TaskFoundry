from __future__ import annotations

import json

import pytest

from taskfoundry.model import (
    Actor,
    ContractError,
    QuestionDesignBrief,
    RunState,
    SourceQuestion,
)
from taskfoundry.store import RunStore, SequenceConflict


def valid_brief() -> QuestionDesignBrief:
    sources = (
        SourceQuestion("q1", "p1", "goal one", "method one", "candidate"),
        SourceQuestion("q2", "p2", "goal two", "method two", "metric"),
    )
    return QuestionDesignBrief(
        brief_id="brief-1",
        question_type="method-selection",
        title="Choose a method",
        scientific_goal="Select a transferable method",
        research_object="held-out regimes",
        source_questions=sources,
        method_space=("m1", "m2"),
        evidence_roles={"q1": "candidate", "q2": "metric"},
        public_inputs=("training.csv",),
        required_outputs=("result.csv",),
        hidden_evaluation_axes=("transfer",),
        environment_capabilities=("python",),
        difficulty_hypothesis="Requires grouped generalization",
        solvability_argument="Public complete families identify the relation",
    )


def test_brief_validates_and_serializes() -> None:
    value = valid_brief().to_dict()
    assert value["schema_version"] == 1
    assert value["source_questions"][0]["source_id"] == "q1"
    assert QuestionDesignBrief.from_dict(value) == valid_brief()


def test_brief_rejects_solution_target_over_half_hour() -> None:
    value = valid_brief().to_dict()
    value["target_solution_time_sec"] = 1801
    with pytest.raises(ContractError, match="within 1800"):
        QuestionDesignBrief.from_dict(value)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"source_questions": ()}, "source_questions"),
        ({"method_space": ()}, "method_space"),
        ({"public_inputs": ()}, "public_inputs"),
    ],
)
def test_brief_rejects_incomplete_contract(change: dict, message: str) -> None:
    values = valid_brief().__dict__ | change
    with pytest.raises(ContractError, match=message):
        QuestionDesignBrief(**values).validate()


def test_store_commits_event_before_snapshot(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    initial = store.initialize("run-1")
    next_snapshot = store.advance(initial, state=RunState.POLICIES_LOCKED)
    event, saved = store.commit(
        actor=Actor.TEACHER,
        event_type="policies.locked",
        idempotency_key="lock-1",
        payload={"digest": "abc"},
        snapshot=next_snapshot,
    )
    assert event.sequence == saved.sequence == 1
    assert store.read_snapshot() == saved
    assert store.events() == [event]


def test_store_idempotent_commit_rejects_different_operation(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    initial = store.initialize("run-1")
    first = store.advance(initial, state=RunState.POLICIES_LOCKED)
    expected_event, _ = store.commit(
        actor=Actor.TEACHER,
        event_type="policies.locked",
        idempotency_key="same-key",
        payload={},
        snapshot=first,
    )
    stale_retry = store.advance(first, state=RunState.ENVIRONMENT_DISCOVERY)
    with pytest.raises(ContractError, match="another operation"):
        store.commit(
            actor=Actor.TEACHER,
            event_type="ignored.retry",
            idempotency_key="same-key",
            payload={"ignored": True},
            snapshot=stale_retry,
        )

    event, snapshot = store.commit(
        actor=Actor.TEACHER,
        event_type="policies.locked",
        idempotency_key="same-key",
        payload={},
        snapshot=stale_retry,
    )
    assert event == expected_event
    assert snapshot.state is RunState.POLICIES_LOCKED


def test_store_recovers_snapshot_when_crash_follows_journal_append(tmp_path, monkeypatch) -> None:
    store = RunStore(tmp_path / "run")
    initial = store.initialize("run-1")
    target = store.advance(initial, state=RunState.POLICIES_LOCKED)
    original = store._write_snapshot

    def crash(*args, **kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr(store, "_write_snapshot", crash)
    with pytest.raises(OSError, match="simulated crash"):
        store.commit(
            actor=Actor.TEACHER,
            event_type="policies.locked",
            idempotency_key="recoverable",
            payload={},
            snapshot=target,
        )
    monkeypatch.setattr(store, "_write_snapshot", original)
    assert store.read_snapshot() == target


def test_store_rejects_sequence_conflict(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    initial = store.initialize("run-1")
    invalid = store.advance(store.advance(initial))
    with pytest.raises(SequenceConflict):
        store.commit(
            actor=Actor.SYSTEM,
            event_type="invalid",
            idempotency_key="invalid",
            payload={},
            snapshot=invalid,
        )


def test_store_rejects_corrupt_journal(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    bad = {
        "schema_version": 1,
        "sequence": 2,
        "timestamp": "now",
        "actor": "teacher",
        "event_type": "bad",
        "idempotency_key": "bad",
        "payload": {},
    }
    store.events_path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(ContractError, match="sequence gap"):
        store.events()


def test_initialize_refuses_different_run(tmp_path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize("run-1")
    with pytest.raises(ContractError, match="different run"):
        store.initialize("run-2")

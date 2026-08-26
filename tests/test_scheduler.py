from __future__ import annotations

from pathlib import Path

import pytest

from taskfoundry.model import Actor, RunState
from taskfoundry.model import ContractError
from taskfoundry.scheduler import QuestionScheduler, QueueState
from taskfoundry.store import RunStore


def _run(root: Path, question_id: str) -> Path:
    path = root / question_id
    RunStore(path).initialize(question_id)
    return path


def _set_state(run_dir: Path, state: RunState, key: str) -> None:
    store = RunStore(run_dir)
    current = store.read_snapshot()
    assert current is not None
    store.commit(
        actor=Actor.SYSTEM,
        event_type="test.state.changed",
        idempotency_key=key,
        payload={},
        snapshot=store.advance(current, state=state),
    )


def test_scheduler_activates_five_questions_by_default(tmp_path: Path) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    for index in range(1, 7):
        scheduler.enqueue(f"q{index}", _run(tmp_path, f"q{index}"))

    snapshot = scheduler.snapshot()
    active = [item.question_id for item in snapshot.questions if item.state is QueueState.ACTIVE]
    waiting = [item.question_id for item in snapshot.questions if item.state is QueueState.WAITING]

    assert active == ["q1", "q2", "q3", "q4", "q5"]
    assert waiting == ["q6"]


def test_scheduler_can_raise_teacher_limit_and_fill_new_slots(tmp_path: Path) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    for index in range(1, 8):
        scheduler.enqueue(f"q{index}", _run(tmp_path, f"q{index}"))

    result = scheduler.configure(max_active=7)

    assert result.activated == ("q6", "q7")
    assert result.snapshot.max_active == 7


def test_scheduler_lower_limit_does_not_stop_active_questions(tmp_path: Path) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    runs = {f"q{index}": _run(tmp_path, f"q{index}") for index in range(1, 7)}
    for question_id, run_dir in runs.items():
        scheduler.enqueue(question_id, run_dir)

    lowered = scheduler.configure(max_active=2)

    assert lowered.activated == ()
    assert lowered.snapshot.max_active == 2
    assert sum(item.state is QueueState.ACTIVE for item in lowered.snapshot.questions) == 5
    _set_state(runs["q1"], RunState.COMPLETED, "test:q1:completed")
    after_one_release = scheduler.tick()
    assert after_one_release.activated == ()
    assert sum(item.state is QueueState.ACTIVE for item in after_one_release.snapshot.questions) == 4


@pytest.mark.parametrize("limit", [0, 201])
def test_scheduler_rejects_teacher_limit_outside_supported_range(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ContractError, match="between 1 and 200"):
        QuestionScheduler(tmp_path / "scheduler").configure(max_active=limit)


def test_scheduler_releases_completed_question_and_fills_next(tmp_path: Path) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    runs = {f"q{index}": _run(tmp_path, f"q{index}") for index in range(1, 7)}
    for question_id, run_dir in runs.items():
        scheduler.enqueue(question_id, run_dir)
    _set_state(runs["q2"], RunState.COMPLETED, "test:q2:completed")

    result = scheduler.tick()

    assert result.released == ("q2",)
    assert result.activated == ("q6",)
    states = {item.question_id: item.state for item in result.snapshot.questions}
    assert states["q2"] is QueueState.COMPLETED
    assert states["q6"] is QueueState.ACTIVE


def test_scheduler_defers_timeout_and_fills_next(tmp_path: Path) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    runs = {f"q{index}": _run(tmp_path, f"q{index}") for index in range(1, 7)}
    for question_id, run_dir in runs.items():
        scheduler.enqueue(question_id, run_dir)
    _set_state(runs["q1"], RunState.DEFERRED_TIMEOUT, "test:q1:deferred")

    result = scheduler.tick()

    assert result.released == ("q1",)
    assert result.activated == ("q6",)
    states = {item.question_id: item.state for item in result.snapshot.questions}
    assert states["q1"] is QueueState.DEFERRED
    assert states["q6"] is QueueState.ACTIVE

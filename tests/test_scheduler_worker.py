from pathlib import Path

from taskfoundry.codex_agent import DispatchReceipt
from taskfoundry.model import Actor
from taskfoundry.scheduler import QuestionScheduler, QueueState
from taskfoundry.scheduler_worker import SchedulerWorker
from taskfoundry.store import RunStore


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    def queue(self, *, role, thread_id, prompt_path):
        assert role is Actor.TEACHER
        assert prompt_path.is_file()
        self.calls += 1
        return DispatchReceipt(f"message-{self.calls}", thread_id, role)


class FailingDispatcher:
    def queue(self, *, role, thread_id, prompt_path):
        raise RuntimeError("control plane unavailable")


def _configured_scheduler(tmp_path: Path) -> QuestionScheduler:
    run = tmp_path / "run"
    RunStore(run).initialize("q3")
    prompt = tmp_path / "teacher.md"
    prompt.write_text("[TASKFOUNDRY ROLE=teacher]\n继续正式出题。\n")
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    scheduler.enqueue(
        "q3",
        run,
        teacher_thread_id="teacher-thread",
        teacher_prompt_path=prompt,
    )
    return scheduler


def test_worker_dispatches_one_atomically_claimed_lease(tmp_path: Path) -> None:
    scheduler = _configured_scheduler(tmp_path)
    dispatcher = FakeDispatcher()

    outcomes = SchedulerWorker(scheduler, dispatcher, worker_id="worker-a").run_once()

    assert outcomes[0].status == "DISPATCHED"
    item = next(value for value in scheduler.snapshot().questions if value.question_id == "q3")
    assert item.state is QueueState.LEASED
    assert item.lease is not None and item.lease.dispatch_message_id == "message-1"


def test_two_workers_do_not_repeat_same_dispatch(tmp_path: Path) -> None:
    scheduler = _configured_scheduler(tmp_path)
    dispatcher = FakeDispatcher()
    first = SchedulerWorker(scheduler, dispatcher, worker_id="worker-a")
    second = SchedulerWorker(scheduler, dispatcher, worker_id="worker-b")

    assert first.run_once()[0].status == "DISPATCHED"
    assert second.run_once() == ()
    assert dispatcher.calls == 1


def test_unconfigured_question_does_not_consume_slot(tmp_path: Path) -> None:
    run = tmp_path / "run"
    RunStore(run).initialize("q3")
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    scheduler.enqueue("q3", run)

    outcomes = SchedulerWorker(scheduler, FakeDispatcher(), worker_id="worker-a").run_once()

    assert outcomes == ()
    item = next(value for value in scheduler.snapshot().questions if value.question_id == "q3")
    assert item.state is QueueState.READY
    assert item.lease is None


def test_dispatch_failure_releases_lease_for_recovery(tmp_path: Path) -> None:
    scheduler = _configured_scheduler(tmp_path)

    outcomes = SchedulerWorker(
        scheduler,
        FailingDispatcher(),
        worker_id="worker-a",
    ).run_once()

    assert outcomes[0].status == "RECOVERY_REQUIRED"
    item = next(value for value in scheduler.snapshot().questions if value.question_id == "q3")
    assert item.state is QueueState.RECOVERY_REQUIRED
    assert item.lease is None


def test_worker_skips_lease_lost_to_another_dispatch_worker(tmp_path: Path) -> None:
    scheduler = _configured_scheduler(tmp_path)

    class LostRaceScheduler:
        def tick(self):
            return scheduler.tick()

        def claim_dispatch(self, question_id, *, worker_id):
            assert question_id == "q3"
            return None

    outcomes = SchedulerWorker(
        LostRaceScheduler(),  # type: ignore[arg-type]
        FakeDispatcher(),
        worker_id="worker-b",
    ).run_once()

    assert outcomes == ()

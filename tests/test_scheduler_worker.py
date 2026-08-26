from pathlib import Path

from taskfoundry.codex_agent import DispatchReceipt
from taskfoundry.model import Actor
from taskfoundry.scheduler import QuestionScheduler, QueueState
from taskfoundry.scheduler_worker import SchedulerWorker
from taskfoundry.store import RunStore


class FakeDispatcher:
    def queue(self, *, role, thread_id, prompt_path):
        assert role is Actor.TEACHER
        assert prompt_path.is_file()
        return DispatchReceipt("message-1", thread_id, role)


def test_worker_dispatches_active_unacknowledged_question(tmp_path: Path) -> None:
    run = tmp_path / "run"
    RunStore(run).initialize("q1")
    prompt = tmp_path / "teacher.md"
    prompt.write_text("[TASKFOUNDRY ROLE=teacher]\n继续正式出题。\n")
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    scheduler.enqueue(
        "q1",
        run,
        teacher_thread_id="teacher-thread",
        teacher_prompt_path=prompt,
    )

    outcomes = SchedulerWorker(scheduler, FakeDispatcher()).run_once()

    assert outcomes[0].status == "DISPATCHED"
    item = scheduler.snapshot().questions[0]
    assert item.state is QueueState.ACTIVE
    assert item.dispatch_message_id == "message-1"


def test_worker_does_not_repeat_acknowledged_dispatch(tmp_path: Path) -> None:
    run = tmp_path / "run"
    RunStore(run).initialize("q1")
    prompt = tmp_path / "teacher.md"
    prompt.write_text("[TASKFOUNDRY ROLE=teacher]\n继续正式出题。\n")
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    scheduler.enqueue(
        "q1",
        run,
        teacher_thread_id="teacher-thread",
        teacher_prompt_path=prompt,
    )
    worker = SchedulerWorker(scheduler, FakeDispatcher())
    worker.run_once()

    assert worker.run_once() == ()


def test_worker_requires_recovery_without_dispatch_config(tmp_path: Path) -> None:
    run = tmp_path / "run"
    RunStore(run).initialize("q1")
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    scheduler.enqueue("q1", run)

    outcomes = SchedulerWorker(scheduler, FakeDispatcher()).run_once()

    assert outcomes[0].status == "RECOVERY_REQUIRED"
    assert scheduler.snapshot().questions[0].state is QueueState.RECOVERY_REQUIRED

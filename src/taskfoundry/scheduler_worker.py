"""把持久题目调度决定派发给受信 Codex Teacher 控制面。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .codex_agent import CodexDispatcher
from .model import Actor, ContractError
from .scheduler import QuestionScheduler, QueueState
from .scheduler_state import DispatchState


@dataclass(frozen=True)
class DispatchOutcome:
    """一次自动补位派发的结果。"""

    question_id: str
    status: str
    message_id: str | None = None
    error: str | None = None


class SchedulerWorker:
    """执行一次同步、自动补位和幂等 Teacher 派发。"""

    def __init__(
        self,
        scheduler: QuestionScheduler,
        dispatcher: CodexDispatcher | None = None,
        *,
        worker_id: str | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.dispatcher = dispatcher or CodexDispatcher()
        self.worker_id = worker_id or f"scheduler-worker:{os.getpid()}"

    def run_once(self) -> tuple[DispatchOutcome, ...]:
        """推进队列并派发本轮新激活题目。"""
        result = self.scheduler.tick()
        outcomes: list[DispatchOutcome] = []
        pending = tuple(
            item
            for item in result.snapshot.questions
            if item.state is QueueState.LEASED
            and item.lease is not None
            and item.lease.dispatch_state is DispatchState.PENDING
        )
        for pending_item in pending:
            item = self.scheduler.claim_dispatch(pending_item.question_id, worker_id=self.worker_id)
            if item is None:
                continue
            question_id = item.question_id
            lease = item.lease
            assert lease is not None and item.teacher_thread_id and item.teacher_prompt_path
            try:
                receipt = self.dispatcher.queue(
                    role=Actor.TEACHER,
                    thread_id=item.teacher_thread_id,
                    prompt_path=Path(item.teacher_prompt_path),
                )
            except (ContractError, RuntimeError, OSError) as error:
                self.scheduler.require_recovery(
                    question_id,
                    worker_id=self.worker_id,
                    owner_id=lease.owner_id,
                    lease_id=lease.lease_id,
                    generation=item.generation,
                )
                outcomes.append(DispatchOutcome(question_id, "RECOVERY_REQUIRED", error=str(error)))
                continue
            self.scheduler.record_dispatch(
                question_id,
                receipt.message_id,
                worker_id=self.worker_id,
                owner_id=lease.owner_id,
                lease_id=lease.lease_id,
                generation=item.generation,
            )
            outcomes.append(DispatchOutcome(question_id, "DISPATCHED", message_id=receipt.message_id))
        return tuple(outcomes)

"""把持久题目调度决定派发给受信 Codex Teacher 控制面。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .codex_agent import CodexDispatcher
from .model import Actor, ContractError
from .scheduler import QuestionScheduler, QueueState


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
    ) -> None:
        self.scheduler = scheduler
        self.dispatcher = dispatcher or CodexDispatcher()

    def run_once(self) -> tuple[DispatchOutcome, ...]:
        """推进队列并派发本轮新激活题目。"""
        result = self.scheduler.tick()
        by_id = {item.question_id: item for item in result.snapshot.questions}
        outcomes: list[DispatchOutcome] = []
        pending = tuple(
            item.question_id
            for item in result.snapshot.questions
            if item.state is QueueState.ACTIVE and item.dispatch_message_id is None
        )
        for question_id in pending:
            item = by_id[question_id]
            if not item.teacher_thread_id or not item.teacher_prompt_path:
                self.scheduler.require_recovery(question_id)
                outcomes.append(
                    DispatchOutcome(question_id, "RECOVERY_REQUIRED", error="Teacher dispatch is not configured")
                )
                continue
            try:
                receipt = self.dispatcher.queue(
                    role=Actor.TEACHER,
                    thread_id=item.teacher_thread_id,
                    prompt_path=Path(item.teacher_prompt_path),
                )
            except (ContractError, RuntimeError, OSError) as error:
                self.scheduler.require_recovery(question_id)
                outcomes.append(DispatchOutcome(question_id, "RECOVERY_REQUIRED", error=str(error)))
                continue
            self.scheduler.record_dispatch(question_id, receipt.message_id)
            outcomes.append(DispatchOutcome(question_id, "DISPATCHED", message_id=receipt.message_id))
        return tuple(outcomes)

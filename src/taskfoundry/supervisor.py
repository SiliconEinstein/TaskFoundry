"""Durable owner for persistent Harbor validation and typed recovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sys
import time
from typing import Callable

from .labwright import atomic_json
from .model import Actor, ContractError, RunState
from .model_probe import StrongModelProbe
from .policy import file_sha256
from .recovery import CircuitState, RecoveryAction, RecoveryDecision, RecoveryLedger
from .store import RunStore
from .skillbank import close_skill_batch, record_revision_attribution
from .supervisor_json import parse_time, read_object
from .supervisor_process import LocalProcessAdapter, ProcessAdapter
from .supervisor_state import (
    SupervisorPhase,
    SupervisorPlan,
    SupervisorSnapshot,
    SupervisorStateStore,
)
from .validation_control import TeacherDecision
from .workflow import RunWorkflow, WorkflowError


@dataclass(frozen=True)
class AdvanceResult:
    """One observable transition (or stable wait) from ``run_once``."""

    question_id: str
    phase: str
    action: str
    detail: str = ""


class CampaignSupervisor:
    """Advance registered questions through Harbor without Desktop task handoffs."""

    def __init__(
        self,
        runs_root: Path,
        *,
        recovery_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        process_adapter: ProcessAdapter | None = None,
        model_probe: StrongModelProbe | None = None,
    ) -> None:
        if not runs_root.is_absolute():
            raise ContractError("Supervisor runs root must be absolute")
        self.runs_root = runs_root
        self.clock = clock or (lambda: datetime.now(UTC))
        self.processes = process_adapter or LocalProcessAdapter()
        self.model_probe = model_probe or StrongModelProbe()
        self.recovery = RecoveryLedger(
            recovery_root or runs_root / ".campaign-recovery",
            clock=self.clock,
        )

    def run_once(self) -> tuple[AdvanceResult, ...]:
        """Advance every registered plan at most one durable transition."""
        self._probe_model_transport_if_due()
        self._resume_closed_model_waits()
        return tuple(self._advance(path) for path in self._plan_paths())

    def run_forever(
        self,
        *,
        poll_interval_sec: float = 1.0,
        stop_path: Path | None = None,
    ) -> None:
        """Keep ownership until a caller-created stop file appears."""
        if not 0.1 <= poll_interval_sec <= 60:
            raise ContractError("Supervisor poll interval must be 0.1..60 seconds")
        while stop_path is None or not stop_path.exists():
            self.run_once()
            time.sleep(poll_interval_sec)

    def resume(self, question_id: str) -> AdvanceResult:
        """Resume one satisfied wait or publish one Teacher-owned action."""
        plan_path = self._plan_for(question_id)
        plan = SupervisorPlan.from_path(plan_path)
        store = SupervisorStateStore(Path(plan.run_dir))
        with store.locked():
            current = store.read_unlocked()
            if current.phase is SupervisorPhase.TEACHER_ACTION:
                if current.process_id is None and current.recovery_condition == "revision attribution required":
                    return self._apply_terminal_attribution(plan, store, current)
                return self._apply_teacher_action(plan, store, current)
            if current.phase is not SupervisorPhase.WAITING_EXTERNAL:
                raise ContractError("question is not waiting for an external recovery")
            circuit = self.recovery.circuit()
            if (
                current.last_failure_stage == "MODEL_CONNECTION"
                and circuit.state is not CircuitState.CLOSED
            ):
                raise ContractError("model transport circuit has not recovered")
            next_snapshot = replace(
                current,
                phase=SupervisorPhase.READY,
                sequence=current.sequence + 1,
                updated_at=self._now(),
                next_action_at=None,
                recovery_condition=None,
                process_id=None,
                process_started_at=None,
            )
            store.write_unlocked(next_snapshot)
        return AdvanceResult(question_id, next_snapshot.phase.value, "RESUMED")

    def _advance(self, plan_path: Path) -> AdvanceResult:
        plan = SupervisorPlan.from_path(plan_path)
        store = SupervisorStateStore(Path(plan.run_dir))
        with store.locked():
            current = store.read_unlocked()
            if current.phase is SupervisorPhase.READY:
                return self._launch(plan, store, current)
            if current.phase is SupervisorPhase.RETRY_SCHEDULED:
                if parse_time(str(current.next_action_at)) <= self.clock():
                    return self._launch(plan, store, current)
                return AdvanceResult(plan.question_id, current.phase.value, "WAIT_RETRY")
            if current.phase is SupervisorPhase.RUNNING:
                return self._drive(plan, store, current)
            return AdvanceResult(plan.question_id, current.phase.value, "NO_CHANGE")

    def _launch(
        self,
        plan: SupervisorPlan,
        state_store: SupervisorStateStore,
        current: SupervisorSnapshot,
    ) -> AdvanceResult:
        self._validate_launch_inputs(plan)
        workflow = RunWorkflow(RunStore(Path(plan.run_dir)))
        run = workflow.snapshot
        if run.state is RunState.AUTHORING:
            run = workflow.freeze_and_start_validation_session(
                Actor.TEACHER,
                package=Path(plan.package_path),
                validation_session_id=plan.validation_session_id,
                researcher_thread_id=None,
                idempotency_key=f"supervisor:freeze:{plan.validation_session_id}",
            )
        if run.state is not RunState.BLIND_VALIDATION:
            raise ContractError(
                f"Supervisor cannot launch Harbor from run state {run.state.value}"
            )
        attempt = current.runtime_attempt + 1
        request_id = f"{plan.validation_session_id}-runtime-{attempt:03d}"
        config_path = self._materialize_job_config(plan, Path(str(run.package_path)))
        handoff = workflow.issue_researcher_request(
            Actor.TEACHER,
            request_id=request_id,
            job_config_path=config_path,
        )
        handoff_path = Path(handoff.request_path).parent / "handoff.json"
        atomic_json(handoff_path, asdict(handoff))
        receipt_path = handoff_path.with_name("researcher-receipt.json")
        command = self._worker_command(plan, handoff_path, receipt_path)
        process_id = self.processes.launch(
            command,
            cwd=Path(plan.run_dir),
            stdout_path=handoff_path.with_name("supervisor-worker.stdout.log"),
            stderr_path=handoff_path.with_name("supervisor-worker.stderr.log"),
        )
        next_snapshot = replace(
            current,
            phase=SupervisorPhase.RUNNING,
            sequence=current.sequence + 1,
            runtime_attempt=attempt,
            updated_at=self._now(),
            request_id=request_id,
            handoff_path=str(handoff_path.resolve()),
            receipt_path=str(receipt_path.resolve()),
            process_id=process_id,
            process_started_at=self._now(),
            next_action_at=None,
            last_failure_stage=None,
            recovery_condition=None,
        )
        state_store.write_unlocked(next_snapshot)
        return AdvanceResult(plan.question_id, next_snapshot.phase.value, "HARBOR_STARTED")

    def _drive(
        self,
        plan: SupervisorPlan,
        state_store: SupervisorStateStore,
        current: SupervisorSnapshot,
    ) -> AdvanceResult:
        decided = self._publish_automatic_decision(plan, current)
        if decided is not None:
            state_store.write_unlocked(decided)
            action = (
                "TEACHER_HINT_REQUIRED"
                if decided.phase is SupervisorPhase.TEACHER_ACTION
                else "ROUND_DECIDED"
            )
            return AdvanceResult(plan.question_id, decided.phase.value, action)
        receipt_path = Path(str(current.receipt_path))
        if receipt_path.is_file():
            return self._finish_worker(plan, state_store, current, receipt_path)
        if self.processes.alive(int(current.process_id or 0)):
            return AdvanceResult(plan.question_id, current.phase.value, "WORKER_RUNNING")
        evidence_path = self._write_lost_worker_evidence(plan, current)
        return self._recover_failure(
            plan,
            state_store,
            current,
            failure_stage="PLATFORM",
            evidence_path=evidence_path,
        )

    def _publish_automatic_decision(
        self, plan: SupervisorPlan, current: SupervisorSnapshot
    ) -> SupervisorSnapshot | None:
        request_dir = Path(str(current.handoff_path)).parent
        controller = request_dir / "validation-control"
        index = current.last_decided_round + 1
        result_path = controller / f"round-{index:02d}-result.json"
        if not result_path.is_file():
            return None
        result = read_object(result_path)
        if result.get("classification") != "SCIENTIFIC_RESULT":
            return None
        phase = result.get("phase")
        score = result.get("reward")
        if type(score) not in (int, float):
            raise ContractError("persistent scientific result lacks a numeric reward")
        if float(score) >= plan.pass_threshold:
            action = "STOP_TOO_EASY" if phase == "BLIND" else "STOP_PASSED"
        elif phase == "BLIND" and index < 3:
            action = "CONTINUE_BLIND"
        elif phase == "HINT" and index >= 5:
            action = "STOP_BLOCKED"
        else:
            return replace(
                current,
                phase=SupervisorPhase.TEACHER_ACTION,
                sequence=current.sequence + 1,
                updated_at=self._now(),
                scientific_round=index,
            )
        RunWorkflow(RunStore(Path(plan.run_dir))).decide_persistent_validation_round(
            Actor.TEACHER,
            request_id=str(current.request_id),
            round_index=index,
            decision=TeacherDecision(action=action),
        )
        return replace(
            current,
            sequence=current.sequence + 1,
            updated_at=self._now(),
            last_decided_round=index,
            scientific_round=index + 1,
        )

    def _apply_teacher_action(
        self,
        plan: SupervisorPlan,
        state_store: SupervisorStateStore,
        current: SupervisorSnapshot,
    ) -> AdvanceResult:
        action_path = Path(plan.run_dir) / "supervisor" / "teacher-action.json"
        value = read_object(action_path)
        allowed = {"CONTINUE_HINT", "STOP_BLOCKED"}
        if value.get("action") not in allowed:
            raise ContractError("Teacher action must continue with a hint or stop")
        decision = TeacherDecision(
            action=str(value["action"]),
            hint=value.get("hint"),
            teacher_declares_non_answer=value.get("teacher_declares_non_answer"),
        )
        RunWorkflow(RunStore(Path(plan.run_dir))).decide_persistent_validation_round(
            Actor.TEACHER,
            request_id=str(current.request_id),
            round_index=current.scientific_round,
            decision=decision,
        )
        next_snapshot = replace(
            current,
            phase=SupervisorPhase.RUNNING,
            sequence=current.sequence + 1,
            updated_at=self._now(),
            last_decided_round=current.scientific_round,
            scientific_round=current.scientific_round + 1,
        )
        state_store.write_unlocked(next_snapshot)
        return AdvanceResult(plan.question_id, next_snapshot.phase.value, "TEACHER_ACTION_APPLIED")

    def _finish_worker(
        self,
        plan: SupervisorPlan,
        state_store: SupervisorStateStore,
        current: SupervisorSnapshot,
        receipt_path: Path,
    ) -> AdvanceResult:
        receipt = read_object(receipt_path)
        classification = receipt.get("classification")
        if classification == "SCIENTIFIC_RESULT":
            run = RunWorkflow(RunStore(Path(plan.run_dir))).record_persistent_validation_session(
                Actor.TEACHER,
                request_id=str(current.request_id),
                idempotency_key=f"supervisor:import:{current.request_id}",
            )
            self.recovery.clear_round(
                plan.question_id, run.question_revision, current.scientific_round
            )
            next_snapshot = replace(
                current,
                phase=SupervisorPhase.TEACHER_ACTION,
                sequence=current.sequence + 1,
                updated_at=self._now(),
                process_id=None,
                process_started_at=None,
                last_evidence_sha256=file_sha256(receipt_path),
                recovery_condition="revision attribution required",
            )
            state_store.write_unlocked(next_snapshot)
            return AdvanceResult(
                plan.question_id,
                next_snapshot.phase.value,
                "REVISION_ATTRIBUTION_REQUIRED",
                run.state.value,
            )
        failure_stage = str(receipt.get("failure_stage") or "PLATFORM")
        self._import_platform_session_if_possible(plan, current)
        return self._recover_failure(
            plan,
            state_store,
            current,
            failure_stage=failure_stage,
            evidence_path=receipt_path,
        )

    def _recover_failure(
        self,
        plan: SupervisorPlan,
        state_store: SupervisorStateStore,
        current: SupervisorSnapshot,
        *,
        failure_stage: str,
        evidence_path: Path,
    ) -> AdvanceResult:
        run = RunStore(Path(plan.run_dir)).read_snapshot()
        if run is None:
            raise ContractError("Supervisor run disappeared during recovery")
        digest = file_sha256(evidence_path)
        decision = self.recovery.record_failure(
            question_id=plan.question_id,
            revision=run.question_revision,
            scientific_round=current.scientific_round,
            failure_stage=failure_stage,
            evidence_sha256=digest,
        )
        if run.state is RunState.BLOCKED:
            next_snapshot = self._blocked_runtime_snapshot(
                current, failure_stage=failure_stage, digest=digest
            )
            state_store.write_unlocked(next_snapshot)
            return AdvanceResult(
                plan.question_id,
                next_snapshot.phase.value,
                "TEACHER_RUNTIME_MIGRATION_REQUIRED",
                str(next_snapshot.recovery_condition),
            )
        next_snapshot = self._recovery_snapshot(
            current,
            failure_stage=failure_stage,
            digest=digest,
            decision=decision,
        )
        state_store.write_unlocked(next_snapshot)
        return AdvanceResult(
            plan.question_id,
            next_snapshot.phase.value,
            decision.action.value,
            decision.reason,
        )

    def _blocked_runtime_snapshot(
        self,
        current: SupervisorSnapshot,
        *,
        failure_stage: str,
        digest: str,
    ) -> SupervisorSnapshot:
        return replace(
            current,
            phase=SupervisorPhase.TEACHER_ACTION,
            sequence=current.sequence + 1,
            updated_at=self._now(),
            process_id=None,
            process_started_at=None,
            next_action_at=None,
            last_failure_stage=failure_stage,
            last_evidence_sha256=digest,
            recovery_condition=(
                "persistent runtime failed after scientific progress; "
                "Teacher must migrate to a fresh unchanged-package session"
            ),
        )

    def _recovery_snapshot(
        self,
        current: SupervisorSnapshot,
        *,
        failure_stage: str,
        digest: str,
        decision: RecoveryDecision,
    ) -> SupervisorSnapshot:
        common = dict(
            sequence=current.sequence + 1,
            updated_at=self._now(),
            process_id=None,
            process_started_at=None,
            last_failure_stage=failure_stage,
            last_evidence_sha256=digest,
        )
        if decision.action is RecoveryAction.RETRY_RUNTIME:
            return replace(
                current,
                phase=SupervisorPhase.RETRY_SCHEDULED,
                next_action_at=(
                    self.clock() + timedelta(seconds=int(decision.retry_after_sec or 0))
                ).isoformat(),
                recovery_condition=None,
                **common,
            )
        elif decision.action is RecoveryAction.WAIT_EXTERNAL:
            circuit = self.recovery.circuit()
            return replace(
                current,
                phase=SupervisorPhase.WAITING_EXTERNAL,
                next_action_at=(
                    circuit.next_probe_at
                    if failure_stage == "MODEL_CONNECTION"
                    else None
                ),
                recovery_condition=decision.recovery_condition,
                **common,
            )
        return replace(
            current,
            phase=SupervisorPhase.TEACHER_ACTION,
            next_action_at=None,
            recovery_condition=decision.reason,
            **common,
        )

    def _import_platform_session_if_possible(
        self, plan: SupervisorPlan, current: SupervisorSnapshot
    ) -> None:
        try:
            RunWorkflow(RunStore(Path(plan.run_dir))).record_persistent_validation_session(
                Actor.TEACHER,
                request_id=str(current.request_id),
                idempotency_key=f"supervisor:import:{current.request_id}",
            )
        except WorkflowError:
            # Evidence remains immutable. Deterministic import defects are surfaced
            # by the subsequent Teacher-audit phase, never converted to science.
            return

    def _apply_terminal_attribution(
        self,
        plan: SupervisorPlan,
        state_store: SupervisorStateStore,
        current: SupervisorSnapshot,
    ) -> AdvanceResult:
        evidence = Path(plan.run_dir) / "supervisor" / "revision-attribution.json"
        question = int(plan.question_id.removeprefix("q"))
        recorded = record_revision_attribution(question, evidence)
        run = RunStore(Path(plan.run_dir)).read_snapshot()
        if run is None:
            raise ContractError("run disappeared before Skill batch closure")
        contract = run.evidence.get("teacher_skill_contract", {})
        author = contract.get("author", {}) if isinstance(contract, dict) else {}
        batch_id = author.get("batch_id") if isinstance(author, dict) else None
        batch_questions = (
            author.get("batch_questions") if isinstance(author, dict) else None
        )
        if not isinstance(batch_id, str) or not isinstance(batch_questions, list):
            raise ContractError("terminal revision lacks its frozen Skill batch")
        close_skill_batch(batch_id, tuple(batch_questions))
        next_snapshot = replace(
            current,
            phase=SupervisorPhase.FINISHED,
            sequence=current.sequence + 1,
            updated_at=self._now(),
            recovery_condition=None,
        )
        state_store.write_unlocked(next_snapshot)
        return AdvanceResult(
            plan.question_id,
            next_snapshot.phase.value,
            "REVISION_ATTRIBUTED",
            str(recorded["attribution_path"]),
        )

    def _materialize_job_config(self, plan: SupervisorPlan, package: Path) -> Path:
        value = read_object(Path(plan.job_config_path))
        try:
            value["tasks"][0]["path"] = str(package.resolve())
            payload = value["agents"][0]["kwargs"]["persistent_validation"]
            payload["validation_session_id"] = plan.validation_session_id
            payload["pass_threshold"] = plan.pass_threshold
        except (KeyError, IndexError, TypeError) as error:
            raise ContractError("Supervisor JobConfig is not persistent") from error
        output = Path(plan.run_dir) / "supervisor" / "runtime-job-config.json"
        if output.exists():
            if read_object(output) != value:
                raise ContractError("Supervisor runtime JobConfig drifted")
        else:
            atomic_json(output, value)
        return output

    def _worker_command(
        self, plan: SupervisorPlan, handoff_path: Path, receipt_path: Path
    ) -> list[str]:
        return [
            sys.executable,
            "-m",
            "taskfoundry.cli",
            "runtime-researcher-run",
            str(handoff_path),
            plan.runtime_path,
            "--env-file",
            plan.env_file,
            "--receipt",
            str(receipt_path),
            "--overall-timeout-sec",
            str(plan.overall_timeout_sec),
            "--queue-root",
            plan.queue_root,
        ]

    def _validate_launch_inputs(self, plan: SupervisorPlan) -> None:
        for name in ("package_path", "job_config_path", "runtime_path", "env_file"):
            path = Path(str(getattr(plan, name)))
            if not path.exists() or path.is_symlink():
                raise ContractError(f"Supervisor launch input is unavailable: {name}")
        Path(plan.queue_root).mkdir(parents=True, exist_ok=True)

    def _write_lost_worker_evidence(
        self, plan: SupervisorPlan, current: SupervisorSnapshot
    ) -> Path:
        path = Path(plan.run_dir) / "supervisor" / (
            f"lost-worker-runtime-{current.runtime_attempt:03d}.json"
        )
        value = {
            "schema_version": 1,
            "question_id": plan.question_id,
            "request_id": current.request_id,
            "runtime_attempt": current.runtime_attempt,
            "failure_stage": "PLATFORM",
            "observed_at": self._now(),
        }
        if path.exists() and read_object(path) != value:
            raise ContractError("lost-worker evidence drifted")
        if not path.exists():
            atomic_json(path, value)
        return path

    def _plan_paths(self) -> tuple[Path, ...]:
        return tuple(sorted(self.runs_root.glob("*/supervisor/plan.json")))

    def _probe_model_transport_if_due(self) -> None:
        waiting = []
        for path in self._plan_paths():
            plan = SupervisorPlan.from_path(path)
            snapshot = SupervisorStateStore(Path(plan.run_dir)).read()
            if (
                snapshot.phase is SupervisorPhase.WAITING_EXTERNAL
                and snapshot.last_failure_stage == "MODEL_CONNECTION"
            ):
                waiting.append(plan)
        if not waiting:
            return
        owner = f"campaign-supervisor-{os.getpid()}"
        claimed = self.recovery.claim_model_probe(owner)
        if claimed is None:
            return
        output = self.recovery.root / "model-probes" / (
            f"probe-{self.clock().strftime('%Y%m%dT%H%M%S%fZ')}.json"
        )
        evidence = self.model_probe.run(
            env_file=Path(waiting[0].env_file),
            output_path=output,
        )
        self.recovery.finish_model_probe(
            worker_id=owner,
            succeeded=evidence.succeeded,
            evidence_sha256=file_sha256(output),
        )

    def _resume_closed_model_waits(self) -> None:
        if self.recovery.circuit().state is not CircuitState.CLOSED:
            return
        for path in self._plan_paths():
            plan = SupervisorPlan.from_path(path)
            store = SupervisorStateStore(Path(plan.run_dir))
            with store.locked():
                current = store.read_unlocked()
                if (
                    current.phase is SupervisorPhase.WAITING_EXTERNAL
                    and current.last_failure_stage == "MODEL_CONNECTION"
                ):
                    store.write_unlocked(
                        replace(
                            current,
                            phase=SupervisorPhase.READY,
                            sequence=current.sequence + 1,
                            updated_at=self._now(),
                            next_action_at=None,
                            recovery_condition=None,
                        )
                    )

    def _plan_for(self, question_id: str) -> Path:
        matches = [
            path
            for path in self._plan_paths()
            if read_object(path).get("question_id") == question_id
        ]
        if len(matches) != 1:
            raise ContractError("question must have exactly one Supervisor plan")
        return matches[0]

    def _now(self) -> str:
        return self.clock().isoformat()


def register_supervisor_plan(plan: SupervisorPlan, *, now: str | None = None) -> SupervisorSnapshot:
    """Register one immutable plan without exposing state-store details to CLI."""
    return SupervisorStateStore(Path(plan.run_dir)).register(
        plan,
        now=now or datetime.now(UTC).isoformat(),
    )

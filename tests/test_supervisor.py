from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path

from taskfoundry.supervisor import CampaignSupervisor
from taskfoundry.supervisor_process import LocalProcessAdapter
from taskfoundry.labwright import atomic_json
from taskfoundry.model_probe import ProbeEvidence
from taskfoundry.supervisor_state import (
    SupervisorPhase,
    SupervisorPlan,
    SupervisorSnapshot,
    SupervisorStateStore,
)


class FakeProcesses:
    def __init__(self, *, alive: bool = True) -> None:
        self.is_alive = alive
        self.launches = []

    def launch(self, command, *, cwd, stdout_path, stderr_path):
        self.launches.append(command)
        return 8123

    def alive(self, process_id: int) -> bool:
        return self.is_alive


class SuccessfulProbe:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, env_file, output_path, timeout_sec=90):
        self.calls += 1
        evidence = ProbeEvidence(
            succeeded=True,
            checked_at="2026-08-31T12:16:00+00:00",
            elapsed_sec=0.1,
        )
        atomic_json(output_path, evidence.__dict__)
        return evidence


def test_local_process_adapter_rejects_unrelated_reused_pid() -> None:
    """A durable PID from another namespace must not match an unrelated process."""
    assert LocalProcessAdapter().alive(os.getpid()) is False


def _plan(tmp_path: Path) -> SupervisorPlan:
    run = tmp_path / "runs" / "q12-run"
    return SupervisorPlan(
        question_id="q12",
        run_dir=str(run.resolve()),
        package_path=str((tmp_path / "package").resolve()),
        job_config_path=str((tmp_path / "job.json").resolve()),
        runtime_path=str((tmp_path / "runtime.json").resolve()),
        env_file=str((tmp_path / ".env").resolve()),
        queue_root=str((tmp_path / "queue").resolve()),
        validation_session_id="q12-r16-persistent-01",
    )


def _running(tmp_path: Path, *, last_decided_round: int = 0) -> SupervisorSnapshot:
    request = Path(_plan(tmp_path).run_dir) / "researcher-requests" / "request-1"
    request.mkdir(parents=True)
    return SupervisorSnapshot(
        question_id="q12",
        phase=SupervisorPhase.RUNNING,
        sequence=1,
        runtime_attempt=1,
        scientific_round=last_decided_round + 1,
        updated_at="2026-08-31T12:00:00+00:00",
        request_id="request-1",
        handoff_path=str((request / "handoff.json").resolve()),
        receipt_path=str((request / "researcher-receipt.json").resolve()),
        process_id=8123,
        process_started_at="2026-08-31T12:00:00+00:00",
        last_decided_round=last_decided_round,
    )


def _register_running(tmp_path: Path, snapshot: SupervisorSnapshot) -> None:
    plan = _plan(tmp_path)
    store = SupervisorStateStore(Path(plan.run_dir))
    store.register(plan, now="2026-08-31T11:59:00+00:00")
    with store.locked():
        store.write_unlocked(snapshot)


def test_run_once_keeps_live_worker_without_busy_state_change(tmp_path: Path) -> None:
    _register_running(tmp_path, _running(tmp_path))
    supervisor = CampaignSupervisor(
        tmp_path / "runs",
        clock=lambda: datetime(2026, 8, 31, 12, 1, tzinfo=UTC),
        process_adapter=FakeProcesses(),
    )

    result = supervisor.run_once()

    assert result[0].action == "WORKER_RUNNING"
    assert SupervisorStateStore(Path(_plan(tmp_path).run_dir)).read().sequence == 1


def test_first_perfect_blind_is_stopped_as_too_easy(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _running(tmp_path)
    _register_running(tmp_path, snapshot)
    controller = Path(snapshot.handoff_path).parent / "validation-control"
    controller.mkdir()
    (controller / "round-01-result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "validation_session_id": _plan(tmp_path).validation_session_id,
                "trial_id": "trial-1",
                "round_index": 1,
                "phase": "BLIND",
                "classification": "SCIENTIFIC_RESULT",
                "reward": 1.0,
                "verifier_result": {"reward": 1.0},
                "artifact_manifest_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    decisions = []

    class FakeWorkflow:
        def __init__(self, store):
            pass

        def decide_persistent_validation_round(self, actor, **kwargs):
            decisions.append(kwargs["decision"].action)

    monkeypatch.setattr("taskfoundry.supervisor.RunWorkflow", FakeWorkflow)
    supervisor = CampaignSupervisor(
        tmp_path / "runs",
        process_adapter=FakeProcesses(),
    )

    result = supervisor.run_once()

    assert result[0].action == "ROUND_DECIDED"
    assert decisions == ["STOP_TOO_EASY"]
    assert (
        SupervisorStateStore(Path(_plan(tmp_path).run_dir)).read().last_decided_round
        == 1
    )


def test_third_failed_blind_waits_for_teacher_hint(tmp_path: Path) -> None:
    snapshot = _running(tmp_path, last_decided_round=2)
    _register_running(tmp_path, snapshot)
    controller = Path(snapshot.handoff_path).parent / "validation-control"
    controller.mkdir()
    (controller / "round-03-result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "validation_session_id": _plan(tmp_path).validation_session_id,
                "trial_id": "trial-1",
                "round_index": 3,
                "phase": "BLIND",
                "classification": "SCIENTIFIC_RESULT",
                "reward": 0.2,
                "verifier_result": {"reward": 0.2},
                "artifact_manifest_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    supervisor = CampaignSupervisor(
        tmp_path / "runs",
        process_adapter=FakeProcesses(),
    )

    result = supervisor.run_once()

    assert result[0].action == "TEACHER_HINT_REQUIRED"
    assert (
        SupervisorStateStore(Path(_plan(tmp_path).run_dir)).read().phase
        is SupervisorPhase.TEACHER_ACTION
    )


def test_teacher_hint_relaunches_bound_continuation_when_owner_died(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = replace(
        _running(tmp_path, last_decided_round=2),
        phase=SupervisorPhase.TEACHER_ACTION,
        scientific_round=3,
    )
    _register_running(tmp_path, snapshot)
    run_dir = Path(_plan(tmp_path).run_dir)
    supervisor_dir = run_dir / "supervisor"
    supervisor_dir.mkdir(exist_ok=True)
    (supervisor_dir / "teacher-action.json").write_text(
        json.dumps(
            {
                "action": "CONTINUE_HINT",
                "hint": "Use a grouped, physics-informed soft-transition basis.",
                "teacher_declares_non_answer": True,
            }
        ),
        encoding="utf-8",
    )
    decisions = []

    class FakeWorkflow:
        def __init__(self, store):
            pass

        def decide_persistent_validation_round(self, actor, **kwargs):
            decisions.append(kwargs["decision"].action)

    processes = FakeProcesses(alive=False)
    monkeypatch.setattr("taskfoundry.supervisor.RunWorkflow", FakeWorkflow)
    monkeypatch.setattr(
        CampaignSupervisor,
        "_persistent_recovery_command",
        lambda self, plan, current, action_path: ["recovery-worker"],
    )
    supervisor = CampaignSupervisor(
        tmp_path / "runs",
        process_adapter=processes,
    )

    result = supervisor.resume("q12")

    state = SupervisorStateStore(run_dir).read()
    assert result.action == "TEACHER_ACTION_APPLIED_RECOVERY"
    assert decisions == ["CONTINUE_HINT"]
    assert processes.launches == [["recovery-worker"]]
    assert state.phase is SupervisorPhase.RUNNING
    assert state.process_id == 8123
    assert state.last_decided_round == 3


def test_resume_external_retries_bound_persistent_continuation(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = replace(
        _running(tmp_path, last_decided_round=3),
        phase=SupervisorPhase.WAITING_EXTERNAL,
        scientific_round=4,
        process_id=None,
        process_started_at=None,
        last_failure_stage="PLATFORM",
        recovery_condition="provider health probe succeeds",
    )
    _register_running(tmp_path, snapshot)
    request_dir = Path(snapshot.handoff_path).parent
    controller = request_dir / "validation-control"
    controller.mkdir()
    (controller / "round-03-decision.json").write_text(
        json.dumps({"action": "CONTINUE_HINT"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        CampaignSupervisor,
        "_persistent_recovery_command",
        lambda self, plan, current, action_path: ["recovery-worker"],
    )
    processes = FakeProcesses(alive=False)
    supervisor = CampaignSupervisor(
        tmp_path / "runs",
        process_adapter=processes,
    )

    resumed = supervisor.resume("q12")
    result = supervisor.run_once()

    state = SupervisorStateStore(Path(_plan(tmp_path).run_dir)).read()
    assert resumed.phase == SupervisorPhase.RETRY_SCHEDULED.value
    assert result[0].action == "PERSISTENT_CONTINUATION_RETRIED"
    assert processes.launches == [["recovery-worker"]]
    assert state.phase is SupervisorPhase.RUNNING
    assert state.request_id == "request-1"
    assert state.runtime_attempt == 1


def test_dead_worker_without_receipt_uses_retry_budget(tmp_path: Path) -> None:
    snapshot = _running(tmp_path)
    _register_running(tmp_path, snapshot)
    run_dir = Path(_plan(tmp_path).run_dir)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": "q12-run",
                "state": "BLIND_VALIDATION",
                "sequence": 0,
                "question_revision": "r16",
            }
        ),
        encoding="utf-8",
    )
    supervisor = CampaignSupervisor(
        tmp_path / "runs",
        clock=lambda: datetime(2026, 8, 31, 12, 1, tzinfo=UTC),
        process_adapter=FakeProcesses(alive=False),
    )

    result = supervisor.run_once()

    state = SupervisorStateStore(run_dir).read()
    assert result[0].action == "RETRY_RUNTIME"
    assert state.phase is SupervisorPhase.RETRY_SCHEDULED
    assert state.next_action_at == "2026-08-31T12:01:30+00:00"


def test_retry_closes_every_prior_lost_worker_request(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    supervisor_root = Path(plan.run_dir) / "supervisor"
    supervisor_root.mkdir(parents=True)
    for index in (1, 2):
        (supervisor_root / f"lost-worker-runtime-{index:03d}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "question_id": "q12",
                    "request_id": f"persistent-runtime-{index:03d}",
                    "runtime_attempt": index,
                    "failure_stage": "PLATFORM",
                    "observed_at": "2026-08-31T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
    current = replace(
        _running(tmp_path),
        phase=SupervisorPhase.RETRY_SCHEDULED,
        request_id="persistent-runtime-002",
        process_id=None,
        process_started_at=None,
        next_action_at="2026-08-31T12:01:30+00:00",
    )

    assert CampaignSupervisor(tmp_path / "runs")._closed_request_ids_for_retry(
        plan, current
    ) == ("persistent-runtime-001", "persistent-runtime-002")


def test_shared_model_probe_closes_circuit_and_resumes_all_waiters(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _plan(tmp_path)
    store = SupervisorStateStore(Path(plan.run_dir))
    store.register(plan, now="2026-08-31T12:00:00+00:00")
    with store.locked():
        store.write_unlocked(
            SupervisorSnapshot(
                question_id="q12",
                phase=SupervisorPhase.WAITING_EXTERNAL,
                sequence=1,
                runtime_attempt=1,
                scientific_round=1,
                updated_at="2026-08-31T12:00:00+00:00",
                last_failure_stage="MODEL_CONNECTION",
                last_evidence_sha256="a" * 64,
                next_action_at="2026-08-31T12:15:00+00:00",
                recovery_condition="strong model transport probe succeeds",
            )
        )
    now = [datetime(2026, 8, 31, 12, 0, tzinfo=UTC)]
    clock = lambda: now[0]
    probe = SuccessfulProbe()
    supervisor = CampaignSupervisor(
        tmp_path / "runs",
        clock=clock,
        process_adapter=FakeProcesses(),
        model_probe=probe,
    )
    supervisor.recovery.record_failure(
        question_id="q12",
        revision="r16",
        scientific_round=1,
        failure_stage="MODEL_CONNECTION",
        evidence_sha256="a" * 64,
    )
    now[0] = datetime(2026, 8, 31, 12, 16, tzinfo=UTC)
    monkeypatch.setattr(
        supervisor,
        "_advance",
        lambda path: type("Result", (), {
            "question_id": "q12",
            "phase": "READY",
            "action": "NO_CHANGE",
            "detail": "",
        })(),
    )

    supervisor.run_once()

    assert probe.calls == 1
    assert store.read().phase is SupervisorPhase.READY
    assert supervisor.recovery.circuit().state.value == "CLOSED"

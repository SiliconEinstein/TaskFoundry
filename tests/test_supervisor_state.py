from __future__ import annotations

from pathlib import Path

import pytest

from taskfoundry.model import ContractError
from taskfoundry.supervisor_state import (
    SupervisorPhase,
    SupervisorPlan,
    SupervisorSnapshot,
    SupervisorStateStore,
)


def plan(tmp_path: Path) -> SupervisorPlan:
    return SupervisorPlan(
        question_id="q12",
        run_dir=str((tmp_path / "run").resolve()),
        package_path=str((tmp_path / "task").resolve()),
        job_config_path=str((tmp_path / "job.json").resolve()),
        runtime_path=str((tmp_path / "runtime.json").resolve()),
        env_file="/personal/TaskFoundry/.env",
        queue_root=str((tmp_path / "queue").resolve()),
        validation_session_id="q12-r16-persistent-01",
    )


def test_register_is_idempotent_and_starts_ready(tmp_path: Path) -> None:
    store = SupervisorStateStore(tmp_path / "run")

    first = store.register(plan(tmp_path), now="2026-08-31T12:00:00+00:00")
    second = store.register(plan(tmp_path), now="2026-08-31T13:00:00+00:00")

    assert first == second
    assert first.phase is SupervisorPhase.READY
    assert store.plan_path.is_file()


def test_register_rejects_plan_drift(tmp_path: Path) -> None:
    store = SupervisorStateStore(tmp_path / "run")
    store.register(plan(tmp_path), now="2026-08-31T12:00:00+00:00")

    with pytest.raises(ContractError, match="bound differently"):
        store.register(
            SupervisorPlan(**(plan(tmp_path).__dict__ | {"pass_threshold": 0.9})),
            now="2026-08-31T12:01:00+00:00",
        )


def test_plan_rejects_relative_or_inline_runtime_configuration(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="must be absolute"):
        SupervisorPlan(**(plan(tmp_path).__dict__ | {"env_file": ".env"})).validate()


def test_running_snapshot_requires_recoverable_process_identity() -> None:
    with pytest.raises(ContractError, match="process identity"):
        SupervisorSnapshot(
            question_id="q12",
            phase=SupervisorPhase.RUNNING,
            sequence=1,
            runtime_attempt=1,
            scientific_round=1,
            updated_at="2026-08-31T12:00:00+00:00",
            request_id="q12-a01",
            handoff_path="/tmp/handoff.json",
            receipt_path="/tmp/receipt.json",
            process_id=123,
        ).validate()

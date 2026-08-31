from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path

import pytest

from taskfoundry.model import ContractError
from taskfoundry.recovery import (
    CircuitState,
    MODEL_CIRCUIT_COOLDOWN_SEC,
    RecoveryAction,
    RecoveryLedger,
    RecoveryPolicy,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def record(ledger: RecoveryLedger, stage: str, index: int):
    return ledger.record_failure(
        question_id="q10",
        revision="r6",
        scientific_round=3,
        failure_stage=stage,
        evidence_sha256=digest(f"{stage}-{index}"),
    )


def test_transient_runtime_failures_use_fixed_budget_then_wait(tmp_path: Path) -> None:
    ledger = RecoveryLedger(tmp_path)

    decisions = [record(ledger, "SANDBOX", index) for index in range(1, 5)]

    assert [item.action for item in decisions] == [
        RecoveryAction.RETRY_RUNTIME,
        RecoveryAction.RETRY_RUNTIME,
        RecoveryAction.RETRY_RUNTIME,
        RecoveryAction.WAIT_EXTERNAL,
    ]
    assert [item.retry_after_sec for item in decisions[:3]] == [30, 120, 300]
    assert decisions[-1].recovery_condition == "provider health probe succeeds"


def test_model_failure_opens_one_shared_circuit_and_claims_one_probe(
    tmp_path: Path,
) -> None:
    clock = Clock()
    ledger = RecoveryLedger(tmp_path, clock=clock)

    decision = record(ledger, "MODEL_CONNECTION", 1)
    opened = ledger.circuit()

    assert decision.action is RecoveryAction.WAIT_EXTERNAL
    assert opened.state is CircuitState.OPEN
    assert ledger.claim_model_probe("worker-a") is None

    clock.advance(MODEL_CIRCUIT_COOLDOWN_SEC)
    claimed = ledger.claim_model_probe("worker-a")
    assert claimed is not None and claimed.state is CircuitState.HALF_OPEN
    assert ledger.claim_model_probe("worker-b") is None

    closed = ledger.finish_model_probe(
        worker_id="worker-a", succeeded=True, evidence_sha256=digest("probe-pass")
    )
    assert closed.state is CircuitState.CLOSED
    assert closed.failure_count == 0


def test_failed_probe_reopens_full_cooldown(tmp_path: Path) -> None:
    clock = Clock()
    ledger = RecoveryLedger(tmp_path, clock=clock)
    record(ledger, "MODEL_CONNECTION", 1)
    clock.advance(MODEL_CIRCUIT_COOLDOWN_SEC)
    assert ledger.claim_model_probe("worker-a") is not None

    reopened = ledger.finish_model_probe(
        worker_id="worker-a", succeeded=False, evidence_sha256=digest("probe-fail")
    )

    assert reopened.state is CircuitState.OPEN
    assert reopened.failure_count == 2
    assert ledger.claim_model_probe("worker-a") is None


def test_deterministic_and_scientific_stages_do_not_retry() -> None:
    policy = RecoveryPolicy()

    assert (
        policy.decide("VERIFIER", 1).action is RecoveryAction.TEACHER_AUDIT
    )
    assert (
        policy.decide("HARNESS_BOOTSTRAP", 1).action
        is RecoveryAction.TEACHER_AUDIT
    )
    assert (
        policy.decide("SCIENTIFIC_EXECUTION", 1).action
        is RecoveryAction.SCIENTIFIC_DECISION
    )


def test_successful_round_clears_only_its_runtime_failures(tmp_path: Path) -> None:
    ledger = RecoveryLedger(tmp_path)
    record(ledger, "SANDBOX", 1)
    ledger.record_failure(
        question_id="q10",
        revision="r6",
        scientific_round=2,
        failure_stage="SANDBOX",
        evidence_sha256=digest("round-2"),
    )

    ledger.clear_round("q10", "r6", 3)

    state = (tmp_path / "runtime-failures.json").read_text()
    assert "q10|r6|3|SANDBOX" not in state
    assert "q10|r6|2|SANDBOX" in state


def test_recovery_rejects_non_digest_evidence(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="identity"):
        RecoveryLedger(tmp_path).record_failure(
            question_id="q10",
            revision="r6",
            scientific_round=3,
            failure_stage="SANDBOX",
            evidence_sha256="not-a-digest",
        )

from __future__ import annotations

from dataclasses import replace

import pytest

from taskfoundry.model import ContractError
from taskfoundry.validation import AttemptEvidence, HealthEvidence, decide_validation


HEALTHY = HealthEvidence(True, True, True, True, True, True)


def attempt(index, *, mode="blind", score=0.4, classification="SCIENTIFIC_RESULT", **changes):
    values = {
        "request_id": f"request-{mode}-{index}",
        "attempt_index": index,
        "mode": mode,
        "classification": classification,
        "score": score if classification == "SCIENTIFIC_RESULT" else None,
        "frozen_contract_digest": "a" * 64,
        "job_id": f"job-{mode}-{index}",
        "trial_id": f"trial-{mode}-{index}",
        "sandbox_id": f"sandbox-{mode}-{index}",
        "session_id": f"session-{mode}-{index}",
        "wall_time_sec": 100,
        "leakage_free": True,
        "leakage_evidence_sha256": "e" * 64,
        "hint_sha256": "b" * 64 if mode == "hint" else None,
    }
    return AttemptEvidence(**(values | changes))


def test_three_blind_failures_then_hint_pass_completes() -> None:
    attempts = [attempt(1), attempt(2), attempt(3)]
    assert decide_validation(attempts, HEALTHY).action == "NEXT_HINT"
    attempts.append(attempt(1, mode="hint", score=0.9))
    assert decide_validation(attempts, HEALTHY, final_health_bound=True).action == "COMPLETE"


def test_blind_pass_is_too_easy() -> None:
    assert decide_validation([attempt(1, score=0.85)], HEALTHY).action == "TOO_EASY"


@pytest.mark.parametrize("classification", ["ENVIRONMENT_FAILURE", "HARNESS_FAILURE", "PLATFORM_FAILURE"])
def test_nonscientific_failure_retries_without_count(classification) -> None:
    decision = decide_validation([attempt(1, classification=classification)], HEALTHY)
    assert decision.action == "RETRY_SAME_ATTEMPT"
    assert decision.scientific_blind_count == 0


def test_one_timeout_defers_and_two_require_review() -> None:
    first = attempt(1, classification="DEFERRED_TIMEOUT")
    assert decide_validation([first], HEALTHY).action == "DEFER_NEXT_QUESTION"
    second = replace(first, request_id="r2", job_id="j2")
    assert decide_validation([first, second], HEALTHY).action == "HUMAN_REVIEW"


def test_hinted_pass_cannot_override_health_failure() -> None:
    attempts = [attempt(1), attempt(2), attempt(3), attempt(1, mode="hint", score=1)]
    unhealthy = replace(HEALTHY, adversarial_low_score=False)
    assert decide_validation(attempts, unhealthy).action == "BLOCKED_HEALTH"


def test_execution_contract_failure_requires_teacher_audit() -> None:
    value = attempt(1, classification="EXECUTION_CONTRACT_FAILURE")
    assert decide_validation([value], HEALTHY).action == "TEACHER_AUDIT"


@pytest.mark.parametrize(
    "change,message",
    [
        ({"score": float("nan")}, "finite score"),
        ({"mode": "blind", "hint_sha256": "b" * 64}, "cannot contain"),
        ({"mode": "hint", "hint_sha256": None}, "requires a reviewed"),
    ],
)
def test_attempt_contract_rejects_invalid_evidence(change, message) -> None:
    with pytest.raises(ContractError, match=message):
        replace(attempt(1), **change).validate()


@pytest.mark.parametrize("field", ["job_id", "trial_id", "sandbox_id", "session_id"])
def test_scientific_attempts_must_be_fresh(field) -> None:
    first, second = attempt(1), attempt(2)
    second = replace(second, **{field: getattr(first, field)})
    with pytest.raises(ContractError, match=f"fresh {field}"):
        decide_validation([first, second], HEALTHY)


def test_scientific_attempts_share_frozen_contract() -> None:
    with pytest.raises(ContractError, match="frozen contract"):
        decide_validation([attempt(1), replace(attempt(2), frozen_contract_digest="b" * 64)], HEALTHY)

from __future__ import annotations

from dataclasses import replace

import pytest

from taskfoundry.model import ContractError
from taskfoundry.validation import AttemptEvidence, HealthEvidence, decide_validation


HEALTHY = HealthEvidence(True, True, True, True, True, True)


def attempt(
    index, *, mode="blind", score=0.4, classification="SCIENTIFIC_RESULT", **changes
):
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


def test_three_blind_failures_then_hint_passes_validation() -> None:
    attempts = [attempt(1), attempt(2), attempt(3)]
    assert decide_validation(attempts, HEALTHY).action == "NEXT_HINT"
    attempts.append(attempt(1, mode="hint", score=0.9))
    assert (
        decide_validation(attempts, HEALTHY, final_health_bound=True).action
        == "VALIDATION_PASSED"
    )


def test_blind_pass_is_too_easy() -> None:
    assert decide_validation([attempt(1, score=0.85)], HEALTHY).action == "TOO_EASY"


def test_unknown_result_classification_fails_closed() -> None:
    with pytest.raises(ContractError, match="classification"):
        decide_validation([attempt(1, classification="TYPO")], HEALTHY)


def test_score_just_below_threshold_continues_blind() -> None:
    assert (
        decide_validation([attempt(1, score=0.849999)], HEALTHY).action == "NEXT_BLIND"
    )


def test_hint_before_three_scientific_blinds_is_rejected() -> None:
    with pytest.raises(ContractError, match="exactly three preceding"):
        decide_validation([attempt(1), attempt(1, mode="hint", score=0.9)], HEALTHY)


def test_fourth_scientific_blind_is_rejected() -> None:
    with pytest.raises(ContractError, match="only three"):
        decide_validation([attempt(1), attempt(2), attempt(3), attempt(4)], HEALTHY)


def test_hint_indexes_must_be_contiguous() -> None:
    evidence = [attempt(1), attempt(2), attempt(3), attempt(2, mode="hint", score=0.7)]
    with pytest.raises(ContractError, match="hint attempt indexes"):
        decide_validation(evidence, HEALTHY)


def test_two_failed_reviewed_hints_require_human_review() -> None:
    evidence = [
        attempt(1),
        attempt(2),
        attempt(3),
        attempt(1, mode="hint", score=0.7),
        attempt(2, mode="hint", score=0.8),
    ]
    assert decide_validation(evidence, HEALTHY).action == "HUMAN_REVIEW"


def test_third_hint_is_rejected() -> None:
    evidence = [
        attempt(1),
        attempt(2),
        attempt(3),
        attempt(1, mode="hint", score=0.7),
        attempt(2, mode="hint", score=0.8),
        attempt(3, mode="hint", score=0.84),
    ]
    with pytest.raises(ContractError, match="at most two"):
        decide_validation(evidence, HEALTHY)


@pytest.mark.parametrize(
    "classification", ["ENVIRONMENT_FAILURE", "HARNESS_FAILURE", "PLATFORM_FAILURE"]
)
def test_nonscientific_failure_retries_without_count(classification) -> None:
    decision = decide_validation([attempt(1, classification=classification)], HEALTHY)
    assert decision.action == "RETRY_SAME_ATTEMPT"
    assert decision.scientific_blind_count == 0


def test_platform_retry_cannot_reuse_an_existing_sandbox_identity() -> None:
    first = attempt(1, classification="PLATFORM_FAILURE")
    second = replace(
        first,
        request_id="request-retry",
        job_id="job-retry",
        trial_id="trial-retry",
        session_id="session-retry",
    )

    with pytest.raises(ContractError, match="fresh sandbox_id"):
        decide_validation([first, second], HEALTHY)


def test_provider_sandbox_cannot_be_reused_across_agent_and_verifier_roles() -> None:
    first = attempt(1, classification="PLATFORM_FAILURE")
    second = replace(
        first,
        request_id="request-cross-role",
        job_id="job-cross-role",
        trial_id="trial-cross-role",
        sandbox_id="agent-cross-role",
        verifier_sandbox_id=first.sandbox_id,
        session_id="session-cross-role",
    )

    with pytest.raises(ContractError, match="across roles"):
        decide_validation([first, second], HEALTHY)


def test_pre_sandbox_platform_failure_may_retry_in_a_fresh_sandbox() -> None:
    first = attempt(
        1,
        classification="PLATFORM_FAILURE",
        trial_id="",
        sandbox_id="",
        session_id="",
    )
    second = replace(
        first,
        request_id="request-retry",
        job_id="job-retry",
        sandbox_id="sandbox-retry",
    )

    assert decide_validation([first, second], HEALTHY).action == "RETRY_SAME_ATTEMPT"


def test_incomplete_evidence_retries_without_count_or_execution_identity() -> None:
    incomplete = attempt(
        1,
        classification="EVIDENCE_INCOMPLETE",
        job_id="",
        trial_id="",
        sandbox_id="",
        session_id="",
    )

    decision = decide_validation([incomplete], HEALTHY)

    assert decision.action == "RETRY_SAME_ATTEMPT"
    assert decision.scientific_blind_count == 0


def test_one_timeout_defers_and_two_require_review() -> None:
    first = attempt(1, classification="DEFERRED_TIMEOUT")
    assert decide_validation([first], HEALTHY).action == "DEFER_NEXT_QUESTION"
    second = replace(
        first,
        request_id="r2",
        job_id="j2",
        trial_id="t2",
        sandbox_id="sandbox2",
        session_id="session2",
    )
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
        decide_validation(
            [attempt(1), replace(attempt(2), frozen_contract_digest="b" * 64)], HEALTHY
        )


def linear_attempt(
    index: int, *, mode: str = "blind", score: float = 0.4
) -> AttemptEvidence:
    """构造共享外层 Researcher validation session 中的一轮。"""
    return attempt(
        index,
        mode=mode,
        score=score,
        validation_session_id="validation-session-1",
        researcher_thread_id="researcher-thread-1",
        verifier_sandbox_id=f"verifier-sandbox-{index}",
        prior_round_receipt_sha256s=tuple("a" * 64 for _ in range(index - 1)),
        prior_round_history_paths=tuple(
            f"/history/{position}" for position in range(index - 1)
        ),
        schema_version=2,
    )


def test_linear_session_stops_immediately_when_first_blind_passes() -> None:
    first = linear_attempt(1, score=1.0)

    assert decide_validation([first], HEALTHY).action == "TOO_EASY"


def test_linear_session_continues_only_after_failed_blinds() -> None:
    first = linear_attempt(1, score=0.0)
    second = linear_attempt(2, score=0.84)

    assert decide_validation([first], HEALTHY).action == "NEXT_BLIND"
    assert decide_validation([first, second], HEALTHY).action == "NEXT_BLIND"


def test_linear_session_reuses_outer_thread_but_requires_fresh_sandbox() -> None:
    first = linear_attempt(1)
    second = linear_attempt(2)
    assert decide_validation([first, second], HEALTHY).action == "NEXT_BLIND"

    with pytest.raises(ContractError, match="same Researcher thread"):
        decide_validation(
            [first, replace(second, researcher_thread_id="another-thread")],
            HEALTHY,
        )
    with pytest.raises(ContractError, match="fresh sandbox_id"):
        decide_validation(
            [first, replace(second, sandbox_id=first.sandbox_id)], HEALTHY
        )


def test_linear_session_requires_complete_prior_round_chain() -> None:
    first = linear_attempt(1)
    second = replace(
        linear_attempt(2),
        prior_round_receipt_sha256s=(),
        prior_round_history_paths=(),
    )
    with pytest.raises(ContractError, match="prior round records"):
        decide_validation([first, second], HEALTHY)


def test_linear_session_rejects_changed_session_and_bad_digest() -> None:
    first = linear_attempt(1)
    second = linear_attempt(2)
    with pytest.raises(ContractError, match="one validation session"):
        decide_validation(
            [first, replace(second, validation_session_id="another-session")],
            HEALTHY,
        )
    with pytest.raises(ContractError, match="digest is invalid"):
        replace(second, prior_round_receipt_sha256s=("z" * 64,)).validate()


def test_linear_attempt_rejects_unknown_schema_and_missing_identity() -> None:
    with pytest.raises(ContractError, match="unsupported"):
        replace(attempt(1), schema_version=4).validate()
    with pytest.raises(ContractError, match="validation session"):
        replace(attempt(1), schema_version=2).validate()


def persistent_attempt(index: int, *, mode: str = "blind", score: float = 0.4):
    return replace(
        attempt(index, mode=mode, score=score),
        job_id="persistent-job",
        trial_id="persistent-trial",
        sandbox_id="persistent-agent-sandbox",
        session_id="persistent-codex-session",
        verifier_sandbox_id=f"fresh-verifier-{index}",
        validation_session_id="persistent-validation-session",
        researcher_thread_id="persistent-researcher-thread",
        control_result_sha256=(f"{index:x}" * 64)[:64],
        control_decision_sha256=(f"{index + 5:x}" * 64)[:64],
        schema_version=3,
    )


def test_persistent_session_reuses_agent_runtime_and_refreshes_verifier() -> None:
    attempts = [
        persistent_attempt(1),
        persistent_attempt(2),
        persistent_attempt(3),
        persistent_attempt(4, mode="hint", score=1.0),
    ]

    assert decide_validation(attempts, HEALTHY).action == "VALIDATION_PASSED"

    with pytest.raises(ContractError, match="fresh verifier"):
        decide_validation(
            [attempts[0], replace(attempts[1], verifier_sandbox_id="fresh-verifier-1")],
            HEALTHY,
        )


def test_persistent_session_first_blind_pass_is_too_easy() -> None:
    assert (
        decide_validation([persistent_attempt(1, score=1.0)], HEALTHY).action
        == "TOO_EASY"
    )

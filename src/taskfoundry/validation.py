"""与 Harbor 进程成败解耦的科学难度验证门。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Iterable

from .model import ContractError


class JobClassification(StrEnum):
    """进入验证状态机的封闭结果分类。"""

    SCIENTIFIC_RESULT = "SCIENTIFIC_RESULT"
    DEFERRED_TIMEOUT = "DEFERRED_TIMEOUT"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    HARNESS_FAILURE = "HARNESS_FAILURE"
    PLATFORM_FAILURE = "PLATFORM_FAILURE"
    EXECUTION_CONTRACT_FAILURE = "EXECUTION_CONTRACT_FAILURE"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"


@dataclass(frozen=True)
class AttemptEvidence:
    """一次 Researcher 尝试的已审计证据。"""

    request_id: str
    attempt_index: int
    mode: str
    classification: str
    score: float | None
    frozen_contract_digest: str
    job_id: str
    trial_id: str
    sandbox_id: str
    session_id: str
    wall_time_sec: float
    verifier_sandbox_id: str | None = None
    context_sha256: str | None = None
    hint_sha256: str | None = None
    leakage_free: bool = False
    leakage_evidence_sha256: str | None = None
    validation_session_id: str | None = None
    researcher_thread_id: str | None = None
    prior_round_receipt_sha256s: tuple[str, ...] = ()
    prior_round_history_paths: tuple[str, ...] = ()
    control_result_sha256: str | None = None
    control_decision_sha256: str | None = None
    schema_version: int = 1

    def validate(self) -> None:
        """拒绝不完整、非有限值或阶段不一致的证据。"""
        if self.schema_version not in {1, 2, 3}:
            raise ContractError("unsupported attempt evidence schema_version")
        if self.mode not in {"blind", "hint"} or self.attempt_index < 1:
            raise ContractError("invalid attempt stage")
        try:
            JobClassification(self.classification)
        except ValueError as error:
            raise ContractError("unknown result classification") from error
        if self.classification == "SCIENTIFIC_RESULT":
            if (
                self.score is None
                or not math.isfinite(self.score)
                or not 0 <= self.score <= 1
            ):
                raise ContractError(
                    "scientific result requires a finite score in [0, 1]"
                )
        elif self.score is not None:
            raise ContractError("non-scientific failure cannot carry a score")
        if self.mode == "blind" and self.hint_sha256 is not None:
            raise ContractError("blind attempt cannot contain a hint")
        if self.mode == "hint" and self.hint_sha256 is None:
            raise ContractError("hint attempt requires a reviewed hint digest")
        if self.wall_time_sec < 0 or not all(
            (self.request_id, self.frozen_contract_digest)
        ):
            raise ContractError("attempt identity and non-negative time are required")
        if self.classification == "SCIENTIFIC_RESULT" and not all(
            (self.job_id, self.trial_id, self.sandbox_id, self.session_id)
        ):
            raise ContractError(
                "scientific result requires complete execution identity"
            )
        if self.leakage_free and self.leakage_evidence_sha256 is None:
            raise ContractError("leakage-free status requires audited evidence")
        if self.schema_version == 2:
            if not self.validation_session_id or not self.researcher_thread_id:
                raise ContractError(
                    "linear attempt requires validation session and Researcher thread"
                )
            if (
                self.classification == "SCIENTIFIC_RESULT"
                and not self.verifier_sandbox_id
            ):
                raise ContractError(
                    "verified linear attempt requires a verifier sandbox"
                )
            if len(self.prior_round_receipt_sha256s) != self.attempt_index - 1:
                raise ContractError("linear attempt must bind all prior round records")
            if len(self.prior_round_history_paths) != self.attempt_index - 1:
                raise ContractError(
                    "linear attempt must bind all prior round histories"
                )
            if any(
                not _full_sha256(value) for value in self.prior_round_receipt_sha256s
            ):
                raise ContractError("prior round record digest is invalid")
        if self.schema_version == 3:
            if not self.validation_session_id or not self.researcher_thread_id:
                raise ContractError(
                    "persistent attempt requires session and Researcher thread"
                )
            if (
                self.classification == "SCIENTIFIC_RESULT"
                and not self.verifier_sandbox_id
            ):
                raise ContractError(
                    "persistent scientific round requires a verifier sandbox"
                )
            if self.prior_round_receipt_sha256s or self.prior_round_history_paths:
                raise ContractError(
                    "persistent rounds do not inject serialized history"
                )
            if not _full_sha256(self.control_result_sha256):
                raise ContractError("persistent round lacks bound result evidence")
            if self.classification == "SCIENTIFIC_RESULT":
                if not _full_sha256(self.control_decision_sha256):
                    raise ContractError(
                        "persistent scientific round lacks Teacher decision"
                    )
            elif self.control_decision_sha256 is not None:
                raise ContractError(
                    "persistent platform failure cannot bind a Teacher decision"
                )


@dataclass(frozen=True)
class HealthEvidence:
    """题目完成前必须满足的非难度健康门。"""

    oracle_full_score: bool
    independent_honest_executed: bool
    adversarial_low_score: bool
    environment_stable: bool
    package_compliant: bool
    no_hidden_leakage: bool

    @property
    def preflight_passed(self) -> bool:
        """首次解题前只排除尚未由真实 trace 证明的 Stable 环境门。"""
        return all(
            (
                self.oracle_full_score,
                self.independent_honest_executed,
                self.adversarial_low_score,
                self.package_compliant,
                self.no_hidden_leakage,
            )
        )

    @property
    def passed(self) -> bool:
        """返回是否所有健康门都已通过。"""
        return all(self.__dict__.values())


@dataclass(frozen=True)
class ValidationDecision:
    """由证据账本确定性计算出的下一动作。"""

    action: str
    reason: str
    scientific_blind_count: int
    consecutive_timeouts: int


def decide_validation(
    attempts: Iterable[AttemptEvidence],
    health: HealthEvidence,
    *,
    pass_threshold: float = 0.85,
    final_health_bound: bool = False,
) -> ValidationDecision:
    """计算下一验证门；最终完成还必须有绑定当前运行的完整健康证据。"""
    evidence = list(attempts)
    for item in evidence:
        item.validate()
    _validate_freshness(evidence)
    scientific = [
        item for item in evidence if item.classification == "SCIENTIFIC_RESULT"
    ]
    blind = [item for item in scientific if item.mode == "blind"]
    hinted = [item for item in scientific if item.mode == "hint"]
    consecutive_timeouts = _consecutive_timeouts(evidence)
    if consecutive_timeouts >= 2:
        return ValidationDecision(
            "HUMAN_REVIEW",
            "two consecutive scientific timeouts",
            len(blind),
            consecutive_timeouts,
        )
    if evidence and evidence[-1].classification == "DEFERRED_TIMEOUT":
        return ValidationDecision(
            "DEFER_NEXT_QUESTION",
            "scientific execution reached 3600 seconds",
            len(blind),
            consecutive_timeouts,
        )
    if evidence and evidence[-1].classification in {
        "ENVIRONMENT_FAILURE",
        "HARNESS_FAILURE",
        "PLATFORM_FAILURE",
    }:
        return ValidationDecision(
            "RETRY_SAME_ATTEMPT",
            "non-scientific failure does not count",
            len(blind),
            consecutive_timeouts,
        )
    if evidence and evidence[-1].classification == "EVIDENCE_INCOMPLETE":
        return ValidationDecision(
            "RETRY_SAME_ATTEMPT",
            "canonical Harbor evidence is incomplete and does not count",
            len(blind),
            consecutive_timeouts,
        )
    if evidence and evidence[-1].classification == "EXECUTION_CONTRACT_FAILURE":
        return ValidationDecision(
            "TEACHER_AUDIT",
            "Researcher crossed a role or environment boundary",
            len(blind),
            consecutive_timeouts,
        )
    if any(item.score >= pass_threshold for item in blind if item.score is not None):
        return ValidationDecision(
            "TOO_EASY",
            "a fresh blind attempt met the pass threshold",
            len(blind),
            consecutive_timeouts,
        )
    continuous_session = bool(scientific) and all(
        item.schema_version in {2, 3} for item in scientific
    )
    if continuous_session and len(blind) < 3:
        return ValidationDecision(
            "NEXT_BLIND",
            "continue the same Researcher session only after a failed blind round",
            len(blind),
            consecutive_timeouts,
        )
    if len(blind) < 3:
        return ValidationDecision(
            "NEXT_BLIND",
            "three scientific blind failures are required",
            len(blind),
            consecutive_timeouts,
        )
    if any(item.score >= pass_threshold for item in hinted if item.score is not None):
        if not continuous_session and (
            not health.passed
            or not final_health_bound
            or not all(item.leakage_free for item in scientific)
        ):
            return ValidationDecision(
                "BLOCKED_HEALTH",
                "difficulty passed but a health gate failed",
                len(blind),
                consecutive_timeouts,
            )
        return ValidationDecision(
            "VALIDATION_PASSED",
            "three blind failures and a healthy hinted pass",
            len(blind),
            consecutive_timeouts,
        )
    if len(hinted) >= 2:
        return ValidationDecision(
            "HUMAN_REVIEW",
            "two reviewed non-answer hints did not establish solvability",
            len(blind),
            consecutive_timeouts,
        )
    return ValidationDecision(
        "NEXT_HINT",
        "blind gate passed; issue the next reviewed non-answer hint",
        len(blind),
        consecutive_timeouts,
    )


def _validate_freshness(attempts: list[AttemptEvidence]) -> None:
    scientific = [
        item for item in attempts if item.classification == "SCIENTIFIC_RESULT"
    ]
    if not scientific:
        _validate_execution_identity_freshness(attempts)
        return
    digests = {item.frozen_contract_digest for item in scientific}
    if len(digests) != 1:
        raise ContractError("scientific attempts do not share one frozen contract")
    schemas = {item.schema_version for item in scientific}
    if len(schemas) != 1:
        raise ContractError("scientific attempts cannot mix validation protocols")
    persistent_session = schemas == {3}
    if persistent_session:
        _validate_persistent_identities(scientific)
    else:
        _validate_execution_identity_freshness(attempts)
    linear_session = schemas == {2}
    if linear_session:
        validation_sessions = {item.validation_session_id for item in scientific}
        researcher_threads = {item.researcher_thread_id for item in scientific}
        if len(validation_sessions) != 1:
            raise ContractError("linear attempts must share one validation session")
        if len(researcher_threads) != 1:
            raise ContractError("linear attempts must use the same Researcher thread")
        for position, item in enumerate(scientific):
            if len(item.prior_round_receipt_sha256s) != position:
                raise ContractError("linear attempts must bind all prior round records")
    freshness_fields = ["job_id", "trial_id", "sandbox_id", "session_id"]
    if linear_session:
        freshness_fields.append("verifier_sandbox_id")
    if not persistent_session:
        for field in freshness_fields:
            values = [getattr(item, field) for item in scientific]
            if len(values) != len(set(values)):
                raise ContractError(f"scientific attempts must use fresh {field}")
    modes = [item.mode for item in scientific]
    first_hint = next(
        (index for index, mode in enumerate(modes) if mode == "hint"), len(modes)
    )
    if first_hint < len(modes) and (
        first_hint != 3 or any(mode == "blind" for mode in modes[first_hint:])
    ):
        raise ContractError(
            "hint attempts require exactly three preceding scientific blinds"
        )
    if len([mode for mode in modes if mode == "blind"]) > 3:
        raise ContractError("only three scientific blind attempts are allowed")
    blind_indexes = [item.attempt_index for item in scientific if item.mode == "blind"]
    if blind_indexes != list(range(1, len(blind_indexes) + 1)):
        raise ContractError("blind attempt indexes must be contiguous from one")
    hint_indexes = [item.attempt_index for item in scientific if item.mode == "hint"]
    expected_hint_indexes = (
        list(range(4, 4 + len(hint_indexes)))
        if linear_session or persistent_session
        else list(range(1, len(hint_indexes) + 1))
    )
    if hint_indexes != expected_hint_indexes:
        raise ContractError("hint attempt indexes must be contiguous from one")
    if len(hint_indexes) > 2:
        raise ContractError("at most two reviewed hint attempts are allowed")


def _validate_persistent_identities(scientific: list[AttemptEvidence]) -> None:
    """A persistent session reuses Agent identities and refreshes only verifiers."""
    for field in (
        "validation_session_id",
        "researcher_thread_id",
        "job_id",
        "trial_id",
        "sandbox_id",
        "session_id",
    ):
        values = {getattr(item, field) for item in scientific}
        if len(values) != 1 or not next(iter(values)):
            raise ContractError(f"persistent rounds must share one {field}")
    verifiers = [item.verifier_sandbox_id for item in scientific]
    if len(verifiers) != len(set(verifiers)) or any(not value for value in verifiers):
        raise ContractError(
            "persistent rounds require a fresh verifier sandbox each time"
        )
    if scientific[0].sandbox_id in verifiers:
        raise ContractError(
            "persistent Agent and verifier sandboxes must remain separate"
        )


def _validate_execution_identity_freshness(attempts: list[AttemptEvidence]) -> None:
    """任何已创建的 Harbor 执行身份都不能在平台重试或科学轮次间复用。"""
    for field in (
        "job_id",
        "trial_id",
        "sandbox_id",
        "verifier_sandbox_id",
        "session_id",
    ):
        values = [getattr(item, field) for item in attempts if getattr(item, field)]
        if len(values) != len(set(values)):
            raise ContractError(f"attempts must use fresh {field}")
    provider_sandboxes = [
        identity
        for item in attempts
        for identity in (item.sandbox_id, item.verifier_sandbox_id)
        if identity
    ]
    if len(provider_sandboxes) != len(set(provider_sandboxes)):
        raise ContractError("attempts must not reuse a provider sandbox across roles")


def _consecutive_timeouts(attempts: list[AttemptEvidence]) -> int:
    count = 0
    for item in reversed(attempts):
        if item.classification != "DEFERRED_TIMEOUT":
            break
        count += 1
    return count


def _full_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

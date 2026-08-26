"""带角色约束的单题 TaskFoundry 应用模块。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .labwright import EnvironmentReceipt
from .health import BoundHealthEvidence
from .model import Actor, ContractError, QuestionDesignBrief, RunSnapshot, RunState
from .package import lint_package
from .policy import file_sha256
from .question_types import QuestionTypeRegistry
from .researcher import ResearcherReceipt, ResearcherRequest
from .store import RunStore
from .validation import AttemptEvidence, HealthEvidence, decide_validation


class WorkflowError(RuntimeError):
    """角色或状态尝试非法流程转换时抛出。"""


class RunWorkflow:
    """只允许通过证据充分且角色有权的转换推进运行。"""

    def __init__(self, store: RunStore, question_types: QuestionTypeRegistry | None = None) -> None:
        self.store = store
        self.question_types = question_types or QuestionTypeRegistry()

    @property
    def snapshot(self) -> RunSnapshot:
        """返回当前运行快照。"""
        current = self.store.read_snapshot()
        if current is None:
            raise WorkflowError("run is not initialized")
        return current

    def attach_brief(self, actor: Actor, path: Path, idempotency_key: str) -> RunSnapshot:
        """绑定独立生成并通过题型校验的设计大纲。"""
        current = self._guard(actor, {Actor.TEACHER}, {RunState.DESIGNING})
        if not path.is_file():
            raise ContractError("QuestionDesignBrief file is missing")
        brief = QuestionDesignBrief.from_dict(self._json_object(path))
        self.question_types.validate(brief)
        next_snapshot = self.store.advance(current, brief_path=str(path.resolve()))
        return self._commit(actor, "brief.attached", idempotency_key, {"sha256": file_sha256(path)}, next_snapshot)

    def lock_policies(self, actor: Actor, path: Path, idempotency_key: str) -> RunSnapshot:
        """出题前锁定固定规范和题型规范。"""
        current = self._guard(actor, {Actor.TEACHER}, {RunState.DESIGNING})
        if current.brief_path is None or not path.is_file():
            raise WorkflowError("brief and policy lock are both required")
        next_snapshot = self.store.advance(
            current,
            state=RunState.POLICIES_LOCKED,
            policy_lock_path=str(path.resolve()),
        )
        return self._commit(actor, "policies.locked", idempotency_key, {"sha256": file_sha256(path)}, next_snapshot)

    def request_environment(self, actor: Actor, idempotency_key: str) -> RunSnapshot:
        """把环境配置控制权交给 Labwright。"""
        current = self._guard(actor, {Actor.TEACHER}, {RunState.POLICIES_LOCKED})
        next_snapshot = self.store.advance(current, state=RunState.ENVIRONMENT_DISCOVERY)
        return self._commit(actor, "environment.requested", idempotency_key, {}, next_snapshot)

    def environment_ready(
        self,
        actor: Actor,
        receipt: EnvironmentReceipt,
        idempotency_key: str,
    ) -> RunSnapshot:
        """只接受 Stable 状态的 Labwright 回执。"""
        current = self._guard(actor, {Actor.LABWRIGHT}, {RunState.ENVIRONMENT_DISCOVERY})
        receipt.validate()
        next_snapshot = self.store.advance(
            current,
            state=RunState.ENVIRONMENT_READY,
            environment_key=receipt.environment_key,
            evidence=current.evidence | {"environment": receipt.to_dict()},
        )
        return self._commit(actor, "environment.ready", idempotency_key, {"key": receipt.environment_key}, next_snapshot)

    def begin_authoring(self, actor: Actor, idempotency_key: str) -> RunSnapshot:
        """规范锁定后开始出题；零 attempt 的旧环境发现 run 留痕迁入。"""
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {
                RunState.POLICIES_LOCKED,
                RunState.ENVIRONMENT_DISCOVERY,
                RunState.ENVIRONMENT_READY,
            },
        )
        migrating_runtime_first = current.state is RunState.ENVIRONMENT_DISCOVERY
        if migrating_runtime_first and current.attempts:
            raise WorkflowError("runtime-first migration only accepts legacy runs without attempts")
        next_snapshot = self.store.advance(current, state=RunState.AUTHORING)
        if migrating_runtime_first:
            return self._commit(
                actor,
                "authoring.runtime_first.migrated",
                idempotency_key,
                {"from_state": RunState.ENVIRONMENT_DISCOVERY.value},
                next_snapshot,
            )
        return self._commit(actor, "authoring.started", idempotency_key, {}, next_snapshot)

    def refresh_revision_environment(
        self,
        actor: Actor,
        receipt: EnvironmentReceipt,
        idempotency_key: str,
    ) -> RunSnapshot:
        """下次冻结题包前绑定修订后的公开环境。"""
        current = self._guard(actor, {Actor.LABWRIGHT}, {RunState.AUTHORING})
        superseded = current.evidence.get("superseded_revision")
        if not isinstance(superseded, dict) or current.package_path is not None:
            raise WorkflowError("environment refresh requires an unfrozen evidence-backed revision")
        receipt.validate()
        next_snapshot = self.store.advance(
            current,
            environment_key=receipt.environment_key,
            evidence=current.evidence | {"environment": receipt.to_dict()},
        )
        return self._commit(
            actor,
            "revision.environment.refreshed",
            idempotency_key,
            {"key": receipt.environment_key},
            next_snapshot,
        )

    def freeze_package(self, actor: Actor, package: Path, idempotency_key: str) -> RunSnapshot:
        """冻结一份合规题包摘要，供 Oracle 和 Researcher 使用。"""
        current = self._guard(actor, {Actor.TEACHER}, {RunState.AUTHORING})
        report = lint_package(package)
        if not report.passed:
            raise WorkflowError("package lint failed")
        next_snapshot = self.store.advance(
            current,
            state=RunState.PACKAGE_FROZEN,
            package_path=report.package_path,
            package_digest=report.sha256,
            evidence=current.evidence | {"package_lint": report.to_dict()},
        )
        return self._commit(actor, "package.frozen", idempotency_key, {"sha256": report.sha256}, next_snapshot)

    def accept_health(
        self,
        actor: Actor,
        health: HealthEvidence,
        evidence_refs: Iterable[str],
        idempotency_key: str,
    ) -> RunSnapshot:
        """仅由独立 Reviewer 记录首次 blind 前的快速健康门。"""
        current = self._guard(actor, {Actor.REVIEWER}, {RunState.PACKAGE_FROZEN})
        refs = tuple(evidence_refs)
        if not health.preflight_passed or not refs:
            raise WorkflowError("preflight health gates and evidence references are required")
        state = RunState.ORACLE_PASSED if health.passed else RunState.PREFLIGHT_PASSED
        next_snapshot = self.store.advance(
            current,
            state=state,
            evidence=current.evidence | {"health": asdict(health), "health_evidence_refs": refs},
        )
        event_type = "health.accepted" if health.passed else "health.preflight.accepted"
        return self._commit(actor, event_type, idempotency_key, {"refs": refs}, next_snapshot)

    def accept_bound_health(
        self,
        actor: Actor,
        bundle: BoundHealthEvidence,
        idempotency_key: str,
    ) -> RunSnapshot:
        """只接受绑定当前题包、环境和修订的完整健康回执。"""
        current = self._guard(
            actor,
            {Actor.REVIEWER},
            {
                RunState.PACKAGE_FROZEN, RunState.BLIND_VALIDATION, RunState.HINT_VALIDATION,
                RunState.TOO_EASY, RunState.DEFERRED_TIMEOUT, RunState.BLOCKED,
            },
        )
        if current.package_digest is None or current.environment_key is None:
            raise WorkflowError("frozen package and Stable environment are required")
        if current.state is not RunState.PACKAGE_FROZEN and not any(
            item.get("classification") == "SCIENTIFIC_RESULT" for item in current.attempts
        ):
            raise WorkflowError("runtime bound health requires a scientific trace")
        bundle.assert_current(
            current.package_digest,
            current.environment_key,
            current.question_revision,
        )
        next_state = current.state
        next_evidence = current.evidence | {
            "health": asdict(bundle.health),
            "bound_health": bundle.to_dict(),
        }
        if current.state is RunState.PACKAGE_FROZEN:
            next_state = RunState.ORACLE_PASSED
        elif current.state is RunState.BLOCKED:
            if current.evidence.get("validation_decision", {}).get("action") != "BLOCKED_HEALTH":
                raise WorkflowError("bound health cannot recover another blocked condition")
            decision = decide_validation(
                (AttemptEvidence(**item) for item in current.attempts),
                bundle.health,
                final_health_bound=True,
            )
            if decision.action != "COMPLETE":
                raise WorkflowError("bound health does not satisfy final completion gates")
            next_state = RunState.COMPLETED
            next_evidence["validation_decision"] = asdict(decision)
        next_snapshot = self.store.advance(
            current,
            state=next_state,
            evidence=next_evidence,
        )
        return self._commit(
            actor,
            "health.bound.accepted",
            idempotency_key,
            {"revision": bundle.question_revision},
            next_snapshot,
        )

    def start_blind_validation(self, actor: Actor, idempotency_key: str) -> RunSnapshot:
        """快速健康门通过后开放首次 blind；最终完成仍要求完整健康门。"""
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.PREFLIGHT_PASSED, RunState.ORACLE_PASSED},
        )
        next_snapshot = self.store.advance(current, state=RunState.BLIND_VALIDATION)
        return self._commit(actor, "validation.blind.started", idempotency_key, {}, next_snapshot)

    def bind_runtime_environment(
        self,
        actor: Actor,
        receipt: EnvironmentReceipt,
        idempotency_key: str,
    ) -> RunSnapshot:
        """首次科学 trace 后绑定由真实运行时增量固化的 Stable 环境。"""
        current = self._guard(
            actor,
            {Actor.LABWRIGHT},
            {
                RunState.BLIND_VALIDATION,
                RunState.HINT_VALIDATION,
                RunState.TOO_EASY,
                RunState.DEFERRED_TIMEOUT,
            },
        )
        scientific = [
            item for item in current.attempts if item.get("classification") == "SCIENTIFIC_RESULT"
        ]
        if not scientific:
            raise WorkflowError("runtime environment requires a scientific trace")
        receipt.validate()
        next_snapshot = self.store.advance(
            current,
            environment_key=receipt.environment_key,
            evidence=current.evidence
            | {
                "environment": receipt.to_dict(),
            },
        )
        return self._commit(
            actor,
            "runtime.environment.bound",
            idempotency_key,
            {"key": receipt.environment_key},
            next_snapshot,
        )

    def begin_revision(
        self,
        actor: Actor,
        revision: str,
        reason_evidence: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """难度或可解性未达标时返回 Teacher 修订。"""
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.TOO_EASY, RunState.HINT_VALIDATION},
        )
        if not revision or not reason_evidence.is_file():
            raise WorkflowError("revision identity and too-easy evidence are required")
        if (
            current.state is RunState.HINT_VALIDATION
            and current.evidence.get("validation_decision", {}).get("action") != "NEXT_HINT"
        ):
            raise WorkflowError("solvability revision requires audited failed hint evidence")
        next_snapshot = self.store.advance(
            current,
            state=RunState.AUTHORING,
            question_revision=revision,
            attempts=(),
            package_path=None,
            package_digest=None,
            evidence=current.evidence | {
                "superseded_revision": {
                    "next_revision": revision,
                    "reason_evidence": str(reason_evidence.resolve()),
                    "reason_sha256": file_sha256(reason_evidence),
                }
            },
        )
        return self._commit(
            actor, "revision.started", idempotency_key, {"revision": revision}, next_snapshot
        )

    def begin_environment_revision(
        self,
        actor: Actor,
        revision: str,
        reason_evidence: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """环境契约变化时替换尚未正式尝试的冻结修订。"""
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.PACKAGE_FROZEN, RunState.ORACLE_PASSED, RunState.BLIND_VALIDATION},
        )
        if current.attempts:
            raise WorkflowError("environment migration cannot discard audited attempts")
        if not revision or not reason_evidence.is_file():
            raise WorkflowError("environment migration requires revision and reason evidence")
        next_snapshot = self.store.advance(
            current,
            state=RunState.AUTHORING,
            question_revision=revision,
            attempts=(),
            package_path=None,
            package_digest=None,
            evidence=current.evidence
            | {
                "superseded_revision": {
                    "next_revision": revision,
                    "reason_evidence": str(reason_evidence.resolve()),
                    "reason_sha256": file_sha256(reason_evidence),
                    "reason_kind": "ENVIRONMENT_MIGRATION",
                }
            },
        )
        return self._commit(
            actor,
            "revision.environment.started",
            idempotency_key,
            {"revision": revision},
            next_snapshot,
        )

    def audit_researcher_receipt(
        self,
        actor: Actor,
        *,
        request_path: Path,
        capability_path: Path,
        receipt_path: Path,
        leakage_evidence_path: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """记录尝试前核对不可变 Researcher 产物。"""
        request = ResearcherRequest.from_dict(self._json_object(request_path))
        receipt = ResearcherReceipt(**self._json_object(receipt_path))
        capability = self._json_object(capability_path)
        current = self.snapshot
        if (
            capability.get("status") != "CONSUMED"
            or capability.get("request_sha256") != file_sha256(request_path)
            or receipt.request_id != request.request_id
        ):
            raise WorkflowError("attempt is not backed by a consumed Researcher capability")
        if (
            request.run_id != current.run_id
            or request.question_revision != current.question_revision
            or request.package_sha256 != current.package_digest
        ):
            raise WorkflowError("attempt does not target this run and frozen package")
        if not leakage_evidence_path.is_file():
            raise WorkflowError("attempt requires independent leakage audit evidence")
        identifiers = (receipt.job_id, receipt.trial_id, receipt.sandbox_id, receipt.session_id)
        if receipt.classification == "SCIENTIFIC_RESULT" and not all(identifiers):
            raise WorkflowError("scientific receipt lacks fresh execution identities")
        if receipt.result_path:
            result = Path(receipt.result_path)
            if not result.is_file() or receipt.result_sha256 != file_sha256(result):
                raise WorkflowError("Researcher result bytes do not match its receipt")
        attempt = AttemptEvidence(
            request_id=request.request_id,
            attempt_index=request.attempt_index,
            mode=request.mode,
            classification=receipt.classification,
            score=receipt.reward,
            frozen_contract_digest=request.package_sha256,
            job_id=receipt.job_id or f"failure:{request.request_id}",
            trial_id=receipt.trial_id or f"failure:{request.request_id}",
            sandbox_id=receipt.sandbox_id or f"failure:{request.request_id}",
            session_id=receipt.session_id or f"failure:{request.request_id}",
            wall_time_sec=receipt.wall_time_sec or 0,
            context_sha256=request.context_digests[0] if request.context_digests else None,
            hint_sha256=request.context_digests[0] if request.mode == "hint" else None,
            leakage_free=True,
            leakage_evidence_sha256=file_sha256(leakage_evidence_path),
        )
        return self._record_attempt(actor, attempt, idempotency_key)

    def _record_attempt(
        self,
        actor: Actor,
        attempt: AttemptEvidence,
        idempotency_key: str,
    ) -> RunSnapshot:
        """审计完成的 Researcher 回执并计算下一状态。"""
        current = self._guard(
            actor,
            {Actor.TEACHER, Actor.REVIEWER},
            {RunState.BLIND_VALIDATION, RunState.HINT_VALIDATION, RunState.DEFERRED_TIMEOUT},
        )
        attempts = tuple((*current.attempts, asdict(attempt)))
        evidence_objects = [AttemptEvidence(**value) for value in attempts]
        health = HealthEvidence(**current.evidence["health"])
        decision = decide_validation(
            evidence_objects,
            health,
            final_health_bound="bound_health" in current.evidence,
        )
        state = {
            "NEXT_BLIND": RunState.BLIND_VALIDATION,
            "NEXT_HINT": RunState.HINT_VALIDATION,
            "RETRY_SAME_ATTEMPT": current.state,
            "TEACHER_AUDIT": RunState.BLOCKED,
            "DEFER_NEXT_QUESTION": RunState.DEFERRED_TIMEOUT,
            "HUMAN_REVIEW": RunState.HUMAN_REVIEW,
            "TOO_EASY": RunState.TOO_EASY,
            "BLOCKED_HEALTH": RunState.BLOCKED,
            "COMPLETE": RunState.COMPLETED,
        }[decision.action]
        next_snapshot = self.store.advance(
            current,
            state=state,
            attempts=attempts,
            consecutive_timeouts=decision.consecutive_timeouts,
            evidence=current.evidence | {"validation_decision": asdict(decision)},
        )
        return self._commit(
            actor,
            "attempt.audited",
            idempotency_key,
            {"request_id": attempt.request_id, "decision": decision.action},
            next_snapshot,
        )

    @staticmethod
    def _json_object(path: Path) -> dict:
        if not path.is_file():
            raise WorkflowError(f"evidence file is missing: {path}")
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise WorkflowError("evidence JSON must be an object")
        return value

    def _guard(self, actor: Actor, actors: set[Actor], states: set[RunState]) -> RunSnapshot:
        current = self.snapshot
        if actor not in actors:
            raise WorkflowError(f"{actor.value} does not own this transition")
        if current.state not in states:
            raise WorkflowError(f"transition is invalid from {current.state.value}")
        return current

    def _commit(
        self,
        actor: Actor,
        event_type: str,
        idempotency_key: str,
        payload: dict,
        snapshot: RunSnapshot,
    ) -> RunSnapshot:
        return self.store.commit(
            actor=actor,
            event_type=event_type,
            idempotency_key=idempotency_key,
            payload=payload,
            snapshot=snapshot,
        )[1]

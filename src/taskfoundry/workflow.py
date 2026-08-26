"""带角色约束的单题 TaskFoundry 应用模块。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .labwright import ArtifactIdentity, EnvironmentReceipt
from .labwright_runtime import DeltaReceipt, DeltaState, ImageSealPlan
from .health import BoundHealthEvidence, EvidenceReference, HealthGate
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
        migrating_runtime_first = current.state in {
            RunState.ENVIRONMENT_DISCOVERY,
            RunState.ENVIRONMENT_READY,
        }
        if migrating_runtime_first and current.attempts:
            raise WorkflowError("runtime-first migration only accepts legacy runs without attempts")
        next_snapshot = self.store.advance(
            current,
            state=RunState.AUTHORING,
            environment_key=None if migrating_runtime_first else current.environment_key,
            evidence=(
                self._revision_neutral_evidence(current.evidence)
                if migrating_runtime_first
                else current.evidence
            ),
        )
        if migrating_runtime_first:
            return self._commit(
                actor,
                "authoring.runtime_first.migrated",
                idempotency_key,
                {"from_state": current.state.value},
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

    def attach_design_evidence(
        self,
        actor: Actor,
        *,
        source_role_map_path: Path,
        ground_truth_ledger_path: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """在冻结题包前绑定来源角色图和可独立复算的 Ground Truth 账本。"""
        current = self._guard(actor, {Actor.TEACHER}, {RunState.AUTHORING})
        if current.brief_path is None:
            raise WorkflowError("design evidence requires the bound question brief")
        brief_path = Path(current.brief_path)
        brief = QuestionDesignBrief.from_dict(self._json_object(brief_path))
        brief_sha256 = file_sha256(brief_path)
        role_map = self._json_object(source_role_map_path)
        expected_identity = {
            "schema_version": 1,
            "question_revision": current.question_revision,
            "brief_sha256": brief_sha256,
        }
        if any(role_map.get(key) != value for key, value in expected_identity.items()):
            raise WorkflowError("source role map does not bind the current brief and revision")
        if (
            role_map.get("evidence_type") != "source-role-map"
            or role_map.get("source_ids") != [item.source_id for item in brief.source_questions]
            or role_map.get("roles") != brief.evidence_roles
            or role_map.get("license_review_pass") is not True
        ):
            raise WorkflowError("source role map is incomplete or inconsistent with the brief")
        source_hashes = role_map.get("immutable_source_sha256s")
        if (
            not isinstance(source_hashes, dict)
            or set(source_hashes) != {item.source_id for item in brief.source_questions}
            or any(not self._full_sha256(value) for value in source_hashes.values())
        ):
            raise WorkflowError("source role map requires one immutable digest per source")
        ledger = self._json_object(ground_truth_ledger_path)
        if any(ledger.get(key) != value for key, value in expected_identity.items()):
            raise WorkflowError("Ground Truth ledger does not bind the current brief and revision")
        quantities = ledger.get("scored_quantities")
        required_hashes = (
            "producer_sha256",
            "independent_crosscheck_sha256",
            "scoring_contract_sha256",
        )
        if (
            ledger.get("evidence_type") != "ground-truth-ledger"
            or ledger.get("derived_reference") is not True
            or ledger.get("crosscheck_pass") is not True
            or not isinstance(quantities, list)
            or not quantities
            or any(not isinstance(item, str) or not item.strip() for item in quantities)
            or any(not self._full_sha256(ledger.get(key)) for key in required_hashes)
        ):
            raise WorkflowError("Ground Truth ledger is incomplete or not independently closed")
        evidence = {
            "source_role_map": {
                "path": str(source_role_map_path.resolve()),
                "sha256": file_sha256(source_role_map_path),
            },
            "ground_truth_ledger": {
                "path": str(ground_truth_ledger_path.resolve()),
                "sha256": file_sha256(ground_truth_ledger_path),
            },
        }
        next_snapshot = self.store.advance(
            current,
            evidence=current.evidence | {"design_evidence": evidence},
        )
        return self._commit(
            actor,
            "design.evidence.attached",
            idempotency_key,
            {name: value["sha256"] for name, value in evidence.items()},
            next_snapshot,
        )

    def freeze_package(self, actor: Actor, package: Path, idempotency_key: str) -> RunSnapshot:
        """冻结一份合规题包摘要，供 Oracle 和 Researcher 使用。"""
        current = self._guard(actor, {Actor.TEACHER}, {RunState.AUTHORING})
        design_evidence = current.evidence.get("design_evidence")
        if not isinstance(design_evidence, dict) or not self._design_evidence_unchanged(
            design_evidence
        ):
            raise WorkflowError("source role map and Ground Truth ledger are required before freeze")
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
        if not self._current_runtime_environment_bound(current):
            raise WorkflowError("bound health requires the current runtime closure and Stable receipt")
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
            if decision.action != "VALIDATION_PASSED":
                raise WorkflowError("bound health does not satisfy final completion gates")
            next_state = RunState.VALIDATION_PASSED
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
                RunState.BLOCKED,
            },
        )
        if (
            current.state is RunState.BLOCKED
            and current.evidence.get("validation_decision", {}).get("action") != "BLOCKED_HEALTH"
        ):
            raise WorkflowError("runtime environment cannot recover another blocked condition")
        scientific = [
            item for item in current.attempts if item.get("classification") == "SCIENTIFIC_RESULT"
        ]
        if not scientific:
            raise WorkflowError("runtime environment requires a scientific trace")
        receipt.validate()
        closure = current.evidence.get("runtime_closure")
        if (
            not isinstance(closure, dict)
            or receipt.schema_version != 2
            or receipt.runtime_closure_path != closure.get("path")
            or receipt.runtime_closure_sha256 != closure.get("sha256")
        ):
            raise WorkflowError("Stable environment does not bind the accepted runtime closure")
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

    def accept_runtime_delta(
        self,
        actor: Actor,
        receipt_path: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """接纳绑定 source Researcher 与独立 builder 的运行时增量。"""
        current = self._guard(
            actor,
            {Actor.LABWRIGHT},
            {RunState.BLIND_VALIDATION, RunState.HINT_VALIDATION, RunState.DEFERRED_TIMEOUT},
        )
        receipt = self._delta_receipt(receipt_path)
        if (
            receipt.run_id != current.run_id
            or receipt.question_revision != current.question_revision
            or receipt.package_sha256 != current.package_digest
        ):
            raise WorkflowError("runtime delta targets another run, revision, or package")
        source = next(
            (
                item
                for item in current.attempts
                if item.get("request_id") == receipt.source_researcher_request_id
                and item.get("sandbox_id") == receipt.source_sandbox_id
            ),
            None,
        )
        if not isinstance(source, dict) or source.get("classification") not in {
            "ENVIRONMENT_FAILURE",
            "HARNESS_FAILURE",
            "PLATFORM_FAILURE",
        }:
            raise WorkflowError("runtime delta lacks its audited source failure")
        delta_evidence = dict(current.evidence.get("runtime_deltas", {}))
        if receipt.request_id in delta_evidence:
            raise WorkflowError("runtime delta request is already accepted")
        delta_evidence[receipt.request_id] = {
            "path": str(receipt_path.resolve()),
            "sha256": file_sha256(receipt_path),
            "receipt": receipt.to_dict(),
            "source_attempt_index": source.get("attempt_index"),
        }
        next_snapshot = self.store.advance(
            current,
            evidence=current.evidence | {"runtime_deltas": delta_evidence},
        )
        return self._commit(
            actor,
            "runtime.delta.accepted",
            idempotency_key,
            {
                "request_id": receipt.request_id,
                "source_researcher_request_id": receipt.source_researcher_request_id,
                "builder_runtime_request_id": receipt.builder_runtime_request_id,
            },
            next_snapshot,
        )

    def accept_runtime_closure(
        self,
        actor: Actor,
        plan_path: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """把 fresh 科学 trace 与已验证 delta 闭合为 Stable 构建输入。"""
        current = self._guard(
            actor,
            {Actor.LABWRIGHT},
            {
                RunState.BLIND_VALIDATION,
                RunState.HINT_VALIDATION,
                RunState.TOO_EASY,
                RunState.DEFERRED_TIMEOUT,
                RunState.BLOCKED,
            },
        )
        plan = self._image_seal_plan(plan_path)
        if (
            plan.run_id != current.run_id
            or plan.question_revision != current.question_revision
            or plan.package_sha256 != current.package_digest
        ):
            raise WorkflowError("runtime closure targets another run, revision, or package")
        trace = self._json_object(Path(plan.scientific_trace_path))
        scientific = self._runtime_scientific_attempt(current, trace)
        self._validate_accepted_deltas(current, plan, scientific)
        next_snapshot = self.store.advance(
            current,
            evidence=current.evidence
            | {
                "runtime_closure": {
                    "path": str(plan_path.resolve()),
                    "sha256": file_sha256(plan_path),
                    "scientific_request_id": scientific["request_id"],
                    "scientific_sandbox_id": scientific["sandbox_id"],
                    "delta_request_ids": [value["request_id"] for value in plan.delta_receipts],
                }
            },
        )
        return self._commit(
            actor,
            "runtime.closure.accepted",
            idempotency_key,
            {"sha256": file_sha256(plan_path)},
            next_snapshot,
        )

    def begin_revision(
        self,
        actor: Actor,
        revision: str,
        reason_evidence: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """fresh blind 过线时返回 Teacher 做同题科学难度修订。"""
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.TOO_EASY},
        )
        if not revision or not reason_evidence.is_file():
            raise WorkflowError("revision identity and too-easy evidence are required")
        if current.evidence.get("validation_decision", {}).get("action") != "TOO_EASY":
            raise WorkflowError("difficulty revision requires the matching audited decision")
        reason = self._json_object(reason_evidence)
        expected = {
            "schema_version": 1,
            "evidence_type": "difficulty-revision",
            "source_package_sha256": current.package_digest,
            "next_revision": revision,
            "scientific_objective_unchanged": True,
        }
        if any(reason.get(key) != value for key, value in expected.items()):
            raise WorkflowError("difficulty revision evidence does not bind the audited revision")
        if not isinstance(reason.get("change_summary"), str) or not reason["change_summary"].strip():
            raise WorkflowError("difficulty revision requires a non-empty change summary")
        next_snapshot = self.store.advance(
            current,
            state=RunState.AUTHORING,
            question_revision=revision,
            attempts=(),
            package_path=None,
            package_digest=None,
            environment_key=None,
            evidence=self._revision_neutral_evidence(current.evidence) | {
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
            environment_key=None,
            evidence=self._revision_neutral_evidence(current.evidence)
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
        hint_review_path: Path | None = None,
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
        leakage = self._json_object(leakage_evidence_path)
        leakage_expected = {
            "schema_version": 1,
            "evidence_type": "leakage-audit",
            "verdict": "PASS",
            "reviewer_independent": True,
            "leakage_free": True,
            "request_id": request.request_id,
            "package_sha256": current.package_digest,
            "job_id": receipt.job_id,
            "trial_id": receipt.trial_id,
            "sandbox_id": receipt.sandbox_id,
            "session_id": receipt.session_id,
        }
        if any(leakage.get(key) != value for key, value in leakage_expected.items()):
            raise WorkflowError("attempt requires a bound independent leakage PASS")
        hint_sha256, extra_evidence = self._approved_hint(
            current,
            request,
            hint_review_path,
        )
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
            hint_sha256=hint_sha256,
            leakage_free=True,
            leakage_evidence_sha256=file_sha256(leakage_evidence_path),
        )
        return self._record_attempt(actor, attempt, idempotency_key, extra_evidence=extra_evidence)

    def _record_attempt(
        self,
        actor: Actor,
        attempt: AttemptEvidence,
        idempotency_key: str,
        *,
        extra_evidence: dict | None = None,
    ) -> RunSnapshot:
        """审计完成的 Researcher 回执并计算下一状态。"""
        current = self._guard(
            actor,
            {Actor.REVIEWER},
            {RunState.BLIND_VALIDATION, RunState.HINT_VALIDATION, RunState.DEFERRED_TIMEOUT},
        )
        if current.state is RunState.BLIND_VALIDATION and attempt.mode != "blind":
            raise WorkflowError("blind validation accepts only blind attempts")
        if current.state is RunState.HINT_VALIDATION and attempt.mode != "hint":
            raise WorkflowError("hint validation accepts only reviewed hint attempts")
        attempts = tuple((*current.attempts, asdict(attempt)))
        evidence_objects = [AttemptEvidence(**value) for value in attempts]
        health = HealthEvidence(**current.evidence["health"])
        decision = decide_validation(
            evidence_objects,
            health,
            final_health_bound=self._current_final_health_bound(current),
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
            "VALIDATION_PASSED": RunState.VALIDATION_PASSED,
        }[decision.action]
        next_snapshot = self.store.advance(
            current,
            state=state,
            attempts=attempts,
            consecutive_timeouts=decision.consecutive_timeouts,
            evidence=current.evidence
            | (extra_evidence or {})
            | {"validation_decision": asdict(decision)},
        )
        return self._commit(
            actor,
            "attempt.audited",
            idempotency_key,
            {"request_id": attempt.request_id, "decision": decision.action},
            next_snapshot,
        )

    def accept_post_validation(
        self,
        actor: Actor,
        evidence_path: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """独立最终复审通过后才把已验证题目标为完成。"""
        current = self._guard(actor, {Actor.REVIEWER}, {RunState.VALIDATION_PASSED})
        evidence = self._json_object(evidence_path)
        expected = {
            "schema_version": 1,
            "evidence_type": "post-validation",
            "verdict": "PASS_FINAL_PROGRESSION",
            "reviewer_independent": True,
            "package_sha256": current.package_digest,
            "question_revision": current.question_revision,
        }
        if any(evidence.get(key) != value for key, value in expected.items()):
            raise WorkflowError("post-validation evidence does not bind the current revision")
        if not self._current_final_health_bound(current):
            raise WorkflowError("post-validation requires current runtime closure and bound health")
        next_snapshot = self.store.advance(
            current,
            state=RunState.COMPLETED,
            evidence=current.evidence
            | {
                "post_validation": {
                    "path": str(evidence_path.resolve()),
                    "sha256": file_sha256(evidence_path),
                }
            },
        )
        return self._commit(
            actor,
            "validation.post.accepted",
            idempotency_key,
            {"sha256": file_sha256(evidence_path)},
            next_snapshot,
        )

    @staticmethod
    def _revision_neutral_evidence(evidence: dict) -> dict:
        """移除会随题包、环境或科学修订失效的证据。"""
        revision_scoped = {
            "package_lint",
            "design_evidence",
            "health",
            "health_evidence_refs",
            "bound_health",
            "environment",
            "runtime_deltas",
            "runtime_closure",
            "validation_decision",
            "hint_reviews",
            "post_validation",
        }
        return {key: value for key, value in evidence.items() if key not in revision_scoped}

    @staticmethod
    def _design_evidence_unchanged(evidence: dict) -> bool:
        """复验两份设计证据引用仍指向相同常规文件。"""
        try:
            for name in ("source_role_map", "ground_truth_ledger"):
                link = evidence[name]
                path = Path(link["path"])
                if path.is_symlink() or not path.is_file() or file_sha256(path) != link["sha256"]:
                    return False
        except (KeyError, OSError, TypeError):
            return False
        return True

    @staticmethod
    def _full_sha256(value: object) -> bool:
        """判断值是否为小写完整 SHA-256。"""
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _current_final_health_bound(self, current: RunSnapshot) -> bool:
        """复验当前修订的 closure、Stable 回执和完整健康证据。"""
        try:
            bound = current.evidence.get("bound_health")
            if not isinstance(bound, dict) or not self._current_runtime_environment_bound(current):
                return False
            bound_value = dict(bound)
            bound_value["health"] = HealthEvidence(**bound_value["health"])
            bound_value["references"] = tuple(
                EvidenceReference(
                    gate=HealthGate(item["gate"]),
                    path=item["path"],
                    sha256=item["sha256"],
                )
                for item in bound_value["references"]
            )
            health = BoundHealthEvidence(**bound_value)
            health.assert_current(
                str(current.package_digest),
                str(current.environment_key),
                current.question_revision,
            )
        except (ContractError, KeyError, OSError, TypeError, ValueError, WorkflowError):
            return False
        return True

    def _current_runtime_environment_bound(self, current: RunSnapshot) -> bool:
        """复验当前题包的 runtime closure 与 schema-v2 Stable 回执。"""
        try:
            closure = current.evidence.get("runtime_closure")
            environment = current.evidence.get("environment")
            if not isinstance(closure, dict) or not isinstance(environment, dict):
                return False
            closure_path = Path(closure["path"])
            if file_sha256(closure_path) != closure["sha256"]:
                return False
            plan = self._image_seal_plan(closure_path)
            if (
                plan.run_id != current.run_id
                or plan.question_revision != current.question_revision
                or plan.package_sha256 != current.package_digest
            ):
                return False
            environment_value = dict(environment)
            environment_value["artifact"] = ArtifactIdentity(**environment_value["artifact"])
            environment_value["resource_digests"] = tuple(environment_value["resource_digests"])
            receipt = EnvironmentReceipt(**environment_value)
            receipt.validate()
            if (
                current.environment_key != receipt.environment_key
                or receipt.schema_version != 2
                or receipt.runtime_closure_path != str(closure_path.resolve())
                or receipt.runtime_closure_sha256 != closure["sha256"]
            ):
                return False
        except (ContractError, KeyError, OSError, TypeError, ValueError, WorkflowError):
            return False
        return True

    @staticmethod
    def _runtime_scientific_attempt(current: RunSnapshot, trace: dict) -> dict:
        """找出 closure 绑定的 fresh 科学重试并核对冻结题包。"""
        scientific = next(
            (
                item
                for item in current.attempts
                if item.get("classification") == "SCIENTIFIC_RESULT"
                and item.get("request_id") == trace.get("researcher_request_id")
                and item.get("sandbox_id") == trace.get("sandbox_id")
            ),
            None,
        )
        if not isinstance(scientific, dict):
            raise WorkflowError("runtime closure lacks its audited fresh scientific retry")
        if scientific.get("frozen_contract_digest") != current.package_digest:
            raise WorkflowError("runtime closure scientific retry targets another package")
        return scientific

    @staticmethod
    def _validate_accepted_deltas(
        current: RunSnapshot,
        plan: ImageSealPlan,
        scientific: dict,
    ) -> None:
        """确认 closure 只包含本 run 已接纳且保持 attempt 身份的 delta。"""
        accepted = current.evidence.get("runtime_deltas", {})
        if not isinstance(accepted, dict):
            raise WorkflowError("accepted runtime delta ledger is invalid")
        for value in plan.delta_receipts:
            evidence = accepted.get(value.get("request_id"))
            if not isinstance(evidence, dict) or evidence.get("receipt") != value:
                raise WorkflowError("runtime closure contains an unaccepted delta receipt")
            if evidence.get("source_attempt_index") != scientific.get("attempt_index"):
                raise WorkflowError("runtime recovery changed the scientific attempt identity")

    def _approved_hint(
        self,
        current: RunSnapshot,
        request: ResearcherRequest,
        hint_review_path: Path | None,
    ) -> tuple[str | None, dict]:
        """读取并绑定 hint review；blind 返回空 hint evidence。"""
        if request.mode != "hint":
            return None, {}
        if hint_review_path is None:
            raise WorkflowError("hint attempt requires independent hint review evidence")
        review = self._json_object(hint_review_path)
        approved = review.get("approved_context_digests")
        if (
            review.get("schema_version") != 1
            or review.get("evidence_type") != "hint-review"
            or review.get("verdict") != "PASS"
            or review.get("contains_answer") is not False
            or review.get("package_sha256") != current.package_digest
            or review.get("question_revision") != current.question_revision
            or not isinstance(approved, list)
            or approved != list(request.context_digests)
            or review.get("hint_sha256") not in approved
        ):
            raise WorkflowError("hint review does not approve this frozen request")
        hint_sha256 = review["hint_sha256"]
        evidence = {
            "hint_reviews": current.evidence.get("hint_reviews", {})
            | {
                hint_sha256: {
                    "path": str(hint_review_path.resolve()),
                    "sha256": file_sha256(hint_review_path),
                }
            }
        }
        return hint_sha256, evidence

    @classmethod
    def _delta_receipt(cls, path: Path) -> DeltaReceipt:
        """严格读取并复验一份 schema-v2 增量回执。"""
        value = cls._json_object(path)
        try:
            converted = dict(value)
            converted["state"] = DeltaState(converted["state"])
            converted["baseline_artifact"] = ArtifactIdentity(**converted["baseline_artifact"])
            converted["builder_artifact"] = ArtifactIdentity(**converted["builder_artifact"])
            receipt = DeltaReceipt(**converted)
        except (KeyError, TypeError, ValueError) as error:
            raise WorkflowError("runtime delta receipt schema is invalid") from error
        receipt.validate()
        return receipt

    @classmethod
    def _image_seal_plan(cls, path: Path) -> ImageSealPlan:
        """严格读取并复验一份 runtime-first Stable 构建计划。"""
        value = cls._json_object(path)
        try:
            converted = dict(value)
            converted["baseline_artifact"] = ArtifactIdentity(**converted["baseline_artifact"])
            converted["delta_receipts"] = tuple(converted["delta_receipts"])
            plan = ImageSealPlan(**converted)
        except (KeyError, TypeError, ValueError) as error:
            raise WorkflowError("runtime closure schema is invalid") from error
        plan.validate()
        return plan

    @staticmethod
    def _json_object(path: Path) -> dict:
        if not path.is_file():
            raise WorkflowError(f"evidence file is missing: {path}")
        import json

        def strict_object(pairs: list[tuple[str, object]]) -> dict:
            """把单层 JSON 对象转换成拒绝重复键的字典。"""
            value: dict = {}
            for key, item in pairs:
                if key in value:
                    raise WorkflowError(f"duplicate evidence JSON key: {key}")
                value[key] = item
            return value

        def reject_nonfinite(token: str) -> None:
            """拒绝 Python JSON 解码器默认放行的非有限常量。"""
            raise WorkflowError(f"non-finite evidence JSON number: {token}")

        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=strict_object,
            parse_constant=reject_nonfinite,
        )
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

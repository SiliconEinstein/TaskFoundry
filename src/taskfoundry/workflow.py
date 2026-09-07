"""带角色约束的单题 TaskFoundry 应用模块。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .labwright import ArtifactIdentity, EnvironmentReceipt, atomic_json
from .labwright_runtime import DeltaReceipt, DeltaState, ImageSealPlan
from .launch import LaunchContractError, freeze_package_snapshot
from .harbor_evidence import (
    HarborEvidenceImporter,
    VerifiedPersistentSession,
    compact_legacy_round_history,
)
from .model import Actor, ContractError, QuestionDesignBrief, RunSnapshot, RunState
from .policy import file_sha256
from .question_types import QuestionTypeRegistry
from .researcher import (
    CapabilityStore,
    ApprovedHint,
    IssuedHandoff,
    ResearcherError,
    ResearcherRequest,
    runtime_researcher_identity,
)
from .skillbank import validate_activation, validate_latest_brief
from .store import RunStore
from .validation import (
    AttemptEvidence,
    HealthEvidence,
    JobClassification,
    decide_validation,
)
from .validation_control import TeacherDecision, TeacherValidationController
from .validation_session import ValidationSessionRegistry


class WorkflowError(RuntimeError):
    """角色或状态尝试非法流程转换时抛出。"""


class RunWorkflow:
    """只允许通过证据充分且角色有权的转换推进运行。"""

    def __init__(
        self,
        store: RunStore,
        question_types: QuestionTypeRegistry | None = None,
        *,
        skill_validator: Callable[[Path, int, str], dict] | None = None,
        brief_locator: Callable[[int], tuple[Path, Path]] | None = None,
    ) -> None:
        self.store = store
        self.question_types = question_types or QuestionTypeRegistry()
        self.skill_validator = skill_validator or (
            lambda path, question, stage: validate_activation(
                path,
                question=question,
                stage=stage,
            )
        )
        self.brief_locator = brief_locator or validate_latest_brief

    @property
    def snapshot(self) -> RunSnapshot:
        """返回当前运行快照。"""
        current = self.store.read_snapshot()
        if current is None:
            raise WorkflowError("run is not initialized")
        return current

    def attach_brief(
        self, actor: Actor, path: Path, idempotency_key: str
    ) -> RunSnapshot:
        """绑定独立生成并通过题型校验的设计大纲。"""
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "brief.attached",
            {"sha256": file_sha256(path) if path.is_file() else ""},
        )
        if repeated is not None:
            return repeated
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.DESIGNING, RunState.AUTHORING},
        )
        skill_contract = current.evidence.get("teacher_skill_contract")
        if not isinstance(skill_contract, dict) or "outline" not in skill_contract:
            raise WorkflowError(
                "outline Skill activation must be bound before the brief"
            )
        question = skill_contract.get("question")
        if type(question) is not int:
            raise WorkflowError("Teacher Skill contract lacks its question number")
        json_path, markdown_path = self.brief_locator(question)
        if path.resolve() != json_path.resolve():
            raise WorkflowError(
                "brief must be the numbered question's current JSON brief"
            )
        if not path.is_file():
            raise ContractError("QuestionDesignBrief file is missing")
        brief = QuestionDesignBrief.from_dict(self._json_object(path))
        self.question_types.validate(brief)
        markdown = markdown_path.read_text(encoding="utf-8")
        if brief.brief_id not in markdown or brief.title not in markdown:
            raise WorkflowError(
                "JSON and Markdown QuestionDesignBrief do not identify the same brief"
            )
        brief_evidence = {
            "json_path": str(json_path.resolve()),
            "json_sha256": file_sha256(json_path),
            "markdown_path": str(markdown_path.resolve()),
            "markdown_sha256": file_sha256(markdown_path),
            "brief_id": brief.brief_id,
        }
        next_snapshot = self.store.advance(
            current,
            brief_path=str(path.resolve()),
            evidence=current.evidence | {"question_brief": brief_evidence},
        )
        return self._commit(
            actor,
            "brief.attached",
            idempotency_key,
            {"sha256": file_sha256(path)},
            next_snapshot,
        )

    def accept_teacher_skill_activation(
        self,
        actor: Actor,
        *,
        question: int,
        stage: str,
        activation_path: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """把 outline/author 的完整 SkillFoundry activation 绑定到正式 run。"""
        if stage not in {"outline", "author"}:
            raise WorkflowError("Teacher Skill stage must be outline or author")
        activation_sha256 = (
            file_sha256(activation_path) if activation_path.is_file() else ""
        )
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "teacher.skill.bound",
            {"stage": stage, "activation_sha256": activation_sha256},
        )
        if repeated is not None:
            return repeated
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.DESIGNING, RunState.AUTHORING},
        )
        contract = dict(current.evidence.get("teacher_skill_contract", {}))
        if contract and contract.get("question") != question:
            raise WorkflowError("Teacher Skill stages target different questions")
        if stage == "author" and not isinstance(
            current.evidence.get("question_brief"), dict
        ):
            raise WorkflowError(
                "author Skill activation requires the bound latest brief"
            )
        value = self.skill_validator(activation_path, question, stage)
        prompt = Path(str(value["prompt_bundle"]["path"]))
        record = {
            "activation_path": str(activation_path.resolve()),
            "activation_sha256": activation_sha256,
            "prompt_path": str(prompt.resolve()),
            "prompt_sha256": file_sha256(prompt),
            "batch_id": value.get("batch_id"),
            "batch_questions": value.get("batch_questions"),
            "stable_generation": value.get("stable_generation"),
            "stable_lock_sha256": value.get("stable_lock_snapshot", {}).get("sha256"),
            "input_set_sha256": value.get("input_set_sha256"),
        }
        contract.update(question=question)
        contract[stage] = record
        next_snapshot = self.store.advance(
            current,
            evidence=current.evidence | {"teacher_skill_contract": contract},
        )
        return self._commit(
            actor,
            "teacher.skill.bound",
            idempotency_key,
            {"stage": stage, "activation_sha256": activation_sha256},
            next_snapshot,
        )

    def lock_policies(
        self, actor: Actor, path: Path, idempotency_key: str
    ) -> RunSnapshot:
        """旧零散 policy lock 已退出正式路径；规范只能来自冻结 Skill activation。"""
        raise WorkflowError(
            "standalone policy locks are superseded by Teacher Skill activation"
        )

    def begin_authoring(self, actor: Actor, idempotency_key: str) -> RunSnapshot:
        """outline、Brief 与 author Skill 全部绑定且未漂移后开始出题。"""
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "authoring.started",
            {},
        )
        if repeated is not None:
            return repeated
        current = self._guard(actor, {Actor.TEACHER}, {RunState.DESIGNING})
        self._revalidate_teacher_knowledge(current, require_author=True)
        next_snapshot = self.store.advance(current, state=RunState.AUTHORING)
        return self._commit(
            actor, "authoring.started", idempotency_key, {}, next_snapshot
        )

    def freeze_and_start_validation_session(
        self,
        actor: Actor,
        *,
        package: Path,
        validation_session_id: str,
        researcher_thread_id: str | None,
        idempotency_key: str,
    ) -> RunSnapshot:
        """最小启动检查后原子冻结题包并打开线性 Harbor 验证会话。"""
        from .package import package_sha256

        source_package_sha256 = package_sha256(package)
        effective_researcher_id = researcher_thread_id or runtime_researcher_identity(
            validation_session_id
        )
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "validation.session.started",
            {
                "validation_session_id": validation_session_id,
                "researcher_thread_id": effective_researcher_id,
                "source_package_sha256": source_package_sha256,
            },
        )
        if repeated is not None:
            return repeated
        current = self._guard(actor, {Actor.TEACHER}, {RunState.AUTHORING})
        self._revalidate_teacher_knowledge(current, require_author=True)
        if not validation_session_id.strip() or not effective_researcher_id.strip():
            raise WorkflowError("validation session and Researcher thread are required")
        snapshot_path = (
            self.store.run_dir
            / "question-revisions"
            / current.question_revision
            / "task"
        )
        try:
            report = freeze_package_snapshot(package, snapshot_path)
        except LaunchContractError as error:
            raise WorkflowError(f"package launch probe failed: {error}") from error
        registry = self._validation_session_registry()
        registry.reserve(
            run_id=current.run_id,
            question_revision=current.question_revision,
            validation_session_id=validation_session_id,
            researcher_thread_id=effective_researcher_id,
            package_sha256=report.package_sha256,
        )
        session = {
            "schema_version": 2,
            "validation_session_id": validation_session_id,
            "researcher_thread_id": effective_researcher_id,
            "execution_owner": (
                "harbor-runtime"
                if researcher_thread_id is None
                else "desktop-thread"
            ),
            "question_revision": current.question_revision,
            "package_sha256": report.package_sha256,
            "status": "ACTIVE",
        }
        next_snapshot = self.store.advance(
            current,
            state=RunState.BLIND_VALIDATION,
            package_path=report.package_path,
            package_digest=report.package_sha256,
            attempts=(),
            evidence=current.evidence
            | {
                "package_lint": report.to_dict(),
                "validation_session": session,
            },
        )
        return self._commit(
            actor,
            "validation.session.started",
            idempotency_key,
            {
                "package_sha256": report.package_sha256,
                "source_package_sha256": source_package_sha256,
                "validation_session_id": validation_session_id,
                "researcher_thread_id": effective_researcher_id,
            },
            next_snapshot,
        )

    def recover_validation_session_from_request(
        self, actor: Actor, request_path: Path, idempotency_key: str
    ) -> RunSnapshot:
        """从已封存的真实 Harbor request 恢复丢失的 validation session。"""
        current = self._guard(actor, {Actor.TEACHER}, {RunState.PACKAGE_FROZEN})
        request = self._json_object(request_path)
        if (
            request.get("run_id") != current.run_id
            or request.get("package_sha256") != current.package_digest
            or not request.get("request_id")
        ):
            raise WorkflowError("recovery request does not match frozen run")
        session_id = str(request.get("validation_session_id") or f"recovered:{request['request_id']}")
        researcher_id = str(request.get("researcher_thread_id", ""))
        if not researcher_id:
            raise WorkflowError("recovery request lacks researcher thread")
        self._validation_session_registry().reserve(
            run_id=current.run_id,
            question_revision=current.question_revision,
            validation_session_id=session_id,
            researcher_thread_id=researcher_id,
            package_sha256=current.package_digest or "",
        )
        session = {
            "schema_version": 2,
            "validation_session_id": session_id,
            "researcher_thread_id": researcher_id,
            "execution_owner": "harbor-recovery",
            "question_revision": current.question_revision,
            "package_sha256": current.package_digest,
            "status": "ACTIVE",
            "recovered_from_request": str(request_path.resolve()),
        }
        next_snapshot = self.store.advance(
            current, state=RunState.BLIND_VALIDATION,
            evidence=current.evidence | {"validation_session": session},
        )
        return self._commit(actor, "validation.session.recovered", idempotency_key, {"request_id": request["request_id"]}, next_snapshot)

    def issue_researcher_request(
        self,
        actor: Actor,
        *,
        request_id: str,
        job_config_path: Path,
        approved_hint_path: Path | None = None,
        closed_request_ids: tuple[str, ...] = (),
    ) -> IssuedHandoff:
        """从当前线性会话派生并签发唯一合法 Researcher 请求。"""
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {
                RunState.BLIND_VALIDATION,
                RunState.HINT_VALIDATION,
                RunState.DEFERRED_TIMEOUT,
            },
        )
        session = current.evidence.get("validation_session")
        if not isinstance(session, dict) or session.get("status") != "ACTIVE":
            raise WorkflowError("an active direct validation session is required")
        receipts = tuple(session.get("round_receipt_sha256s", ()))
        histories = tuple(session.get("round_history_paths", ()))
        if len(receipts) != len(histories):
            raise WorkflowError("validation session history ledger is inconsistent")
        if not job_config_path.is_file():
            raise WorkflowError(
                "Researcher launch artifact is unavailable: file is missing"
            )
        try:
            job_config = self._json_object(job_config_path)
            agents = job_config.get("agents", [])
            persistent_payload = (
                agents[0].get("kwargs", {}).get("persistent_validation")
                if isinstance(agents, list)
                and len(agents) == 1
                and isinstance(agents[0], dict)
                else None
            )
        except (OSError, UnicodeError) as error:
            raise WorkflowError(
                f"Researcher launch artifact is unavailable: {error}"
            ) from error
        if isinstance(persistent_payload, dict):
            return self._issue_persistent_researcher_request(
                current=current,
                session=session,
                request_id=request_id,
                job_config_path=job_config_path,
                persistent_payload=persistent_payload,
                approved_hint_path=approved_hint_path,
                closed_request_ids=closed_request_ids,
            )
        mode = "hint" if current.state is RunState.HINT_VALIDATION else "blind"
        round_index = len(receipts) + 1
        if mode == "hint" and approved_hint_path is None:
            raise WorkflowError("hint validation requires a Teacher-approved hint")
        if mode == "blind" and approved_hint_path is not None:
            raise WorkflowError("blind validation cannot include a Teacher hint")
        try:
            hint_sha256 = (
                file_sha256(approved_hint_path)
                if approved_hint_path is not None
                else None
            )
            job_config_sha256 = file_sha256(job_config_path)
        except OSError as error:
            raise WorkflowError(
                f"Researcher launch artifact is unavailable: {error}"
            ) from error
        request = ResearcherRequest(
            request_id=request_id,
            run_id=current.run_id,
            question_revision=current.question_revision,
            attempt_index=round_index,
            mode=mode,
            package_path=str(current.package_path),
            package_sha256=str(current.package_digest),
            job_config_path=str(job_config_path.resolve()),
            job_config_sha256=job_config_sha256,
            context_digests=receipts + ((hint_sha256,) if hint_sha256 else ()),
            researcher_thread_id=str(session.get("researcher_thread_id", "")),
            validation_session_id=str(session.get("validation_session_id", "")),
            round_index=round_index,
            prior_round_receipt_sha256s=receipts,
            prior_round_history_paths=histories,
            approved_hint_path=(
                str(approved_hint_path.resolve()) if approved_hint_path else None
            ),
            approved_hint_sha256=hint_sha256,
            schema_version=2,
        )
        round_ledger = current.evidence.get("harbor_rounds", {})
        if not isinstance(round_ledger, dict):
            raise WorkflowError("canonical Harbor round ledger is invalid")
        closed_request_ids = tuple(
            request_id_value
            for request_id_value, evidence in round_ledger.items()
            if isinstance(request_id_value, str)
            and isinstance(evidence, dict)
            and evidence.get("classification") != "SCIENTIFIC_RESULT"
            and isinstance(evidence.get("request"), dict)
            and evidence["request"].get("validation_session_id")
            == request.validation_session_id
            and evidence["request"].get("round_index") == request.round_index
        )
        try:
            request.validate()
            return CapabilityStore(self.store.run_dir / "researcher-requests").issue(
                Actor.TEACHER,
                request,
                closed_request_ids=closed_request_ids,
            )
        except (ContractError, OSError, ResearcherError) as error:
            raise WorkflowError(
                f"Researcher request does not match current workflow: {error}"
            ) from error

    def _issue_persistent_researcher_request(
        self,
        *,
        current: RunSnapshot,
        session: dict,
        request_id: str,
        job_config_path: Path,
        persistent_payload: dict,
        approved_hint_path: Path | None,
        closed_request_ids: tuple[str, ...],
    ) -> IssuedHandoff:
        """Issue one request for the whole Teacher-controlled validation session."""
        if current.state is not RunState.BLIND_VALIDATION or any(
            item.get("classification") == "SCIENTIFIC_RESULT"
            for item in current.attempts
        ):
            raise WorkflowError(
                "persistent validation retry is allowed only before any scientific round"
            )
        if approved_hint_path is not None:
            raise WorkflowError(
                "persistent hints are published by Teacher between rounds"
            )
        if persistent_payload.get("validation_session_id") != session.get(
            "validation_session_id"
        ):
            raise WorkflowError(
                "persistent JobConfig targets another validation session"
            )
        controller_dir = Path(str(persistent_payload.get("controller_dir", "")))
        job_config = self._json_object(job_config_path)
        agents = job_config.get("agents", [])
        model_name = (
            agents[0].get("model_name")
            if isinstance(agents, list)
            and len(agents) == 1
            and isinstance(agents[0], dict)
            else None
        )
        if not isinstance(model_name, str) or not model_name:
            raise WorkflowError("persistent JobConfig model identity is invalid")
        session_ledger = current.evidence.get("persistent_harbor_sessions", {})
        if not isinstance(session_ledger, dict):
            raise WorkflowError("persistent Harbor session ledger is invalid")
        request = ResearcherRequest(
            request_id=request_id,
            run_id=current.run_id,
            question_revision=current.question_revision,
            attempt_index=1,
            mode="interactive",
            package_path=str(current.package_path),
            package_sha256=str(current.package_digest),
            job_config_path=str(job_config_path.resolve()),
            job_config_sha256=file_sha256(job_config_path),
            context_digests=(),
            researcher_thread_id=str(session.get("researcher_thread_id", "")),
            validation_session_id=str(session.get("validation_session_id", "")),
            controller_dir=str(controller_dir.resolve()),
            model=model_name,
            schema_version=(
                4
                if str(session.get("execution_owner", "")) == "harbor-runtime"
                else 3
            ),
        )
        try:
            request.validate()
            return CapabilityStore(self.store.run_dir / "researcher-requests").issue(
                Actor.TEACHER,
                request,
                closed_request_ids=tuple(dict.fromkeys((*session_ledger, *closed_request_ids))),
            )
        except (ContractError, OSError, ResearcherError) as error:
            raise WorkflowError(
                f"persistent Researcher request is invalid: {error}"
            ) from error

    def decide_persistent_validation_round(
        self,
        actor: Actor,
        *,
        request_id: str,
        round_index: int,
        decision: TeacherDecision,
    ) -> Path:
        """Publish Teacher's next-round decision while Harbor keeps Agent alive."""
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.BLIND_VALIDATION, RunState.HINT_VALIDATION},
        )
        session = current.evidence.get("validation_session")
        if not isinstance(session, dict) or session.get("status") != "ACTIVE":
            raise WorkflowError("an active validation session is required")
        request_path = (
            self.store.run_dir / "researcher-requests" / request_id / "request.json"
        )
        try:
            request = ResearcherRequest.from_dict(self._json_object(request_path))
        except (ContractError, OSError, UnicodeError) as error:
            raise WorkflowError(
                f"persistent request cannot be loaded: {error}"
            ) from error
        if (
            request.schema_version not in {3, 4}
            or request.validation_session_id != session.get("validation_session_id")
            or request.package_sha256 != current.package_digest
        ):
            raise WorkflowError("persistent request targets another active session")
        controller = TeacherValidationController(
            Path(str(request.controller_dir)),
            str(request.validation_session_id),
        )
        result_path = controller.root / f"round-{round_index:02d}-result.json"
        try:
            return controller.decide(result_path, decision)
        except ContractError as error:
            raise WorkflowError(
                f"Teacher validation decision is invalid: {error}"
            ) from error

    def bind_runtime_environment(
        self,
        actor: Actor,
        receipt: EnvironmentReceipt,
        idempotency_key: str,
    ) -> RunSnapshot:
        """首次科学 trace 后绑定由真实运行时增量固化的 Stable 环境。"""
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "runtime.environment.bound",
            {"key": receipt.environment_key},
        )
        if repeated is not None:
            return repeated
        current = self._guard(
            actor,
            {Actor.LABWRIGHT},
            {RunState.RUNTIME_FINALIZATION},
        )
        scientific = [
            item
            for item in current.attempts
            if item.get("classification") == "SCIENTIFIC_RESULT"
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
            raise WorkflowError(
                "Stable environment does not bind the accepted runtime closure"
            )
        next_snapshot = self.store.advance(
            current,
            state=RunState.PUBLICATION_PENDING,
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
            {
                RunState.BLIND_VALIDATION,
                RunState.HINT_VALIDATION,
                RunState.DEFERRED_TIMEOUT,
                RunState.RUNTIME_FINALIZATION,
            },
        )
        receipt = self._delta_receipt(receipt_path)
        if (
            receipt.run_id != current.run_id
            or receipt.question_revision != current.question_revision
            or receipt.package_sha256 != current.package_digest
        ):
            raise WorkflowError(
                "runtime delta targets another run, revision, or package"
            )
        source = next(
            (
                item
                for item in current.attempts
                if item.get("request_id") == receipt.source_researcher_request_id
                and item.get("sandbox_id") == receipt.source_sandbox_id
            ),
            None,
        )
        accepted_failure_classes = {
            "ENVIRONMENT_FAILURE",
            "HARNESS_FAILURE",
            "PLATFORM_FAILURE",
        }
        source_classification = source.get("classification") if isinstance(source, dict) else None
        scientific_capability_failure = (
            isinstance(source, dict)
            and source_classification == "SCIENTIFIC_RESULT"
            and self._audited_scientific_capability_failure(current, receipt)
        )
        if not isinstance(source, dict) or (
            source_classification not in accepted_failure_classes
            and not scientific_capability_failure
        ):
            raise WorkflowError("runtime delta lacks its audited source failure")
        next_evidence = dict(current.evidence)
        if current.state is RunState.RUNTIME_FINALIZATION:
            existing_closure = next_evidence.get("runtime_closure")
            if isinstance(existing_closure, dict):
                if (
                    existing_closure.get("delta_request_ids") != []
                    or existing_closure.get("scientific_request_id")
                    != receipt.source_researcher_request_id
                    or existing_closure.get("scientific_sandbox_id")
                    != receipt.source_sandbox_id
                    or not scientific_capability_failure
                ):
                    raise WorkflowError(
                        "late runtime delta cannot supersede the accepted closure"
                    )
                superseded = list(
                    next_evidence.get("superseded_runtime_closures", [])
                )
                superseded.append(
                    {
                        "path": existing_closure.get("path"),
                        "sha256": existing_closure.get("sha256"),
                        "reason": (
                            "TRACE_BACKED_DELTA_DISCOVERED_DURING_FINALIZATION"
                        ),
                    }
                )
                next_evidence["superseded_runtime_closures"] = superseded
                next_evidence.pop("runtime_closure")
        delta_evidence = dict(current.evidence.get("runtime_deltas", {}))
        if receipt.request_id in delta_evidence:
            raise WorkflowError("runtime delta request is already accepted")
        delta_evidence[receipt.request_id] = {
            "path": str(receipt_path.resolve()),
            "sha256": file_sha256(receipt_path),
            "receipt": receipt.to_dict(),
            "source_attempt_index": source.get("attempt_index"),
        }
        next_evidence["runtime_deltas"] = delta_evidence
        next_snapshot = self.store.advance(
            current,
            evidence=next_evidence,
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

    @classmethod
    def _audited_scientific_capability_failure(
        cls,
        current: RunSnapshot,
        receipt: DeltaReceipt,
    ) -> bool:
        """接受科学轮内独立记录、且不改变科学计数的能力探针失败。"""
        trace = cls._json_object(Path(receipt.source_trace_path))
        return (
            trace.get("classification")
            in {"ENVIRONMENT_FAILURE", "HARNESS_FAILURE", "PLATFORM_FAILURE"}
            and trace.get("run_id") == current.run_id
            and trace.get("question_revision") == current.question_revision
            and trace.get("package_sha256") == current.package_digest
            and trace.get("researcher_request_id")
            == receipt.source_researcher_request_id
            and trace.get("sandbox_id") == receipt.source_sandbox_id
            and trace.get("scientific_round_classification_unchanged")
            == "SCIENTIFIC_RESULT"
            and trace.get("scientific_round_count_increment") is False
            and isinstance(trace.get("failure_stage"), str)
            and bool(trace["failure_stage"].strip())
        )

    def accept_runtime_closure(
        self,
        actor: Actor,
        plan_path: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """把 fresh 科学 trace 与已验证 delta 闭合为 Stable 构建输入。"""
        closure_sha256 = file_sha256(plan_path)
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "runtime.closure.accepted",
            {"sha256": closure_sha256},
        )
        if repeated is not None:
            return repeated
        current = self._guard(
            actor,
            {Actor.LABWRIGHT},
            {RunState.RUNTIME_FINALIZATION},
        )
        plan = self._image_seal_plan(plan_path)
        if (
            plan.run_id != current.run_id
            or plan.question_revision != current.question_revision
            or plan.package_sha256 != current.package_digest
        ):
            raise WorkflowError(
                "runtime closure targets another run, revision, or package"
            )
        trace = self._json_object(Path(plan.scientific_trace_path))
        scientific = self._runtime_scientific_attempt(current, trace)
        self._validate_accepted_deltas(current, plan, scientific)
        existing_closure = current.evidence.get("runtime_closure")
        if (
            isinstance(existing_closure, dict)
            and existing_closure.get("sha256") != closure_sha256
        ):
            raise WorkflowError("runtime closure is immutable once accepted")
        next_snapshot = self.store.advance(
            current,
            evidence=current.evidence
            | {
                "runtime_closure": {
                    "path": str(plan_path.resolve()),
                    "sha256": closure_sha256,
                    "scientific_request_id": scientific["request_id"],
                    "scientific_sandbox_id": scientific["sandbox_id"],
                    "delta_request_ids": [
                        value["request_id"] for value in plan.delta_receipts
                    ],
                }
            },
        )
        return self._commit(
            actor,
            "runtime.closure.accepted",
            idempotency_key,
            {"sha256": closure_sha256},
            next_snapshot,
        )

    def begin_revision(
        self,
        actor: Actor,
        revision: str,
        reason_evidence: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """验证终局或 formal 阻断后返回 Teacher 做同题兄弟修订。"""
        self._reconcile_validation_registry(self.snapshot)
        reason_sha256 = (
            file_sha256(reason_evidence) if reason_evidence.is_file() else ""
        )
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "revision.started",
            {"revision": revision, "reason_sha256": reason_sha256},
        )
        if repeated is not None:
            return repeated
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.TOO_EASY, RunState.BLOCKED, RunState.RUNTIME_FINALIZATION},
        )
        if not revision or not reason_evidence.is_file():
            raise WorkflowError("revision identity and terminal evidence are required")
        if revision == current.question_revision:
            raise WorkflowError("difficulty revision must use a new revision identity")
        decision = current.evidence.get("validation_decision", {}).get("action")
        expected_decision = (
            "TOO_EASY"
            if current.state is RunState.TOO_EASY
            else "STOP_BLOCKED"
            if current.state is RunState.BLOCKED
            else "VALIDATION_PASSED"
        )
        if decision != expected_decision:
            raise WorkflowError("difficulty revision requires the matching audited decision")
        reason = self._json_object(reason_evidence)
        expected_evidence_type = (
            "formal-repair-revision"
            if current.state is RunState.RUNTIME_FINALIZATION
            else "difficulty-revision"
        )
        expected = {
            "schema_version": 1,
            "evidence_type": expected_evidence_type,
            "source_package_sha256": current.package_digest,
            "next_revision": revision,
            "scientific_objective_unchanged": True,
        }
        if any(reason.get(key) != value for key, value in expected.items()):
            raise WorkflowError(
                "difficulty revision evidence does not bind the audited revision"
            )
        if current.state is RunState.RUNTIME_FINALIZATION and (
            reason.get("formal_review_verdict") != "BLOCKED"
            or reason.get("required_revalidation") is not True
        ):
            raise WorkflowError(
                "formal repair revision requires a blocked review and revalidation"
            )
        if (
            not isinstance(reason.get("change_summary"), str)
            or not reason["change_summary"].strip()
        ):
            raise WorkflowError(
                "difficulty revision requires a non-empty change summary"
            )
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
                }
            },
        )
        return self._commit(
            actor,
            "revision.started",
            idempotency_key,
            {"revision": revision, "reason_sha256": reason_sha256},
            next_snapshot,
        )

    def migrate_incomplete_validation_session(
        self,
        actor: Actor,
        revision: str,
        idempotency_key: str,
    ) -> RunSnapshot:
        """关闭旧验证模型的未完成会话，并以新修订返回 authoring。"""
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "validation.session.migrated",
            {"revision": revision},
        )
        if repeated is not None:
            return repeated
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {
                RunState.PACKAGE_FROZEN,
                RunState.BLIND_VALIDATION,
                RunState.HINT_VALIDATION,
                RunState.DEFERRED_TIMEOUT,
                RunState.BLOCKED,
                RunState.HUMAN_REVIEW,
                RunState.COMPLETED,
            },
        )
        if not revision.strip() or revision == current.question_revision:
            raise WorkflowError("migration requires a distinct new revision")
        session = current.evidence.get("validation_session")
        if (
            isinstance(session, dict)
            and session.get("schema_version") == 2
            and session.get("status") == "ACTIVE"
        ):
            session_id = session.get("validation_session_id")
            if not isinstance(session_id, str) or not session_id:
                raise WorkflowError(
                    "linear session migration lacks its session identity"
                )
            self._validation_session_registry().close(
                session_id, status="CLOSED_MIGRATED"
            )
        migrated = {
            "source_revision": current.question_revision,
            "source_package_sha256": current.package_digest,
            "source_state": current.state.value,
            "status": "CLOSED_MIGRATED",
        }
        next_snapshot = self.store.advance(
            current,
            state=RunState.AUTHORING,
            question_revision=revision,
            attempts=(),
            package_path=None,
            package_digest=None,
            environment_key=None,
            evidence=self._revision_neutral_evidence(current.evidence)
            | {"migrated_validation_session": migrated},
        )
        return self._commit(
            actor,
            "validation.session.migrated",
            idempotency_key,
            {"revision": revision},
            next_snapshot,
        )

    def record_validation_round(
        self,
        actor: Actor,
        *,
        request_id: str,
        idempotency_key: str,
    ) -> RunSnapshot:
        """从 canonical Harbor 原始产物重算并接纳一轮验证证据。"""
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "attempt.audited",
            {"request_id": request_id},
        )
        if repeated is not None:
            self._reconcile_validation_registry(repeated)
            return repeated
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {
                RunState.BLIND_VALIDATION,
                RunState.HINT_VALIDATION,
                RunState.DEFERRED_TIMEOUT,
            },
        )
        session = current.evidence.get("validation_session")
        if not isinstance(session, dict) or session.get("status") != "ACTIVE":
            raise WorkflowError("an active direct validation session is required")
        verified = HarborEvidenceImporter(
            self.store.run_dir / "researcher-requests"
        ).import_round(request_id)
        request = verified.request
        expected = {
            "run_id": current.run_id,
            "question_revision": current.question_revision,
            "package_sha256": current.package_digest,
            "validation_session_id": session.get("validation_session_id"),
            "researcher_thread_id": session.get("researcher_thread_id"),
        }
        if any(getattr(request, key) != value for key, value in expected.items()):
            raise WorkflowError("round targets another package or validation session")
        accepted_receipts = tuple(session.get("round_receipt_sha256s", ()))
        accepted_history_paths = tuple(session.get("round_history_paths", ()))
        repair_ledger = current.evidence.get("history_delivery_repairs", ())
        source_receipts = tuple(
            item.get("source_sha256")
            for item in repair_ledger
            if isinstance(item, dict)
            and item.get("delivery_sha256") in accepted_receipts
        )
        source_history_paths = tuple(
            item.get("source_path")
            for item in repair_ledger
            if isinstance(item, dict)
            and item.get("delivery_sha256") in accepted_receipts
        )
        receipts_match = request.prior_round_receipt_sha256s in {
            accepted_receipts,
            source_receipts,
        }
        histories_match = request.prior_round_history_paths in {
            accepted_history_paths,
            source_history_paths,
        }
        if not receipts_match:
            raise WorkflowError("round does not bind the accepted prior records")
        if not histories_match:
            raise WorkflowError(
                "round does not bind the accepted prior history bundles"
            )
        attempt = AttemptEvidence(
            request_id=request.request_id,
            attempt_index=request.attempt_index,
            mode=request.mode,
            classification=verified.classification.value,
            score=verified.score,
            frozen_contract_digest=request.package_sha256,
            job_id=verified.job_id,
            trial_id=verified.trial_id,
            sandbox_id=verified.agent_sandbox_id,
            verifier_sandbox_id=verified.verifier_sandbox_id,
            session_id=verified.harness_session_id,
            wall_time_sec=verified.wall_time_sec,
            context_sha256=request.context_digests[0]
            if request.context_digests
            else None,
            hint_sha256=(
                request.context_digests[-1]
                if request.mode == "hint"
                and len(request.context_digests) > len(accepted_receipts)
                else None
            ),
            validation_session_id=request.validation_session_id,
            researcher_thread_id=request.researcher_thread_id,
            prior_round_receipt_sha256s=request.prior_round_receipt_sha256s,
            prior_round_history_paths=request.prior_round_history_paths,
            schema_version=2,
        )
        updated_session = dict(session)
        if verified.classification.value == "SCIENTIFIC_RESULT":
            if not verified.round_history_sha256 or not verified.round_history_path:
                raise WorkflowError(
                    "scientific result lacks its Agent-visible history bundle"
                )
            updated_session["round_receipt_sha256s"] = [
                *accepted_receipts,
                verified.round_history_sha256,
            ]
            updated_session["round_history_paths"] = [
                *accepted_history_paths,
                verified.round_history_path,
            ]
        snapshot = self._record_attempt(
            actor,
            attempt,
            idempotency_key,
            extra_evidence={
                "validation_session": updated_session,
                "harbor_rounds": {
                    **current.evidence.get("harbor_rounds", {}),
                    request.request_id: asdict(verified),
                },
            },
        )
        if snapshot.state is RunState.TOO_EASY:
            self._validation_session_registry().close(
                request.validation_session_id or "",
                status="CLOSED_TOO_EASY",
            )
        elif snapshot.state is RunState.VALIDATION_PASSED:
            self._validation_session_registry().close(
                request.validation_session_id or "",
                status="CLOSED_VALIDATION_PASSED",
            )
        return snapshot

    def decide_validation_round(
        self,
        actor: Actor,
        *,
        request_id: str,
        action: str,
        hint_path: Path | None = None,
    ) -> RunSnapshot:
        """Record a Teacher decision for a non-persistent linear session."""
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.BLIND_VALIDATION, RunState.HINT_VALIDATION},
        )
        expected = current.evidence.get("validation_decision", {}).get("action")
        if action == "CONTINUE_HINT":
            if expected != "NEXT_HINT" or hint_path is None or not hint_path.is_file():
                raise WorkflowError("direct session is not ready for a hint")
            content = hint_path.read_text(encoding="utf-8")
            if not content.strip() or len(content.encode()) > 4096:
                raise WorkflowError("hint is empty or too large")
            next_state = RunState.HINT_VALIDATION
            decision = {"action": action, "request_id": request_id,
                        "hint": content, "teacher_declares_non_answer": True,
                        "hint_sha256": file_sha256(hint_path)}
        elif action == "CONTINUE_BLIND":
            if expected != "NEXT_BLIND":
                raise WorkflowError("direct session is not ready for blind continuation")
            next_state = RunState.BLIND_VALIDATION
            decision = {"action": action, "request_id": request_id}
        elif action in {"STOP_TOO_EASY", "STOP_PASSED", "STOP_BLOCKED"}:
            if action == "STOP_TOO_EASY": next_state = RunState.TOO_EASY
            elif action == "STOP_PASSED": next_state = RunState.VALIDATION_PASSED
            else: next_state = RunState.BLOCKED
            decision = {"action": action, "request_id": request_id}
        else:
            raise WorkflowError("unsupported direct validation decision")
        session = dict(current.evidence.get("validation_session", {}))
        session["decision"] = action
        if next_state in {RunState.TOO_EASY, RunState.VALIDATION_PASSED, RunState.BLOCKED}:
            session["status"] = "CLOSED"
        snapshot = self.store.advance(
            current, state=next_state,
            evidence=current.evidence | {"validation_decision": decision,
                                          "validation_session": session},
        )
        return self._commit(actor, "validation.decision", f"validation:decision:{request_id}:{action}",
                            decision, snapshot)

    def record_persistent_validation_session(
        self,
        actor: Actor,
        *,
        request_id: str,
        idempotency_key: str,
    ) -> RunSnapshot:
        """Import every round from one completed persistent Harbor trial atomically."""
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "validation.session.audited",
            {"request_id": request_id},
        )
        if repeated is not None:
            self._reconcile_validation_registry(repeated)
            return repeated
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.BLIND_VALIDATION, RunState.HINT_VALIDATION},
        )
        session = current.evidence.get("validation_session")
        if not isinstance(session, dict) or session.get("status") != "ACTIVE":
            raise WorkflowError("an active validation session is required")
        try:
            verified = HarborEvidenceImporter(
                self.store.run_dir / "researcher-requests"
            ).import_persistent_session(request_id)
        except ContractError as error:
            raise WorkflowError(
                f"persistent Harbor evidence is invalid: {error}"
            ) from error
        request = verified.request
        expected = {
            "run_id": current.run_id,
            "question_revision": current.question_revision,
            "package_sha256": current.package_digest,
            "validation_session_id": session.get("validation_session_id"),
            "researcher_thread_id": session.get("researcher_thread_id"),
        }
        if any(getattr(request, key) != value for key, value in expected.items()):
            raise WorkflowError(
                "persistent trial targets another package or validation session"
            )
        hint_bindings = self._materialize_persistent_hints(verified)
        imported_attempts = tuple(
            AttemptEvidence(
                request_id=f"{request.request_id}.round-{item.round_index:02d}",
                attempt_index=item.round_index,
                mode=item.mode,
                classification=item.classification.value,
                score=item.score,
                frozen_contract_digest=request.package_sha256,
                job_id=verified.job_id,
                trial_id=verified.trial_id,
                sandbox_id=verified.agent_sandbox_id,
                verifier_sandbox_id=item.verifier_sandbox_id,
                session_id=verified.harness_session_id,
                wall_time_sec=item.wall_time_sec,
                hint_sha256=(
                    hint_bindings[item.round_index][1]
                    if item.round_index in hint_bindings
                    else None
                ),
                validation_session_id=request.validation_session_id,
                researcher_thread_id=request.researcher_thread_id,
                control_result_sha256=item.result_sha256,
                control_decision_sha256=item.decision_sha256,
                schema_version=request.schema_version,
            )
            for item in verified.rounds
        )
        attempts = tuple(
            [AttemptEvidence(**value) for value in current.attempts]
            + list(imported_attempts)
        )
        health = HealthEvidence(True, True, True, True, True, True)
        calculated = decide_validation(attempts, health, final_health_bound=True)
        final_round = verified.rounds[-1]
        terminal = final_round.decision_action
        platform_terminal = (
            final_round.classification is not JobClassification.SCIENTIFIC_RESULT
        )
        prior_scientific_in_runtime = any(
            item.classification is JobClassification.SCIENTIFIC_RESULT
            for item in verified.rounds[:-1]
        )
        expected_terminal = {
            "TOO_EASY": "STOP_TOO_EASY",
            "VALIDATION_PASSED": "STOP_PASSED",
        }.get(calculated.action)
        if expected_terminal is not None and terminal != expected_terminal:
            raise WorkflowError(
                "Teacher terminal action conflicts with calculated validation state"
            )
        if platform_terminal and prior_scientific_in_runtime:
            state = RunState.BLOCKED
            action = "PLATFORM_RECOVERY_REQUIRED"
            registry_status = "ACTIVE"
        elif platform_terminal:
            state = RunState.BLIND_VALIDATION
            action = "RETRY_SAME_ATTEMPT"
            registry_status = "ACTIVE"
        elif terminal == "STOP_TOO_EASY":
            state = RunState.TOO_EASY
            action = "TOO_EASY"
            registry_status = "CLOSED_TOO_EASY"
        elif terminal == "STOP_PASSED":
            state = RunState.VALIDATION_PASSED
            action = "VALIDATION_PASSED"
            registry_status = "CLOSED_VALIDATION_PASSED"
        else:
            state = RunState.BLOCKED
            action = "STOP_BLOCKED"
            registry_status = "CLOSED_BLOCKED"
        updated_session = dict(session)
        updated_session.update(
            {
                "schema_version": 3,
                "status": registry_status,
                "decision": action,
                "request_id": request.request_id,
                "job_id": verified.job_id,
                "trial_id": verified.trial_id,
                "agent_sandbox_id": verified.agent_sandbox_id,
                "harness_session_id": verified.harness_session_id,
                "round_count": len(verified.rounds),
            }
        )
        session_ledger = current.evidence.get("persistent_harbor_sessions", {})
        if not isinstance(session_ledger, dict):
            raise WorkflowError("persistent Harbor session ledger is invalid")
        verified_value = asdict(verified)
        for round_value in verified_value["rounds"]:
            binding = hint_bindings.get(round_value["round_index"])
            if binding is not None:
                round_value["approved_hint_path"] = str(binding[0].resolve())
                round_value["approved_hint_sha256"] = binding[1]
        next_snapshot = self.store.advance(
            current,
            state=state,
            attempts=tuple(asdict(item) for item in attempts),
            consecutive_timeouts=0,
            evidence=current.evidence
            | {
                "validation_session": updated_session,
                "validation_decision": {
                    **asdict(calculated),
                    "action": action,
                    "teacher_terminal_action": terminal,
                },
                "persistent_harbor_sessions": session_ledger
                | {request.request_id: verified_value},
            },
        )
        if registry_status != "ACTIVE":
            self._validation_session_registry().close(
                str(request.validation_session_id),
                status=registry_status,
            )
        return self._commit(
            actor,
            "validation.session.audited",
            idempotency_key,
            {"request_id": request_id, "decision": action},
            next_snapshot,
        )

    def _materialize_persistent_hints(
        self,
        verified: VerifiedPersistentSession,
    ) -> dict[int, tuple[Path, str]]:
        """从前一轮 Teacher 决策派生并封签持久 hint 的公共字节。"""
        bindings: dict[int, tuple[Path, str]] = {}
        rounds = {item.round_index: item for item in verified.rounds}
        for item in verified.rounds:
            if item.mode != "hint":
                continue
            predecessor = rounds.get(item.round_index - 1)
            if predecessor is None or predecessor.decision_path is None:
                raise WorkflowError("persistent hint 缺少前序 Teacher 决策")
            decision_path = Path(predecessor.decision_path)
            decision = self._json_object(decision_path)
            if (
                decision.get("action") != "CONTINUE_HINT"
                or decision.get("teacher_declares_non_answer") is not True
                or not isinstance(decision.get("hint"), str)
            ):
                raise WorkflowError("persistent hint 前序决策没有非答案提示")
            hint = ApprovedHint(
                validation_session_id=str(verified.request.validation_session_id),
                round_index=item.round_index - 3,
                content=str(decision["hint"]),
                teacher_declares_non_answer=True,
            )
            hint.validate()
            path = (
                self.store.run_dir
                / "researcher-requests"
                / verified.request.request_id
                / "approved-hints"
                / f"round-{item.round_index:02d}.json"
            )
            if path.exists():
                try:
                    if ApprovedHint.from_path(path) != hint:
                        raise WorkflowError("persistent approved hint bytes drifted")
                except ContractError as error:
                    raise WorkflowError("persistent approved hint is invalid") from error
            else:
                atomic_json(path, hint.to_dict())
            bindings[item.round_index] = (path, file_sha256(path))
        return bindings

    def repair_validation_history_delivery(
        self,
        actor: Actor,
        *,
        idempotency_key: str,
    ) -> RunSnapshot:
        """把旧全量 transcript 改为有来源绑定的紧凑交付物，不改变科学轮次。"""
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "validation.history_delivery_repaired",
            {},
        )
        if repeated is not None:
            return repeated
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {RunState.BLIND_VALIDATION, RunState.HINT_VALIDATION},
        )
        session = current.evidence.get("validation_session")
        if not isinstance(session, dict) or session.get("status") != "ACTIVE":
            raise WorkflowError("an active validation session is required")
        decision = current.evidence.get("validation_decision")
        if (
            not isinstance(decision, dict)
            or decision.get("action") != "RETRY_SAME_ATTEMPT"
        ):
            raise WorkflowError(
                "history delivery repair requires a non-scientific retry"
            )
        source_paths = tuple(session.get("round_history_paths", ()))
        source_digests = tuple(session.get("round_receipt_sha256s", ()))
        if not source_paths or len(source_paths) != len(source_digests):
            raise WorkflowError("validation history ledger is incomplete")
        repaired_paths: list[str] = []
        repaired_digests: list[str] = []
        repairs: list[dict[str, object]] = []
        repair_root = (
            self.store.run_dir / "researcher-requests/history-delivery-repairs"
        )
        for source_name, source_digest in zip(
            source_paths, source_digests, strict=True
        ):
            source = Path(source_name)
            if not source.is_file() or file_sha256(source) != source_digest:
                raise WorkflowError("accepted round history bytes drifted")
            destination = repair_root / source_digest / "round-history.json"
            try:
                digest = compact_legacy_round_history(source, destination)
            except ContractError as error:
                raise WorkflowError(
                    f"round history delivery repair failed: {error}"
                ) from error
            if destination.stat().st_size > 65536:
                raise WorkflowError(
                    "compact round history exceeds the 64 KiB delivery limit"
                )
            repaired_paths.append(str(destination.resolve()))
            repaired_digests.append(digest)
            repairs.append(
                {
                    "source_path": str(source.resolve()),
                    "source_sha256": source_digest,
                    "delivery_path": str(destination.resolve()),
                    "delivery_sha256": digest,
                    "delivery_bytes": destination.stat().st_size,
                }
            )
        updated_session = dict(session)
        updated_session["round_history_paths"] = repaired_paths
        updated_session["round_receipt_sha256s"] = repaired_digests
        evidence = dict(current.evidence)
        evidence["validation_session"] = updated_session
        evidence["history_delivery_repairs"] = [
            *evidence.get("history_delivery_repairs", []),
            *repairs,
        ]
        next_snapshot = self.store.advance(current, evidence=evidence)
        return self._commit(
            actor,
            "validation.history_delivery_repaired",
            idempotency_key,
            {},
            next_snapshot,
        )

    def begin_runtime_finalization(
        self,
        actor: Actor,
        idempotency_key: str,
    ) -> RunSnapshot:
        """科学验证通过后进入真实运行环境固化阶段。"""
        current_before = self.snapshot
        self._reconcile_validation_registry(current_before)
        repeated = self._idempotent_snapshot(
            idempotency_key,
            "runtime.finalization.started",
            {"package_sha256": current_before.package_digest},
        )
        if repeated is not None:
            return repeated
        current = self._guard(actor, {Actor.TEACHER}, {RunState.VALIDATION_PASSED})
        next_snapshot = self.store.advance(current, state=RunState.RUNTIME_FINALIZATION)
        return self._commit(
            actor,
            "runtime.finalization.started",
            idempotency_key,
            {"package_sha256": current.package_digest},
            next_snapshot,
        )

    def complete_without_runtime_environment(
        self,
        actor: Actor,
        evidence_path: Path,
        idempotency_key: str,
    ) -> RunSnapshot:
        """完成发布资格；Labwright 环境固化是可选的后续复用步骤。"""
        current = self._guard(actor, {Actor.TEACHER}, {RunState.RUNTIME_FINALIZATION})
        if not evidence_path.is_file():
            raise WorkflowError("environment reuse evidence is required")
        evidence = self._json_object(evidence_path)
        if evidence.get("status") not in {"OPTIONAL_UNAVAILABLE", "OPTIONAL_DEFERRED"}:
            raise WorkflowError("optional environment evidence must declare unavailable or deferred")
        next_evidence = dict(current.evidence)
        next_evidence["environment_reuse"] = {
            "status": evidence["status"],
            "reason": str(evidence.get("reason", "")),
            "path": str(evidence_path.resolve()),
            "sha256": file_sha256(evidence_path),
        }
        next_snapshot = self.store.advance(
            current, state=RunState.COMPLETED, evidence=next_evidence
        )
        return self._commit(
            actor,
            "publication.runtime_environment_optional",
            idempotency_key,
            {"status": evidence["status"]},
            next_snapshot,
        )

    def _record_attempt(
        self,
        actor: Actor,
        attempt: AttemptEvidence,
        idempotency_key: str,
        *,
        extra_evidence: dict | None = None,
    ) -> RunSnapshot:
        """审计完成的 Researcher 回执并计算下一状态。"""
        direct_session = isinstance(
            self.snapshot.evidence.get("validation_session"), dict
        )
        if not direct_session:
            raise WorkflowError("canonical linear validation session is required")
        current = self._guard(
            actor,
            {Actor.TEACHER},
            {
                RunState.BLIND_VALIDATION,
                RunState.HINT_VALIDATION,
                RunState.DEFERRED_TIMEOUT,
            },
        )
        if current.state is RunState.BLIND_VALIDATION and attempt.mode != "blind":
            raise WorkflowError("blind validation accepts only blind attempts")
        if current.state is RunState.HINT_VALIDATION and attempt.mode != "hint":
            raise WorkflowError("hint validation accepts only reviewed hint attempts")
        attempts = tuple((*current.attempts, asdict(attempt)))
        evidence_objects = [AttemptEvidence(**value) for value in attempts]
        health = HealthEvidence(True, True, True, True, True, True)
        decision = decide_validation(
            evidence_objects,
            health,
            final_health_bound=True,
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
        merged_evidence = current.evidence | (extra_evidence or {})
        if direct_session and isinstance(
            merged_evidence.get("validation_session"), dict
        ):
            session = dict(merged_evidence["validation_session"])
            session["status"] = (
                "VALIDATION_PASSED"
                if state is RunState.VALIDATION_PASSED
                else "CLOSED_TOO_EASY"
                if state is RunState.TOO_EASY
                else "ACTIVE"
            )
            session["decision"] = decision.action
            merged_evidence["validation_session"] = session
        next_snapshot = self.store.advance(
            current,
            state=state,
            attempts=attempts,
            consecutive_timeouts=decision.consecutive_timeouts,
            evidence=merged_evidence | {"validation_decision": asdict(decision)},
        )
        return self._commit(
            actor,
            "attempt.audited",
            idempotency_key,
            {"request_id": attempt.request_id, "decision": decision.action},
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
            "validation_session",
            "persistent_harbor_sessions",
            "harbor_rounds",
            "hint_reviews",
            "post_validation",
        }
        return {
            key: value for key, value in evidence.items() if key not in revision_scoped
        }

    def _validation_session_registry(self) -> ValidationSessionRegistry:
        """返回所有 sibling run 共用的会话身份注册表。"""
        return ValidationSessionRegistry(
            self.store.run_dir.parent / "validation-sessions.jsonl"
        )

    def _revalidate_teacher_knowledge(
        self,
        current: RunSnapshot,
        *,
        require_author: bool,
    ) -> None:
        """在 authoring 与 freeze 边界重新验证 Skill、prompt 和两份 Brief 字节。"""
        contract = current.evidence.get("teacher_skill_contract")
        brief = current.evidence.get("question_brief")
        if not isinstance(contract, dict) or not isinstance(brief, dict):
            raise WorkflowError("Teacher Skill contract and latest brief must be bound")
        question = contract.get("question")
        if type(question) is not int:
            raise WorkflowError("Teacher Skill question identity is invalid")
        stages = ("outline", "author") if require_author else ("outline",)
        batch_ids: set[str] = set()
        batch_rosters: set[tuple[int, ...]] = set()
        stable_contracts: set[tuple[int, str]] = set()
        for stage in stages:
            record = contract.get(stage)
            if not isinstance(record, dict):
                raise WorkflowError(f"{stage} Skill activation is not bound")
            path = Path(str(record.get("activation_path", "")))
            if not path.is_file() or file_sha256(path) != record.get(
                "activation_sha256"
            ):
                raise WorkflowError(f"{stage} Skill activation bytes drifted")
            value = self.skill_validator(path, question, stage)
            batch_id = value.get("batch_id")
            if not isinstance(batch_id, str) or not batch_id:
                raise WorkflowError(
                    f"{stage} Skill activation lacks its batch identity"
                )
            batch_ids.add(batch_id)
            roster = value.get("batch_questions")
            if not isinstance(roster, list) or any(
                type(item) is not int for item in roster
            ):
                raise WorkflowError(
                    f"{stage} Skill activation lacks its frozen batch roster"
                )
            batch_rosters.add(tuple(roster))
            stable_generation = value.get("stable_generation")
            snapshot = value.get("stable_lock_snapshot")
            stable_sha256 = (
                snapshot.get("sha256") if isinstance(snapshot, dict) else None
            )
            if type(stable_generation) is not int or not isinstance(stable_sha256, str):
                raise WorkflowError(
                    f"{stage} Skill activation lacks its frozen stable version"
                )
            stable_contracts.add((stable_generation, stable_sha256))
            prompt = Path(str(value["prompt_bundle"]["path"]))
            if (
                str(prompt.resolve()) != record.get("prompt_path")
                or file_sha256(prompt) != record.get("prompt_sha256")
                or value.get("batch_id") != record.get("batch_id")
                or value.get("batch_questions") != record.get("batch_questions")
                or value.get("stable_generation") != record.get("stable_generation")
                or stable_sha256 != record.get("stable_lock_sha256")
                or value.get("input_set_sha256") != record.get("input_set_sha256")
            ):
                raise WorkflowError(f"{stage} Skill activation contract drifted")
        if len(batch_ids) != 1:
            raise WorkflowError(
                "outline and author Skill activations must use one frozen batch"
            )
        if len(batch_rosters) != 1:
            raise WorkflowError(
                "outline and author Skill activations must use one frozen batch roster"
            )
        if len(stable_contracts) != 1:
            raise WorkflowError(
                "outline and author Skill activations must use one frozen stable version"
            )
        for kind in ("json", "markdown"):
            path = Path(str(brief.get(f"{kind}_path", "")))
            if not path.is_file() or file_sha256(path) != brief.get(f"{kind}_sha256"):
                raise WorkflowError(
                    "QuestionDesignBrief bytes drifted after Skill resolution"
                )

    def _reconcile_validation_registry(self, snapshot: RunSnapshot) -> None:
        """幂等重放时修复 run 已提交而 registry 尚未关闭的崩溃窗口。"""
        session = snapshot.evidence.get("validation_session")
        if not isinstance(session, dict):
            return
        session_id = session.get("validation_session_id")
        status = {
            RunState.TOO_EASY: "CLOSED_TOO_EASY",
            RunState.VALIDATION_PASSED: "CLOSED_VALIDATION_PASSED",
        }.get(snapshot.state)
        if isinstance(session_id, str) and status is not None:
            self._validation_session_registry().reconcile(session_id, status=status)

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
            raise WorkflowError(
                "runtime closure lacks its audited fresh scientific retry"
            )
        if scientific.get("frozen_contract_digest") != current.package_digest:
            raise WorkflowError(
                "runtime closure scientific retry targets another package"
            )
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
                raise WorkflowError(
                    "runtime closure contains an unaccepted delta receipt"
                )
            if evidence.get("source_attempt_index") != scientific.get("attempt_index"):
                raise WorkflowError(
                    "runtime recovery changed the scientific attempt identity"
                )

    @classmethod
    def _delta_receipt(cls, path: Path) -> DeltaReceipt:
        """严格读取并复验一份 schema-v2 增量回执。"""
        value = cls._json_object(path)
        try:
            converted = dict(value)
            converted["state"] = DeltaState(converted["state"])
            converted["baseline_artifact"] = ArtifactIdentity(
                **converted["baseline_artifact"]
            )
            converted["builder_artifact"] = ArtifactIdentity(
                **converted["builder_artifact"]
            )
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
            converted["baseline_artifact"] = ArtifactIdentity(
                **converted["baseline_artifact"]
            )
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

    def _guard(
        self, actor: Actor, actors: set[Actor], states: set[RunState]
    ) -> RunSnapshot:
        current = self.snapshot
        if actor not in actors:
            raise WorkflowError(f"{actor.value} does not own this transition")
        if current.state not in states:
            raise WorkflowError(f"transition is invalid from {current.state.value}")
        return current

    def _idempotent_snapshot(
        self,
        idempotency_key: str,
        event_type: str,
        expected_payload: dict[str, object],
    ) -> RunSnapshot | None:
        """在状态门前识别已成功提交的同一调用，保证安全重放。"""
        for event in self.store.events():
            if event.idempotency_key != idempotency_key:
                continue
            if event.event_type != event_type or any(
                event.payload.get(key) != value
                for key, value in expected_payload.items()
            ):
                raise WorkflowError(
                    "idempotency key already belongs to another operation"
                )
            return self.snapshot
        return None

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

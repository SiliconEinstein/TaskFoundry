"""从 canonical Harbor 原始产物导入不可由调用者填写的轮次证据。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from .model import ContractError
from .researcher import (
    has_model_transport_failure,
    ResearcherError,
    ResearcherRequest,
    sha256_file,
    validate_launch_contract,
    validate_request_id,
)
from .validation import JobClassification


class HarborEvidenceError(ContractError):
    """Harbor 原始证据不完整、不一致或不可认证。"""


@dataclass(frozen=True)
class VerifiedRoundEvidence:
    """由 importer 根据权威字节重算得到的一轮验证证据。"""

    request: ResearcherRequest
    request_path: str
    request_sha256: str
    capability_path: str
    capability_sha256: str
    job_config_path: str
    job_config_sha256: str
    classification: JobClassification
    score: float | None
    job_id: str
    trial_id: str
    agent_sandbox_id: str
    verifier_sandbox_id: str | None
    harness_session_id: str
    wall_time_sec: float
    job_result_path: str
    job_result_sha256: str | None
    trial_result_path: str | None
    trial_result_sha256: str | None
    job_log_path: str | None
    job_log_sha256: str | None
    provider_identity_path: str | None
    provider_identity_sha256: str | None
    round_history_path: str | None
    round_history_sha256: str | None


@dataclass(frozen=True)
class VerifiedPersistentRound:
    """One graded round inside a persistent Harbor trial."""

    round_index: int
    mode: str
    classification: JobClassification
    score: float | None
    wall_time_sec: float
    verifier_sandbox_id: str | None
    result_path: str
    result_sha256: str
    decision_path: str | None
    decision_sha256: str | None
    decision_action: str | None
    artifact_manifest_path: str
    artifact_manifest_sha256: str


@dataclass(frozen=True)
class VerifiedPersistentSession:
    """Canonical evidence for all rounds in one persistent Researcher runtime."""

    request: ResearcherRequest
    request_path: str
    request_sha256: str
    capability_path: str
    capability_sha256: str
    job_config_path: str
    job_config_sha256: str
    job_id: str
    trial_id: str
    agent_sandbox_id: str
    harness_session_id: str
    wall_time_sec: float
    job_result_path: str
    job_result_sha256: str
    trial_result_path: str
    trial_result_sha256: str
    job_log_path: str
    job_log_sha256: str
    provider_identity_path: str | None
    provider_identity_sha256: str | None
    rounds: tuple[VerifiedPersistentRound, ...]


class HarborEvidenceImporter:
    """把固定 handoff 和 Harbor 目录收敛成单个可信领域对象。"""

    def __init__(self, handoff_root: Path) -> None:
        self.handoff_root = handoff_root

    def import_round(self, request_id: str) -> VerifiedRoundEvidence:
        """严格导入一次已经兑换并完成的 Harbor 请求。"""
        try:
            validate_request_id(request_id)
        except ContractError as error:
            raise HarborEvidenceError(str(error)) from error
        request_dir = self.handoff_root / request_id
        request_path = request_dir / "request.json"
        capability_path = request_dir / "capability.json"
        request = ResearcherRequest.from_dict(_read_object(request_path))
        if request.request_id != request_id:
            raise HarborEvidenceError("request_id 与 canonical handoff 目录不一致")
        self._validate_redemption(request, request_path, capability_path)
        config_path = Path(request.job_config_path).resolve()
        if config_path.parent != request_dir.resolve():
            raise HarborEvidenceError("JobConfig 不在 canonical request 目录内")
        try:
            validate_launch_contract(request)
        except ResearcherError as error:
            raise HarborEvidenceError(f"Harbor 启动契约不闭合: {error}") from error
        config = _read_object(config_path)
        job_dir = _job_directory(config, request_dir)
        job_result_path = job_dir / "result.json"
        try:
            job_result = _read_object(job_result_path)
            classification, score = _classify(job_result, job_dir)
            job_id = _required_string(job_result, "id", "Harbor job")
            wall_time_sec = _wall_time(job_result)
            trial_result_path, trial_result = _optional_trial_result(
                job_dir,
                required=classification is JobClassification.SCIENTIFIC_RESULT,
            )
        except HarborEvidenceError:
            return self._incomplete(
                request, request_path, capability_path, config_path, job_result_path
            )
        try:
            trial_id = (
                _required_string(trial_result, "id", "Harbor trial")
                if trial_result is not None
                else ""
            )
            session_id = (
                _harness_session_id(trial_result, trial_result_path.parent)
                if trial_result is not None and trial_result_path is not None
                else ""
            )
        except HarborEvidenceError:
            return self._incomplete(
                request, request_path, capability_path, config_path, job_result_path
            )
        job_log = job_dir / "job.log"
        provider_identity_path = job_dir / "provider-identities.json"
        try:
            agent_sandbox, verifier_sandbox = _provider_sandboxes(
                job_log,
                provider_identity_path,
                required=classification is JobClassification.SCIENTIFIC_RESULT,
            )
        except HarborEvidenceError:
            return self._incomplete(
                request, request_path, capability_path, config_path, job_result_path
            )
        if classification is JobClassification.SCIENTIFIC_RESULT and not all(
            (job_id, trial_id, session_id, agent_sandbox, verifier_sandbox)
        ):
            return self._incomplete(
                request, request_path, capability_path, config_path, job_result_path
            )
        history_path: Path | None = None
        history_sha256: str | None = None
        if classification is JobClassification.SCIENTIFIC_RESULT:
            try:
                assert trial_result_path is not None
                history_path = _write_round_history(
                    request,
                    request_dir,
                    trial_result_path.parent,
                )
                history_sha256 = sha256_file(history_path)
            except (HarborEvidenceError, OSError, UnicodeError):
                return self._incomplete(
                    request,
                    request_path,
                    capability_path,
                    config_path,
                    job_result_path,
                )
        return VerifiedRoundEvidence(
            request=request,
            request_path=str(request_path.resolve()),
            request_sha256=sha256_file(request_path),
            capability_path=str(capability_path.resolve()),
            capability_sha256=sha256_file(capability_path),
            job_config_path=str(config_path),
            job_config_sha256=sha256_file(config_path),
            classification=classification,
            score=score,
            job_id=job_id,
            trial_id=trial_id,
            agent_sandbox_id=agent_sandbox,
            verifier_sandbox_id=verifier_sandbox,
            harness_session_id=session_id,
            wall_time_sec=wall_time_sec,
            job_result_path=str(job_result_path.resolve()),
            job_result_sha256=sha256_file(job_result_path),
            trial_result_path=(
                str(trial_result_path.resolve()) if trial_result_path else None
            ),
            trial_result_sha256=(
                sha256_file(trial_result_path) if trial_result_path else None
            ),
            job_log_path=(str(job_log.resolve()) if job_log.is_file() else None),
            job_log_sha256=(sha256_file(job_log) if job_log.is_file() else None),
            provider_identity_path=(
                str(provider_identity_path.resolve())
                if provider_identity_path.is_file()
                else None
            ),
            provider_identity_sha256=(
                sha256_file(provider_identity_path)
                if provider_identity_path.is_file()
                else None
            ),
            round_history_path=str(history_path.resolve()) if history_path else None,
            round_history_sha256=history_sha256,
        )

    def import_persistent_session(self, request_id: str) -> VerifiedPersistentSession:
        """Import one completed host-controlled persistent validation trial."""
        try:
            validate_request_id(request_id)
        except ContractError as error:
            raise HarborEvidenceError(str(error)) from error
        request_dir = self.handoff_root / request_id
        request_path = request_dir / "request.json"
        capability_path = request_dir / "capability.json"
        request = ResearcherRequest.from_dict(_read_object(request_path))
        if request.request_id != request_id or request.schema_version != 3:
            raise HarborEvidenceError(
                "persistent request identity or schema is invalid"
            )
        self._validate_redemption(request, request_path, capability_path)
        config_path = Path(request.job_config_path).resolve()
        if config_path.parent != request_dir.resolve():
            raise HarborEvidenceError("JobConfig 不在 canonical request 目录内")
        try:
            validate_launch_contract(request)
        except ResearcherError as error:
            raise HarborEvidenceError(f"Harbor 启动契约不闭合: {error}") from error
        config = _read_object(config_path)
        try:
            persistent_payload = config["agents"][0]["kwargs"]["persistent_validation"]
            pass_threshold = float(persistent_payload["pass_threshold"])
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise HarborEvidenceError(
                "persistent JobConfig lacks its pass threshold"
            ) from error
        pre_job_failure = self._pre_job_platform_failure(
            request=request,
            request_path=request_path,
            capability_path=capability_path,
            config_path=config_path,
        )
        if pre_job_failure is not None:
            return pre_job_failure
        job_dir = _job_directory(config, request_dir)
        job_result_path = job_dir / "result.json"
        job_result = _read_object(job_result_path)
        job_id = _required_string(job_result, "id", "Harbor job")
        trial_result_path, trial_result = _optional_trial_result(job_dir, required=False)
        if trial_result_path is None or trial_result is None:
            return self._pre_trial_platform_failure(
                request=request,
                request_path=request_path,
                capability_path=capability_path,
                config_path=config_path,
                job_result_path=job_result_path,
                job_result=job_result,
                job_id=job_id,
                job_dir=job_dir,
            )
        wall_time_sec = _wall_time(job_result)
        trial_id = _required_string(trial_result, "id", "Harbor trial")
        job_log_path = job_dir / "job.log"
        identity_path = job_dir / "provider-identities.json"
        steps = trial_result.get("step_results")
        if not isinstance(steps, list) or not steps:
            job_classification, _ = _classify(job_result, job_dir)
            if (
                job_classification is JobClassification.SCIENTIFIC_RESULT
                or not isinstance(trial_result.get("exception_info"), dict)
            ):
                raise HarborEvidenceError("persistent trial lacks step results")
            session_id = ""
            agent_sandbox = ""
            identity_path_value: str | None = None
            identity_sha256: str | None = None
            rounds = (
                VerifiedPersistentRound(
                    round_index=1,
                    mode="blind",
                    classification=job_classification,
                    score=None,
                    wall_time_sec=wall_time_sec,
                    verifier_sandbox_id=None,
                    result_path=str(trial_result_path.resolve()),
                    result_sha256=sha256_file(trial_result_path),
                    decision_path=None,
                    decision_sha256=None,
                    decision_action=None,
                    artifact_manifest_path="",
                    artifact_manifest_sha256=hashlib.sha256(b"").hexdigest(),
                ),
            )
        else:
            session_id = _persistent_harness_session_id(
                trial_result, trial_result_path.parent
            )
            agent_sandbox, verifier_sandboxes = _provider_sandbox_sequence(
                job_log_path,
                identity_path,
            )
            identity_path_value = str(identity_path.resolve())
            identity_sha256 = sha256_file(identity_path)
            rounds = self._persistent_rounds(
                request=request,
                trial_id=trial_id,
                trial_result=trial_result,
                trial_dir=trial_result_path.parent,
                verifier_sandboxes=verifier_sandboxes,
                pass_threshold=pass_threshold,
            )
        if not rounds:
            raise HarborEvidenceError("persistent validation produced no rounds")
        if any(
            item.decision_action in {"STOP_TOO_EASY", "STOP_PASSED", "STOP_BLOCKED"}
            for item in rounds[:-1]
        ):
            raise HarborEvidenceError(
                "persistent validation continued after a terminal decision"
            )
        terminal_round = rounds[-1]
        terminal = terminal_round.decision_action
        if terminal_round.classification is JobClassification.SCIENTIFIC_RESULT:
            if terminal not in {"STOP_TOO_EASY", "STOP_PASSED", "STOP_BLOCKED"}:
                raise HarborEvidenceError(
                    "persistent validation lacks a terminal Teacher decision"
                )
            if not session_id:
                raise HarborEvidenceError(
                    "persistent scientific session lacks one Codex conversation"
                )
        elif terminal is not None:
            raise HarborEvidenceError(
                "platform failure cannot carry a Teacher decision"
            )
        stats = job_result.get("stats")
        if not isinstance(stats, dict):
            raise HarborEvidenceError("persistent Harbor job lacks stats")
        if (
            terminal_round.classification is JobClassification.SCIENTIFIC_RESULT
            and stats.get("n_errored_trials", 0)
        ):
            raise HarborEvidenceError(
                "persistent scientific session ended as an errored job"
            )
        return VerifiedPersistentSession(
            request=request,
            request_path=str(request_path.resolve()),
            request_sha256=sha256_file(request_path),
            capability_path=str(capability_path.resolve()),
            capability_sha256=sha256_file(capability_path),
            job_config_path=str(config_path),
            job_config_sha256=sha256_file(config_path),
            job_id=job_id,
            trial_id=trial_id,
            agent_sandbox_id=agent_sandbox,
            harness_session_id=session_id,
            wall_time_sec=wall_time_sec,
            job_result_path=str(job_result_path.resolve()),
            job_result_sha256=sha256_file(job_result_path),
            trial_result_path=str(trial_result_path.resolve()),
            trial_result_sha256=sha256_file(trial_result_path),
            job_log_path=str(job_log_path.resolve()),
            job_log_sha256=sha256_file(job_log_path),
            provider_identity_path=identity_path_value,
            provider_identity_sha256=identity_sha256,
            rounds=rounds,
        )

    def _pre_job_platform_failure(
        self,
        *,
        request: ResearcherRequest,
        request_path: Path,
        capability_path: Path,
        config_path: Path,
    ) -> VerifiedPersistentSession | None:
        """Bind a host receipt when the Harbor launcher fails before job creation."""
        matches: list[tuple[Path, dict[str, Any]]] = []
        for receipt_path in sorted(
            self.handoff_root.parent.glob("validation/*/researcher-receipt*.json")
        ):
            try:
                receipt = _read_object(receipt_path)
            except HarborEvidenceError:
                continue
            if (
                receipt.get("request_id") == request.request_id
                and receipt.get("classification") == "PLATFORM_FAILURE"
                and receipt.get("failure_stage") == "PLATFORM"
                and receipt.get("reward") is None
                and receipt.get("job_id") is None
                and receipt.get("trial_id") is None
                and receipt.get("result_path") is None
                and receipt.get("result_sha256") is None
                and type(receipt.get("exit_code")) is int
                and receipt["exit_code"] != 0
            ):
                matches.append((receipt_path, receipt))
        if not matches:
            return None
        if len(matches) != 1:
            raise HarborEvidenceError(
                "pre-job platform failure requires one matching host receipt"
            )
        receipt_path, receipt = matches[0]
        try:
            wall_time_sec = float(receipt["wall_time_sec"])
        except (KeyError, TypeError, ValueError) as error:
            raise HarborEvidenceError("host receipt wall time is invalid") from error
        if not math.isfinite(wall_time_sec) or wall_time_sec < 0:
            raise HarborEvidenceError("host receipt wall time is invalid")
        execution_dir = request_path.parent / "researcher-execution"
        launcher_logs = [
            path
            for path in (execution_dir / "stderr.log", execution_dir / "stdout.log")
            if path.is_file() and path.stat().st_size
        ]
        if len(launcher_logs) != 1:
            raise HarborEvidenceError("pre-job platform failure lacks launcher log")
        launcher_log_path = launcher_logs[0]
        receipt_sha256 = sha256_file(receipt_path)
        empty_manifest_sha256 = hashlib.sha256(b"").hexdigest()
        return VerifiedPersistentSession(
            request=request,
            request_path=str(request_path.resolve()),
            request_sha256=sha256_file(request_path),
            capability_path=str(capability_path.resolve()),
            capability_sha256=sha256_file(capability_path),
            job_config_path=str(config_path),
            job_config_sha256=sha256_file(config_path),
            job_id="",
            trial_id="",
            agent_sandbox_id="",
            harness_session_id="",
            wall_time_sec=wall_time_sec,
            job_result_path=str(receipt_path.resolve()),
            job_result_sha256=receipt_sha256,
            trial_result_path=str(receipt_path.resolve()),
            trial_result_sha256=receipt_sha256,
            job_log_path=str(launcher_log_path.resolve()),
            job_log_sha256=sha256_file(launcher_log_path),
            provider_identity_path=None,
            provider_identity_sha256=None,
            rounds=(
                VerifiedPersistentRound(
                    round_index=1,
                    mode="blind",
                    classification=JobClassification.PLATFORM_FAILURE,
                    score=None,
                    wall_time_sec=wall_time_sec,
                    verifier_sandbox_id=None,
                    result_path=str(receipt_path.resolve()),
                    result_sha256=receipt_sha256,
                    decision_path=None,
                    decision_sha256=None,
                    decision_action=None,
                    artifact_manifest_path="",
                    artifact_manifest_sha256=empty_manifest_sha256,
                ),
            ),
        )

    def _pre_trial_platform_failure(
        self,
        *,
        request: ResearcherRequest,
        request_path: Path,
        capability_path: Path,
        config_path: Path,
        job_result_path: Path,
        job_result: dict[str, Any],
        job_id: str,
        job_dir: Path,
    ) -> VerifiedPersistentSession:
        """Bind a host receipt when Harbor aborts before creating a trial."""
        receipt_paths = sorted(
            self.handoff_root.parent.glob("validation/*/researcher-receipt*.json")
        )
        matches: list[tuple[Path, dict[str, Any]]] = []
        job_result_sha256 = sha256_file(job_result_path)
        for receipt_path in receipt_paths:
            try:
                receipt = _read_object(receipt_path)
            except HarborEvidenceError:
                continue
            if (
                receipt.get("request_id") == request.request_id
                and receipt.get("classification") == "PLATFORM_FAILURE"
                and receipt.get("failure_stage") == "PLATFORM"
                and receipt.get("reward") is None
                and receipt.get("job_id") == job_id
                and receipt.get("result_path") == str(job_result_path.resolve())
                and receipt.get("result_sha256") == job_result_sha256
                and type(receipt.get("exit_code")) is int
                and receipt["exit_code"] != 0
            ):
                matches.append((receipt_path, receipt))
        if len(matches) != 1:
            raise HarborEvidenceError(
                "pre-trial platform failure requires one matching host receipt"
            )
        receipt_path, receipt = matches[0]
        try:
            wall_time_sec = float(receipt["wall_time_sec"])
        except (KeyError, TypeError, ValueError) as error:
            raise HarborEvidenceError("host receipt wall time is invalid") from error
        if not math.isfinite(wall_time_sec) or wall_time_sec < 0:
            raise HarborEvidenceError("host receipt wall time is invalid")
        job_log_path = job_dir / "job.log"
        if not job_log_path.is_file():
            raise HarborEvidenceError("pre-trial platform failure lacks job log")
        receipt_sha256 = sha256_file(receipt_path)
        empty_manifest_sha256 = hashlib.sha256(b"").hexdigest()
        return VerifiedPersistentSession(
            request=request,
            request_path=str(request_path.resolve()),
            request_sha256=sha256_file(request_path),
            capability_path=str(capability_path.resolve()),
            capability_sha256=sha256_file(capability_path),
            job_config_path=str(config_path),
            job_config_sha256=sha256_file(config_path),
            job_id=job_id,
            trial_id="",
            agent_sandbox_id="",
            harness_session_id="",
            wall_time_sec=wall_time_sec,
            job_result_path=str(job_result_path.resolve()),
            job_result_sha256=job_result_sha256,
            trial_result_path=str(receipt_path.resolve()),
            trial_result_sha256=receipt_sha256,
            job_log_path=str(job_log_path.resolve()),
            job_log_sha256=sha256_file(job_log_path),
            provider_identity_path=None,
            provider_identity_sha256=None,
            rounds=(
                VerifiedPersistentRound(
                    round_index=1,
                    mode="blind",
                    classification=JobClassification.PLATFORM_FAILURE,
                    score=None,
                    wall_time_sec=wall_time_sec,
                    verifier_sandbox_id=None,
                    result_path=str(receipt_path.resolve()),
                    result_sha256=receipt_sha256,
                    decision_path=None,
                    decision_sha256=None,
                    decision_action=None,
                    artifact_manifest_path="",
                    artifact_manifest_sha256=empty_manifest_sha256,
                ),
            ),
        )

    @staticmethod
    def _persistent_rounds(
        *,
        request: ResearcherRequest,
        trial_id: str,
        trial_result: dict[str, Any],
        trial_dir: Path,
        verifier_sandboxes: tuple[str, ...],
        pass_threshold: float,
    ) -> tuple[VerifiedPersistentRound, ...]:
        raw_steps = trial_result.get("step_results")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise HarborEvidenceError("persistent trial lacks round steps")
        controller = Path(request.controller_dir or "").resolve()
        verified: list[VerifiedPersistentRound] = []
        verifier_position = 0
        for index, step in enumerate(raw_steps, start=1):
            if (
                not isinstance(step, dict)
                or step.get("step_name") != f"round-{index:02d}"
            ):
                raise HarborEvidenceError("persistent round step order is invalid")
            result_path = controller / f"round-{index:02d}-result.json"
            result = _read_object(result_path)
            phase = result.get("phase")
            classification = result.get("classification")
            reward = result.get("reward")
            verifier = step.get("verifier_result")
            rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
            manifest_path = (
                trial_dir
                / "steps"
                / f"round-{index:02d}"
                / "artifacts"
                / "manifest.json"
            )
            manifest_digest = (
                sha256_file(manifest_path)
                if manifest_path.is_file() and not manifest_path.is_symlink()
                else hashlib.sha256(b"").hexdigest()
            )
            if (
                result.get("schema_version") != 1
                or result.get("validation_session_id") != request.validation_session_id
                or result.get("trial_id") != trial_id
                or result.get("round_index") != index
                or phase not in {"BLIND", "HINT"}
                or result.get("artifact_manifest_sha256") != manifest_digest
            ):
                raise HarborEvidenceError(
                    "persistent round result does not match trial bytes"
                )
            if classification == "SCIENTIFIC_RESULT":
                if (
                    type(reward) not in (int, float)
                    or not math.isfinite(float(reward))
                    or not 0 <= float(reward) <= 1
                    or result.get("verifier_result") != rewards
                    or step.get("exception_info") is not None
                    or verifier_position >= len(verifier_sandboxes)
                    or not manifest_path.is_file()
                    or manifest_path.is_symlink()
                ):
                    raise HarborEvidenceError(
                        "persistent scientific round is incomplete"
                    )
                verifier_sandbox: str | None = verifier_sandboxes[verifier_position]
                verifier_position += 1
                round_classification = JobClassification.SCIENTIFIC_RESULT
                score: float | None = float(reward)
            elif classification == "PLATFORM_FAILURE":
                if (
                    reward is not None
                    or step.get("exception_info") is None
                    or index != len(raw_steps)
                ):
                    raise HarborEvidenceError(
                        "persistent platform failure is inconsistent"
                    )
                remaining_verifiers = len(verifier_sandboxes) - verifier_position
                if remaining_verifiers > 1:
                    raise HarborEvidenceError(
                        "platform failure has ambiguous verifier identities"
                    )
                verifier_sandbox = (
                    verifier_sandboxes[verifier_position]
                    if remaining_verifiers == 1
                    else None
                )
                verifier_position += remaining_verifiers
                round_classification = JobClassification.PLATFORM_FAILURE
                score = None
            else:
                raise HarborEvidenceError("persistent round classification is unknown")
            decision_path = controller / f"round-{index:02d}-decision.json"
            decision: dict[str, Any] | None = None
            action: str | None = None
            if round_classification is JobClassification.SCIENTIFIC_RESULT:
                decision = _read_object(decision_path)
                action_value = decision.get("action")
                if (
                    decision.get("schema_version") != 1
                    or decision.get("validation_session_id")
                    != request.validation_session_id
                    or decision.get("round_index") != index
                    or decision.get("result_sha256") != sha256_file(result_path)
                    or action_value
                    not in {
                        "CONTINUE_BLIND",
                        "CONTINUE_HINT",
                        "STOP_TOO_EASY",
                        "STOP_PASSED",
                        "STOP_BLOCKED",
                    }
                ):
                    raise HarborEvidenceError(
                        "persistent Teacher decision binding is invalid"
                    )
                action = str(action_value)
                _validate_persistent_progression(
                    index=index,
                    phase=phase,
                    score=float(reward),
                    action=action,
                    decision=decision,
                    threshold=pass_threshold,
                )
            elif decision_path.exists():
                raise HarborEvidenceError(
                    "platform failure has an unexpected Teacher decision"
                )
            verified.append(
                VerifiedPersistentRound(
                    round_index=index,
                    mode=phase.lower(),
                    classification=round_classification,
                    score=score,
                    wall_time_sec=_step_wall_time(step),
                    verifier_sandbox_id=verifier_sandbox,
                    result_path=str(result_path),
                    result_sha256=sha256_file(result_path),
                    decision_path=(
                        str(decision_path) if decision is not None else None
                    ),
                    decision_sha256=(
                        sha256_file(decision_path) if decision is not None else None
                    ),
                    decision_action=action,
                    artifact_manifest_path=str(manifest_path.resolve()),
                    artifact_manifest_sha256=manifest_digest,
                )
            )
        if verifier_position != len(verifier_sandboxes):
            raise HarborEvidenceError("unused verifier sandbox identity remains")
        return tuple(verified)

    @staticmethod
    def _incomplete(
        request: ResearcherRequest,
        request_path: Path,
        capability_path: Path,
        config_path: Path,
        result_path: Path,
    ) -> VerifiedRoundEvidence:
        """把缺失或不可解析的 Harbor 结果记录为不计科学轮次的证据。"""
        partial = _partial_attempt_evidence(result_path)
        return VerifiedRoundEvidence(
            request=request,
            request_path=str(request_path.resolve()),
            request_sha256=sha256_file(request_path),
            capability_path=str(capability_path.resolve()),
            capability_sha256=sha256_file(capability_path),
            job_config_path=str(config_path.resolve()),
            job_config_sha256=sha256_file(config_path),
            classification=JobClassification.EVIDENCE_INCOMPLETE,
            score=None,
            job_id=partial["job_id"],
            trial_id=partial["trial_id"],
            agent_sandbox_id=partial["agent_sandbox_id"],
            verifier_sandbox_id=partial["verifier_sandbox_id"],
            harness_session_id=partial["harness_session_id"],
            wall_time_sec=partial["wall_time_sec"],
            job_result_path=str(result_path.resolve()),
            job_result_sha256=(
                sha256_file(result_path) if result_path.is_file() else None
            ),
            trial_result_path=partial["trial_result_path"],
            trial_result_sha256=partial["trial_result_sha256"],
            job_log_path=partial["job_log_path"],
            job_log_sha256=partial["job_log_sha256"],
            provider_identity_path=partial["provider_identity_path"],
            provider_identity_sha256=partial["provider_identity_sha256"],
            round_history_path=None,
            round_history_sha256=None,
        )

    @staticmethod
    def _validate_redemption(
        request: ResearcherRequest,
        request_path: Path,
        capability_path: Path,
    ) -> None:
        capability = _read_object(capability_path)
        if (
            capability.get("schema_version") != 2
            or capability.get("status") != "CONSUMED"
            or capability.get("request_sha256") != sha256_file(request_path)
            or capability.get("researcher_thread_id") != request.researcher_thread_id
            or capability.get("consumed_by_thread_id") != request.researcher_thread_id
        ):
            raise HarborEvidenceError("capability 缺少可信的兑换身份")
        if (capability_path.parent / "token").exists():
            raise HarborEvidenceError("已消费 capability 仍保留启动 token")


def compact_legacy_round_history(source: Path, destination: Path) -> str:
    """从已封签的 schema-1/2 历史派生更小的 schema-2 交付物。"""
    if not source.is_file() or source.is_symlink():
        raise HarborEvidenceError("legacy round history 不是普通文件")
    value = _read_object(source)
    version = value.get("schema_version")
    if version == 1:
        compact = _compact_schema_one_history(value, source)
    elif version == 2:
        compact = _compact_schema_two_history(value, source)
    else:
        raise HarborEvidenceError("只有 schema-1/2 round history 需要迁移")
    encoded = (
        json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != encoded:
            raise HarborEvidenceError("compact round history 已存在但字节身份不同")
        return sha256_file(destination)
    descriptor, temporary = tempfile.mkstemp(
        prefix="round-history.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return sha256_file(destination)


def _compact_schema_one_history(value: dict[str, Any], source: Path) -> dict[str, Any]:
    """保留既有 schema-1 到 schema-2 的无损动作迁移语义。"""
    content = value.get("transcript")
    expected = value.get("transcript_sha256")
    if not isinstance(content, str) or not content.strip():
        raise HarborEvidenceError("legacy round history 缺少 transcript")
    actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if actual != expected:
        raise HarborEvidenceError("legacy round history transcript 哈希不匹配")
    compact = _round_history_identity(value)
    compact.update(
        {
            "schema_version": 2,
            "source_round_history_sha256": sha256_file(source),
            "transcript_format": "codex-jsonl-lossless-actions-bounded-outputs",
            "transcript_events": _compact_agent_transcript(content),
        }
    )
    return compact


def _compact_schema_two_history(value: dict[str, Any], source: Path) -> dict[str, Any]:
    """去掉累计历史中的大段命令正文，同时保留可追溯语义和摘要。"""
    events = value.get("transcript_events")
    if not isinstance(events, list) or not events:
        raise HarborEvidenceError("schema-2 round history 缺少 transcript events")
    compact_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise HarborEvidenceError("schema-2 round history event 无效")
        event_type = event["type"]
        retained = {key: event[key] for key in ("type", "line") if key in event}
        if event_type == "thread_started":
            retained["thread_id"] = event.get("thread_id")
        elif event_type == "agent_message":
            text = event.get("text")
            retained["text"] = text[:4000] if isinstance(text, str) else ""
        elif event_type == "command_execution":
            command = event.get("command")
            if not isinstance(command, str):
                command = ""
            retained.update(
                {
                    "command_preview": command[:512],
                    "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                    "exit_code": event.get("exit_code"),
                    "output_bytes": event.get("output_bytes"),
                    "output_sha256": event.get("output_sha256"),
                }
            )
        elif event_type == "turn_completed":
            retained["usage"] = event.get("usage")
        compact_events.append(retained)
    compact = _round_history_identity(value)
    compact.update(
        {
            "schema_version": 2,
            "source_round_history_sha256": sha256_file(source),
            "transcript_format": "codex-jsonl-semantic-compact-v1",
            "transcript_events": compact_events,
        }
    )
    return compact


def _round_history_identity(value: dict[str, Any]) -> dict[str, Any]:
    """提取每种可交付 round history 必须保留的身份字段。"""
    keys = (
        "validation_session_id",
        "round_index",
        "request_id",
        "question_revision",
        "package_sha256",
        "transcript_sha256",
    )
    if any(key not in value for key in keys):
        raise HarborEvidenceError("round history 身份字段不完整")
    return {key: value[key] for key in keys}


def _partial_attempt_evidence(result_path: Path) -> dict[str, Any]:
    """尽可能保留 incomplete attempt 已产生的 fresh execution 身份。"""
    job_dir = result_path.parent
    job_id = ""
    wall_time_sec = 0.0
    if result_path.is_file() and not result_path.is_symlink():
        try:
            job = _read_object(result_path)
            job_id = job.get("id") if isinstance(job.get("id"), str) else ""
            wall_time_sec = _wall_time(job)
        except HarborEvidenceError:
            job_id, wall_time_sec = "", 0.0
    trial_path: Path | None = None
    trial: dict[str, Any] | None = None
    try:
        trial_path, trial = _optional_trial_result(job_dir, required=False)
    except HarborEvidenceError:
        trial_path, trial = None, None
    trial_id = (
        trial.get("id")
        if isinstance(trial, dict) and isinstance(trial.get("id"), str)
        else ""
    )
    session_id = ""
    if trial is not None and trial_path is not None:
        try:
            session_id = _harness_session_id(trial, trial_path.parent)
        except (HarborEvidenceError, OSError, UnicodeError):
            session_id = ""
    job_log = job_dir / "job.log"
    identity = job_dir / "provider-identities.json"
    agent = verifier = ""
    try:
        agent, verifier = _provider_sandboxes(job_log, identity, required=False)
    except HarborEvidenceError:
        agent, verifier = "", ""
    return {
        "job_id": job_id,
        "trial_id": trial_id,
        "agent_sandbox_id": agent,
        "verifier_sandbox_id": verifier,
        "harness_session_id": session_id,
        "wall_time_sec": wall_time_sec,
        "trial_result_path": str(trial_path.resolve()) if trial_path else None,
        "trial_result_sha256": sha256_file(trial_path) if trial_path else None,
        "job_log_path": str(job_log.resolve()) if job_log.is_file() else None,
        "job_log_sha256": sha256_file(job_log) if job_log.is_file() else None,
        "provider_identity_path": str(identity.resolve())
        if identity.is_file()
        else None,
        "provider_identity_sha256": sha256_file(identity)
        if identity.is_file()
        else None,
    }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"禁止非有限 JSON 常量: {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HarborEvidenceError(
            f"无法严格解析 Harbor 证据 {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HarborEvidenceError(f"Harbor 证据根节点必须是对象: {path}")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"重复 JSON 键: {key}")
        value[key] = item
    return value


def _job_directory(config: dict[str, Any], request_dir: Path) -> Path:
    jobs_dir = config.get("jobs_dir")
    job_name = config.get("job_name")
    if not isinstance(jobs_dir, str) or not isinstance(job_name, str) or not job_name:
        raise HarborEvidenceError("JobConfig 缺少 canonical jobs_dir/job_name")
    canonical_root = (request_dir / "harbor-jobs").resolve()
    job_dir = (Path(jobs_dir) / job_name).resolve()
    if (
        Path(jobs_dir).resolve() != canonical_root
        or canonical_root not in job_dir.parents
    ):
        raise HarborEvidenceError("Harbor job 结果不在 canonical request 目录内")
    return job_dir


def _optional_trial_result(
    job_dir: Path,
    *,
    required: bool,
) -> tuple[Path | None, dict[str, Any] | None]:
    paths = sorted(path for path in job_dir.glob("*/result.json") if path.is_file())
    if not paths and not required:
        return None, None
    if len(paths) != 1:
        raise HarborEvidenceError("Harbor job 必须恰好包含一个 trial result")
    return paths[0], _read_object(paths[0])


def _classify(
    job: dict[str, Any], job_dir: Path
) -> tuple[JobClassification, float | None]:
    stats = job.get("stats")
    if not isinstance(stats, dict):
        raise HarborEvidenceError("Harbor job result 缺少 stats")
    evaluations = stats.get("evals")
    if not isinstance(evaluations, dict):
        raise HarborEvidenceError("Harbor job result 缺少 evals")
    exception_names = " ".join(
        str(name)
        for evaluation in evaluations.values()
        if isinstance(evaluation, dict)
        for name in (evaluation.get("exception_stats") or {})
    )
    if "AgentTimeoutError" in exception_names and has_model_transport_failure(job_dir):
        return JobClassification.HARNESS_FAILURE, None
    if "AgentTimeoutError" in exception_names:
        return JobClassification.DEFERRED_TIMEOUT, None
    if stats.get("n_errored_trials", 0):
        lowered = exception_names.lower()
        if any(
            word in lowered for word in ("image", "sandbox", "environmentsetup", "pull")
        ):
            return JobClassification.ENVIRONMENT_FAILURE, None
        if any(
            word in lowered
            for word in ("agent", "harness", "bootstrap", "auth", "model")
        ):
            return JobClassification.HARNESS_FAILURE, None
        return JobClassification.PLATFORM_FAILURE, None
    rewards: list[float] = []
    for evaluation in evaluations.values():
        if not isinstance(evaluation, dict):
            raise HarborEvidenceError("Harbor eval 必须是对象")
        metrics = evaluation.get("metrics", ())
        if not isinstance(metrics, list):
            raise HarborEvidenceError("Harbor metrics 必须是列表")
        for metric in metrics:
            if not isinstance(metric, dict):
                raise HarborEvidenceError("Harbor metric 必须是对象")
            raw = metric.get("reward", metric.get("mean"))
            if type(raw) in (int, float) and math.isfinite(float(raw)):
                rewards.append(float(raw))
    if len(rewards) != 1 or not 0 <= rewards[0] <= 1:
        raise HarborEvidenceError("Harbor 科学结果必须恰好包含一个有限 reward")
    return JobClassification.SCIENTIFIC_RESULT, rewards[0]


def _required_string(value: dict[str, Any], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise HarborEvidenceError(f"{label} 缺少 {key}")
    return result


def _harness_session_id(trial: dict[str, Any], trial_dir: Path) -> str:
    agent_result = trial.get("agent_result") or {}
    metadata = (
        (agent_result.get("metadata") or {}) if isinstance(agent_result, dict) else {}
    )
    trace = (metadata.get("trace") or {}) if isinstance(metadata, dict) else {}
    session = trace.get("session_id") if isinstance(trace, dict) else None
    if isinstance(session, str) and session:
        return session
    transcript = trial_dir / "agent/codex.txt"
    if not transcript.is_file():
        return ""
    for line in transcript.read_text(encoding="utf-8", errors="strict").splitlines():
        try:
            event = json.loads(line, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError):
            continue
        if event.get("type") == "thread.started" and isinstance(
            event.get("thread_id"), str
        ):
            return event["thread_id"]
    return ""


def _persistent_harness_session_id(trial: dict[str, Any], trial_dir: Path) -> str:
    """Require every resume turn to bind the same Codex conversation identity."""
    identities: list[str] = []
    steps = trial.get("step_results")
    if not isinstance(steps, list) or not steps:
        raise HarborEvidenceError("persistent trial lacks step results")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise HarborEvidenceError("persistent trial step is invalid")
        agent_result = step.get("agent_result")
        metadata = (
            agent_result.get("metadata") if isinstance(agent_result, dict) else None
        )
        trace = metadata.get("trace") if isinstance(metadata, dict) else None
        identity = trace.get("session_id") if isinstance(trace, dict) else None
        if not isinstance(identity, str) or not identity:
            transcript = (
                trial_dir / "steps" / f"round-{index:02d}" / "agent" / "codex.txt"
            )
            identity = _codex_session_from_transcript(transcript)
        if not identity:
            continue
        identities.append(identity)
    if len(set(identities)) > 1:
        raise HarborEvidenceError(
            "persistent rounds do not share one Codex conversation"
        )
    return identities[0] if identities else ""


def _codex_session_from_transcript(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        return ""
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        try:
            event = json.loads(line, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError):
            continue
        if event.get("type") == "thread.started" and isinstance(
            event.get("thread_id"), str
        ):
            return event["thread_id"]
    return ""


def _validate_persistent_progression(
    *,
    index: int,
    phase: str,
    score: float,
    action: str,
    decision: dict[str, Any],
    threshold: float = 0.85,
) -> None:
    """Recompute the fixed three-blind-then-hint protocol from canonical bytes."""
    passed = score >= threshold
    if index <= 3:
        if phase != "BLIND":
            raise HarborEvidenceError("the first three scientific rounds must be blind")
        allowed = (
            {"STOP_TOO_EASY"}
            if passed
            else {"CONTINUE_BLIND"}
            if index < 3
            else {"CONTINUE_HINT", "STOP_BLOCKED"}
        )
    else:
        if phase != "HINT" or index > 5:
            raise HarborEvidenceError("persistent hint round index is invalid")
        allowed = (
            {"STOP_PASSED"}
            if passed
            else {"CONTINUE_HINT", "STOP_BLOCKED"}
            if index < 5
            else {"STOP_BLOCKED"}
        )
    if action not in allowed:
        raise HarborEvidenceError("Teacher decision violates persistent progression")
    if action == "CONTINUE_HINT":
        hint = decision.get("hint")
        if (
            not isinstance(hint, str)
            or not hint.strip()
            or hint != hint.strip()
            or decision.get("teacher_declares_non_answer") is not True
        ):
            raise HarborEvidenceError(
                "persistent hint lacks Teacher non-answer declaration"
            )
    elif (
        decision.get("hint") is not None
        or decision.get("teacher_declares_non_answer") is not None
    ):
        raise HarborEvidenceError("non-hint decision contains hint fields")


def _provider_sandboxes(
    log_path: Path,
    identity_path: Path,
    *,
    required: bool,
) -> tuple[str, str]:
    if not identity_path.exists() and not required:
        return "", ""
    try:
        value = _read_object(identity_path)
        agent = value.get("agent_sandbox_id")
        verifier = value.get("verifier_sandbox_id")
        schema_version = value.get("schema_version")
        verifier_ids = value.get("verifier_sandbox_ids")
        if schema_version == 2:
            if not isinstance(verifier_ids, list) or any(
                not isinstance(item, str) or not item for item in verifier_ids
            ):
                raise HarborEvidenceError("provider verifier sandbox 序列无效")
            if verifier_ids:
                verifier = verifier_ids[-1]
        invalid_identity = (
            schema_version not in {1, 2}
            or value.get("source") != "harbor-adapter-job-log"
            or not log_path.is_file()
            or log_path.is_symlink()
            or value.get("job_log_sha256") != sha256_file(log_path)
            or not isinstance(agent, str)
            or not isinstance(verifier, str)
            or (bool(agent) and bool(verifier) and agent == verifier)
        )
        if required:
            invalid_identity = invalid_identity or not agent or not verifier
        else:
            invalid_identity = invalid_identity or not (agent or verifier)
        if invalid_identity:
            raise HarborEvidenceError(
                "结构化 provider sandbox 身份未绑定 Harbor adapter 日志"
            )
    except (OSError, UnicodeDecodeError) as error:
        raise HarborEvidenceError(f"无法读取 provider sandbox 身份: {error}") from error
    return agent, verifier


def _provider_sandbox_sequence(
    log_path: Path,
    identity_path: Path,
) -> tuple[str, tuple[str, ...]]:
    """Return one persistent Agent sandbox and every fresh verifier sandbox."""
    value = _read_object(identity_path)
    if value.get("schema_version") != 2:
        raise HarborEvidenceError(
            "persistent validation requires provider identity schema 2"
        )
    agent = value.get("agent_sandbox_id")
    verifiers = value.get("verifier_sandbox_ids")
    if (
        value.get("source") != "harbor-adapter-job-log"
        or not log_path.is_file()
        or log_path.is_symlink()
        or value.get("job_log_sha256") != sha256_file(log_path)
        or not isinstance(agent, str)
        or not agent
        or not isinstance(verifiers, list)
        or any(not isinstance(item, str) or not item for item in verifiers)
        or agent in verifiers
        or len(verifiers) != len(set(verifiers))
    ):
        raise HarborEvidenceError(
            "persistent provider sandbox identities are incomplete"
        )
    return agent, tuple(verifiers)


def _wall_time(job: dict[str, Any]) -> float:
    try:
        started = datetime.fromisoformat(str(job["started_at"]).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(
            str(job["finished_at"]).replace("Z", "+00:00")
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HarborEvidenceError("Harbor job 时间戳无效") from error
    elapsed = (finished - started).total_seconds()
    if elapsed < 0:
        raise HarborEvidenceError("Harbor job 时间顺序无效")
    return elapsed


def _step_wall_time(step: dict[str, Any]) -> float:
    """Compute one round's elapsed Agent-plus-verifier interval."""
    timestamps: list[datetime] = []
    for field in ("agent_execution", "verifier"):
        value = step.get(field)
        if not isinstance(value, dict):
            continue
        for key in ("started_at", "finished_at"):
            raw = value.get(key)
            if raw is None:
                continue
            try:
                timestamps.append(
                    datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                )
            except ValueError as error:
                raise HarborEvidenceError(
                    "persistent round timestamp is invalid"
                ) from error
    if len(timestamps) < 2:
        raise HarborEvidenceError("persistent round lacks complete timing evidence")
    elapsed = (max(timestamps) - min(timestamps)).total_seconds()
    if elapsed < 0:
        raise HarborEvidenceError("persistent round timing order is invalid")
    return elapsed


def _write_round_history(
    request: ResearcherRequest,
    request_dir: Path,
    trial_dir: Path,
) -> Path:
    """只从 Agent 可见 transcript 构造下一轮可见的 canonical 历史包。"""
    transcript = trial_dir / "agent/codex.txt"
    if not transcript.is_file() or transcript.is_symlink():
        raise HarborEvidenceError("科学结果缺少 Agent 可见 transcript")
    content = transcript.read_text(encoding="utf-8", errors="strict")
    if not content.strip():
        raise HarborEvidenceError("Agent 可见 transcript 为空")
    value = {
        "schema_version": 2,
        "validation_session_id": request.validation_session_id,
        "round_index": request.round_index,
        "request_id": request.request_id,
        "question_revision": request.question_revision,
        "package_sha256": request.package_sha256,
        "transcript_sha256": sha256_file(transcript),
        "transcript_format": "codex-jsonl-lossless-actions-bounded-outputs",
        "transcript_events": _compact_agent_transcript(content),
    }
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    destination = request_dir / "round-history.json"
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != encoded:
            raise HarborEvidenceError("round history 已存在但字节身份不同")
        return destination
    descriptor, temporary = tempfile.mkstemp(
        prefix="round-history.",
        suffix=".tmp",
        dir=request_dir,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def _compact_agent_transcript(content: str) -> list[dict[str, Any]]:
    """保留 Agent 的推理动作，同时限制外部命令输出，避免命令行传输溢出。"""
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        event_type = value.get("type")
        item = value.get("item")
        if event_type == "thread.started" and isinstance(value.get("thread_id"), str):
            events.append(
                {
                    "line": line_number,
                    "type": "thread_started",
                    "thread_id": value["thread_id"],
                }
            )
        elif event_type == "item.completed" and isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "agent_message":
                events.append(
                    {
                        "line": line_number,
                        "type": "agent_message",
                        "text": str(item.get("text", "")),
                    }
                )
            elif item_type == "command_execution":
                output = str(item.get("aggregated_output", ""))
                events.append(
                    {
                        "line": line_number,
                        "type": "command_execution",
                        "command": str(item.get("command", "")),
                        "exit_code": item.get("exit_code"),
                        "output": _bounded_text(output),
                        "output_sha256": hashlib.sha256(
                            output.encode("utf-8")
                        ).hexdigest(),
                        "output_bytes": len(output.encode("utf-8")),
                    }
                )
        elif event_type == "turn.completed":
            events.append(
                {
                    "line": line_number,
                    "type": "turn_completed",
                    "usage": value.get("usage", {}),
                }
            )
    if not events:
        raise HarborEvidenceError("Agent transcript 不含可复用的完成事件")
    return events


def _bounded_text(value: str, *, limit: int = 4096) -> str:
    """确定性保留短输出；长输出保留等长首尾和截断标记。"""
    if len(value) <= limit:
        return value
    half = limit // 2
    omitted = len(value) - 2 * half
    return f"{value[:half]}\n...[omitted {omitted} characters]...\n{value[-half:]}"

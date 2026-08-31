"""Teacher 向 Researcher 交接，并由 Researcher 独占 Harbor 执行。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
from typing import Any
from enum import StrEnum

from .harbor import ResearcherRuntime, command_for
from .model import Actor, ContractError
from .package import package_sha256


class ResearcherError(RuntimeError):
    """Researcher 交接或执行契约被破坏时抛出。"""


class FailureStage(StrEnum):
    """Harbor 尝试失败所在的可恢复阶段。"""

    IMAGE = "IMAGE"
    SANDBOX = "SANDBOX"
    HARNESS_BOOTSTRAP = "HARNESS_BOOTSTRAP"
    MODEL_CONNECTION = "MODEL_CONNECTION"
    SCIENTIFIC_EXECUTION = "SCIENTIFIC_EXECUTION"
    VERIFIER = "VERIFIER"
    PLATFORM = "PLATFORM"


@dataclass(frozen=True)
class ApprovedHint:
    """由 Teacher 明确声明不含答案的一份提示上下文。"""

    validation_session_id: str
    round_index: int
    content: str
    teacher_declares_non_answer: bool
    schema_version: int = 1

    def validate(self) -> None:
        """只验证 Teacher 声明、会话绑定和非空提示，不解释自然语言。"""
        if self.schema_version != 1:
            raise ContractError("unsupported approved hint schema_version")
        if (
            not self.validation_session_id
            or self.round_index < 1
            or not self.content
            or self.content != self.content.strip()
        ):
            raise ContractError("approved hint identity and content are required")
        if self.teacher_declares_non_answer is not True:
            raise ContractError("Teacher must declare the hint as non-answer")

    def to_dict(self) -> dict[str, Any]:
        """返回稳定 JSON 表示。"""
        self.validate()
        return asdict(self)

    @classmethod
    def from_path(cls, path: Path) -> "ApprovedHint":
        """严格解析磁盘上的 Teacher 提示声明。"""
        try:
            value = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
            )
            if not isinstance(value, dict):
                raise ValueError("root must be an object")
            hint = cls(**value)
            hint.validate()
            return hint
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            raise ContractError(f"approved hint is invalid: {error}") from error


@dataclass(frozen=True)
class JobOutcome:
    """从 Harbor 权威结果中解析出的分层结果。"""

    classification: str
    reward: float | None
    result_path: str | None
    failure_stage: FailureStage | None = None


_MODEL_TRANSPORT_FAILURE_MARKERS = (
    "reconnecting... waiting for network",
    "connection failed: error sending request",
    "falling back from websockets to https transport",
)


@dataclass(frozen=True)
class ResearcherRequest:
    """一次全新正式科学尝试的冻结请求。"""

    request_id: str
    run_id: str
    question_revision: str
    attempt_index: int
    mode: str
    package_path: str
    package_sha256: str
    job_config_path: str
    job_config_sha256: str
    context_digests: tuple[str, ...]
    researcher_thread_id: str
    validation_session_id: str | None = None
    round_index: int | None = None
    prior_round_receipt_sha256s: tuple[str, ...] = ()
    prior_round_history_paths: tuple[str, ...] = ()
    approved_hint_path: str | None = None
    approved_hint_sha256: str | None = None
    controller_dir: str | None = None
    harness: str = "codex"
    model: str = "matmaster/gpt-5.6-sol"
    target_solution_time_sec: int = 1800
    scientific_timeout_sec: int = 3600
    schema_version: int = 1

    def validate(self) -> None:
        """约束冻结字节、角色目标、正式模型和时间预算。"""
        if self.schema_version not in {1, 2, 3}:
            raise ContractError("unsupported Researcher request schema_version")
        if self.mode not in {"blind", "hint", "interactive"} or self.attempt_index < 1:
            raise ContractError("invalid Researcher attempt mode or index")
        if self.schema_version != 3 and self.mode == "interactive":
            raise ContractError("interactive mode requires request schema_version 3")
        if self.schema_version == 3 and self.mode != "interactive":
            raise ContractError("persistent request must use interactive mode")
        if self.schema_version == 1 and self.mode == "blind" and self.context_digests:
            raise ContractError("blind attempts require empty context")
        if self.mode == "hint" and not self.context_digests:
            raise ContractError("hint attempts require reviewed context evidence")
        if self.schema_version == 2:
            self._validate_linear_session()
        if self.schema_version == 3:
            self._validate_persistent_session()
        supported = {
            ("dsh", "deepseek-v4-pro"),
            ("codex", "deepseek-v4-pro-202606"),
            # 仅用于回读已封签的历史 OAuth 请求；新请求默认由 Strong Gateway 生成。
            ("codex", "gpt-5.6-sol"),
            ("codex", "matmaster/gpt-5.6-sol"),
        }
        if (self.harness, self.model) not in supported:
            raise ContractError("unsupported Researcher harness and model pairing")
        if self.scientific_timeout_sec != 3600:
            raise ContractError("scientific timeout must remain 3600 seconds")
        if not 1 <= self.target_solution_time_sec <= 1800:
            raise ContractError("target solution time must be within 1800 seconds")
        validate_request_id(self.request_id)
        if not all((self.run_id, self.question_revision, self.researcher_thread_id)):
            raise ContractError("Researcher request identity is incomplete")
        if package_sha256(Path(self.package_path)) != self.package_sha256:
            raise ContractError("package bytes changed after freeze")
        if sha256_file(Path(self.job_config_path)) != self.job_config_sha256:
            raise ContractError("Harbor job config changed after handoff")

    def _validate_linear_session(self) -> None:
        """把后续轮次绑定到同一外层会话和全部前序记录。"""
        if not self.validation_session_id or self.round_index != self.attempt_index:
            raise ContractError(
                "linear request requires matching session and round identity"
            )
        expected_prior_count = self.round_index - 1
        if len(self.prior_round_receipt_sha256s) != expected_prior_count:
            raise ContractError("linear request must bind all prior round records")
        if len(self.prior_round_history_paths) != expected_prior_count:
            raise ContractError(
                "linear request must bind all prior round history bundles"
            )
        try:
            history_digests = tuple(
                sha256_file(Path(path))
                for path in self.prior_round_history_paths
                if Path(path).is_file() and not Path(path).is_symlink()
            )
        except OSError as error:
            raise ContractError(
                "linear request history bundle cannot be read"
            ) from error
        if len(history_digests) != expected_prior_count:
            raise ContractError("linear request history bundle must be a regular file")
        if history_digests != self.prior_round_receipt_sha256s:
            raise ContractError(
                "linear request history bundle bytes do not match their digests"
            )
        if (
            tuple(self.context_digests[:expected_prior_count])
            != self.prior_round_receipt_sha256s
        ):
            raise ContractError(
                "linear request context does not contain its prior round records"
            )
        if self.mode == "blind" and len(self.context_digests) != expected_prior_count:
            raise ContractError(
                "linear blind context is limited to prior round records"
            )
        if self.mode == "blind" and (
            self.approved_hint_path is not None or self.approved_hint_sha256 is not None
        ):
            raise ContractError("linear blind request cannot bind an approved hint")
        if self.mode == "hint":
            if len(self.context_digests) != expected_prior_count + 1:
                raise ContractError(
                    "linear hint context requires exactly one approved hint"
                )
            if not self.approved_hint_path or not _full_sha256(
                self.approved_hint_sha256
            ):
                raise ContractError("linear hint requires a bound approved hint")
            hint_path = Path(self.approved_hint_path)
            if sha256_file(hint_path) != self.approved_hint_sha256:
                raise ContractError("approved hint bytes changed after request freeze")
            if self.context_digests[-1] != self.approved_hint_sha256:
                raise ContractError(
                    "linear hint context does not end with the approved hint"
                )
            hint = ApprovedHint.from_path(hint_path)
            if (
                hint.validation_session_id != self.validation_session_id
                or hint.round_index != self.round_index
            ):
                raise ContractError("approved hint targets another session or round")
        if any(not _full_sha256(value) for value in self.prior_round_receipt_sha256s):
            raise ContractError("prior round record digest is invalid")

    def _validate_persistent_session(self) -> None:
        """Bind one request to one host-controlled persistent Harbor trial."""
        if not self.validation_session_id or self.attempt_index != 1:
            raise ContractError("persistent request requires one session-level attempt")
        if self.round_index is not None:
            raise ContractError(
                "persistent request does not represent an individual round"
            )
        if any(
            (
                self.context_digests,
                self.prior_round_receipt_sha256s,
                self.prior_round_history_paths,
                self.approved_hint_path,
                self.approved_hint_sha256,
            )
        ):
            raise ContractError(
                "persistent request cannot inject round history or hints"
            )
        controller = Path(self.controller_dir or "")
        if not controller.is_absolute():
            raise ContractError(
                "persistent request requires an absolute controller directory"
            )
        try:
            config = json.loads(Path(self.job_config_path).read_text(encoding="utf-8"))
            agents = config.get("agents", [])
            payload = agents[0].get("kwargs", {}).get("persistent_validation")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            AttributeError,
            IndexError,
        ) as error:
            raise ContractError("persistent Harbor config cannot be read") from error
        if (
            not isinstance(payload, dict)
            or payload.get("validation_session_id") != self.validation_session_id
            or Path(str(payload.get("controller_dir", ""))).resolve()
            != controller.resolve()
        ):
            raise ContractError(
                "persistent Harbor config targets another controller session"
            )

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的正式请求。"""
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResearcherRequest":
        """读取请求，并为未声明 harness 的历史 DSH 请求恢复原语义。"""
        converted = dict(value)
        converted["context_digests"] = tuple(converted.get("context_digests", ()))
        converted["prior_round_receipt_sha256s"] = tuple(
            converted.get("prior_round_receipt_sha256s", ())
        )
        converted["prior_round_history_paths"] = tuple(
            converted.get("prior_round_history_paths", ())
        )
        if "harness" not in converted:
            config = json.loads(
                Path(converted["job_config_path"]).read_text(encoding="utf-8")
            )
            agents = config.get("agents", [])
            model_name = agents[0].get("model_name") if len(agents) == 1 else None
            if model_name == "deepseek/deepseek-v4-pro":
                converted.update(harness="dsh", model="deepseek-v4-pro")
            elif isinstance(model_name, str):
                converted.update(harness="codex", model=model_name)
        request = cls(**converted)
        request.validate()
        return request


def validate_request_id(value: str) -> None:
    """把 request_id 限制为不能逃出 handoff root 的单个路径分量。"""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None
    ):
        raise ContractError("request_id must be one safe path component")


@dataclass(frozen=True)
class IssuedHandoff:
    """交给指定 Researcher 任务的文件路径。"""

    request_path: str
    capability_path: str
    token_path: str


@dataclass(frozen=True)
class ResearcherReceipt:
    """Harbor 结束后由 Researcher 返回的可审计结果。"""

    request_id: str
    classification: str
    reward: float | None
    result_path: str | None
    started_at: str
    finished_at: str
    exit_code: int
    result_sha256: str | None = None
    job_id: str | None = None
    trial_id: str | None = None
    sandbox_id: str | None = None
    session_id: str | None = None
    wall_time_sec: float | None = None
    failure_stage: str | None = None
    schema_version: int = 1


class CapabilityStore:
    """签发一次性且绑定任务的启动 capability。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def issue(
        self,
        actor: Actor,
        request: ResearcherRequest,
        *,
        closed_request_ids: tuple[str, ...] = (),
    ) -> IssuedHandoff:
        """持久化请求和一次性 capability；只有 Teacher 可以签发。"""
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".issue.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return self._issue_locked(
                actor, request, closed_request_ids=closed_request_ids
            )

    def _issue_locked(
        self,
        actor: Actor,
        request: ResearcherRequest,
        *,
        closed_request_ids: tuple[str, ...],
    ) -> IssuedHandoff:
        """在 handoff root 排他锁内冻结唯一 request 和 capability。"""
        if actor is not Actor.TEACHER:
            raise ResearcherError("only Teacher may issue a Researcher request")
        request.validate()
        if request.schema_version in {2, 3}:
            for path in self.root.glob("*/request.json"):
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    capability_path = path.parent / "capability.json"
                    capability = (
                        json.loads(capability_path.read_text(encoding="utf-8"))
                        if capability_path.is_file()
                        else {}
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ResearcherError(
                        "existing Researcher request ledger is invalid"
                    ) from error
                if (
                    isinstance(existing, dict)
                    and existing.get("validation_session_id")
                    == request.validation_session_id
                    and (
                        request.schema_version == 3
                        or existing.get("round_index") == request.round_index
                    )
                    and capability.get("status") != "REVOKED"
                    and path.parent.name not in closed_request_ids
                ):
                    raise ResearcherError(
                        "validation session round already has a Researcher request"
                    )
        directory = self.root / request.request_id
        if directory.exists():
            raise ResearcherError("request_id has already been issued")
        source_config = Path(request.job_config_path)
        try:
            config = json.loads(source_config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ResearcherError("Researcher JobConfig cannot be frozen") from error
        if not isinstance(config, dict):
            raise ResearcherError("Researcher JobConfig root must be an object")
        source_history_paths = [
            Path(path) for path in request.prior_round_history_paths
        ]
        if any(path.is_symlink() for path in source_history_paths):
            raise ResearcherError("round history bundle cannot be a symlink")
        context_paths = [path.resolve() for path in source_history_paths]
        for history_path in context_paths:
            try:
                history_path.relative_to(self.root.resolve())
            except ValueError as error:
                raise ResearcherError(
                    "round history must remain in the canonical handoff root"
                ) from error
            if history_path.name != "round-history.json" or not history_path.is_file():
                raise ResearcherError("round history bundle is not canonical")
        if request.mode == "hint":
            context_paths.append(Path(str(request.approved_hint_path)).resolve())
        directory.mkdir(parents=True)
        if request.schema_version == 3:
            controller_dir = (directory / "validation-control").resolve()
            try:
                config["agents"][0]["kwargs"]["persistent_validation"][
                    "controller_dir"
                ] = str(controller_dir)
            except (KeyError, IndexError, TypeError) as error:
                raise ResearcherError(
                    "persistent JobConfig payload is invalid"
                ) from error
            request = replace(request, controller_dir=str(controller_dir))
        job_slug = request.request_id.replace("-", "_")[:48]
        job_suffix = hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()[:12]
        config["job_name"] = f"{job_slug}_{job_suffix}"
        config["jobs_dir"] = str((directory / "harbor-jobs").resolve())
        config["extra_instruction_paths"] = [str(path) for path in context_paths]
        config_path = directory / "job-config.json"
        write_json(config_path, config)
        request = replace(
            request,
            job_config_path=str(config_path.resolve()),
            job_config_sha256=sha256_file(config_path),
        )
        request.validate()
        validate_launch_contract(request)
        request_path = directory / "request.json"
        capability_path = directory / "capability.json"
        token_path = directory / "token"
        token = secrets.token_urlsafe(32)
        write_json(request_path, request.to_dict())
        write_json(
            capability_path,
            {
                "schema_version": 2,
                "request_sha256": sha256_file(request_path),
                "researcher_thread_id": request.researcher_thread_id,
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "status": "ISSUED",
            },
        )
        token_path.write_text(token, encoding="utf-8")
        token_path.chmod(0o600)
        return IssuedHandoff(str(request_path), str(capability_path), str(token_path))

    def revoke(self, handoff: IssuedHandoff, *, reason: str) -> None:
        """撤销尚未兑换的请求；保留 request/capability 字节账本供审计。"""
        if not reason.strip():
            raise ResearcherError("capability revocation requires a reason")
        capability_path = Path(handoff.capability_path)
        token_path = Path(handoff.token_path)
        lock_path = capability_path.with_suffix(".lock")
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            capability = json.loads(capability_path.read_text(encoding="utf-8"))
            if capability.get("status") != "ISSUED":
                raise ResearcherError("only an issued capability may be revoked")
            capability["status"] = "REVOKED"
            capability["revoked_at"] = datetime.now(UTC).isoformat()
            capability["revocation_reason"] = reason
            write_json(capability_path, capability)
            if token_path.exists():
                token_path.unlink()

    def redeem(
        self, handoff: IssuedHandoff, current_thread_id: str
    ) -> ResearcherRequest:
        """只允许指定 Researcher 任务消费 capability。"""
        request_path = Path(handoff.request_path)
        capability_path = Path(handoff.capability_path)
        token_path = Path(handoff.token_path)
        lock_path = capability_path.with_suffix(".lock")
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            capability = json.loads(capability_path.read_text(encoding="utf-8"))
            if capability.get("status") != "ISSUED":
                raise ResearcherError("Researcher capability is not active")
            if current_thread_id != capability.get("researcher_thread_id"):
                raise ResearcherError("current task is not the designated Researcher")
            if sha256_file(request_path) != capability.get("request_sha256"):
                raise ResearcherError("Researcher request changed after issue")
            if not token_path.is_file():
                raise ResearcherError("Researcher capability token is missing")
            token = token_path.read_text(encoding="utf-8")
            if not secrets.compare_digest(
                hashlib.sha256(token.encode()).hexdigest(), capability["token_sha256"]
            ):
                raise ResearcherError("Researcher capability token is invalid")
            value = json.loads(request_path.read_text(encoding="utf-8"))
            request = ResearcherRequest.from_dict(value)
            validate_launch_contract(request)
            capability["status"] = "CONSUMED"
            capability["consumed_at"] = datetime.now(UTC).isoformat()
            capability["consumed_by_thread_id"] = current_thread_id
            write_json(capability_path, capability)
            token_path.unlink()
            return request


def execute_harbor(
    *,
    request: ResearcherRequest,
    runtime: ResearcherRuntime,
    env_file: Path,
    overall_timeout_sec: int = 7200,
) -> ResearcherReceipt:
    """Researcher 成功兑换后，不经 shell 执行 Harbor。"""
    runtime.validate()
    validate_launch_contract(request)
    started = datetime.now(UTC).isoformat()
    config_path = Path(request.job_config_path)
    command = command_for(config_path, env_file)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    job_dir = Path(config["jobs_dir"]) / config["job_name"]
    log_dir = config_path.parent / "researcher-execution"
    log_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    adapter_pythonpath = runtime.adapter_pythonpath
    if adapter_pythonpath:
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = adapter_pythonpath + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
    try:
        with (
            (log_dir / "stdout.log").open("wb") as stdout,
            (log_dir / "stderr.log").open("wb") as stderr,
        ):
            completed = subprocess.run(
                command,
                cwd=Path(request.package_path).parent,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                timeout=overall_timeout_sec,
                check=False,
            )
        exit_code = completed.returncode
        outcome = classify_job_outcome(job_dir, exit_code)
        classification, reward, result_path = (
            outcome.classification,
            outcome.reward,
            outcome.result_path,
        )
    except subprocess.TimeoutExpired:
        exit_code = 124
        outcome = JobOutcome("PLATFORM_FAILURE", None, None, FailureStage.PLATFORM)
        classification, reward, result_path = outcome.classification, None, None
    _capture_provider_identities(job_dir)
    finished = datetime.now(UTC)
    identifiers = extract_attempt_identifiers(Path(result_path)) if result_path else {}
    return ResearcherReceipt(
        request_id=request.request_id,
        classification=classification,
        reward=reward,
        result_path=result_path,
        started_at=started,
        finished_at=finished.isoformat(),
        exit_code=exit_code,
        result_sha256=sha256_file(Path(result_path)) if result_path else None,
        wall_time_sec=(finished - datetime.fromisoformat(started)).total_seconds(),
        failure_stage=outcome.failure_stage.value if outcome.failure_stage else None,
        **identifiers,
    )


_SANDBOX_CREATED = re.compile(r"^Sandbox created: ([A-Za-z0-9][A-Za-z0-9._-]*)$")


def _capture_provider_identities(job_dir: Path) -> None:
    """把 Harbor adapter 日志中的远端 sandbox 身份固化为严格结构化证据。"""
    log_path = job_dir / "job.log"
    if not log_path.is_file() or log_path.is_symlink():
        return
    try:
        matches = [
            match.group(1)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if (match := _SANDBOX_CREATED.fullmatch(line))
        ]
    except (OSError, UnicodeDecodeError):
        return
    if not matches or len(set(matches)) != len(matches):
        return
    write_json(
        job_dir / "provider-identities.json",
        {
            "schema_version": 2,
            "source": "harbor-adapter-job-log",
            "job_log_sha256": sha256_file(log_path),
            "agent_sandbox_id": matches[0],
            "verifier_sandbox_id": matches[1] if len(matches) == 2 else "",
            "verifier_sandbox_ids": matches[1:],
        },
    )


def extract_attempt_identifiers(result_path: Path) -> dict[str, str | None]:
    """提取全新 job、trial、环境实例和 harness 会话身份。"""
    job = json.loads(result_path.read_text(encoding="utf-8"))
    trial_files = sorted(
        path for path in result_path.parent.glob("*/result.json") if path != result_path
    )
    if len(trial_files) != 1:
        return {
            "job_id": str(job.get("id") or ""),
            "trial_id": None,
            "sandbox_id": None,
            "session_id": None,
        }
    trial = json.loads(trial_files[0].read_text(encoding="utf-8"))
    agent_result = trial.get("agent_result") or {}
    trace = (agent_result.get("metadata") or {}).get("trace") or {}
    session_id = str(trace.get("session_id") or "")
    if not session_id:
        session_id = _codex_thread_id(trial_files[0].parent / "agent" / "codex.txt")
    setup = trial.get("environment_setup", {})
    instance_material = "\0".join(
        str(value)
        for value in (
            job.get("id"),
            trial.get("id"),
            setup.get("started_at"),
            setup.get("finished_at"),
        )
    )
    return {
        "job_id": str(job.get("id") or ""),
        "trial_id": str(trial.get("id") or ""),
        "sandbox_id": hashlib.sha256(instance_material.encode()).hexdigest(),
        "session_id": session_id,
    }


def _codex_thread_id(path: Path) -> str:
    """从 Agent 可见 JSONL 中恢复 Codex 执行身份。"""
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get("type") == "thread.started" and isinstance(
            value.get("thread_id"), str
        ):
            return value["thread_id"]
    return ""


def validate_launch_contract(request: ResearcherRequest) -> None:
    """把冻结请求绑定到精确题包、模型、项目配置和上下文。"""
    config = json.loads(Path(request.job_config_path).read_text(encoding="utf-8"))
    tasks = config.get("tasks", [])
    agents = config.get("agents", [])
    if (
        len(tasks) != 1
        or Path(tasks[0].get("path", "")).resolve()
        != Path(request.package_path).resolve()
    ):
        raise ResearcherError("Harbor config task does not match the frozen package")
    expected_model, expected_name = (
        ("deepseek/deepseek-v4-pro", None)
        if request.harness == "dsh"
        else (request.model, None)
    )
    if (
        len(agents) != 1
        or agents[0].get("model_name") != expected_model
        or (expected_name is not None and agents[0].get("name") != expected_name)
    ):
        raise ResearcherError(
            "Harbor config model does not match the Researcher contract"
        )
    environment = config.get("environment", {})
    project_id = environment.get("kwargs", {}).get("project_id")
    if environment.get("type") != "lbg" or (
        project_id is not None
        and (not isinstance(project_id, int) or project_id <= 0)
    ):
        raise ResearcherError("Harbor config lacks an authorized LBG project")
    contexts = tuple(config.get("extra_instruction_paths", ()))
    actual = tuple(sha256_file(Path(path)) for path in contexts)
    if actual != request.context_digests:
        raise ResearcherError("Harbor context bytes do not match the frozen request")


def classify_job_result(
    job_dir: Path, exit_code: int
) -> tuple[str, float | None, str | None]:
    """兼容旧调用者的三元结果接口。"""
    outcome = classify_job_outcome(job_dir, exit_code)
    return outcome.classification, outcome.reward, outcome.result_path


def classify_job_outcome(job_dir: Path, exit_code: int) -> JobOutcome:
    """区分科学结果、超时和各个基础设施失败阶段。"""
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        return JobOutcome("PLATFORM_FAILURE", None, None, FailureStage.PLATFORM)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    stats = result.get("stats", {})
    exception_names = " ".join(
        name
        for evaluation in stats.get("evals", {}).values()
        for name in evaluation.get("exception_stats", {})
    )
    if "AgentTimeoutError" in exception_names and has_model_transport_failure(job_dir):
        return JobOutcome(
            "HARNESS_FAILURE",
            None,
            str(result_path),
            FailureStage.MODEL_CONNECTION,
        )
    if "AgentTimeoutError" in exception_names:
        return JobOutcome(
            "DEFERRED_TIMEOUT",
            None,
            str(result_path),
            FailureStage.SCIENTIFIC_EXECUTION,
        )
    if stats.get("n_errored_trials", 0):
        stage = _failure_stage(exception_names)
        kind = (
            "ENVIRONMENT_FAILURE"
            if stage in {FailureStage.IMAGE, FailureStage.SANDBOX}
            else "HARNESS_FAILURE"
            if stage in {FailureStage.HARNESS_BOOTSTRAP, FailureStage.MODEL_CONNECTION}
            else "PLATFORM_FAILURE"
        )
        return JobOutcome(kind, None, str(result_path), stage)
    rewards = []
    for evaluation in stats.get("evals", {}).values():
        for metric in evaluation.get("metrics", []):
            value = metric.get("reward", metric.get("mean"))
            if type(value) in (int, float) and math.isfinite(float(value)):
                rewards.append(float(value))
    if len(rewards) != 1 or exit_code != 0:
        return JobOutcome(
            "PLATFORM_FAILURE", None, str(result_path), FailureStage.PLATFORM
        )
    return JobOutcome("SCIENTIFIC_RESULT", float(rewards[0]), str(result_path))


def has_model_transport_failure(job_dir: Path) -> bool:
    """识别被 Harbor 外层超时掩盖的 Codex 模型连接失败。"""
    for path in sorted(job_dir.glob("*/agent/codex.txt")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as stream:
                content = stream.read(2 * 1024 * 1024).lower()
        except OSError:
            continue
        if any(marker in content for marker in _MODEL_TRANSPORT_FAILURE_MARKERS):
            return True
    return False


def _failure_stage(exception_names: str) -> FailureStage:
    lowered = exception_names.lower()
    if any(word in lowered for word in ("image", "pull")):
        return FailureStage.IMAGE
    if any(word in lowered for word in ("sandbox", "environmentsetup")):
        return FailureStage.SANDBOX
    if any(word in lowered for word in ("auth", "unauthorized", "api", "model")):
        return FailureStage.MODEL_CONNECTION
    if any(word in lowered for word in ("agent", "harness", "bootstrap")):
        return FailureStage.HARNESS_BOOTSTRAP
    if "verifier" in lowered:
        return FailureStage.VERIFIER
    return FailureStage.PLATFORM


def sha256_file(path: Path) -> str:
    """计算一个请求或配置文件的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    """为单写者原子写入私有交接状态。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会被普通 JSON decoder 静默覆盖的重复键。"""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _full_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

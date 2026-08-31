"""为支持的 Researcher 运行时生成确定性 Harbor 配置。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, TypeAlias

from .model import ContractError


_JOB_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
DEFAULT_FORMAL_HARNESS = "codex"
DEFAULT_FORMAL_MODEL = "matmaster/gpt-5.6-sol"


@dataclass(frozen=True)
class DshRuntime:
    """位于题目镜像之外的固定 DSH harness 覆盖层输入。"""

    agent_import: str
    adapter_pythonpath: str
    development_artifact_path: str
    development_artifact_sha256: str
    harness_version: str
    node_runtime_path: str
    node_runtime_sha256: str
    node_runtime_version: str

    def validate(self) -> None:
        """要求本地 harness 与 Node 产物均已固定。"""
        for path_value, digest in (
            (self.development_artifact_path, self.development_artifact_sha256),
            (self.node_runtime_path, self.node_runtime_sha256),
        ):
            path = Path(path_value)
            if not path.is_file():
                raise ContractError(f"DSH runtime artifact is missing: {path}")
            if file_sha256(path) != digest:
                raise ContractError(f"DSH runtime artifact SHA-256 mismatch: {path}")
        adapter_paths = self.adapter_pythonpath.split(os.pathsep)
        if (
            not adapter_paths
            or any(not Path(path).is_dir() for path in adapter_paths)
            or ":" not in self.agent_import
        ):
            raise ContractError("DSH adapter import is invalid")


@dataclass(frozen=True)
class CodexRuntime:
    """固定的内置 Codex harness 设置。"""

    model_name: str = "deepseek-v4-pro-202606"
    reasoning_effort: str = "high"
    agent_import: str = "codex"
    adapter_pythonpath: str | None = None
    credential_profile: str = "standard"
    portable_binary_path: str | None = None
    portable_binary_sha256: str | None = None
    portable_binary_version: str | None = None
    portable_code_mode_host_path: str | None = None
    portable_code_mode_host_sha256: str | None = None

    def validate(self) -> None:
        """拒绝正式 Codex 运行的模型、推理强度或凭证配置漂移。"""
        if self.model_name not in {
            "deepseek-v4-pro-202606",
            "matmaster/gpt-5.6-sol",
        }:
            raise ContractError("unsupported Codex Researcher model")
        if self.reasoning_effort != "high":
            raise ContractError("Codex Researcher reasoning effort must be high")
        if self.credential_profile not in {"standard", "strong"}:
            raise ContractError("unsupported Codex credential profile")
        if self.model_name == "matmaster/gpt-5.6-sol":
            adapter_paths = (self.adapter_pythonpath or "").split(os.pathsep)
            if (
                self.credential_profile != "strong"
                or ":" not in self.agent_import
                or not adapter_paths
                or any(not Path(path).is_dir() for path in adapter_paths)
            ):
                raise ContractError(
                    "GPT-5.6 requires the strong gateway namespace adapter"
                )
        portable = (
            self.portable_binary_path,
            self.portable_binary_sha256,
            self.portable_binary_version,
            self.portable_code_mode_host_path,
            self.portable_code_mode_host_sha256,
        )
        if any(portable) and not all(portable):
            raise ContractError(
                "portable Codex runtime fields must be provided together"
            )
        if self.portable_binary_path:
            binary = Path(self.portable_binary_path)
            host = Path(str(self.portable_code_mode_host_path))
            for label, path, expected in (
                ("binary", binary, self.portable_binary_sha256),
                ("code-mode host", host, self.portable_code_mode_host_sha256),
            ):
                if not path.is_file() or file_sha256(path) != expected:
                    raise ContractError(
                        f"portable Codex {label} is missing or has checksum drift"
                    )
            adapter_paths = (self.adapter_pythonpath or "").split(os.pathsep)
            if (
                not adapter_paths
                or any(not Path(path).is_dir() for path in adapter_paths)
                or ":" not in self.agent_import
            ):
                raise ContractError("portable Codex adapter import is invalid")


def formal_codex_runtime(
    adapter_pythonpath: str,
    *,
    portable_binary_path: str,
    portable_binary_sha256: str,
    portable_binary_version: str,
    portable_code_mode_host_path: str,
    portable_code_mode_host_sha256: str,
) -> CodexRuntime:
    """构造新题正式验证使用的 Codex + GPT‑5.6 运行配置。"""
    runtime = CodexRuntime(
        model_name=DEFAULT_FORMAL_MODEL,
        reasoning_effort="high",
        agent_import="taskfoundry.portable_codex_agent:PortableCodex",
        adapter_pythonpath=adapter_pythonpath,
        credential_profile="strong",
        portable_binary_path=portable_binary_path,
        portable_binary_sha256=portable_binary_sha256,
        portable_binary_version=portable_binary_version,
        portable_code_mode_host_path=portable_code_mode_host_path,
        portable_code_mode_host_sha256=portable_code_mode_host_sha256,
    )
    runtime.validate()
    return runtime


ResearcherRuntime: TypeAlias = DshRuntime | CodexRuntime


@dataclass(frozen=True)
class HarborJobSpec:
    """由 Researcher 拥有的一次全新 Harbor 任务输入。"""

    job_name: str
    jobs_dir: str
    task_path: str
    context_paths: tuple[str, ...]
    lbg_project_id: int | None
    model_name: str = "deepseek/deepseek-v4-pro"
    environment_type: str = "lbg"

    def validate(self) -> None:
        """拒绝有歧义任务、不支持的环境提供方和模型漂移。"""
        if not _JOB_NAME.fullmatch(self.job_name):
            raise ContractError("invalid Harbor job name")
        if self.model_name not in {
            "deepseek/deepseek-v4-pro",
            "deepseek-v4-pro-202606",
            # 保留历史 JobConfig 的解析兼容；正式默认仍是 matmaster 命名空间。
            "gpt-5.6-sol",
            "matmaster/gpt-5.6-sol",
        }:
            raise ContractError(
                "Researcher model must be an approved v4-pro or GPT-5.6 identifier"
            )
        if self.environment_type != "lbg" or (
            self.lbg_project_id is not None and self.lbg_project_id <= 0
        ):
            raise ContractError(
                "V1 Harbor environment must be an authorized LBG project"
            )
        if not Path(self.task_path).is_dir():
            raise ContractError("Harbor task path does not exist")
        if not Path(self.jobs_dir).is_dir():
            raise ContractError("Harbor jobs directory does not exist")
        for context in self.context_paths:
            if not Path(context).is_file():
                raise ContractError(f"approved context file does not exist: {context}")


@dataclass(frozen=True)
class PersistentValidationSpec:
    """Host-side control contract for one persistent Harbor validation trial."""

    validation_session_id: str
    controller_dir: str
    pass_threshold: float = 0.85
    max_blind_rounds: int = 3
    max_hint_rounds: int = 2
    decision_timeout_sec: int = 10800
    schema_version: int = 1

    def validate(self) -> None:
        """Match Harbor's strict bounds before serializing the JobConfig."""
        controller = Path(self.controller_dir)
        if self.schema_version != 1 or not self.validation_session_id.strip():
            raise ContractError("persistent validation identity is invalid")
        if not controller.is_absolute():
            raise ContractError("persistent validation controller dir must be absolute")
        if not 0 < self.pass_threshold <= 1:
            raise ContractError("persistent validation threshold must be in (0, 1]")
        if self.max_blind_rounds != 3 or self.max_hint_rounds not in {1, 2}:
            raise ContractError("persistent validation requires three blind rounds")
        if not 1 <= self.decision_timeout_sec <= 86400:
            raise ContractError("persistent validation decision timeout is invalid")

    def to_dict(self) -> dict[str, Any]:
        """Return the exact custom Agent payload consumed by Harbor."""
        self.validate()
        return {
            "schema_version": self.schema_version,
            "validation_session_id": self.validation_session_id,
            "controller_dir": str(Path(self.controller_dir).resolve()),
            "pass_threshold": self.pass_threshold,
            "max_blind_rounds": self.max_blind_rounds,
            "max_hint_rounds": self.max_hint_rounds,
            "decision_timeout_sec": self.decision_timeout_sec,
        }


def build_job_config(
    spec: HarborJobSpec,
    runtime: ResearcherRuntime,
    *,
    persistent_validation: PersistentValidationSpec | None = None,
) -> dict[str, Any]:
    """构造正式 Researcher 消费的精确 Harbor JobConfig。"""
    spec.validate()
    runtime.validate()
    if isinstance(runtime, DshRuntime):
        if spec.model_name != "deepseek/deepseek-v4-pro":
            raise ContractError("DSH Researcher model must be deepseek/deepseek-v4-pro")
        agent = {
            "name": runtime.agent_import,
            "model_name": spec.model_name,
            "kwargs": {
                "development_artifact_path": runtime.development_artifact_path,
                "development_artifact_sha256": runtime.development_artifact_sha256,
                "version": runtime.harness_version,
                "permission_mode": "danger-full-access",
                "node_runtime_path": runtime.node_runtime_path,
                "node_runtime_sha256": runtime.node_runtime_sha256,
                "node_runtime_version": runtime.node_runtime_version,
            },
            "env": {
                "DEEPSEEK_API_KEY": "${LLM_API_KEY}",
                "DEEPSEEK_BASE_URL": "${LLM_BASE_URL}",
                "DSH_TELEMETRY_DISABLED": "1",
            },
        }
    else:
        if spec.model_name != runtime.model_name:
            raise ContractError("Codex job model does not match its pinned runtime")
        kwargs: dict[str, Any] = {"reasoning_effort": runtime.reasoning_effort}
        if runtime.portable_binary_path:
            kwargs.update(
                {
                    "portable_binary_path": runtime.portable_binary_path,
                    "portable_binary_sha256": runtime.portable_binary_sha256,
                    "portable_binary_version": runtime.portable_binary_version,
                    "portable_code_mode_host_path": runtime.portable_code_mode_host_path,
                    "portable_code_mode_host_sha256": runtime.portable_code_mode_host_sha256,
                }
            )
        if runtime.credential_profile == "strong":
            codex_env = {
                "OPENAI_API_KEY": "${STRONG_API_KEY}",
                "OPENAI_BASE_URL": "${STRONG_BASE_URL}",
                "HTTP_PROXY": "${STRONG_PROXY}",
                "HTTPS_PROXY": "${STRONG_PROXY}",
                "ALL_PROXY": "${STRONG_PROXY}",
            }
        else:
            codex_env = {
                "OPENAI_API_KEY": "${LLM_API_KEY}",
                "OPENAI_BASE_URL": "${LLM_BASE_URL}",
            }
        agent = {
            "name": runtime.agent_import,
            "model_name": runtime.model_name,
            "kwargs": kwargs,
            "env": codex_env,
        }
        if persistent_validation is not None:
            if not runtime.portable_binary_path:
                raise ContractError(
                    "persistent validation requires the portable Codex runtime"
                )
            persistent_validation.validate()
            Path(persistent_validation.controller_dir).mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
            agent["name"] = "taskfoundry.persistent_codex_agent:PersistentPortableCodex"
            agent["kwargs"]["persistent_validation"] = persistent_validation.to_dict()
    if persistent_validation is not None and isinstance(runtime, DshRuntime):
        raise ContractError("persistent validation currently requires Codex")
    return {
        "job_name": spec.job_name,
        "jobs_dir": str(Path(spec.jobs_dir).resolve()),
        "debug": True,
        "n_concurrent_trials": 1,
        "environment": {
            "type": "lbg",
            "kwargs": {
                "project_id": spec.lbg_project_id,
                "mount_user_storage": False,
                "sandbox_create_retries": 6,
                "sandbox_create_retry_delay_seconds": 60,
            },
        },
        "agents": [agent],
        "tasks": [{"path": str(Path(spec.task_path).resolve())}],
        "extra_instruction_paths": [
            str(Path(path).resolve()) for path in spec.context_paths
        ],
    }


def runtime_from_dict(value: dict[str, Any]) -> ResearcherRuntime:
    """加载运行时描述，并兼容没有 harness 字段的历史 DSH JSON。"""
    runtime_value = dict(value)
    harness = runtime_value.pop("harness", "dsh")
    if harness == "dsh":
        return DshRuntime(**runtime_value)
    if harness == "codex":
        return CodexRuntime(**runtime_value)
    raise ContractError(f"unsupported Researcher harness: {harness}")


def write_job_config(path: Path, config: dict[str, Any]) -> None:
    """原子写入 Harbor JobConfig。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def command_for(config_path: Path, env_file: Path) -> tuple[str, ...]:
    """返回 argv 元组，绝不把凭证插值进 shell。"""
    if not config_path.is_file() or not env_file.is_file():
        raise ContractError("Harbor config and credential env file must exist")
    return (
        "harbor",
        "run",
        "--config",
        str(config_path),
        "--env-file",
        str(env_file),
        "--yes",
    )


def file_sha256(path: Path) -> str:
    """计算一个本地运行时产物的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

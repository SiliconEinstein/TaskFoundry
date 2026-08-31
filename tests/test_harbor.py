from __future__ import annotations

import hashlib
import json

import pytest

from taskfoundry.harbor import (
    CodexRuntime,
    DshRuntime,
    HarborJobSpec,
    PersistentValidationSpec,
    build_job_config,
    command_for,
    formal_codex_runtime,
    runtime_from_dict,
    write_job_config,
)
from taskfoundry.model import ContractError


def runtime(tmp_path):
    artifact = tmp_path / "dsh.tar.gz"
    node = tmp_path / "node"
    adapter = tmp_path / "adapter"
    artifact.write_bytes(b"dsh")
    node.write_bytes(b"node")
    adapter.mkdir()
    return DshRuntime(
        agent_import="adapter:Agent",
        adapter_pythonpath=str(adapter),
        development_artifact_path=str(artifact),
        development_artifact_sha256=hashlib.sha256(b"dsh").hexdigest(),
        harness_version="1",
        node_runtime_path=str(node),
        node_runtime_sha256=hashlib.sha256(b"node").hexdigest(),
        node_runtime_version="v22",
    )


def spec(tmp_path, **changes):
    task = tmp_path / "task"
    jobs = tmp_path / "jobs"
    task.mkdir(exist_ok=True)
    jobs.mkdir(exist_ok=True)
    values = {
        "job_name": "question__blind__a01",
        "jobs_dir": str(jobs),
        "task_path": str(task),
        "context_paths": (),
        "lbg_project_id": 42,
    }
    return HarborJobSpec(**(values | changes))


def test_build_job_config_pins_dsh_model_and_secret_names(tmp_path) -> None:
    config = build_job_config(spec(tmp_path), runtime(tmp_path))
    agent = config["agents"][0]
    assert agent["model_name"] == "deepseek/deepseek-v4-pro"
    assert agent["env"]["DEEPSEEK_API_KEY"] == "${LLM_API_KEY}"
    assert config["n_concurrent_trials"] == 1
    assert config["environment"]["kwargs"]["mount_user_storage"] is False


def test_build_job_config_supports_codex_v4_pro(tmp_path) -> None:
    config = build_job_config(
        spec(tmp_path, model_name="deepseek-v4-pro-202606"),
        CodexRuntime(),
    )
    assert config["agents"] == [
        {
            "name": "codex",
            "model_name": "deepseek-v4-pro-202606",
            "kwargs": {"reasoning_effort": "high"},
                "env": {
                    "OPENAI_API_KEY": "${LLM_API_KEY}",
                "OPENAI_BASE_URL": "${LLM_BASE_URL}",
            },
        }
    ]


def test_runtime_loader_preserves_legacy_dsh_and_loads_codex(tmp_path) -> None:
    legacy = runtime(tmp_path)
    assert runtime_from_dict(legacy.__dict__) == legacy
    assert runtime_from_dict({"harness": "codex"}) == CodexRuntime()


def test_build_job_config_supports_codex_gpt_56(tmp_path) -> None:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    runtime_value = CodexRuntime(
        model_name="matmaster/gpt-5.6-sol",
        agent_import="taskfoundry.gateway_codex_agent:GatewayCodex",
        adapter_pythonpath=str(adapter),
        credential_profile="strong",
    )
    config = build_job_config(
        spec(tmp_path, model_name=runtime_value.model_name),
        runtime_value,
    )
    assert config["agents"][0]["model_name"] == "matmaster/gpt-5.6-sol"
    assert config["agents"][0]["env"] == {
        "OPENAI_API_KEY": "${STRONG_API_KEY}",
        "OPENAI_BASE_URL": "${STRONG_BASE_URL}",
        "HTTP_PROXY": "${STRONG_PROXY}",
        "HTTPS_PROXY": "${STRONG_PROXY}",
        "ALL_PROXY": "${STRONG_PROXY}",
    }


def test_build_job_config_allows_env_owned_lbg_project_id(tmp_path) -> None:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    runtime_value = CodexRuntime(
        model_name="matmaster/gpt-5.6-sol",
        agent_import="taskfoundry.gateway_codex_agent:GatewayCodex",
        adapter_pythonpath=str(adapter),
        credential_profile="strong",
    )
    config = build_job_config(
        spec(tmp_path, model_name=runtime_value.model_name, lbg_project_id=None),
        runtime_value,
    )
    assert config["environment"]["kwargs"]["project_id"] is None


def test_formal_runtime_is_codex_gpt_56(tmp_path) -> None:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    binary = tmp_path / "codex"
    host = tmp_path / "codex-code-mode-host"
    binary.write_bytes(b"codex")
    host.write_bytes(b"host")
    runtime_value = formal_codex_runtime(
        str(adapter),
        portable_binary_path=str(binary),
        portable_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        portable_binary_version="1.2.3",
        portable_code_mode_host_path=str(host),
        portable_code_mode_host_sha256=hashlib.sha256(host.read_bytes()).hexdigest(),
    )
    assert runtime_value.model_name == "matmaster/gpt-5.6-sol"
    assert runtime_value.credential_profile == "strong"
    assert (
        runtime_value.agent_import == "taskfoundry.portable_codex_agent:PortableCodex"
    )
    config = build_job_config(
        spec(tmp_path, model_name=runtime_value.model_name), runtime_value
    )
    kwargs = config["agents"][0]["kwargs"]
    assert kwargs["portable_code_mode_host_path"] == str(host)
    assert config["agents"][0]["env"] == {
        "OPENAI_API_KEY": "${STRONG_API_KEY}",
        "OPENAI_BASE_URL": "${STRONG_BASE_URL}",
        "HTTP_PROXY": "${STRONG_PROXY}",
        "HTTPS_PROXY": "${STRONG_PROXY}",
        "ALL_PROXY": "${STRONG_PROXY}",
    }


def test_job_config_enables_one_persistent_teacher_controlled_session(tmp_path) -> None:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    binary = tmp_path / "codex"
    host = tmp_path / "codex-code-mode-host"
    binary.write_bytes(b"codex")
    host.write_bytes(b"host")
    runtime_value = formal_codex_runtime(
        str(adapter),
        portable_binary_path=str(binary),
        portable_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        portable_binary_version="1.2.3",
        portable_code_mode_host_path=str(host),
        portable_code_mode_host_sha256=hashlib.sha256(host.read_bytes()).hexdigest(),
    )
    controller = tmp_path / "controller"
    config = build_job_config(
        spec(tmp_path, model_name=runtime_value.model_name),
        runtime_value,
        persistent_validation=PersistentValidationSpec(
            validation_session_id="session-1",
            controller_dir=str(controller),
            pass_threshold=0.85,
        ),
    )

    agent = config["agents"][0]
    assert agent["name"] == "taskfoundry.persistent_codex_agent:PersistentPortableCodex"
    assert agent["kwargs"]["persistent_validation"] == {
        "schema_version": 1,
        "validation_session_id": "session-1",
        "controller_dir": str(controller.resolve()),
        "pass_threshold": 0.85,
        "max_blind_rounds": 3,
        "max_hint_rounds": 2,
        "decision_timeout_sec": 10800,
    }


@pytest.mark.parametrize(
    "runtime_value,message",
    [
        (CodexRuntime(model_name="unknown"), "unsupported Codex"),
        (CodexRuntime(reasoning_effort="low"), "reasoning effort"),
        (CodexRuntime(credential_profile="unknown"), "credential profile"),
        (CodexRuntime(model_name="matmaster/gpt-5.6-sol"), "strong gateway"),
        (CodexRuntime(portable_binary_path="missing"), "provided together"),
        (
            CodexRuntime(
                portable_binary_path="missing",
                portable_binary_sha256="a" * 64,
                portable_binary_version="1",
            ),
            "provided together",
        ),
    ],
)
def test_codex_runtime_rejects_invalid_formal_configuration(
    runtime_value, message
) -> None:
    with pytest.raises(ContractError, match=message):
        runtime_value.validate()


def test_runtime_loader_and_job_builder_reject_harness_drift(tmp_path) -> None:
    with pytest.raises(ContractError, match="unsupported Researcher harness"):
        runtime_from_dict({"harness": "unknown"})
    with pytest.raises(ContractError, match="DSH Researcher model"):
        build_job_config(
            spec(tmp_path, model_name="deepseek-v4-pro-202606"),
            runtime(tmp_path),
        )
    with pytest.raises(ContractError, match="pinned runtime"):
        build_job_config(
            spec(tmp_path, model_name="matmaster/gpt-5.6-sol"),
            CodexRuntime(),
        )


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"job_name": "bad name"}, "job name"),
        ({"model_name": "deepseek/deepseek-v4-flash"}, "v4-pro"),
        ({"lbg_project_id": 0}, "authorized LBG"),
    ],
)
def test_job_contract_rejects_drift(tmp_path, changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        build_job_config(spec(tmp_path, **changes), runtime(tmp_path))


def test_runtime_checksum_is_verified(tmp_path) -> None:
    value = runtime(tmp_path)
    object.__setattr__(value, "node_runtime_sha256", "0" * 64)
    with pytest.raises(ContractError, match="mismatch"):
        build_job_config(spec(tmp_path), value)


def test_context_must_exist(tmp_path) -> None:
    missing = tmp_path / "missing.md"
    with pytest.raises(ContractError, match="context"):
        build_job_config(
            spec(tmp_path, context_paths=(str(missing),)), runtime(tmp_path)
        )


def test_write_and_command_do_not_expand_credentials(tmp_path) -> None:
    config_path = tmp_path / "job.json"
    config = build_job_config(spec(tmp_path), runtime(tmp_path))
    write_job_config(config_path, config)
    assert json.loads(config_path.read_text()) == config
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=secret\n")
    command = command_for(config_path, env_file)
    assert command[0:2] == ("harbor", "run")
    assert "secret" not in " ".join(command)

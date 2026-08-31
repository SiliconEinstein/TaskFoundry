from unittest.mock import AsyncMock

import pytest

from taskfoundry.persistent_codex_agent import PersistentPortableCodex


def test_persistent_codex_uses_one_new_session_then_resumes() -> None:
    agent = object.__new__(PersistentPortableCodex)
    agent._persistent_session_started = False

    assert agent._codex_exec_subcommand() == "exec"
    assert agent._codex_exec_subcommand() == "exec resume --last"
    assert agent._cleanup_codex_home_after_run() is False
    assert agent._reset_codex_config_before_run() is True


@pytest.mark.asyncio
async def test_persistent_codex_run_resumes_same_remote_session(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    agent = PersistentPortableCodex(
        logs_dir=tmp_path / "logs",
        persistent_validation={
            "schema_version": 1,
            "validation_session_id": "session-1",
            "controller_dir": str(tmp_path / "controller"),
            "pass_threshold": 0.85,
            "max_blind_rounds": 3,
            "max_hint_rounds": 2,
            "decision_timeout_sec": 10800,
        },
        portable_binary_path=tmp_path / "codex",
        portable_binary_sha256="a" * 64,
        portable_binary_version="1",
        portable_code_mode_host_path=tmp_path / "host",
        portable_code_mode_host_sha256="b" * 64,
        model_name="openai/o3",
    )
    environment = AsyncMock()
    environment.default_user = "agent"
    environment.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    await agent.run("first", environment, AsyncMock())
    await agent.run("second", environment, AsyncMock())

    commands = [
        call.kwargs.get("command", "") for call in environment.exec.call_args_list
    ]
    assert sum("codex exec --dangerously" in command for command in commands) == 1
    assert sum("codex exec resume --last " in command for command in commands) == 1
    assert sum(': >"$CODEX_HOME/config.toml"' in command for command in commands) == 2
    assert all(
        'rm -rf /tmp/codex-secrets "$CODEX_HOME"' not in command for command in commands
    )
    assert all("features.respect_system_proxy" not in command for command in commands)
    assert all(
        "features.unbounded_connection_retries" not in command
        for command in commands
    )

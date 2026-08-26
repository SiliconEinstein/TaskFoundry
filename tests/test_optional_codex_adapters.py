from __future__ import annotations

import asyncio
import hashlib
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


class FakeCodex:
    def __init__(self, logs_dir=None, *args, **kwargs) -> None:
        self.model_name = kwargs.get("model_name", "matmaster/gpt-5.6-sol")

    async def run(self, instruction, environment, context) -> None:
        environment.observed_model = self.model_name

    async def exec_as_root(self, environment, *, command):
        environment.root_command = command

    async def exec_as_agent(self, environment, *, command):
        environment.agent_command = command
        return SimpleNamespace(stdout="codex-cli 1.2.3\n")


def _install_fake_harbor(monkeypatch) -> None:
    modules = {
        "harbor": ModuleType("harbor"),
        "harbor.agents": ModuleType("harbor.agents"),
        "harbor.agents.installed": ModuleType("harbor.agents.installed"),
        "harbor.agents.installed.codex": ModuleType("harbor.agents.installed.codex"),
        "harbor.environments": ModuleType("harbor.environments"),
        "harbor.environments.base": ModuleType("harbor.environments.base"),
        "harbor.models": ModuleType("harbor.models"),
        "harbor.models.agent": ModuleType("harbor.models.agent"),
        "harbor.models.agent.context": ModuleType("harbor.models.agent.context"),
    }
    modules["harbor.agents.installed.codex"].Codex = FakeCodex
    modules["harbor.environments.base"].BaseEnvironment = object
    modules["harbor.models.agent.context"].AgentContext = object
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_gateway_adapter_preserves_namespaced_model(monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    sys.modules.pop("taskfoundry.gateway_codex_agent", None)
    module = importlib.import_module("taskfoundry.gateway_codex_agent")
    adapter = module.GatewayCodex(model_name="matmaster/gpt-5.6-sol")
    environment = SimpleNamespace(observed_model=None)

    asyncio.run(adapter.run("solve", environment, object()))

    assert environment.observed_model == "matmaster/gpt-5.6-sol"
    assert adapter.model_name == "matmaster/gpt-5.6-sol"
    assert module._GatewayModelName("a/b").split("/") == ["a/b"]
    assert module._GatewayModelName("a b").split() == ["a", "b"]


def test_portable_adapter_uploads_and_verifies_binary(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    sys.modules.pop("taskfoundry.portable_codex_agent", None)
    module = importlib.import_module("taskfoundry.portable_codex_agent")
    binary = tmp_path / "codex"
    host = tmp_path / "codex-code-mode-host"
    binary.write_bytes(b"portable-codex")
    host.write_bytes(b"portable-host")
    adapter = module.PortableCodex(
        tmp_path,
        binary,
        hashlib.sha256(binary.read_bytes()).hexdigest(),
        "1.2.3",
        host,
        hashlib.sha256(host.read_bytes()).hexdigest(),
        model_name="matmaster/gpt-5.6-sol",
    )

    class Environment:
        async def upload_file(self, source, target):
            self.uploads = getattr(self, "uploads", []) + [(source, target)]

    environment = Environment()
    asyncio.run(adapter.install(environment))

    assert [target for _, target in environment.uploads] == [
        "/installed-agent/codex/codex",
        "/installed-agent/codex/codex-code-mode-host",
    ]
    assert "sha256sum -c" in environment.root_command
    assert "codex-code-mode-host" in environment.root_command
    assert environment.agent_command == "codex --version"
    asyncio.run(adapter.run("solve", environment, object()))
    assert environment.observed_model == "matmaster/gpt-5.6-sol"


def test_portable_adapter_rejects_missing_or_drifted_binary(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    sys.modules.pop("taskfoundry.portable_codex_agent", None)
    module = importlib.import_module("taskfoundry.portable_codex_agent")
    missing = module.PortableCodex(
        tmp_path, tmp_path / "missing", "a" * 64, "1", tmp_path / "host", "b" * 64
    )
    with pytest.raises(FileNotFoundError):
        asyncio.run(missing.install(object()))

    binary = tmp_path / "codex"
    host = tmp_path / "codex-code-mode-host"
    binary.write_bytes(b"content")
    host.write_bytes(b"host")
    drifted = module.PortableCodex(
        tmp_path, binary, "a" * 64, "1", host, hashlib.sha256(host.read_bytes()).hexdigest()
    )
    with pytest.raises(RuntimeError, match="SHA-256"):
        asyncio.run(drifted.install(object()))

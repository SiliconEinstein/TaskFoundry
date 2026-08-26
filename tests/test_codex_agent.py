from __future__ import annotations

import subprocess

import pytest

from taskfoundry.codex_agent import CodexDispatcher
from taskfoundry.model import Actor, ContractError


def test_dispatches_file_prompt_and_parses_receipt(tmp_path, monkeypatch) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("[TASKFOUNDRY ROLE=researcher]\nRun the request.\n")

    def fake_run(command, **kwargs):
        assert command[:4] == ["codex", "queue", "--thread", "abcd-1234"]
        assert "Run the request" in command[-1]
        return subprocess.CompletedProcess(command, 0, "Queued message dead-beef for thread abcd-1234.\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    receipt = CodexDispatcher().queue(role=Actor.RESEARCHER, thread_id="abcd-1234", prompt_path=prompt)
    assert receipt.message_id == "dead-beef"


def test_requires_role_boundary(tmp_path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do something.\n")
    with pytest.raises(ContractError, match="ROLE=teacher"):
        CodexDispatcher().queue(role=Actor.TEACHER, thread_id="thread", prompt_path=prompt)


def test_rejects_failed_or_ambiguous_dispatch(tmp_path, monkeypatch) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("[TASKFOUNDRY ROLE=reviewer]\nReview.\n")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "no receipt", ""),
    )
    with pytest.raises(RuntimeError, match="valid queue receipt"):
        CodexDispatcher().queue(role=Actor.REVIEWER, thread_id="thread", prompt_path=prompt)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 2, "", "offline"),
    )
    with pytest.raises(RuntimeError, match="offline"):
        CodexDispatcher().queue(role=Actor.REVIEWER, thread_id="thread", prompt_path=prompt)


def test_dispatch_rejects_unsupported_role_and_missing_input(tmp_path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("[TASKFOUNDRY ROLE=system]\nrun\n")
    with pytest.raises(ContractError, match="supports"):
        CodexDispatcher().queue(role=Actor.SYSTEM, thread_id="thread", prompt_path=prompt)
    with pytest.raises(ContractError, match="required"):
        CodexDispatcher().queue(role=Actor.TEACHER, thread_id="", prompt_path=tmp_path / "missing")

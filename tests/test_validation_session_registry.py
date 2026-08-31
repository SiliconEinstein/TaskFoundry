"""全局线性验证会话注册契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taskfoundry.model import ContractError
from taskfoundry.validation_session import ValidationSessionRegistry


def reserve(
    registry: ValidationSessionRegistry,
    *,
    run_id: str = "run-one",
    revision: str = "r1",
    session: str = "session-one",
    thread: str = "thread-one",
):
    return registry.reserve(
        run_id=run_id,
        question_revision=revision,
        validation_session_id=session,
        researcher_thread_id=thread,
        package_sha256="a" * 64,
    )


def test_registry_rejects_reused_revision_session_and_thread(tmp_path: Path) -> None:
    registry = ValidationSessionRegistry(tmp_path / "validation-sessions.jsonl")
    reserve(registry)

    with pytest.raises(ContractError, match="revision"):
        reserve(registry, session="session-two", thread="thread-two")
    with pytest.raises(ContractError, match="session"):
        reserve(registry, run_id="run-two", revision="r2", thread="thread-two")
    with pytest.raises(ContractError, match="thread"):
        reserve(registry, run_id="run-two", revision="r2", session="session-two")


def test_closed_session_still_cannot_be_reused(tmp_path: Path) -> None:
    registry = ValidationSessionRegistry(tmp_path / "validation-sessions.jsonl")
    reserve(registry)
    registry.close("session-one", status="CLOSED_TOO_EASY")

    with pytest.raises(ContractError, match="session"):
        reserve(registry, run_id="run-two", revision="r2", thread="thread-two")


def test_migrated_runtime_revision_reuses_same_thread_in_same_run(tmp_path: Path) -> None:
    registry = ValidationSessionRegistry(tmp_path / "validation-sessions.jsonl")
    reserve(registry)
    registry.close("session-one", status="CLOSED_MIGRATED")

    successor = reserve(registry, revision="r2", session="session-two")

    assert successor.run_id == "run-one"
    assert successor.researcher_thread_id == "thread-one"


def test_exact_reservation_is_idempotent(tmp_path: Path) -> None:
    registry = ValidationSessionRegistry(tmp_path / "validation-sessions.jsonl")
    first = reserve(registry)
    second = reserve(registry)

    assert first == second
    assert len(registry.records()) == 1


def test_exact_closed_reservation_is_not_reopened(tmp_path: Path) -> None:
    registry = ValidationSessionRegistry(tmp_path / "validation-sessions.jsonl")
    reserve(registry)
    registry.close("session-one", status="CLOSED_VALIDATION_PASSED")

    with pytest.raises(ContractError, match="cannot be reserved"):
        reserve(registry)


def test_registry_rejects_invalid_identity_close_and_corrupt_journal(tmp_path: Path) -> None:
    path = tmp_path / "validation-sessions.jsonl"
    registry = ValidationSessionRegistry(path)
    with pytest.raises(ContractError, match="incomplete"):
        reserve(registry, run_id="")
    with pytest.raises(ContractError, match="digest"):
        registry.reserve(
            run_id="run",
            question_revision="r1",
            validation_session_id="session",
            researcher_thread_id="thread",
            package_sha256="bad",
        )
    with pytest.raises(ContractError, match="not registered"):
        registry.close("missing", status="CLOSED_MIGRATED")
    reserve(registry)
    with pytest.raises(ContractError, match="CLOSED"):
        registry.close("session-one", status="ACTIVE")
    registry.close("session-one", status="CLOSED_TOO_EASY")
    with pytest.raises(ContractError, match="closed differently"):
        registry.close("session-one", status="CLOSED_MIGRATED")
    path.write_text('{"event":"UNKNOWN","record":{}}\n', encoding="utf-8")
    with pytest.raises(ContractError, match="registry is invalid"):
        registry.records()

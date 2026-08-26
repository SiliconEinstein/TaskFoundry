"""不使用普通 LLM API 的 Codex 任务派发模块。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

from .model import Actor, ContractError


_QUEUED = re.compile(r"Queued message ([0-9a-f-]+) for thread ([0-9a-f-]+)\.")


@dataclass(frozen=True)
class DispatchReceipt:
    """由 `codex queue` 返回的机器可验证回执。"""

    message_id: str
    thread_id: str
    role: Actor


class CodexDispatcher:
    """向已有且角色明确的 Codex 任务排队派发有限工作。"""

    def __init__(self, codex_binary: str = "codex") -> None:
        self.codex_binary = codex_binary

    def queue(self, *, role: Actor, thread_id: str, prompt_path: Path) -> DispatchReceipt:
        """派发文件提示，并要求权威排队回执。"""
        if role not in {Actor.TEACHER, Actor.RESEARCHER, Actor.REVIEWER}:
            raise ContractError("Codex dispatch supports Teacher, Researcher, or Reviewer")
        if not thread_id or not prompt_path.is_file():
            raise ContractError("thread ID and prompt file are required")
        prompt = prompt_path.read_text(encoding="utf-8")
        boundary = f"[TASKFOUNDRY ROLE={role.value}]"
        if boundary not in prompt:
            raise ContractError(f"prompt must declare {boundary}")
        completed = subprocess.run(
            [self.codex_binary, "queue", "--thread", thread_id, "--message", prompt],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Codex dispatch failed: {completed.stderr.strip()}")
        match = _QUEUED.search(completed.stdout)
        if not match or match.group(2) != thread_id:
            raise RuntimeError("Codex dispatch did not return a valid queue receipt")
        return DispatchReceipt(match.group(1), match.group(2), role)

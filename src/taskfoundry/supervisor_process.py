"""Process adapter used by CampaignSupervisor and its deterministic tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Protocol


class ProcessAdapter(Protocol):
    """Internal seam for launching and observing one Harbor worker."""

    def launch(
        self,
        command: list[str],
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> int:
        """Start one immutable runtime worker and return its host process id."""
        ...

    def alive(self, process_id: int) -> bool:
        """Return whether the recorded worker still owns a live process."""
        ...


class LocalProcessAdapter:
    """Start a detached local controller whose child Harbor job is canonical."""

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    def launch(
        self,
        command: list[str],
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> int:
        """Launch without a shell and preserve stdout/stderr outside evidence JSON."""
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[1])
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = package_root + (
            os.pathsep + existing if existing else ""
        )
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(  # noqa: S603 - host-generated argv
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                start_new_session=True,
            )
        self._children[process.pid] = process
        return process.pid

    def alive(self, process_id: int) -> bool:
        """Observe only a process handle launched by this adapter instance."""
        process = self._children.get(process_id)
        if process is None:
            return False
        if process.poll() is None:
            return True
        self._children.pop(process_id, None)
        return False

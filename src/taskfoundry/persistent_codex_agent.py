"""Portable Codex adapter that retains one conversation inside one Harbor trial."""

from __future__ import annotations

from pathlib import Path
from typing import Any, override

from harbor.trial.validation_control import PersistentValidationConfig
from .portable_codex_agent import PortableCodex


class PersistentPortableCodex(PortableCodex):
    """Run once with ``codex exec`` and continue with ``resume --last``."""

    CLI_FLAGS = PortableCodex.CLI_FLAGS

    def __init__(
        self,
        logs_dir: Path,
        persistent_validation: dict[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        PersistentValidationConfig.from_dict(persistent_validation)
        self._persistent_session_started = False
        super().__init__(logs_dir, *args, **kwargs)

    @override
    def _codex_exec_subcommand(self) -> str:
        if self._persistent_session_started:
            return "exec resume --last"
        self._persistent_session_started = True
        return "exec"

    @override
    def _cleanup_codex_home_after_run(self) -> bool:
        return False

    @override
    def _reset_codex_config_before_run(self) -> bool:
        return True

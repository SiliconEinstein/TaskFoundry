"""Strict JSON and timestamp parsing shared by Supervisor internals."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from .model import ContractError


def read_object(path: Path) -> dict:
    """Read one finite, duplicate-key-free JSON object."""
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ContractError(f"Supervisor JSON is invalid: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"Supervisor JSON root must be an object: {path}")
    return value


def parse_time(value: str) -> datetime:
    """Parse one timezone-aware Supervisor timestamp."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ContractError("Supervisor timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ContractError("Supervisor timestamp must include timezone")
    return parsed


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value

"""Teacher-owned writer for persistent Harbor validation decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from .model import ContractError


_ACTIONS = {
    "CONTINUE_BLIND",
    "CONTINUE_HINT",
    "STOP_TOO_EASY",
    "STOP_PASSED",
    "STOP_BLOCKED",
}


@dataclass(frozen=True)
class TeacherDecision:
    """Teacher's semantic choice after reading one canonical round result."""

    action: str
    hint: str | None = None
    teacher_declares_non_answer: bool | None = None

    def validate(self) -> None:
        """Reject answer-like hints unless Teacher owns the declaration."""
        if self.action not in _ACTIONS:
            raise ContractError("unknown Teacher validation action")
        if self.action == "CONTINUE_HINT":
            if not self.hint or self.hint != self.hint.strip():
                raise ContractError("Teacher hint content is required")
            if self.teacher_declares_non_answer is not True:
                raise ContractError("Teacher must declare the hint as non-answer")
        elif self.hint is not None or self.teacher_declares_non_answer is not None:
            raise ContractError("only CONTINUE_HINT may contain hint fields")


class TeacherValidationController:
    """Bind a Teacher decision to immutable Harbor-published evidence."""

    def __init__(self, controller_dir: Path, validation_session_id: str) -> None:
        if not controller_dir.is_absolute() or not validation_session_id.strip():
            raise ContractError(
                "absolute controller dir and session identity are required"
            )
        self.root = controller_dir
        self.validation_session_id = validation_session_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def decide(self, result_path: Path, decision: TeacherDecision) -> Path:
        """Read one strict result and atomically publish the bound decision."""
        decision.validate()
        result = self._read_result(result_path)
        if result.get("validation_session_id") != self.validation_session_id:
            raise ContractError("round result targets another validation session")
        round_index = result.get("round_index")
        if type(round_index) is not int or round_index < 1:
            raise ContractError("round result index is invalid")
        self._validate_progression(result, decision)
        expected_name = f"round-{round_index:02d}-result.json"
        if (
            result_path.name != expected_name
            or result_path.parent.resolve() != self.root.resolve()
        ):
            raise ContractError("round result path is not canonical")
        result_sha256 = hashlib.sha256(result_path.read_bytes()).hexdigest()
        value = {
            "schema_version": 1,
            "validation_session_id": self.validation_session_id,
            "round_index": round_index,
            "result_sha256": result_sha256,
            **asdict(decision),
        }
        path = self.root / f"round-{round_index:02d}-decision.json"
        _publish_immutable(path, (_canonical_json(value) + "\n").encode())
        return path

    @staticmethod
    def _read_result(path: Path) -> dict[str, Any]:
        try:
            if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
                raise ContractError("round result must be a regular file")
            value = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {token}")
                ),
            )
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError("unsupported result object")
            return value
        except ContractError:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            raise ContractError(f"round result is invalid: {error}") from error

    @staticmethod
    def _validate_progression(
        result: dict[str, Any], decision: TeacherDecision
    ) -> None:
        """Fail before publication when Teacher chooses an illegal next round."""
        index = result.get("round_index")
        phase = result.get("phase")
        classification = result.get("classification")
        score = result.get("reward")
        if (
            type(index) is not int
            or phase not in {"BLIND", "HINT"}
            or classification != "SCIENTIFIC_RESULT"
            or type(score) not in (int, float)
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
        ):
            raise ContractError(
                "Teacher can decide only from a finite scientific result"
            )
        passed = float(score) >= 0.85
        if phase == "BLIND":
            if index > 3:
                raise ContractError("blind round index exceeds three")
            allowed = (
                {"STOP_TOO_EASY"}
                if passed
                else {"CONTINUE_BLIND"}
                if index < 3
                else {"CONTINUE_HINT", "STOP_BLOCKED"}
            )
        else:
            if index not in {4, 5}:
                raise ContractError("hint round index must be four or five")
            allowed = (
                {"STOP_PASSED"}
                if passed
                else {"CONTINUE_HINT", "STOP_BLOCKED"}
                if index == 4
                else {"STOP_BLOCKED"}
            )
        if decision.action not in allowed:
            raise ContractError("Teacher decision violates validation progression")


def _publish_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
            return
        raise ContractError("Teacher decision conflicts with existing bytes")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
        _fsync_directory(path.parent)
    except FileExistsError as error:
        raise ContractError("Teacher decision was concurrently published") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate key: {key}")
        value[key] = item
    return value


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

"""健康门证据绑定与题目修订失效规则。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
from pathlib import Path
from typing import Any, Iterable

from .model import ContractError
from .validation import HealthEvidence


class HealthGate(StrEnum):
    """正式盲测前必须通过的健康门。"""

    PACKAGE = "package"
    ENVIRONMENT = "environment"
    ORACLE = "oracle"
    HONEST = "honest"
    ADVERSARIAL = "adversarial"
    LEAKAGE = "leakage"
    BLIND_VALIDATION = "blind_validation"
    HINT_VALIDATION = "hint_validation"


class RevisionChange(StrEnum):
    """会影响既有证据有效性的题目修改类型。"""

    INSTRUCTION = "instruction"
    VERIFIER = "verifier"
    SOLUTION = "solution"
    PUBLIC_DATA = "public_data"
    ENVIRONMENT = "environment"
    HINT = "hint"


_INVALIDATION: dict[RevisionChange, frozenset[HealthGate]] = {
    RevisionChange.INSTRUCTION: frozenset(
        {HealthGate.PACKAGE, HealthGate.ORACLE, HealthGate.HONEST, HealthGate.ADVERSARIAL,
         HealthGate.LEAKAGE, HealthGate.BLIND_VALIDATION, HealthGate.HINT_VALIDATION}
    ),
    RevisionChange.VERIFIER: frozenset(
        {HealthGate.PACKAGE, HealthGate.ORACLE, HealthGate.HONEST, HealthGate.ADVERSARIAL,
         HealthGate.BLIND_VALIDATION, HealthGate.HINT_VALIDATION}
    ),
    RevisionChange.SOLUTION: frozenset({HealthGate.ORACLE, HealthGate.HONEST}),
    RevisionChange.PUBLIC_DATA: frozenset(
        {HealthGate.PACKAGE, HealthGate.ENVIRONMENT, HealthGate.ORACLE, HealthGate.HONEST,
         HealthGate.ADVERSARIAL, HealthGate.LEAKAGE, HealthGate.BLIND_VALIDATION,
         HealthGate.HINT_VALIDATION}
    ),
    RevisionChange.ENVIRONMENT: frozenset(
        {HealthGate.ENVIRONMENT, HealthGate.ORACLE, HealthGate.HONEST,
         HealthGate.BLIND_VALIDATION, HealthGate.HINT_VALIDATION}
    ),
    RevisionChange.HINT: frozenset({HealthGate.HINT_VALIDATION, HealthGate.LEAKAGE}),
}


@dataclass(frozen=True)
class EvidenceReference:
    """一份不可静默替换的健康门证据。"""

    gate: HealthGate
    path: str
    sha256: str

    @classmethod
    def capture(cls, gate: HealthGate, path: Path) -> "EvidenceReference":
        """读取并绑定一份现有证据文件。"""
        if not path.is_file():
            raise ContractError(f"health evidence is missing: {path}")
        return cls(gate, str(path.resolve()), _sha256(path))

    def validate(self) -> None:
        """确认引用仍指向同一份证据字节。"""
        path = Path(self.path)
        if not path.is_file() or _sha256(path) != self.sha256:
            raise ContractError(f"health evidence changed: {self.path}")


@dataclass(frozen=True)
class BoundHealthEvidence:
    """绑定题包、环境和修订的完整健康门回执。"""

    question_revision: str
    package_sha256: str
    environment_key: str
    health: HealthEvidence
    references: tuple[EvidenceReference, ...]
    created_at: str
    schema_version: int = 1

    @classmethod
    def create(
        cls,
        *,
        question_revision: str,
        package_sha256: str,
        environment_key: str,
        health: HealthEvidence,
        evidence: Iterable[tuple[HealthGate, Path]],
    ) -> "BoundHealthEvidence":
        """从当前证据文件生成不可变绑定回执。"""
        bundle = cls(
            question_revision=question_revision,
            package_sha256=package_sha256,
            environment_key=environment_key,
            health=health,
            references=tuple(EvidenceReference.capture(gate, path) for gate, path in evidence),
            created_at=datetime.now(UTC).isoformat(),
        )
        bundle.validate()
        return bundle

    def validate(self) -> None:
        """检查全部门、标识和证据字节。"""
        if self.schema_version != 1 or not self.question_revision.strip():
            raise ContractError("invalid bound health evidence")
        if not _full_sha256(self.package_sha256) or not _full_sha256(self.environment_key):
            raise ContractError("health evidence requires package and environment SHA-256")
        if not self.health.passed:
            raise ContractError("all health gates must pass")
        gates = {reference.gate for reference in self.references}
        required = {
            HealthGate.PACKAGE,
            HealthGate.ENVIRONMENT,
            HealthGate.ORACLE,
            HealthGate.HONEST,
            HealthGate.ADVERSARIAL,
            HealthGate.LEAKAGE,
        }
        if not required <= gates:
            raise ContractError("bound health evidence is missing required gates")
        for reference in self.references:
            reference.validate()

    def to_dict(self) -> dict[str, Any]:
        """返回可写入事件日志的表示。"""
        self.validate()
        value = asdict(self)
        value["references"] = [
            {**asdict(reference), "gate": reference.gate.value} for reference in self.references
        ]
        return value

    def assert_current(
        self,
        package_sha256: str,
        environment_key: str,
        question_revision: str,
    ) -> None:
        """拒绝把旧回执用于新的题包、环境或题目修订。"""
        self.validate()
        if self.package_sha256 != package_sha256:
            raise ContractError("health evidence targets another package or environment")
        if self.environment_key != environment_key:
            raise ContractError("health evidence targets another package or environment")
        if self.question_revision != question_revision:
            raise ContractError("health evidence targets another revision")


def invalidated_gates(changes: Iterable[RevisionChange]) -> frozenset[HealthGate]:
    """计算一组修订会使哪些既有门失效。"""
    invalidated: set[HealthGate] = set()
    for change in changes:
        invalidated.update(_INVALIDATION[change])
    return frozenset(invalidated)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

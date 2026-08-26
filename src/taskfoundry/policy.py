"""不可变规范锁和证据驱动的规则修订。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .model import ContractError


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def file_sha256(path: Path) -> str:
    """返回一个普通文件的小写 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class PolicyReference:
    """一份规范文档的不可变身份。"""

    policy_id: str
    version: str
    path: str
    sha256: str


@dataclass(frozen=True)
class PolicyLock:
    """一次运行实际使用的固定规范和题型规范。"""

    artifact_contract: PolicyReference
    question_type: PolicyReference
    amendments: tuple[PolicyReference, ...] = ()
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的规范锁表示。"""
        return asdict(self)


@dataclass(frozen=True)
class RuleAmendment:
    """从运行证据中学习的一条待审核或已接受规则。"""

    amendment_id: str
    question_type: str
    parent_policy_sha256: str
    problem: str
    rule_change: str
    evidence_refs: tuple[str, ...]
    status: str
    created_at: str
    regression_refs: tuple[str, ...] = ()
    schema_version: int = 1

    def validate(self) -> None:
        """拒绝不可追溯、可变或格式错误的修订。"""
        if not _IDENTIFIER.fullmatch(self.amendment_id):
            raise ContractError("invalid amendment_id")
        if self.status not in {"proposed", "accepted", "rejected"}:
            raise ContractError("invalid amendment status")
        if len(self.parent_policy_sha256) != 64:
            raise ContractError("parent policy SHA-256 is required")
        if not self.problem.strip() or not self.rule_change.strip() or not self.evidence_refs:
            raise ContractError("amendment requires problem, change, and evidence")


@dataclass(frozen=True)
class RuleObservation:
    """从一次真实出题问题中提取的规则学习输入。"""

    observation_id: str
    run_id: str
    question_type: str
    problem: str
    proposed_rule: str
    evidence_refs: tuple[str, ...]
    regression_refs: tuple[str, ...]

    def validate(self) -> None:
        """要求问题、规则、证据和回归样例同时存在。"""
        if not _IDENTIFIER.fullmatch(self.observation_id):
            raise ContractError("invalid observation_id")
        if not all((self.run_id.strip(), self.question_type.strip(), self.problem.strip(), self.proposed_rule.strip())):
            raise ContractError("rule observation identity and content are required")
        if not self.evidence_refs or not self.regression_refs:
            raise ContractError("rule observation requires evidence and regression cases")


class PolicyRepository:
    """解析规范文件并保存不可变修订记录。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.amendments_dir = self.root / "amendments"

    def lock(self, question_type: str, version: str = "v1") -> PolicyLock:
        """解析固定题包契约和题型规则包。"""
        artifact = self.root / "artifact-contract-v1.md"
        typed = self.root / "question-types" / question_type / f"{version}.md"
        references = (
            self._reference("artifact-contract", "v1", artifact),
            self._reference(question_type, version, typed),
        )
        accepted: list[PolicyReference] = []
        if self.amendments_dir.exists():
            for path in sorted(self.amendments_dir.glob("*.json")):
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("question_type") == question_type and value.get("status") == "accepted":
                    if value.get("parent_policy_sha256") != references[1].sha256:
                        raise ContractError("accepted amendment targets a different policy version")
                    accepted.append(self._reference(value["amendment_id"], "1", path))
        return PolicyLock(references[0], references[1], tuple(accepted))

    def write_lock(self, lock: PolicyLock, path: Path) -> None:
        """原子持久化规范锁。"""
        self._atomic_json(path, lock.to_dict())

    def propose(
        self,
        *,
        amendment_id: str,
        question_type: str,
        parent_policy_sha256: str,
        problem: str,
        rule_change: str,
        evidence_refs: tuple[str, ...],
        regression_refs: tuple[str, ...] = (),
    ) -> Path:
        """创建绑定具体证据的不可变待审核修订。"""
        amendment = RuleAmendment(
            amendment_id=amendment_id,
            question_type=question_type,
            parent_policy_sha256=parent_policy_sha256,
            problem=problem,
            rule_change=rule_change,
            evidence_refs=evidence_refs,
            status="proposed",
            created_at=datetime.now(UTC).isoformat(),
            regression_refs=regression_refs,
        )
        amendment.validate()
        path = self.amendments_dir / f"{amendment_id}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            comparable = json.loads(json.dumps(asdict(amendment)))
            comparable["created_at"] = existing.get("created_at")
            if existing != comparable:
                raise ContractError("amendment_id already identifies different content")
            return path
        self._atomic_json(path, asdict(amendment))
        return path

    def learn_from_observation(
        self,
        observation: RuleObservation,
        *,
        parent_policy_sha256: str,
    ) -> Path:
        """把失败观察转换为带回归样例的待审核规则提案。"""
        observation.validate()
        return self.propose(
            amendment_id=observation.observation_id,
            question_type=observation.question_type,
            parent_policy_sha256=parent_policy_sha256,
            problem=f"run={observation.run_id}: {observation.problem}",
            rule_change=observation.proposed_rule,
            evidence_refs=observation.evidence_refs,
            regression_refs=observation.regression_refs,
        )

    def decide(self, amendment_id: str, status: str) -> Path:
        """记录审核结果，不覆盖原始提案。"""
        if status not in {"accepted", "rejected"}:
            raise ContractError("decision must be accepted or rejected")
        source = self.amendments_dir / f"{amendment_id}.json"
        if not source.is_file():
            raise ContractError("unknown amendment")
        value = json.loads(source.read_text(encoding="utf-8"))
        decision = self.amendments_dir / f"{amendment_id}.{status}.json"
        opposite = self.amendments_dir / f"{amendment_id}.{'rejected' if status == 'accepted' else 'accepted'}.json"
        if opposite.exists():
            raise ContractError("amendment already has the opposite decision")
        value["amendment_id"] = f"{amendment_id}.{status}"
        value["status"] = status
        if decision.exists():
            if json.loads(decision.read_text(encoding="utf-8")) != value:
                raise ContractError("decision record conflicts with existing content")
        else:
            self._atomic_json(decision, value)
        return decision

    def _reference(self, policy_id: str, version: str, path: Path) -> PolicyReference:
        if not path.is_file() or self.root not in path.resolve().parents:
            raise ContractError(f"policy file is unavailable: {path}")
        return PolicyReference(policy_id, version, str(path), file_sha256(path))

    def _atomic_json(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

"""TaskFoundry 各适配器共享的版本化领域契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ContractError(ValueError):
    """公开契约不合法时抛出。"""


class Actor(StrEnum):
    """具有独立写入边界的角色。"""

    TEACHER = "teacher"
    LABWRIGHT = "labwright"
    RESEARCHER = "researcher"
    REVIEWER = "reviewer"
    SYSTEM = "system"


class RunState(StrEnum):
    """单题出题生命周期的顶层状态。"""

    DESIGNING = "DESIGNING"
    POLICIES_LOCKED = "POLICIES_LOCKED"
    ENVIRONMENT_DISCOVERY = "ENVIRONMENT_DISCOVERY"
    ENVIRONMENT_READY = "ENVIRONMENT_READY"
    AUTHORING = "AUTHORING"
    PACKAGE_FROZEN = "PACKAGE_FROZEN"
    PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
    ORACLE_PASSED = "ORACLE_PASSED"
    BLIND_VALIDATION = "BLIND_VALIDATION"
    HINT_VALIDATION = "HINT_VALIDATION"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    RUNTIME_FINALIZATION = "RUNTIME_FINALIZATION"
    TOO_EASY = "TOO_EASY"
    SCIENTIFIC_REDESIGN_REQUIRED = "SCIENTIFIC_REDESIGN_REQUIRED"
    ABANDONED_TOPIC = "ABANDONED_TOPIC"
    DEFERRED_TIMEOUT = "DEFERRED_TIMEOUT"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class SourceQuestion:
    """一道来源题及其在新设计中的实质作用。"""

    source_id: str
    paper_id: str
    research_goal: str
    method: str
    transferred_role: str
    excluded_details: tuple[str, ...] = ()

    def validate(self) -> None:
        """拒绝空映射或仅做表面拼接的来源题。"""
        for name in ("source_id", "paper_id", "research_goal", "method", "transferred_role"):
            if not getattr(self, name).strip():
                raise ContractError(f"source question {name} must be non-empty")


@dataclass(frozen=True)
class BackgroundEvidence:
    """题面背景中的一条证据及其对方法决策的作用。"""

    evidence_id: str
    statement: str
    role: str
    applicable_conditions: str
    decision_effect: str
    linked_methods: tuple[str, ...] = ()
    public_clues: tuple[str, ...] = ()

    def validate(self) -> None:
        """拒绝无法审计的背景条目和伪装成难度的空噪声。"""
        for name in (
            "evidence_id",
            "statement",
            "role",
            "applicable_conditions",
            "decision_effect",
        ):
            if not getattr(self, name).strip():
                raise ContractError(f"background evidence {name} must be non-empty")
        if self.role not in {
            "load_bearing",
            "context_only",
            "conditional_distractor",
        }:
            raise ContractError("unsupported background evidence role")
        if any(not value.strip() for value in (*self.linked_methods, *self.public_clues)):
            raise ContractError("background evidence lists must contain non-empty values")
        if self.role == "conditional_distractor" and (
            not self.linked_methods or not self.public_clues
        ):
            raise ContractError(
                "conditional distractor requires linked methods and public clues"
            )


@dataclass(frozen=True)
class QuestionDesignBrief:
    """与具体题包和执行框架解耦的题目设计大纲。"""

    brief_id: str
    question_type: str
    title: str
    scientific_goal: str
    research_object: str
    source_questions: tuple[SourceQuestion, ...]
    method_space: tuple[str, ...]
    evidence_roles: dict[str, str]
    public_inputs: tuple[str, ...]
    required_outputs: tuple[str, ...]
    hidden_evaluation_axes: tuple[str, ...]
    environment_capabilities: tuple[str, ...]
    difficulty_hypothesis: str
    solvability_argument: str
    background_evidence: tuple[BackgroundEvidence, ...] = ()
    difficulty_progression: dict[str, str] = field(default_factory=dict)
    ground_truth_plan: dict[str, str] = field(default_factory=dict)
    non_goals: tuple[str, ...] = ()
    target_solution_time_sec: int = 1800
    schema_version: int = 1

    def validate(self) -> None:
        """检查后续题型模块所需的最小证据和耗时目标。"""
        if self.schema_version != 1:
            raise ContractError("unsupported QuestionDesignBrief schema_version")
        for name in (
            "brief_id",
            "question_type",
            "title",
            "scientific_goal",
            "research_object",
            "difficulty_hypothesis",
            "solvability_argument",
        ):
            if not getattr(self, name).strip():
                raise ContractError(f"{name} must be non-empty")
        if not self.source_questions:
            raise ContractError("source_questions must be non-empty")
        if not self.method_space:
            raise ContractError("method_space must be non-empty")
        for item in self.source_questions:
            item.validate()
        for item in self.background_evidence:
            item.validate()
        evidence_ids = [item.evidence_id for item in self.background_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ContractError("background evidence identifiers must be unique")
        for name in (
            "public_inputs",
            "required_outputs",
            "hidden_evaluation_axes",
            "environment_capabilities",
        ):
            if not getattr(self, name):
                raise ContractError(f"{name} must be non-empty")
        if self.difficulty_progression:
            expected_levels = {"high", "medium", "low"}
            if set(self.difficulty_progression) != expected_levels or any(
                not value.strip() for value in self.difficulty_progression.values()
            ):
                raise ContractError(
                    "difficulty_progression must define non-empty high, medium, and low levels"
                )
        if self.ground_truth_plan:
            expected_fields = {"primary", "cross_check", "reference_type", "tolerance_basis"}
            if set(self.ground_truth_plan) != expected_fields or any(
                not value.strip() for value in self.ground_truth_plan.values()
            ):
                raise ContractError(
                    "ground_truth_plan must define primary, cross_check, reference_type, and tolerance_basis"
                )
        if not 1 <= self.target_solution_time_sec <= 1800:
            raise ContractError("target_solution_time_sec must be within 1800 seconds")

    def to_dict(self) -> dict[str, Any]:
        """校验后返回可写入 JSON 的表示。"""
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QuestionDesignBrief":
        """解析并校验 JSON 形式的设计大纲。"""
        converted = dict(value)
        converted["source_questions"] = tuple(SourceQuestion(**item) for item in value["source_questions"])
        converted["background_evidence"] = tuple(
            BackgroundEvidence(
                **(
                    item
                    | {
                        "linked_methods": tuple(item.get("linked_methods", ())),
                        "public_clues": tuple(item.get("public_clues", ())),
                    }
                )
            )
            for item in value.get("background_evidence", ())
        )
        for name in (
            "method_space",
            "public_inputs",
            "required_outputs",
            "hidden_evaluation_axes",
            "environment_capabilities",
            "non_goals",
        ):
            converted[name] = tuple(value.get(name, ()))
        brief = cls(**converted)
        brief.validate()
        return brief


@dataclass(frozen=True)
class RunEvent:
    """运行日志中的一条持久事实。"""

    sequence: int
    timestamp: str
    actor: Actor
    event_type: str
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def validate(self) -> None:
        """校验持久日志的不变量。"""
        if self.schema_version != 1 or self.sequence < 1:
            raise ContractError("invalid event version or sequence")
        if not self.event_type or not self.idempotency_key:
            raise ContractError("event_type and idempotency_key are required")

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的表示。"""
        self.validate()
        value = asdict(self)
        value["actor"] = self.actor.value
        return value


@dataclass(frozen=True)
class RunSnapshot:
    """可从运行日志重建的物化视图。"""

    run_id: str
    state: RunState
    sequence: int
    brief_path: str | None = None
    policy_lock_path: str | None = None
    package_path: str | None = None
    package_digest: str | None = None
    environment_key: str | None = None
    question_revision: str = "r1"
    attempts: tuple[dict[str, Any], ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    consecutive_timeouts: int = 0
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的表示。"""
        value = asdict(self)
        value["state"] = self.state.value
        return value

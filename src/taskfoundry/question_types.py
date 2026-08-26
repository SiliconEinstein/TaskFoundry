"""题型模块注册与设计大纲的题型专属校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .model import ContractError, QuestionDesignBrief


class QuestionTypeModule(Protocol):
    """题型模块对下游暴露的最小接口。"""

    question_type: str

    def validate_brief(self, brief: QuestionDesignBrief) -> None:
        """校验题型特有的不变量。"""


@dataclass(frozen=True)
class MethodSelectionModule:
    """多来源方法选择题模块。"""

    question_type: str = "method-selection"

    def validate_brief(self, brief: QuestionDesignBrief) -> None:
        """要求来源多样、方法空间真实且证据角色可追溯。"""
        brief.validate()
        if not 3 <= len(brief.source_questions) <= 5:
            raise ContractError("method-selection requires three to five source questions")
        if len({item.paper_id for item in brief.source_questions}) < 2:
            raise ContractError("method-selection requires at least two papers")
        if len(set(brief.method_space)) < 2:
            raise ContractError("method-selection requires at least two method choices")
        source_ids = {item.source_id for item in brief.source_questions}
        if not source_ids <= set(brief.evidence_roles):
            raise ContractError("every method-selection source requires an evidence role")


class QuestionTypeRegistry:
    """根据大纲中的稳定题型标识选择对应模块。"""

    def __init__(self, modules: tuple[QuestionTypeModule, ...] | None = None) -> None:
        configured = modules or (MethodSelectionModule(),)
        self._modules = {module.question_type: module for module in configured}
        if len(self._modules) != len(configured):
            raise ContractError("question type identifiers must be unique")

    def validate(self, brief: QuestionDesignBrief) -> None:
        """通过题型模块校验设计大纲。"""
        module = self._modules.get(brief.question_type)
        if module is None:
            raise ContractError(f"unsupported question type: {brief.question_type}")
        module.validate_brief(brief)

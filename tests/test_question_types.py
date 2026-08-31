import pytest

from taskfoundry.model import (
    BackgroundEvidence,
    ContractError,
    QuestionDesignBrief,
    SourceQuestion,
)
from taskfoundry.question_types import QuestionTypeRegistry


def _brief() -> QuestionDesignBrief:
    return QuestionDesignBrief(
        brief_id="b1",
        question_type="method-selection",
        title="方法选择",
        scientific_goal="选择可迁移方法",
        research_object="隐藏情形",
        source_questions=(
            SourceQuestion("q1", "p1", "g1", "m1", "候选"),
            SourceQuestion("q2", "p1", "g2", "m2", "诊断"),
            SourceQuestion("q3", "p2", "g3", "m3", "评价"),
        ),
        method_space=("m1", "m2"),
        evidence_roles={"q1": "候选", "q2": "诊断", "q3": "评价"},
        public_inputs=("input",),
        required_outputs=("output",),
        hidden_evaluation_axes=("迁移",),
        environment_capabilities=("python",),
        difficulty_hypothesis="需要迁移判断",
        solvability_argument="公开证据完整",
    )


def test_method_selection_module_accepts_traceable_sources() -> None:
    QuestionTypeRegistry().validate(_brief())


def _evidence_contract() -> tuple[BackgroundEvidence, ...]:
    return (
        BackgroundEvidence(
            "load",
            "The observed response is non-monotone.",
            "load_bearing",
            "All declared cases.",
            "Requires a method that represents the turning point.",
            ("m2",),
            ("validation.csv contains both sides of the turning point",),
        ),
        BackgroundEvidence(
            "context",
            "Both methods are established in this field.",
            "context_only",
            "General literature context.",
            "Establishes plausibility without selecting a method.",
        ),
        BackgroundEvidence(
            "d1",
            "Method one is efficient for monotone responses.",
            "conditional_distractor",
            "The response is monotone.",
            "Exclude it because the current response is non-monotone.",
            ("m1",),
            ("the public response reverses direction",),
        ),
        BackgroundEvidence(
            "d2",
            "Method two can overfit when observations are sparse.",
            "conditional_distractor",
            "Only a few observations are available.",
            "Do not exclude it because the public design is dense.",
            ("m2",),
            ("the public design declares a dense observation grid",),
        ),
    )


def test_method_selection_background_evidence_contract_accepts_fair_distractors() -> None:
    brief = QuestionDesignBrief(
        **(_brief().__dict__ | {"background_evidence": _evidence_contract()})
    )

    from taskfoundry.question_types import MethodSelectionModule

    MethodSelectionModule().validate_background_evidence(brief)


@pytest.mark.parametrize(
    "evidence,message",
    [
        (_evidence_contract()[:2], "roles"),
        (_evidence_contract()[:3], "two conditional distractors"),
        (
            _evidence_contract()[:-1]
            + (
                BackgroundEvidence(
                    "d2",
                    "An alternate method is tempting.",
                    "conditional_distractor",
                    "An alternate regime.",
                    "Exclude it using public evidence.",
                    ("unknown",),
                    ("the public regime differs",),
                ),
            ),
            "unknown method",
        ),
    ],
)
def test_method_selection_background_evidence_contract_rejects_incomplete_maps(
    evidence, message
) -> None:
    from taskfoundry.question_types import MethodSelectionModule

    brief = QuestionDesignBrief(**(_brief().__dict__ | {"background_evidence": evidence}))
    with pytest.raises(ContractError, match=message):
        MethodSelectionModule().validate_background_evidence(brief)


def test_method_selection_module_rejects_single_paper() -> None:
    brief = _brief()
    sources = tuple(
        SourceQuestion(item.source_id, "p1", item.research_goal, item.method, item.transferred_role)
        for item in brief.source_questions
    )
    with pytest.raises(ContractError, match="two papers"):
        QuestionTypeRegistry().validate(QuestionDesignBrief(**(brief.__dict__ | {"source_questions": sources})))


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"source_questions": _brief().source_questions[:2]}, "three to five"),
        ({"method_space": ("m1", "m1")}, "two method choices"),
        ({"evidence_roles": {"q1": "候选"}}, "evidence role"),
        ({"question_type": "unknown"}, "unsupported question type"),
    ],
)
def test_question_type_registry_rejects_invalid_design(changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        QuestionTypeRegistry().validate(QuestionDesignBrief(**(_brief().__dict__ | changes)))

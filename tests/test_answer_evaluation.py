from dataclasses import dataclass
from uuid import uuid4

import pytest

from erp_assistant.evaluation.answer_quality import (
    AnswerQualityEvaluator,
    AnswerQualityThresholds,
)
from erp_assistant.evaluation.models import (
    DatasetProvenance,
    EvaluationCase,
    EvaluationCategory,
    EvaluationDataset,
    GateDecision,
    RelevantSource,
)
from erp_assistant.rag.knowledge_assistant import (
    AnswerCitation,
    AnswerDecision,
    AnswerOutcome,
    AnswerRequest,
    AnswerUnit,
)
from erp_assistant.rag.types import DataClassification, SearchDecision
from tests.identity_factory import trusted_actor as ActorContext


@dataclass
class ScriptedAssistant:
    outcomes: dict[str, AnswerOutcome]

    async def ask(self, request: AnswerRequest) -> AnswerOutcome:
        return self.outcomes[request.question]


def case(
    case_id: str,
    question: str,
    decision: SearchDecision,
    *,
    category: EvaluationCategory,
) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        dataset_version="answer-v1",
        question=question,
        actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        expected_sources=(
            (RelevantSource(document_key="policy", page_start=1, page_end=1),)
            if decision is SearchDecision.MATCHED
            else ()
        ),
        expected_decision=decision,
        categories=frozenset({category}),
        forbidden_document_keys=(
            frozenset({"foreign-policy"})
            if category is EvaluationCategory.TENANT_ISOLATION
            else frozenset()
        ),
        provenance=DatasetProvenance.BUSINESS,
        reviewed=True,
        data_classification=DataClassification.ORDINARY,
    )


def answered(*, supported: bool, document_key: str = "policy") -> AnswerOutcome:
    evidence_ids = ("E1",) if supported else ()
    citations = (
        (
            AnswerCitation(
                evidence_id="E1",
                document_id=uuid4(),
                document_key=document_key,
                document_version=1,
                title="Policy",
                page_start=1,
                page_end=1,
                section_title=None,
                heading_path=(),
                anchor=None,
                source_locator={"pageStart": 1, "pageEnd": 1},
                excerpt="Supported fact.",
            ),
        )
        if supported
        else ()
    )
    return AnswerOutcome(
        trace_id=uuid4(),
        decision=AnswerDecision.ANSWERED,
        units=(AnswerUnit(text="Fact.", evidence_ids=evidence_ids),),
        citations=citations,
    )


def refused() -> AnswerOutcome:
    return AnswerOutcome(
        trace_id=uuid4(),
        decision=AnswerDecision.REFUSED,
        units=(),
        citations=(),
        reason="INSUFFICIENT_KNOWLEDGE",
    )


def production_dataset() -> EvaluationDataset:
    answer_categories = tuple(
        category for category in EvaluationCategory if category is not EvaluationCategory.NO_ANSWER
    )
    cases = tuple(
        case(
            f"answered-{index}",
            f"supported answer {index}",
            SearchDecision.MATCHED,
            category=answer_categories[index % len(answer_categories)],
        )
        for index in range(50)
    ) + tuple(
        case(
            f"refused-{index}",
            f"unknown answer {index}",
            SearchDecision.INSUFFICIENT_KNOWLEDGE,
            category=EvaluationCategory.NO_ANSWER,
        )
        for index in range(50)
    )
    return EvaluationDataset(version="answer-v1", content_hash="a" * 64, cases=cases)


@pytest.mark.asyncio
async def test_answer_quality_gate_requires_citation_support_and_refusal_accuracy() -> None:
    dataset = production_dataset()
    passing = await AnswerQualityEvaluator(
        ScriptedAssistant(
            {
                **{f"supported answer {index}": answered(supported=True) for index in range(50)},
                **{f"unknown answer {index}": refused() for index in range(50)},
            }
        )
    ).run(dataset)
    failing = await AnswerQualityEvaluator(
        ScriptedAssistant(
            {
                **{f"supported answer {index}": answered(supported=False) for index in range(50)},
                **{f"unknown answer {index}": answered(supported=True) for index in range(50)},
            }
        ),
        thresholds=AnswerQualityThresholds(
            citation_support_rate=0.95,
            answer_refusal_accuracy=0.90,
        ),
    ).run(dataset)

    assert passing.metrics.citation_support_rate == 1.0
    assert passing.metrics.answer_refusal_accuracy == 1.0
    assert passing.gate.decision is GateDecision.PASS
    assert failing.metrics.citation_support_rate == 0.0
    assert failing.metrics.answer_refusal_accuracy == 0.0
    assert failing.gate.decision is GateDecision.FAIL
    assert failing.gate.reasons == (
        "CITATION_SUPPORT_RATE_BELOW_THRESHOLD",
        "ANSWER_REFUSAL_ACCURACY_BELOW_THRESHOLD",
    )


@pytest.mark.asyncio
async def test_answer_quality_rejects_code_bound_but_wrong_source() -> None:
    evaluation_case = case(
        "wrong-source",
        "wrong source answer",
        SearchDecision.MATCHED,
        category=EvaluationCategory.CHINESE_NATURAL_LANGUAGE,
    )
    dataset = EvaluationDataset(
        version="answer-v1",
        content_hash="b" * 64,
        cases=(evaluation_case,),
    )

    report = await AnswerQualityEvaluator(
        ScriptedAssistant(
            {"wrong source answer": answered(supported=True, document_key="other-policy")}
        )
    ).run(dataset)

    assert report.metrics.citation_support_rate == 0.0
    assert report.gate.decision is GateDecision.FAIL
    assert "CITATION_SUPPORT_RATE_BELOW_THRESHOLD" in report.gate.reasons


def test_answer_quality_thresholds_cannot_be_lowered() -> None:
    with pytest.raises(ValueError, match="95%"):
        AnswerQualityThresholds(citation_support_rate=0.94)
    with pytest.raises(ValueError, match="thresholds"):
        AnswerQualityThresholds(answer_refusal_accuracy=0.89)

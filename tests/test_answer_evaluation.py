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
)
from erp_assistant.rag.knowledge_assistant import (
    AnswerCitation,
    AnswerDecision,
    AnswerOutcome,
    AnswerRequest,
    AnswerUnit,
)
from erp_assistant.rag.retriever import ActorContext
from erp_assistant.rag.types import DataClassification, SearchDecision


@dataclass
class ScriptedAssistant:
    outcomes: dict[str, AnswerOutcome]

    async def ask(self, request: AnswerRequest) -> AnswerOutcome:
        return self.outcomes[request.question]


def case(case_id: str, question: str, decision: SearchDecision) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        dataset_version="answer-v1",
        question=question,
        actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        expected_sources=(),
        expected_decision=decision,
        categories=frozenset({EvaluationCategory.NO_ANSWER}),
        forbidden_document_keys=frozenset(),
        provenance=DatasetProvenance.BUSINESS,
        reviewed=True,
        data_classification=DataClassification.ORDINARY,
    )


def answered(*, supported: bool) -> AnswerOutcome:
    evidence_ids = ("E1",) if supported else ()
    citations = (
        (
            AnswerCitation(
                evidence_id="E1",
                document_id=uuid4(),
                document_key="policy",
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


@pytest.mark.asyncio
async def test_answer_quality_gate_requires_citation_support_and_refusal_accuracy() -> None:
    dataset = EvaluationDataset(
        version="answer-v1",
        content_hash="a" * 64,
        cases=(
            case("answered", "supported answer", SearchDecision.MATCHED),
            case("refused", "unknown answer", SearchDecision.INSUFFICIENT_KNOWLEDGE),
        ),
    )
    passing = await AnswerQualityEvaluator(
        ScriptedAssistant(
            {
                "supported answer": answered(supported=True),
                "unknown answer": refused(),
            }
        )
    ).run(dataset)
    failing = await AnswerQualityEvaluator(
        ScriptedAssistant(
            {
                "supported answer": answered(supported=False),
                "unknown answer": answered(supported=True),
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

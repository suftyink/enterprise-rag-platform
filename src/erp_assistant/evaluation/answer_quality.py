from dataclasses import dataclass
from typing import Protocol

from erp_assistant.evaluation.models import (
    EvaluationDataset,
    GateDecision,
)
from erp_assistant.rag.knowledge_assistant import (
    AnswerDecision,
    AnswerOutcome,
    AnswerRequest,
)
from erp_assistant.rag.types import SearchDecision


@dataclass(frozen=True)
class AnswerQualityThresholds:
    citation_support_rate: float = 0.95
    answer_refusal_accuracy: float = 0.90

    def __post_init__(self) -> None:
        if not (0 <= self.citation_support_rate <= 1 and 0 <= self.answer_refusal_accuracy <= 1):
            raise ValueError("Answer quality thresholds are invalid")


@dataclass(frozen=True)
class AnswerQualityMetrics:
    case_count: int
    citation_support_rate: float | None
    answer_refusal_accuracy: float | None


@dataclass(frozen=True)
class AnswerQualityGate:
    decision: GateDecision
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AnswerQualityReport:
    dataset_version: str
    dataset_hash: str
    metrics: AnswerQualityMetrics
    gate: AnswerQualityGate


class Assistant(Protocol):
    async def ask(self, request: AnswerRequest) -> AnswerOutcome: ...


class AnswerQualityEvaluator:
    def __init__(
        self,
        assistant: Assistant,
        *,
        thresholds: AnswerQualityThresholds | None = None,
    ) -> None:
        self._assistant = assistant
        self._thresholds = thresholds or AnswerQualityThresholds()

    async def run(self, dataset: EvaluationDataset) -> AnswerQualityReport:
        expected_answers = 0
        supported_answers = 0
        expected_refusals = 0
        correct_refusals = 0
        for case in dataset.cases:
            outcome = await self._assistant.ask(
                AnswerRequest(
                    question=case.question,
                    actor=case.actor,
                    data_classification=case.data_classification,
                )
            )
            if case.expected_decision is SearchDecision.MATCHED:
                expected_answers += 1
                supported_answers += int(self._is_supported(outcome))
            else:
                expected_refusals += 1
                correct_refusals += int(outcome.decision is AnswerDecision.REFUSED)
        citation_support = self._ratio(supported_answers, expected_answers)
        refusal_accuracy = self._ratio(correct_refusals, expected_refusals)
        reasons: list[str] = []
        if citation_support is None:
            reasons.append("CITATION_SUPPORT_RATE_INDETERMINATE")
        elif citation_support < self._thresholds.citation_support_rate:
            reasons.append("CITATION_SUPPORT_RATE_BELOW_THRESHOLD")
        if refusal_accuracy is None:
            reasons.append("ANSWER_REFUSAL_ACCURACY_INDETERMINATE")
        elif refusal_accuracy < self._thresholds.answer_refusal_accuracy:
            reasons.append("ANSWER_REFUSAL_ACCURACY_BELOW_THRESHOLD")
        decision = GateDecision.PASS
        if any(reason.endswith("INDETERMINATE") for reason in reasons):
            decision = GateDecision.INDETERMINATE
        elif reasons:
            decision = GateDecision.FAIL
        return AnswerQualityReport(
            dataset_version=dataset.version,
            dataset_hash=dataset.content_hash,
            metrics=AnswerQualityMetrics(
                case_count=len(dataset.cases),
                citation_support_rate=citation_support,
                answer_refusal_accuracy=refusal_accuracy,
            ),
            gate=AnswerQualityGate(decision=decision, reasons=tuple(reasons)),
        )

    @staticmethod
    def _is_supported(outcome: AnswerOutcome) -> bool:
        citation_ids = {citation.evidence_id for citation in outcome.citations}
        return (
            outcome.decision is AnswerDecision.ANSWERED
            and bool(outcome.units)
            and all(
                bool(unit.evidence_ids) and set(unit.evidence_ids).issubset(citation_ids)
                for unit in outcome.units
            )
        )

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

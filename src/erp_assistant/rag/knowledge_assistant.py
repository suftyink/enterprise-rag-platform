import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from time import perf_counter
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.identity import (
    IdentityAccess,
    IdentityAction,
    IdentityDenied,
    KnowledgeScopeRef,
)
from erp_assistant.models.answer import KnowledgeAnswerFeedback, KnowledgeAnswerRun
from erp_assistant.models.knowledge import KnowledgeDocument
from erp_assistant.operations.contracts import NullTelemetry, Telemetry
from erp_assistant.orchestration.providers import (
    ChatMessage,
    ChatProvider,
    ProviderBoundary,
)
from erp_assistant.rag.answer_feedback_store import AnswerFeedbackStore
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.knowledge_eligibility import (
    authorized_scope_filter,
    retrieval_eligibility_filters,
)
from erp_assistant.rag.retriever import (
    ActorContext,
    RetrievalEvidence,
    RetrievalOutcome,
    RetrievalRequest,
)
from erp_assistant.rag.types import DataClassification, SearchDecision


class AnswerDecision(StrEnum):
    ANSWERED = "ANSWERED"
    REFUSED = "REFUSED"
    ERROR = "ERROR"


class FeedbackRating(StrEnum):
    USEFUL = "USEFUL"
    NOT_USEFUL = "NOT_USEFUL"
    CITATION_ISSUE = "CITATION_ISSUE"


@dataclass(frozen=True)
class AnswerRequest:
    question: str
    actor: ActorContext
    evidence_limit: int = 5
    data_classification: DataClassification = DataClassification.ORDINARY
    trace_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class AnswerUnit:
    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class AnswerCitation:
    evidence_id: str
    document_id: UUID
    document_key: str
    document_version: int
    title: str
    page_start: int | None
    page_end: int | None
    section_title: str | None
    heading_path: tuple[str, ...]
    anchor: str | None
    source_locator: dict[str, object]
    excerpt: str


@dataclass(frozen=True)
class AnswerOutcome:
    trace_id: UUID
    decision: AnswerDecision
    units: tuple[AnswerUnit, ...]
    citations: tuple[AnswerCitation, ...]
    reason: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class FeedbackRequest:
    trace_id: UUID
    actor: ActorContext
    rating: FeedbackRating
    reason: str | None = None


@dataclass(frozen=True)
class FeedbackReceipt:
    feedback_id: UUID
    trace_id: UUID
    answer_decision: AnswerDecision
    rating: FeedbackRating
    reason: str | None
    revision: int
    requires_review: bool


class Retriever(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome: ...


@dataclass(frozen=True)
class _ParsedAnswer:
    decision: AnswerDecision
    units: tuple[AnswerUnit, ...]
    reason: str | None = None


class KnowledgeAssistant:
    _SAFE_MODEL_REFUSAL_REASONS = frozenset({"CONFLICTING_EVIDENCE", "INSUFFICIENT_KNOWLEDGE"})

    def __init__(
        self,
        *,
        retriever: Retriever,
        chat_routes: dict[DataClassification, ChatProvider],
        session_factory: async_sessionmaker[AsyncSession],
        identity_access: IdentityAccess,
        telemetry: Telemetry | None = None,
        context_token_budget: int = 4_096,
    ) -> None:
        if context_token_budget < 64:
            raise ValueError("Answer context token budget is too small")
        self._retriever = retriever
        self._chat_routes = dict(chat_routes)
        self._sessions = session_factory
        self._feedback = AnswerFeedbackStore(session_factory)
        self._identity = identity_access
        self._telemetry = telemetry or NullTelemetry()
        self._context_token_budget = context_token_budget

    async def ask(self, request: AnswerRequest) -> AnswerOutcome:
        if not request.actor.is_trusted:
            raise RagError("UNTRUSTED_ACTOR", "回答请求缺少可信身份")
        trace_id = request.trace_id
        authorized_scopes = await self._authorize(
            request.actor, IdentityAction.ANSWER, trace_id
        )
        retrieval = await self._retriever.retrieve(
            RetrievalRequest(
                query=request.question,
                actor=request.actor,
                evidence_limit=request.evidence_limit,
                data_classification=request.data_classification,
                trace_id=trace_id,
            )
        )
        if retrieval.decision is SearchDecision.INSUFFICIENT_KNOWLEDGE or not retrieval.evidence:
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.REFUSED,
                    units=(),
                    citations=(),
                    reason="INSUFFICIENT_KNOWLEDGE",
                ),
            )
        context_evidence = self._select_context(retrieval.evidence)
        authorized_scopes = await self._authorize(
            request.actor, IdentityAction.ANSWER, trace_id
        )
        evidence_by_id = {
            f"E{index}": evidence for index, evidence in enumerate(context_evidence, start=1)
        }
        if not await self._evidence_is_current(
            request, context_evidence, authorized_scopes
        ):
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.REFUSED,
                    units=(),
                    citations=(),
                    reason="EVIDENCE_CHANGED",
                ),
            )
        provider = self._chat_routes.get(request.data_classification)
        if provider is None:
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.ERROR,
                    units=(),
                    citations=(),
                    reason="CHAT_ROUTE_UNAVAILABLE",
                ),
            )
        try:
            boundary = ProviderBoundary(provider.boundary)
        except (AttributeError, ValueError):
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.ERROR,
                    units=(),
                    citations=(),
                    reason="CHAT_ROUTE_INVALID",
                ),
            )
        if (
            request.data_classification is DataClassification.RESTRICTED
            and boundary is not ProviderBoundary.INTERNAL
        ):
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.ERROR,
                    units=(),
                    citations=(),
                    reason="RESTRICTED_ROUTE_FORBIDDEN",
                ),
            )
        model_started = perf_counter()
        try:
            result = await provider.generate(self._messages(request.question, evidence_by_id))
        except (ConnectionError, RagDependencyError, TimeoutError):
            self._telemetry.emit(
                trace_id=trace_id,
                signal="model_generation",
                stage="model_generation",
                outcome="FAILED",
                attributes={"error_code": "CHAT_PROVIDER_UNAVAILABLE"},
            )
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.ERROR,
                    units=(),
                    citations=(),
                    reason="CHAT_PROVIDER_UNAVAILABLE",
                ),
            )
        self._telemetry.emit(
            trace_id=trace_id,
            signal="model_generation",
            stage="model_generation",
            outcome="SUCCEEDED",
            attributes={
                "duration_ms": round((perf_counter() - model_started) * 1_000, 2),
                "model": result.model,
            },
        )
        authorized_scopes = await self._authorize(
            request.actor, IdentityAction.ANSWER, trace_id
        )
        parsed = self._parse_answer(result.content)
        if parsed is None:
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.REFUSED,
                    units=(),
                    citations=(),
                    reason="INVALID_MODEL_OUTPUT",
                    model=result.model,
                ),
            )
        if parsed.decision is AnswerDecision.REFUSED:
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.REFUSED,
                    units=(),
                    citations=(),
                    reason=parsed.reason or "MODEL_REFUSED",
                    model=result.model,
                ),
            )
        units = parsed.units
        cited_ids = tuple(
            dict.fromkeys(evidence_id for unit in units for evidence_id in unit.evidence_ids)
        )
        if any(evidence_id not in evidence_by_id for evidence_id in cited_ids):
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.REFUSED,
                    units=(),
                    citations=(),
                    reason="UNKNOWN_EVIDENCE_ID",
                    model=result.model,
                ),
            )
        if not units or not cited_ids or any(not unit.evidence_ids for unit in units):
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.REFUSED,
                    units=(),
                    citations=(),
                    reason="UNSUPPORTED_ANSWER",
                    model=result.model,
                ),
            )
        cited_evidence = {item: evidence_by_id[item] for item in cited_ids}
        if not await self._evidence_is_current(
            request, context_evidence, authorized_scopes
        ):
            return await self._finish(
                request,
                AnswerOutcome(
                    trace_id=retrieval.run.trace_id,
                    decision=AnswerDecision.REFUSED,
                    units=(),
                    citations=(),
                    reason="EVIDENCE_CHANGED",
                    model=result.model,
                ),
            )
        citations = tuple(
            self._citation(evidence_id, evidence)
            for evidence_id, evidence in cited_evidence.items()
        )
        return await self._finish(
            request,
            AnswerOutcome(
                trace_id=retrieval.run.trace_id,
                decision=AnswerDecision.ANSWERED,
                units=units,
                citations=citations,
                model=result.model,
            ),
        )

    async def submit_feedback(self, request: FeedbackRequest) -> FeedbackReceipt:
        if not request.actor.is_trusted:
            raise RagError("UNTRUSTED_ACTOR", "反馈请求缺少可信身份")
        reason = request.reason.strip() if request.reason else None
        if reason is not None and (not reason or len(reason) > 500):
            raise ValueError("Feedback reason is invalid")
        feedback = await self._feedback.save(
            trace_id=request.trace_id,
            subject_id=request.actor.subject_id,
            tenant_id=request.actor.tenant_id,
            rating=request.rating.value,
            reason=reason,
        )
        return self._feedback_receipt(feedback)

    async def feedback_review_candidates(self) -> tuple[FeedbackReceipt, ...]:
        rows = await self._feedback.review_candidates(
            (
                FeedbackRating.NOT_USEFUL.value,
                FeedbackRating.CITATION_ISSUE.value,
            )
        )
        return tuple(self._feedback_receipt(row) for row in rows)

    async def _authorize(
        self,
        actor: ActorContext,
        action: IdentityAction,
        trace_id: UUID,
    ) -> tuple[KnowledgeScopeRef, ...]:
        try:
            return await self._identity.authorize_read_scopes(
                actor,
                action,
                trace_id=trace_id,
            )
        except IdentityDenied as exc:
            raise RagError(exc.code, exc.message) from exc

    async def _finish(
        self,
        request: AnswerRequest,
        outcome: AnswerOutcome,
    ) -> AnswerOutcome:
        async with self._sessions() as session:
            run = await session.get(KnowledgeAnswerRun, outcome.trace_id)
            if run is None:
                session.add(
                    KnowledgeAnswerRun(
                        trace_id=outcome.trace_id,
                        subject_id=request.actor.subject_id,
                        tenant_id=request.actor.tenant_id,
                        data_classification=request.data_classification.value,
                        decision=outcome.decision.value,
                        reason=outcome.reason,
                        model=outcome.model,
                    )
                )
            await session.commit()
        self._telemetry.emit(
            trace_id=outcome.trace_id,
            signal="answer",
            stage="answer",
            outcome="SUCCEEDED" if outcome.decision is not AnswerDecision.ERROR else "FAILED",
            attributes={
                "decision": outcome.decision.value,
                "reason_code": outcome.reason or "NONE",
                **({"model": outcome.model} if outcome.model is not None else {}),
            },
        )
        return outcome

    @staticmethod
    def _feedback_receipt(feedback: KnowledgeAnswerFeedback) -> FeedbackReceipt:
        rating = FeedbackRating(feedback.rating)
        return FeedbackReceipt(
            feedback_id=feedback.id,
            trace_id=feedback.trace_id,
            answer_decision=AnswerDecision(feedback.answer_decision),
            rating=rating,
            reason=feedback.reason,
            revision=feedback.revision,
            requires_review=rating is not FeedbackRating.USEFUL,
        )

    @staticmethod
    def _evidence_tokens(evidence: RetrievalEvidence) -> int:
        return 40 + (len(evidence.content) + 3) // 4

    def _select_context(
        self,
        evidence: tuple[RetrievalEvidence, ...],
    ) -> tuple[RetrievalEvidence, ...]:
        selected: list[RetrievalEvidence] = []
        remaining = self._context_token_budget
        for item in evidence:
            tokens = self._evidence_tokens(item)
            if tokens <= remaining:
                selected.append(item)
                remaining -= tokens
                continue
            if not selected and remaining > 40:
                content_chars = (remaining - 40) * 4
                selected.append(replace(item, content=item.content[:content_chars]))
            break
        return tuple(selected)

    @staticmethod
    def _parse_answer(content: str) -> _ParsedAnswer | None:
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        decision = payload.get("decision")
        if decision == AnswerDecision.REFUSED.value:
            reason = payload.get("refusalReason")
            return _ParsedAnswer(
                decision=AnswerDecision.REFUSED,
                units=(),
                reason=(
                    reason
                    if isinstance(reason, str)
                    and reason in KnowledgeAssistant._SAFE_MODEL_REFUSAL_REASONS
                    else None
                ),
            )
        if decision != AnswerDecision.ANSWERED.value:
            return None
        raw_units = payload.get("answerUnits")
        if not isinstance(raw_units, list) or not raw_units:
            return None
        units: list[AnswerUnit] = []
        for raw_unit in raw_units:
            if not isinstance(raw_unit, dict):
                return None
            text = raw_unit.get("text")
            evidence_ids = raw_unit.get("evidenceIds")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 4_000
                or not isinstance(evidence_ids, list)
                or not evidence_ids
                or any(
                    not isinstance(evidence_id, str) or not evidence_id.strip()
                    for evidence_id in evidence_ids
                )
            ):
                return None
            units.append(
                AnswerUnit(
                    text=text.strip(),
                    evidence_ids=tuple(dict.fromkeys(evidence_ids)),
                )
            )
        return _ParsedAnswer(decision=AnswerDecision.ANSWERED, units=tuple(units))

    @staticmethod
    def _messages(
        question: str,
        evidence_by_id: dict[str, RetrievalEvidence],
    ) -> list[ChatMessage]:
        evidence = [
            {
                "evidenceId": evidence_id,
                "content": item.content,
                "sourceLocator": item.source_locator,
            }
            for evidence_id, item in evidence_by_id.items()
        ]
        return [
            ChatMessage(
                role="system",
                content=(
                    "Return one JSON object only. Allowed schemas: "
                    '{"decision":"ANSWERED","answerUnits":['
                    '{"text":"concise factual statement","evidenceIds":["E1"]}]}'
                    " or "
                    '{"decision":"REFUSED","refusalReason":"CONFLICTING_EVIDENCE"}. '
                    "refusalReason must be exactly one of INSUFFICIENT_KNOWLEDGE or "
                    "CONFLICTING_EVIDENCE. "
                    "Use only supplied evidence IDs. Every factual answer unit must "
                    "have non-empty text and at least one evidenceIds entry. If "
                    "authoritative evidence conflicts, refuse with CONFLICTING_EVIDENCE."
                ),
            ),
            ChatMessage(
                role="user",
                content=json.dumps(
                    {"question": question, "evidence": evidence},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ]

    async def _evidence_is_current(
        self,
        request: AnswerRequest,
        evidence: tuple[RetrievalEvidence, ...],
        authorized_scopes: tuple[KnowledgeScopeRef, ...],
    ) -> bool:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(KnowledgeDocument.id, KnowledgeDocument.version).where(
                        KnowledgeDocument.id.in_([item.document_id for item in evidence]),
                        *retrieval_eligibility_filters(),
                        KnowledgeDocument.data_classification == request.data_classification.value,
                        authorized_scope_filter(authorized_scopes),
                    )
                )
            ).all()
        current = {document_id: version for document_id, version in rows}
        return all(current.get(item.document_id) == item.document_version for item in evidence)

    @staticmethod
    def _citation(evidence_id: str, evidence: RetrievalEvidence) -> AnswerCitation:
        return AnswerCitation(
            evidence_id=evidence_id,
            document_id=evidence.document_id,
            document_key=evidence.document_key,
            document_version=evidence.document_version,
            title=evidence.title,
            page_start=evidence.page_start,
            page_end=evidence.page_end,
            section_title=evidence.section_title,
            heading_path=evidence.heading_path,
            anchor=evidence.anchor,
            source_locator=evidence.source_locator,
            excerpt=evidence.content,
        )

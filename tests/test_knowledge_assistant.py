import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import KnowledgeDocument
from erp_assistant.orchestration.providers import (
    ChatMessage,
    ChatResult,
    ProviderBoundary,
)
from erp_assistant.rag.knowledge_assistant import (
    AnswerDecision,
    AnswerRequest,
    FeedbackRating,
    FeedbackRequest,
    KnowledgeAssistant,
)
from erp_assistant.rag.retriever import (
    ActorContext,
    RetrievalEvidence,
    RetrievalOutcome,
    RetrievalRequest,
    RetrievalRun,
)
from erp_assistant.rag.types import (
    DataClassification,
    KnowledgeScope,
    SearchDecision,
)


@dataclass
class DeterministicRetriever:
    outcome: RetrievalOutcome
    requests: list[RetrievalRequest] = field(default_factory=list)

    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        self.requests.append(request)
        return self.outcome


@dataclass
class StructuredChat:
    payload: dict[str, object]
    boundary: ProviderBoundary = ProviderBoundary.CLOUD
    calls: list[list[ChatMessage]] = field(default_factory=list)

    async def generate(self, messages: list[ChatMessage]) -> ChatResult:
        self.calls.append(messages)
        return ChatResult(content=json.dumps(self.payload), model="structured-chat-v1")


@dataclass
class RawChat:
    content: str
    boundary: ProviderBoundary = ProviderBoundary.CLOUD

    async def generate(self, messages: list[ChatMessage]) -> ChatResult:
        del messages
        return ChatResult(content=self.content, model="raw-chat-v1")


class FailingChat:
    boundary = ProviderBoundary.CLOUD

    async def generate(self, messages: list[ChatMessage]) -> ChatResult:
        del messages
        raise TimeoutError("provider timeout")


class BoundarylessChat:
    async def generate(self, messages: list[ChatMessage]) -> ChatResult:
        del messages
        raise AssertionError("provider without a declared boundary must not be called")


@dataclass
class WithdrawingChat(StructuredChat):
    sessions: async_sessionmaker[AsyncSession] | None = None
    document_id: UUID | None = None

    async def generate(self, messages: list[ChatMessage]) -> ChatResult:
        assert self.sessions is not None
        assert self.document_id is not None
        async with self.sessions() as session:
            document = await session.get_one(KnowledgeDocument, self.document_id)
            document.active = False
            document.publication_status = "WITHDRAWN"
            await session.commit()
        return await super().generate(messages)


@pytest_asyncio.fixture
async def answer_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def retrieval_outcome(evidence: tuple[RetrievalEvidence, ...]) -> RetrievalOutcome:
    return RetrievalOutcome(
        decision=(SearchDecision.MATCHED if evidence else SearchDecision.INSUFFICIENT_KNOWLEDGE),
        evidence=evidence,
        run=RetrievalRun(
            trace_id=uuid4(),
            retrieval="test-retrieval",
            embedding_model="test-embedding",
            rerank_model="test-rerank",
            threshold=0.5,
            config_fingerprint="a" * 64,
            elapsed_ms=1.0,
        ),
    )


async def published_evidence(
    sessions: async_sessionmaker[AsyncSession],
    *,
    document_key: str = "returns-policy",
    content: str = "Employees may return unopened stock within 30 days.",
    digest: str = "a" * 64,
    data_classification: DataClassification = DataClassification.ORDINARY,
) -> RetrievalEvidence:
    document_id = uuid4()
    async with sessions() as session:
        session.add(
            KnowledgeDocument(
                id=document_id,
                scope=KnowledgeScope.TENANT.value,
                scope_id="tenant-a",
                tenant_id="tenant-a",
                document_key=document_key,
                version=3,
                title="Returns Policy",
                original_filename="returns.md",
                media_type="text/markdown",
                data_classification=data_classification.value,
                size_bytes=100,
                sha256=digest,
                object_key=f"knowledge/{document_key}.md",
                status="READY",
                active=True,
                publication_status="PUBLISHED",
                retention_status="RETAINED",
                index_artifacts_present=True,
                created_by="author-a",
            )
        )
        await session.commit()
    return RetrievalEvidence(
        chunk_id=uuid4(),
        document_id=document_id,
        document_key=document_key,
        title="Returns Policy",
        scope=KnowledgeScope.TENANT,
        document_version=3,
        page_start=None,
        page_end=None,
        section_title="Return window",
        heading_path=("Returns", "Return window"),
        anchor="return-window",
        block_kind="PARAGRAPH",
        source_locator={"headingPath": ["Returns", "Return window"]},
        content=content,
        score=0.98,
        retrieval_rank=1,
    )


@pytest.mark.asyncio
async def test_ask_returns_answer_with_code_bound_citation(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await published_evidence(answer_sessions)
    retriever = DeterministicRetriever(retrieval_outcome((evidence,)))
    chat = StructuredChat(
        {
            "decision": "ANSWERED",
            "answerUnits": [
                {
                    "text": "Unopened stock can be returned within 30 days.",
                    "evidenceIds": ["E1"],
                }
            ],
            "citations": [
                {
                    "evidenceId": "E1",
                    "title": "fabricated title from model must be ignored",
                }
            ],
        }
    )
    assistant = KnowledgeAssistant(
        retriever=retriever,
        chat_routes={DataClassification.ORDINARY: chat},
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="What is the return window?",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        )
    )

    assert outcome.decision is AnswerDecision.ANSWERED
    assert outcome.units[0].text == "Unopened stock can be returned within 30 days."
    assert outcome.units[0].evidence_ids == ("E1",)
    assert outcome.citations[0].evidence_id == "E1"
    assert outcome.citations[0].document_id == evidence.document_id
    assert outcome.citations[0].document_version == 3
    assert outcome.citations[0].title == "Returns Policy"
    assert outcome.citations[0].source_locator == evidence.source_locator
    assert outcome.citations[0].excerpt == evidence.content


@pytest.mark.asyncio
async def test_ask_rejects_restricted_evidence_before_cloud_chat_call(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await published_evidence(
        answer_sessions, data_classification=DataClassification.RESTRICTED
    )
    retriever = DeterministicRetriever(retrieval_outcome((evidence,)))
    cloud_chat = StructuredChat(
        {"decision": "ANSWERED", "answerUnits": []},
        boundary=ProviderBoundary.CLOUD,
    )
    assistant = KnowledgeAssistant(
        retriever=retriever,
        chat_routes={DataClassification.RESTRICTED: cloud_chat},
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="What is the restricted policy?",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
            data_classification=DataClassification.RESTRICTED,
        )
    )

    assert outcome.decision is AnswerDecision.ERROR
    assert outcome.reason == "RESTRICTED_ROUTE_FORBIDDEN"
    assert cloud_chat.calls == []


@pytest.mark.asyncio
async def test_ask_fails_closed_when_chat_boundary_is_missing(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await published_evidence(
        answer_sessions, data_classification=DataClassification.RESTRICTED
    )
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((evidence,))),
        chat_routes={DataClassification.RESTRICTED: BoundarylessChat()},  # type: ignore[dict-item]
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="What is the restricted policy?",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
            data_classification=DataClassification.RESTRICTED,
        )
    )

    assert outcome.decision is AnswerDecision.ERROR
    assert outcome.reason == "CHAT_ROUTE_INVALID"


@pytest.mark.asyncio
async def test_ask_refuses_unknown_model_evidence_id(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await published_evidence(answer_sessions)
    chat = StructuredChat(
        {
            "decision": "ANSWERED",
            "answerUnits": [{"text": "Fabricated claim.", "evidenceIds": ["E99"]}],
        }
    )
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((evidence,))),
        chat_routes={DataClassification.ORDINARY: chat},
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="Can I invent a citation?",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        )
    )

    assert outcome.decision is AnswerDecision.REFUSED
    assert outcome.reason == "UNKNOWN_EVIDENCE_ID"
    assert outcome.units == ()
    assert outcome.citations == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_reason"),
    [
        (
            {
                "decision": "ANSWERED",
                "answerUnits": [{"text": "Unsupported fact.", "evidenceIds": []}],
            },
            "INVALID_MODEL_OUTPUT",
        ),
        (
            {
                "decision": "REFUSED",
                "refusalReason": "CONFLICTING_EVIDENCE",
            },
            "CONFLICTING_EVIDENCE",
        ),
        (
            {
                "decision": "REFUSED",
                "refusalReason": "raw provider explanation must not escape",
            },
            "MODEL_REFUSED",
        ),
    ],
)
async def test_ask_fails_closed_for_missing_conflicting_or_unsafe_support(
    answer_sessions: async_sessionmaker[AsyncSession],
    payload: dict[str, object],
    expected_reason: str,
) -> None:
    evidence = await published_evidence(answer_sessions)
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((evidence,))),
        chat_routes={DataClassification.ORDINARY: StructuredChat(payload)},
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="Can this evidence support an answer?",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        )
    )

    assert outcome.decision is AnswerDecision.REFUSED
    assert outcome.reason == expected_reason
    assert outcome.units == ()
    assert outcome.citations == ()


@pytest.mark.asyncio
async def test_ask_refuses_malformed_structured_output_without_exposing_text(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await published_evidence(answer_sessions)
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((evidence,))),
        chat_routes={DataClassification.ORDINARY: RawChat("not-json secret claim")},
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="Return malformed output",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        )
    )

    assert outcome.decision is AnswerDecision.REFUSED
    assert outcome.reason == "INVALID_MODEL_OUTPUT"
    assert outcome.units == ()
    assert outcome.citations == ()


@pytest.mark.asyncio
async def test_ask_distinguishes_chat_failure_from_insufficient_evidence(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await published_evidence(answer_sessions)
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((evidence,))),
        chat_routes={DataClassification.ORDINARY: FailingChat()},
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="Will the provider time out?",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        )
    )

    assert outcome.decision is AnswerDecision.ERROR
    assert outcome.reason == "CHAT_PROVIDER_UNAVAILABLE"
    assert outcome.units == ()
    assert outcome.citations == ()


@pytest.mark.asyncio
async def test_ask_refuses_insufficient_evidence_without_chat_call(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    chat = StructuredChat({"decision": "ANSWERED", "answerUnits": []})
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome(())),
        chat_routes={DataClassification.ORDINARY: chat},
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="Unknown policy",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        )
    )

    assert outcome.decision is AnswerDecision.REFUSED
    assert outcome.reason == "INSUFFICIENT_KNOWLEDGE"
    assert chat.calls == []


@pytest.mark.asyncio
async def test_ask_refuses_when_evidence_is_withdrawn_during_generation(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await published_evidence(answer_sessions)
    chat = WithdrawingChat(
        payload={
            "decision": "ANSWERED",
            "answerUnits": [{"text": "A no longer valid answer.", "evidenceIds": ["E1"]}],
        },
        sessions=answer_sessions,
        document_id=evidence.document_id,
    )
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((evidence,))),
        chat_routes={DataClassification.ORDINARY: chat},
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="What changed?",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        )
    )

    assert outcome.decision is AnswerDecision.REFUSED
    assert outcome.reason == "EVIDENCE_CHANGED"
    assert outcome.units == ()
    assert outcome.citations == ()


@pytest.mark.asyncio
async def test_ask_refuses_when_uncited_context_changes_during_generation(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    cited = await published_evidence(answer_sessions, document_key="cited-policy", digest="1" * 64)
    uncited = await published_evidence(
        answer_sessions, document_key="uncited-policy", digest="2" * 64
    )
    chat = WithdrawingChat(
        payload={
            "decision": "ANSWERED",
            "answerUnits": [{"text": "Cited answer.", "evidenceIds": ["E1"]}],
        },
        sessions=answer_sessions,
        document_id=uncited.document_id,
    )
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((cited, uncited))),
        chat_routes={DataClassification.ORDINARY: chat},
        session_factory=answer_sessions,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="Which source applies?",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        )
    )

    assert outcome.decision is AnswerDecision.REFUSED
    assert outcome.reason == "EVIDENCE_CHANGED"
    assert outcome.units == ()
    assert outcome.citations == ()


@pytest.mark.asyncio
async def test_feedback_links_to_answer_trace_and_revises_without_copying_content(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await published_evidence(answer_sessions)
    chat = StructuredChat(
        {
            "decision": "ANSWERED",
            "answerUnits": [{"text": "Thirty days.", "evidenceIds": ["E1"]}],
        }
    )
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((evidence,))),
        chat_routes={DataClassification.ORDINARY: chat},
        session_factory=answer_sessions,
    )
    actor = ActorContext(subject_id="employee-a", tenant_id="tenant-a")
    outcome = await assistant.ask(AnswerRequest(question="Return window?", actor=actor))

    first = await assistant.submit_feedback(
        FeedbackRequest(
            trace_id=outcome.trace_id,
            actor=actor,
            rating=FeedbackRating.NOT_USEFUL,
            reason="The cited section needs review.",
        )
    )
    revised = await assistant.submit_feedback(
        FeedbackRequest(
            trace_id=outcome.trace_id,
            actor=actor,
            rating=FeedbackRating.CITATION_ISSUE,
            reason="The citation locator is unclear.",
        )
    )
    candidates = await assistant.feedback_review_candidates()

    assert first.trace_id == outcome.trace_id
    assert first.answer_decision is AnswerDecision.ANSWERED
    assert first.revision == 1
    assert revised.feedback_id == first.feedback_id
    assert revised.revision == 2
    assert revised.requires_review
    assert candidates == (revised,)
    assert "Return window?" not in repr(candidates)
    assert "Thirty days." not in repr(candidates)


@pytest.mark.asyncio
async def test_feedback_concurrent_duplicate_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "feedback.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    answer_sessions = async_sessionmaker(engine, expire_on_commit=False)
    evidence = await published_evidence(answer_sessions)
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((evidence,))),
        chat_routes={
            DataClassification.ORDINARY: StructuredChat(
                {
                    "decision": "ANSWERED",
                    "answerUnits": [{"text": "Thirty days.", "evidenceIds": ["E1"]}],
                }
            )
        },
        session_factory=answer_sessions,
    )
    actor = ActorContext(subject_id="employee-a", tenant_id="tenant-a")
    outcome = await assistant.ask(AnswerRequest(question="Return window?", actor=actor))
    request = FeedbackRequest(
        trace_id=outcome.trace_id,
        actor=actor,
        rating=FeedbackRating.NOT_USEFUL,
        reason="Needs review.",
    )

    try:
        first, second = await asyncio.gather(
            assistant.submit_feedback(request),
            assistant.submit_feedback(request),
        )

        assert first.feedback_id == second.feedback_id
        assert first.revision == second.revision == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ask_limits_context_while_preserving_evidence_identity_and_locator(
    answer_sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = await published_evidence(
        answer_sessions,
        document_key="first-policy",
        content="A" * 160,
        digest="1" * 64,
    )
    second = await published_evidence(
        answer_sessions,
        document_key="second-policy",
        content="B" * 160,
        digest="2" * 64,
    )
    chat = StructuredChat(
        {
            "decision": "ANSWERED",
            "answerUnits": [{"text": "First policy.", "evidenceIds": ["E1"]}],
        }
    )
    assistant = KnowledgeAssistant(
        retriever=DeterministicRetriever(retrieval_outcome((first, second))),
        chat_routes={DataClassification.ORDINARY: chat},
        session_factory=answer_sessions,
        context_token_budget=72,
    )

    outcome = await assistant.ask(
        AnswerRequest(
            question="Which policy applies?",
            actor=ActorContext(subject_id="employee-a", tenant_id="tenant-a"),
        )
    )
    prompt = json.loads(chat.calls[0][1].content)
    system_prompt = chat.calls[0][0].content

    assert outcome.decision is AnswerDecision.ANSWERED
    assert [item["evidenceId"] for item in prompt["evidence"]] == ["E1"]
    assert prompt["evidence"][0]["sourceLocator"] == first.source_locator
    assert len(prompt["evidence"][0]["content"]) < len(first.content)
    assert outcome.citations[0].excerpt == prompt["evidence"][0]["content"]
    assert "answerUnits" in system_prompt
    assert "refusalReason" in system_prompt

from typing import cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.models.answer import (
    KnowledgeAnswerFeedback,
    KnowledgeAnswerRun,
    utc_now,
)


class AnswerFeedbackStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def save(
        self,
        *,
        trace_id: UUID,
        subject_id: str,
        tenant_id: str,
        rating: str,
        reason: str | None,
    ) -> KnowledgeAnswerFeedback:
        for _ in range(5):
            async with self._sessions() as session:
                run = await session.get(KnowledgeAnswerRun, trace_id)
                if run is None or run.subject_id != subject_id or run.tenant_id != tenant_id:
                    raise LookupError("Answer trace does not exist")
                feedback = await session.scalar(
                    select(KnowledgeAnswerFeedback).where(
                        KnowledgeAnswerFeedback.trace_id == trace_id,
                        KnowledgeAnswerFeedback.subject_id == subject_id,
                    )
                )
                if feedback is None:
                    feedback = KnowledgeAnswerFeedback(
                        trace_id=trace_id,
                        subject_id=subject_id,
                        tenant_id=tenant_id,
                        answer_decision=run.decision,
                        rating=rating,
                        reason=reason,
                    )
                    session.add(feedback)
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        continue
                    return feedback
                if feedback.rating == rating and feedback.reason == reason:
                    return feedback
                result = cast(
                    CursorResult[tuple[object]],
                    await session.execute(
                        update(KnowledgeAnswerFeedback)
                        .where(
                            KnowledgeAnswerFeedback.id == feedback.id,
                            KnowledgeAnswerFeedback.revision == feedback.revision,
                        )
                        .values(
                            rating=rating,
                            reason=reason,
                            revision=feedback.revision + 1,
                            updated_at=utc_now(),
                        )
                    ),
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()
                return await session.get_one(KnowledgeAnswerFeedback, feedback.id)
        raise RuntimeError("Feedback update conflicted repeatedly")

    async def review_candidates(
        self, ratings: tuple[str, ...]
    ) -> tuple[KnowledgeAnswerFeedback, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(KnowledgeAnswerFeedback)
                    .where(KnowledgeAnswerFeedback.rating.in_(ratings))
                    .order_by(
                        KnowledgeAnswerFeedback.updated_at,
                        KnowledgeAnswerFeedback.id,
                    )
                )
            ).all()
        return tuple(rows)

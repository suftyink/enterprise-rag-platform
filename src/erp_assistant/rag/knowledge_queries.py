from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.models.knowledge import KnowledgeIndexJob


async def latest_job(session: AsyncSession, document_id: UUID) -> KnowledgeIndexJob | None:
    return cast(
        KnowledgeIndexJob | None,
        await session.scalar(
            select(KnowledgeIndexJob)
            .where(KnowledgeIndexJob.document_id == document_id)
            .order_by(KnowledgeIndexJob.created_at.desc())
            .limit(1)
        ),
    )

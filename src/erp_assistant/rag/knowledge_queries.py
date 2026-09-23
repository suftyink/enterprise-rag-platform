from collections.abc import Collection
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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


async def latest_jobs(
    session: AsyncSession, document_ids: Collection[UUID]
) -> dict[UUID, KnowledgeIndexJob]:
    if not document_ids:
        return {}
    ranked = (
        select(
            KnowledgeIndexJob,
            func.row_number()
            .over(
                partition_by=KnowledgeIndexJob.document_id,
                order_by=(
                    KnowledgeIndexJob.created_at.desc(),
                    KnowledgeIndexJob.id.desc(),
                ),
            )
            .label("job_rank"),
        )
        .where(KnowledgeIndexJob.document_id.in_(document_ids))
        .subquery()
    )
    ranked_job = aliased(KnowledgeIndexJob, ranked)
    jobs = (
        await session.scalars(select(ranked_job).where(ranked.c.job_rank == 1))
    ).all()
    return {job.document_id: job for job in jobs}

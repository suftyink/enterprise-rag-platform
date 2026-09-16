from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.models.knowledge import KnowledgeCorpusGeneration
from erp_assistant.rag.types import DataClassification


async def lock_corpus_generation(
    session: AsyncSession,
    classification: DataClassification,
) -> KnowledgeCorpusGeneration:
    generation = await session.scalar(
        select(KnowledgeCorpusGeneration)
        .where(
            KnowledgeCorpusGeneration.data_classification == classification.value
        )
        .with_for_update()
    )
    if generation is None:
        generation = KnowledgeCorpusGeneration(
            data_classification=classification.value,
            generation=0,
        )
        session.add(generation)
        await session.flush()
    return generation

import asyncio
import json

from erp_assistant.infrastructure.database import SessionFactory, engine
from erp_assistant.orchestration.provider_factory import get_embedding_provider
from erp_assistant.rag.service import rebuild_active_index
from erp_assistant.rag.vector_store import get_vector_store


async def _run() -> None:
    try:
        async with SessionFactory() as session:
            result = await rebuild_active_index(
                session,
                get_vector_store(),
                get_embedding_provider(),
            )
        print(
            json.dumps(
                {
                    "status": "completed",
                    "documents": result.document_count,
                    "chunks": result.chunk_count,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()

from erp_assistant.core.config import get_settings
from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.infrastructure.object_storage import get_object_storage
from erp_assistant.orchestration.provider_factory import get_embedding_provider
from erp_assistant.rag.cleanup_executor import KnowledgeCleanupExecutor
from erp_assistant.rag.document_indexer import DocumentIndexer
from erp_assistant.rag.knowledge_corpus import KnowledgeCorpus
from erp_assistant.rag.pdf_parser import PypdfParser
from erp_assistant.rag.vector_store import get_vector_store


def get_document_indexer() -> DocumentIndexer:
    return DocumentIndexer(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        parser=PypdfParser(),
        embedding=get_embedding_provider(),
        vector_writer=get_vector_store(),
        settings=get_settings(),
    )


def get_knowledge_corpus() -> KnowledgeCorpus:
    return KnowledgeCorpus(
        session_factory=SessionFactory,
        processor=get_document_indexer(),
    )


def get_cleanup_executor() -> KnowledgeCleanupExecutor:
    return KnowledgeCleanupExecutor(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        vectors=get_vector_store(),
        corpus=get_knowledge_corpus(),
    )

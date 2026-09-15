from functools import partial

from erp_assistant.core.config import get_settings
from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.infrastructure.object_storage import get_object_storage
from erp_assistant.orchestration.provider_factory import get_embedding_provider_for
from erp_assistant.rag.classified_indexer import ClassifiedDocumentIndexer
from erp_assistant.rag.cleanup_executor import KnowledgeCleanupExecutor
from erp_assistant.rag.document_indexer import DocumentIndexer
from erp_assistant.rag.knowledge_corpus import KnowledgeCorpus
from erp_assistant.rag.knowledge_corpus_contracts import CorpusProcessor
from erp_assistant.rag.pdf_parser import PypdfParser
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import get_vector_store, get_vector_store_for


def get_document_indexer_for(classification: DataClassification) -> DocumentIndexer:
    return DocumentIndexer(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        parser=PypdfParser(),
        embedding=get_embedding_provider_for(classification),
        vector_writer=get_vector_store_for(classification),
        settings=get_settings(),
    )


def get_document_indexer() -> CorpusProcessor:
    return ClassifiedDocumentIndexer(
        SessionFactory,
        {
            classification: partial(get_document_indexer_for, classification)
            for classification in (
                DataClassification.ORDINARY,
                DataClassification.RESTRICTED,
            )
        },
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
        vectors_by_classification={
            classification: get_vector_store_for(classification)
            for classification in (
                DataClassification.ORDINARY,
                DataClassification.RESTRICTED,
            )
        },
        corpus=get_knowledge_corpus(),
    )

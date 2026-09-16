from functools import partial

from erp_assistant.core.config import get_settings
from erp_assistant.identity import IdentityAccess
from erp_assistant.identity.audit import SessionSecurityAuditSink
from erp_assistant.identity.composition import identity_components
from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.infrastructure.object_storage import get_object_storage
from erp_assistant.operations.telemetry import get_telemetry
from erp_assistant.orchestration.provider_factory import get_embedding_provider_for
from erp_assistant.rag.classified_indexer import (
    ActiveProfileDocumentIndexer,
    ClassifiedDocumentIndexer,
)
from erp_assistant.rag.cleanup_executor import KnowledgeCleanupExecutor
from erp_assistant.rag.document_indexer import DocumentIndexer
from erp_assistant.rag.knowledge_corpus import KnowledgeCorpus
from erp_assistant.rag.knowledge_corpus_contracts import (
    CorpusProcessor,
    IdentityCorpusAuthorizer,
)
from erp_assistant.rag.pdf_parser import PypdfParser
from erp_assistant.rag.runtime_composition import get_retrieval_runtime_registry
from erp_assistant.rag.runtime_registry import RetrievalRuntimeBinding
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
        telemetry=get_telemetry(),
    )


def _get_profile_document_indexer(binding: RetrievalRuntimeBinding) -> DocumentIndexer:
    indexer = DocumentIndexer(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        parser=PypdfParser(),
        embedding=binding.embedding,
        vector_writer=binding.vector_store,
        settings=binding.settings,
        telemetry=get_telemetry(),
    )
    if (
        indexer.pipeline.parser != binding.parser_version
        or indexer.pipeline.chunking != binding.chunk_version
        or indexer.pipeline.vector_index != binding.vector_store.identity
    ):
        raise ValueError("Active profile parser, chunk, or vector binding does not match")
    return indexer


def get_document_indexer() -> CorpusProcessor:
    if get_settings().retrieval_runtime_deployments:
        return ActiveProfileDocumentIndexer(
            SessionFactory,
            get_retrieval_runtime_registry(),
            _get_profile_document_indexer,
        )
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
    verifier, directory = identity_components(get_settings())
    return KnowledgeCorpus(
        session_factory=SessionFactory,
        processor=get_document_indexer(),
        authorizer=IdentityCorpusAuthorizer(
            IdentityAccess(
                verifier=verifier,
                directory=directory,
                audit=SessionSecurityAuditSink(SessionFactory),
                telemetry=get_telemetry(),
            )
        ),
        telemetry=get_telemetry(),
    )


def get_cleanup_executor() -> KnowledgeCleanupExecutor:
    settings = get_settings()
    registry = get_retrieval_runtime_registry() if settings.retrieval_runtime_deployments else None
    return KnowledgeCleanupExecutor(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        vectors=get_vector_store(),
        vectors_by_classification={
            classification: (
                get_vector_store_for(classification),
                *(registry.vector_stores_for(classification) if registry is not None else ()),
            )
            for classification in (
                DataClassification.ORDINARY,
                DataClassification.RESTRICTED,
            )
        },
        corpus=get_knowledge_corpus(),
    )

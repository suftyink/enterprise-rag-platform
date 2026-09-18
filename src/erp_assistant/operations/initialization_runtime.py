from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace
from typing import cast

from erp_assistant.core.config import RetrievalRuntimeDeploymentSettings, Settings
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.operations.initialization_contracts import (
    InitializationError,
    RuntimeOption,
)
from erp_assistant.orchestration.fake_providers import (
    FakeEmbeddingProvider,
    FakeRerankProvider,
)
from erp_assistant.orchestration.providers import (
    EmbeddingProvider,
    ProviderBoundary,
    RerankProvider,
    embedding_binding_fingerprint,
)
from erp_assistant.rag.retriever import (
    retrieval_configuration,
    retrieval_configuration_fingerprint,
)
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import MilvusVectorStore


def runtime_options(
    settings: Settings, classification: DataClassification
) -> tuple[RuntimeOption, ...]:
    production = settings.app_env.casefold() in {"prod", "production"}
    options: list[RuntimeOption] = []
    if not production:
        embedding = FakeEmbeddingProvider()
        reranker = FakeRerankProvider()
        collection = (
            settings.restricted_milvus_collection
            if classification is DataClassification.RESTRICTED
            else settings.milvus_collection
        )
        options.append(
            RuntimeOption(
                key=f"local-fake-{classification.value.casefold()}",
                label="本地 Fake（仅开发）",
                classification=classification,
                provider_boundary=ProviderBoundary.INTERNAL,
                parser_version="pypdf-v1",
                chunk_version="structured-window-v1",
                embedding_deployment=embedding.deployment,
                embedding_model=embedding.model,
                embedding_dimension=embedding.dimension,
                rerank_deployment=reranker.deployment,
                rerank_model=reranker.model,
                vector_uri=settings.milvus_uri,
                collection_base=collection,
                production_ready=False,
                runtime_settings=settings,
                embedding=embedding,
                reranker=reranker,
            )
        )
    for deployment in settings.retrieval_runtime_deployments:
        if deployment.data_classification != classification.value:
            continue
        try:
            options.append(_deployment_option(settings, deployment))
        except InitializationError:
            continue
    return tuple(options)


def _deployment_option(
    settings: Settings,
    deployment: RetrievalRuntimeDeploymentSettings,
) -> RuntimeOption:
    boundary = ProviderBoundary(deployment.provider_boundary)
    if (
        deployment.data_classification == DataClassification.RESTRICTED.value
        and boundary is not ProviderBoundary.INTERNAL
    ):
        raise InitializationError(
            "RUNTIME_DEPLOYMENT_INVALID",
            "受限数据运行时必须使用 INTERNAL provider boundary",
        )
    embedding = SimpleNamespace(
        deployment=deployment.embedding.deployment,
        model=f"{deployment.embedding.model}:{deployment.embedding.version}",
        dimension=deployment.embedding.dimension,
        boundary=boundary,
    )
    reranker = SimpleNamespace(
        deployment=deployment.reranker.deployment,
        model=f"{deployment.reranker.model}:{deployment.reranker.version}",
        boundary=boundary,
    )
    identity = {
        "classification": deployment.data_classification,
        "providerBoundary": boundary.value,
        "embeddingBaseUrl": str(deployment.embedding.base_url),
        "embeddingDeployment": deployment.embedding.deployment,
        "embeddingModel": deployment.embedding.model,
        "embeddingVersion": deployment.embedding.version,
        "embeddingDimension": deployment.embedding.dimension,
        "rerankBaseUrl": str(deployment.reranker.base_url),
        "rerankDeployment": deployment.reranker.deployment,
        "rerankModel": deployment.reranker.model,
        "rerankVersion": deployment.reranker.version,
        "vectorUri": deployment.vector_index.uri,
        "collection": deployment.vector_index.collection,
        "parser": deployment.parser_version,
        "chunk": deployment.chunk_version,
        "rrfK": deployment.rrf_k,
        "candidateMultiplier": deployment.candidate_multiplier,
        "maxCandidates": deployment.max_candidates,
        "rerankThreshold": deployment.rerank_threshold,
        "chunkTarget": deployment.chunk_target or settings.knowledge_chunk_target,
        "chunkMax": deployment.chunk_max or settings.knowledge_chunk_max,
        "chunkOverlap": (
            deployment.chunk_overlap
            if deployment.chunk_overlap is not None
            else settings.knowledge_chunk_overlap
        ),
    }
    key = "runtime-" + sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    runtime_settings = settings.model_copy(
        update={
            "rag_rrf_k": deployment.rrf_k,
            "rag_candidate_multiplier": deployment.candidate_multiplier,
            "rag_max_candidates": deployment.max_candidates,
            "rag_rerank_threshold": deployment.rerank_threshold,
            "knowledge_chunk_target": (
                deployment.chunk_target
                if deployment.chunk_target is not None
                else settings.knowledge_chunk_target
            ),
            "knowledge_chunk_max": (
                deployment.chunk_max
                if deployment.chunk_max is not None
                else settings.knowledge_chunk_max
            ),
            "knowledge_chunk_overlap": (
                deployment.chunk_overlap
                if deployment.chunk_overlap is not None
                else settings.knowledge_chunk_overlap
            ),
        }
    )
    return RuntimeOption(
        key=key,
        label=(
            f"{deployment.data_classification} · {deployment.embedding.deployment} · "
            f"{deployment.vector_index.collection}"
        ),
        classification=DataClassification(deployment.data_classification),
        provider_boundary=boundary,
        parser_version=deployment.parser_version,
        chunk_version=deployment.chunk_version,
        embedding_deployment=deployment.embedding.deployment,
        embedding_model=f"{deployment.embedding.model}:{deployment.embedding.version}",
        embedding_dimension=deployment.embedding.dimension,
        rerank_deployment=deployment.reranker.deployment,
        rerank_model=f"{deployment.reranker.model}:{deployment.reranker.version}",
        vector_uri=deployment.vector_index.uri,
        collection_base=deployment.vector_index.collection,
        production_ready=True,
        runtime_settings=runtime_settings,
        embedding=cast(EmbeddingProvider, embedding),
        reranker=cast(RerankProvider, reranker),
    )


def _runtime_option(
    settings: Settings,
    classification: DataClassification,
    runtime_key: str,
) -> RuntimeOption:
    option = next(
        (item for item in runtime_options(settings, classification) if item.key == runtime_key),
        None,
    )
    if option is None:
        raise InitializationError(
            "RUNTIME_DEPLOYMENT_UNAVAILABLE",
            "所选初始化 Runtime 未通过当前环境验证",
        )
    return option


def _option_configuration_fingerprint(
    option: RuntimeOption,
    profile: RetrievalProfileRecord,
) -> str:
    vector_store = MilvusVectorStore(
        uri=option.vector_uri,
        collection=option.collection_base,
        dimension=option.embedding_dimension,
        embedding_binding=embedding_binding_fingerprint(option.embedding),
        physical_collection=profile.physical_index_locator,
    )
    configuration = retrieval_configuration(
        settings=option.runtime_settings,
        vector_store=vector_store,
        embedding=option.embedding,
        reranker=option.reranker,
    )
    return retrieval_configuration_fingerprint(configuration)

import json
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.models.knowledge import KnowledgeDocument, RetrievalProfileRecord
from erp_assistant.rag.knowledge_eligibility import retrieval_eligibility_filters
from erp_assistant.rag.types import DataClassification


@dataclass(frozen=True)
class CorpusSnapshot:
    document_ids: tuple[UUID, ...]
    corpus_hash: str

    @property
    def document_count(self) -> int:
        return len(self.document_ids)


@dataclass(frozen=True)
class IndexBuildManifest:
    build_hash: str
    corpus_hash: str
    document_count: int


async def capture_corpus_snapshot(
    session: AsyncSession,
    classification: DataClassification,
) -> CorpusSnapshot:
    rows = (
        await session.execute(
            select(
                KnowledgeDocument.id,
                KnowledgeDocument.scope,
                KnowledgeDocument.scope_id,
                KnowledgeDocument.document_key,
                KnowledgeDocument.version,
                KnowledgeDocument.sha256,
                KnowledgeDocument.governance_revision,
            )
            .where(
                *retrieval_eligibility_filters(),
                KnowledgeDocument.data_classification == classification.value,
            )
            .order_by(KnowledgeDocument.id)
        )
    ).all()
    payload = [
        {
            "id": str(row.id),
            "scope": row.scope,
            "scopeId": row.scope_id,
            "documentKey": row.document_key,
            "version": row.version,
            "sha256": row.sha256,
            "governanceRevision": row.governance_revision,
        }
        for row in rows
    ]
    corpus_hash = sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return CorpusSnapshot(
        document_ids=tuple(row.id for row in rows),
        corpus_hash=corpus_hash,
    )


def build_manifest(
    profile: RetrievalProfileRecord,
    snapshot: CorpusSnapshot,
) -> IndexBuildManifest:
    payload = {
        "profileId": str(profile.id),
        "indexIdentity": profile.index_identity,
        "parserVersion": profile.parser_version,
        "chunkVersion": profile.chunk_version,
        "configurationFingerprint": profile.configuration_fingerprint,
        "corpusHash": snapshot.corpus_hash,
        "documentCount": snapshot.document_count,
    }
    build_hash = sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return IndexBuildManifest(
        build_hash=build_hash,
        corpus_hash=snapshot.corpus_hash,
        document_count=snapshot.document_count,
    )

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.models.knowledge import (
    KnowledgeDocument,
    KnowledgeIndexJob,
    KnowledgeLifecycleEvent,
)
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.types import DataClassification, DocumentStatus, IndexJobStatus
from erp_assistant.workers.dispatcher import DocumentDispatcher, enqueue_document

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ClassificationBackfillResult:
    classified: int
    already_classified: int
    dispatched: int


async def backfill_document_classifications(
    session_factory: async_sessionmaker[AsyncSession],
    dispatch: DocumentDispatcher,
    *,
    manifest: dict[str, DataClassification],
    operator_id: str,
) -> ClassificationBackfillResult:
    _validate_manifest(manifest, operator_id)
    pending_jobs: list[tuple[UUID, UUID]] = []
    newly_classified = 0
    already_classified = 0
    async with session_factory() as session:
        async with session.begin():
            documents = (await session.scalars(select(KnowledgeDocument).with_for_update())).all()
            by_hash: dict[str, list[KnowledgeDocument]] = {}
            for document in documents:
                by_hash.setdefault(document.sha256.casefold(), []).append(document)
            missing = sorted(set(manifest) - set(by_hash))
            unclassified = sorted(
                document.sha256
                for document in documents
                if document.data_classification == DataClassification.UNCLASSIFIED.value
                and document.sha256.casefold() not in manifest
            )
            if missing or unclassified:
                raise RagError(
                    "CLASSIFICATION_MANIFEST_INCOMPLETE",
                    "分类清单含未知哈希或未覆盖全部未分类文档",
                )
            classified_by_backfill = set(
                await session.scalars(
                    select(KnowledgeLifecycleEvent.document_id).where(
                        KnowledgeLifecycleEvent.action == "CLASSIFY"
                    )
                )
            )
            for digest, classification in manifest.items():
                for document in by_hash[digest]:
                    if document.data_classification != DataClassification.UNCLASSIFIED.value:
                        if document.data_classification != classification.value:
                            raise RagError(
                                "CLASSIFICATION_CONFLICT",
                                "分类清单与已确认分类冲突",
                            )
                        already_classified += 1
                        failed_job = await session.scalar(
                            select(KnowledgeIndexJob)
                            .where(
                                KnowledgeIndexJob.document_id == document.id,
                                KnowledgeIndexJob.celery_task_id.is_(None),
                                KnowledgeIndexJob.status.in_(
                                    [
                                        IndexJobStatus.QUEUED.value,
                                        IndexJobStatus.FAILED.value,
                                    ]
                                ),
                            )
                            .order_by(KnowledgeIndexJob.created_at.desc())
                            .limit(1)
                        )
                        if failed_job is not None and document.id in classified_by_backfill:
                            failed_job.status = IndexJobStatus.QUEUED.value
                            failed_job.phase = DocumentStatus.QUEUED.value
                            failed_job.error_code = None
                            failed_job.error_detail = None
                            document.status = DocumentStatus.QUEUED.value
                            document.error_code = None
                            document.error_detail = None
                            pending_jobs.append((document.id, failed_job.id))
                        continue
                    document.data_classification = classification.value
                    newly_classified += 1
                    document.status = DocumentStatus.QUEUED.value
                    document.governance_revision += 1
                    job = KnowledgeIndexJob(
                        id=uuid4(),
                        document_id=document.id,
                        status=IndexJobStatus.QUEUED.value,
                        phase=DocumentStatus.QUEUED.value,
                    )
                    session.add_all(
                        [
                            job,
                            KnowledgeLifecycleEvent(
                                document_id=document.id,
                                action="CLASSIFY",
                                actor_id=operator_id,
                                reason=f"trusted SHA-256 manifest: {classification.value}",
                                document_version=document.version,
                                revision=document.governance_revision,
                                result="APPLIED",
                            ),
                        ]
                    )
                    pending_jobs.append((document.id, job.id))
    dispatched = 0
    dispatch_failed = False
    for document_id, job_id in pending_jobs:
        try:
            task_id = dispatch(document_id)
        except Exception as exc:
            dispatch_failed = True
            async with session_factory() as session:
                stored_job = await session.get(KnowledgeIndexJob, job_id)
                if stored_job is not None:
                    stored_job.status = IndexJobStatus.FAILED.value
                    stored_job.error_code = "CLASSIFICATION_REINDEX_DISPATCH_FAILED"
                    stored_job.error_detail = exc.__class__.__name__
                    failed_document = await session.get(KnowledgeDocument, document_id)
                    if failed_document is not None:
                        failed_document.status = DocumentStatus.FAILED.value
                        failed_document.error_code = stored_job.error_code
                        failed_document.error_detail = stored_job.error_detail
                    await session.commit()
            continue
        async with session_factory() as session:
            stored_job = await session.get(KnowledgeIndexJob, job_id)
            if stored_job is not None:
                stored_job.celery_task_id = task_id
                await session.commit()
        dispatched += 1
    if dispatch_failed:
        raise RagDependencyError(
            "CLASSIFICATION_REINDEX_DISPATCH_FAILED",
            "分类已提交，但部分重建索引任务入队失败；可使用同一清单安全重试",
        )
    return ClassificationBackfillResult(
        classified=newly_classified,
        already_classified=already_classified,
        dispatched=dispatched,
    )


def _validate_manifest(manifest: dict[str, DataClassification], operator_id: str) -> None:
    if not operator_id.strip() or not manifest:
        raise RagError("INVALID_CLASSIFICATION_MANIFEST", "分类清单或操作人无效")
    if any(not _SHA256.fullmatch(digest) for digest in manifest):
        raise RagError("INVALID_CLASSIFICATION_MANIFEST", "分类清单包含无效 SHA-256")
    if any(
        classification is DataClassification.UNCLASSIFIED for classification in manifest.values()
    ):
        raise RagError("INVALID_CLASSIFICATION_MANIFEST", "清单不能把文档标为未分类")


def _load_manifest(path: Path) -> tuple[dict[str, DataClassification], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    operator_id = str(payload["operatorId"])
    entries = payload["documents"]
    manifest = {
        str(item["sha256"]).casefold(): DataClassification(str(item["classification"]))
        for item in entries
    }
    if len(manifest) != len(entries):
        raise RagError("INVALID_CLASSIFICATION_MANIFEST", "分类清单包含重复 SHA-256")
    return manifest, operator_id


async def _run(path: Path) -> ClassificationBackfillResult:
    manifest, operator_id = _load_manifest(path)
    return await backfill_document_classifications(
        SessionFactory,
        enqueue_document,
        manifest=manifest,
        operator_id=operator_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill trusted document classifications")
    parser.add_argument("--manifest", required=True, type=Path)
    result = asyncio.run(_run(parser.parse_args().manifest))
    print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

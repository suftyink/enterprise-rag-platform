import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.api.dependencies import request_trace_id
from erp_assistant.api.knowledge_access import authorize_or_not_found
from erp_assistant.core.config import Settings
from erp_assistant.identity import ActorContext, IdentityAccess, IdentityAction, IdentityResource
from erp_assistant.infrastructure.object_storage import ObjectStorage
from erp_assistant.rag.document_parsers import validate_media_type
from erp_assistant.rag.errors import (
    DocumentValidationError,
    PdfValidationError,
    RagDependencyError,
    RagError,
)
from erp_assistant.rag.pdf_parser import PypdfParser
from erp_assistant.rag.service import DocumentCreateResult, create_document, scope_id_for
from erp_assistant.rag.types import DataClassification, KnowledgeScope
from erp_assistant.workers.dispatcher import DocumentDispatcher


@dataclass(frozen=True)
class KnowledgeUploadCommand:
    scope: KnowledgeScope
    scope_id: str | None
    document_key: str | None
    title: str | None


async def accept_document_upload(
    *,
    request: Request,
    file: UploadFile,
    actor: ActorContext,
    access: IdentityAccess,
    session: AsyncSession,
    settings: Settings,
    storage: ObjectStorage,
    dispatch: DocumentDispatcher,
    command: KnowledgeUploadCommand,
) -> DocumentCreateResult:
    if not settings.rag_enabled:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "RAG_DISABLED", "RAG 功能未启用")
    try:
        target_scope_id = scope_id_for(command.scope, actor.tenant_id, command.scope_id)
    except RagError as exc:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message) from exc
    await authorize_or_not_found(
        access,
        actor,
        (
            IdentityAction.PUBLISH_GLOBAL
            if command.scope is KnowledgeScope.GLOBAL
            else IdentityAction.UPLOAD
        ),
        IdentityResource("knowledge_scope", target_scope_id, command.scope, target_scope_id),
        request,
    )
    content, page_count, filename, media_type = await _read_document(file, settings)
    key = _document_key(command.document_key, filename)
    try:
        return await create_document(
            session,
            storage,
            dispatch,
            content=content,
            page_count=page_count,
            tenant_id=actor.tenant_id,
            operator_id=actor.subject_id,
            scope=command.scope,
            scope_id=target_scope_id,
            document_key=key,
            title=(command.title or Path(filename).stem)[:255],
            original_filename=filename,
            media_type=media_type,
            data_classification=classify_document(content, key, settings),
            trace_id=request_trace_id(request),
        )
    except RagDependencyError as exc:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message) from exc
    except RagError as exc:
        raise _error(status.HTTP_409_CONFLICT, exc.code, exc.message) from exc


def classify_document(
    content: bytes,
    document_key: str,
    settings: Settings,
) -> DataClassification:
    digest = sha256(content).hexdigest()
    if digest in settings.restricted_document_hashes:
        return DataClassification.RESTRICTED
    if digest in settings.ordinary_document_hashes:
        return DataClassification.ORDINARY
    if settings.app_env.casefold() == "production":
        raise RagError(
            "DOCUMENT_CLASSIFICATION_REQUIRED",
            "生产文档必须先在服务端受信分类清单中登记",
        )
    return (
        DataClassification.RESTRICTED
        if document_key.casefold() in settings.restricted_document_keys
        else DataClassification.ORDINARY
    )


def _document_key(value: str | None, filename: str) -> str:
    source = value or Path(filename).stem
    normalized = unicodedata.normalize("NFKC", source).strip().lower()
    normalized = re.sub(r"[^\w\-.\u3400-\u9fff]+", "-", normalized, flags=re.UNICODE)
    normalized = normalized.strip("-._")
    if not normalized:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "INVALID_DOCUMENT_KEY", "文档编号无效")
    return normalized[:128]


async def _read_document(
    file: UploadFile, settings: Settings
) -> tuple[bytes, int | None, str, str]:
    filename = Path(file.filename or "").name
    content = await file.read(settings.knowledge_max_file_size + 1)
    if len(content) > settings.knowledge_max_file_size:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "FILE_TOO_LARGE",
            "文档超过允许的大小限制",
        )
    try:
        declared = validate_media_type(file.content_type or "", content)
    except DocumentValidationError as exc:
        status_code = (
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            if exc.code == "UNSUPPORTED_MEDIA_TYPE"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise _error(status_code, exc.code, exc.message) from exc
    page_count: int | None = None
    if declared == "application/pdf":
        try:
            page_count = PypdfParser().inspect(content, settings.knowledge_max_pages)
        except PdfValidationError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message) from exc
    return content, page_count, filename, declared


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})

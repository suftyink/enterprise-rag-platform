from enum import StrEnum


class KnowledgeScope(StrEnum):
    TENANT = "TENANT"
    GLOBAL = "GLOBAL"


class DocumentStatus(StrEnum):
    UPLOADING = "UPLOADING"
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    CHUNKING = "CHUNKING"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"
    OCR_REQUIRED = "OCR_REQUIRED"
    SUPERSEDED = "SUPERSEDED"


class IndexJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SearchDecision(StrEnum):
    MATCHED = "MATCHED"
    INSUFFICIENT_KNOWLEDGE = "INSUFFICIENT_KNOWLEDGE"

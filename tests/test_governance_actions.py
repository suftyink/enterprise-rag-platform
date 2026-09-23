from uuid import UUID

import pytest

from erp_assistant.models.knowledge import KnowledgeDocument
from erp_assistant.rag.knowledge_action_policy import governance_action_decision
from erp_assistant.rag.knowledge_corpus_contracts import LifecycleAction

DOCUMENT_ID = UUID("99999999-9999-9999-9999-999999999999")
VISIBLE = {LifecycleAction.PUBLISH, LifecycleAction.WITHDRAW, LifecycleAction.ROLLBACK}


@pytest.mark.parametrize(
    ("processing", "publication", "retention", "indexed", "head_current", "running", "expected"),
    [
        ("QUEUED", "UNPUBLISHED", "RETAINED", False, False, False, set()),
        ("READY", "UNPUBLISHED", "RETAINED", True, False, False, set()),
        ("READY", "REVIEW_PENDING", "RETAINED", True, False, False, {LifecycleAction.PUBLISH}),
        ("INDEXING", "REVIEW_PENDING", "RETAINED", True, False, False, set()),
        ("READY", "REVIEW_PENDING", "PURGE_REQUESTED", True, False, False, set()),
        ("READY", "REVIEW_PENDING", "RETAINED", False, False, False, set()),
        ("READY", "REVIEW_PENDING", "RETAINED", True, False, True, set()),
        ("READY", "PUBLISHED", "RETAINED", True, True, False, {LifecycleAction.WITHDRAW}),
        ("READY", "PUBLISHED", "RETAINED", True, False, False, set()),
        ("READY", "SUPERSEDED", "RETAINED", True, False, False, {LifecycleAction.ROLLBACK}),
        ("READY", "SUPERSEDED", "RETAINED", True, False, True, set()),
        ("READY", "WITHDRAWN", "RETAINED", True, False, False, {LifecycleAction.ROLLBACK}),
        ("READY", "WITHDRAWN", "RETAINED", False, False, False, set()),
        ("READY", "REJECTED", "RETAINED", True, False, False, set()),
    ],
)
def test_visible_governance_actions_match_lifecycle_state(
    processing: str,
    publication: str,
    retention: str,
    indexed: bool,
    head_current: bool,
    running: bool,
    expected: set[LifecycleAction],
) -> None:
    document = KnowledgeDocument(
        id=DOCUMENT_ID,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key="policy",
        version=1,
        title="Policy",
        original_filename="policy.md",
        media_type="text/markdown",
        data_classification="ORDINARY",
        size_bytes=1,
        sha256="a" * 64,
        object_key="knowledge/policy.md",
        status=processing,
        active=publication == "PUBLISHED",
        publication_status=publication,
        retention_status=retention,
        index_artifacts_present=indexed,
        created_by="reviewer",
    )

    decision = governance_action_decision(
        document,
        DOCUMENT_ID if head_current else None,
        cleanup_running=running,
    )

    assert set(decision.allowed_actions) & VISIBLE == expected
    assert (decision.unavailable_reason is None) is bool(expected)

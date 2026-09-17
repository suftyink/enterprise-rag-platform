import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import uvicorn
from playwright.sync_api import Browser, Page, Route, sync_playwright
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.api.dependencies import get_identity_access, get_knowledge_assistant
from erp_assistant.core.config import Settings, get_settings
from erp_assistant.identity import IdentityAccess, IdentityMembership, IdentityRole
from erp_assistant.identity.composition import DeterministicIdentityVerifier
from erp_assistant.identity.directory import StaticIdentityDirectory
from erp_assistant.identity.models import SecurityAuditEvent
from erp_assistant.infrastructure.database import Base, get_session
from erp_assistant.infrastructure.object_storage import get_object_storage
from erp_assistant.main import app
from erp_assistant.models.knowledge import KnowledgeDocument, RetrievalProfileRecord
from erp_assistant.models.operations import (
    CorpusReleaseAuditRecord,
    CorpusReleaseRepairRequestRecord,
    PilotAccessRecord,
    PilotReadinessRecord,
)
from erp_assistant.operations.readiness_models import ReleaseIdentity
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.index_manifest import build_manifest, capture_corpus_snapshot
from erp_assistant.rag.knowledge_assistant import (
    AnswerCitation,
    AnswerDecision,
    AnswerOutcome,
    AnswerRequest,
    AnswerUnit,
    FeedbackReceipt,
    FeedbackRequest,
)
from erp_assistant.rag.knowledge_corpus_contracts import (
    CorpusChunkSummary,
    CorpusDocumentView,
    LifecycleAction,
)
from erp_assistant.rag.types import (
    DataClassification,
    DocumentStatus,
    PublicationStatus,
    RetentionStatus,
)
from erp_assistant.web.pilot import PilotAccessDecision, PilotAccessGate
from erp_assistant.web.release_routes import get_release_knowledge_corpus
from erp_assistant.web.routes import get_web_knowledge_corpus
from erp_assistant.web.sessions import (
    OidcTransaction,
    WebSession,
    get_web_session_store,
    new_oidc_transaction,
    oidc_authorization_url,
)
from erp_assistant.workers.dispatcher import get_document_dispatcher

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
ACTIVE_PROFILE_ID = UUID("22222222-2222-2222-2222-222222222222")
CANDIDATE_PROFILE_ID = UUID("33333333-3333-3333-3333-333333333333")
RELEASE_DOCUMENT_ID = UUID("44444444-4444-4444-4444-444444444444")
SECOND_RELEASE_DOCUMENT_ID = UUID("77777777-7777-7777-7777-777777777777")


@dataclass
class MemorySessionStore:
    sessions: dict[str, WebSession] = field(default_factory=dict)
    transactions: dict[str, OidcTransaction] = field(default_factory=dict)

    async def put_session(self, session_id: str, value: WebSession, ttl: int) -> None:
        del ttl
        self.sessions[session_id] = value

    async def get_session(self, session_id: str) -> WebSession | None:
        return self.sessions.get(session_id)

    async def delete_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    async def put_transaction(
        self, transaction_id: str, value: OidcTransaction, ttl: int
    ) -> None:
        del ttl
        self.transactions[transaction_id] = value

    async def pop_transaction(self, transaction_id: str) -> OidcTransaction | None:
        return self.transactions.pop(transaction_id, None)


@dataclass
class FakeAssistant:
    feedback: list[FeedbackRequest] = field(default_factory=list)
    requests: list[AnswerRequest] = field(default_factory=list)

    async def ask(self, request: AnswerRequest) -> AnswerOutcome:
        self.requests.append(request)
        if "故障" in request.question:
            raise RagDependencyError("MODEL_UNAVAILABLE", "回答模型暂时不可用")
        if "无权限" in request.question:
            raise RagError("RESOURCE_NOT_FOUND", "资源不存在")
        if "无效" in request.question:
            raise ValueError("invalid question")
        if "不足" in request.question:
            return AnswerOutcome(
                trace_id=request.trace_id,
                decision=AnswerDecision.REFUSED,
                units=(),
                citations=(),
                reason="INSUFFICIENT_KNOWLEDGE",
            )
        return AnswerOutcome(
            trace_id=request.trace_id,
            decision=AnswerDecision.ANSWERED,
            units=(
                AnswerUnit(
                    text=(
                        "<img src=x onerror=window.__injected=true> "
                        "欧洲仓差异应在 2 个工作日内复核。"
                    ),
                    evidence_ids=("E1",),
                ),
            ),
            citations=(
                AnswerCitation(
                    evidence_id="E1",
                    document_id=RELEASE_DOCUMENT_ID,
                    document_key="eu-warehouse-sop",
                    document_version=3,
                    title="欧洲仓库存差异处理规范",
                    page_start=7,
                    page_end=7,
                    section_title="复核时限",
                    heading_path=("库存治理", "复核时限"),
                    anchor="section-4-2",
                    source_locator={"page": 7},
                    excerpt="运营人员应在发现差异后的 2 个工作日内完成复核。",
                ),
            ),
        )

    async def submit_feedback(self, request: FeedbackRequest) -> FeedbackReceipt:
        self.feedback.append(request)
        return FeedbackReceipt(
            feedback_id=uuid4(),
            trace_id=request.trace_id,
            answer_decision=AnswerDecision.ANSWERED,
            rating=request.rating,
            reason=request.reason,
            revision=1,
            requires_review=False,
        )


@dataclass
class FakeStorage:
    objects: dict[str, bytes] = field(default_factory=dict)

    def put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        del content_type
        self.objects[key] = content

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


@dataclass
class FakeCorpus:
    transitions: list[tuple[UUID, LifecycleAction, str]] = field(default_factory=list)

    async def inspect(
        self,
        document_id: UUID,
        actor: object,
        *,
        trace_id: UUID,
    ) -> CorpusDocumentView:
        del actor, trace_id
        return self._view(document_id, PublicationStatus.PUBLISHED)

    async def transition(
        self,
        document_id: UUID,
        action: LifecycleAction,
        actor: object,
        *,
        reason: str,
        expected_revision: int,
        trace_id: UUID,
    ) -> CorpusDocumentView:
        del actor, expected_revision, trace_id
        self.transitions.append((document_id, action, reason))
        return self._view(document_id, PublicationStatus.PUBLISHED)

    @staticmethod
    def _view(
        document_id: UUID,
        publication_status: PublicationStatus,
    ) -> CorpusDocumentView:
        return CorpusDocumentView(
            document_id=document_id,
            document_key="pilot-guide",
            version=1,
            title="试点操作指南",
            original_filename="pilot.md",
            media_type="text/markdown",
            size_bytes=100,
            sha256="b" * 64,
            processing_status=DocumentStatus.READY,
            publication_status=publication_status,
            retention_status=RetentionStatus.RETAINED,
            revision=1,
            retrieval_eligible=True,
            warnings=(),
            pipeline_revision="pipeline-v1",
            parser_version="markdown-v1",
            chunking_version="chunk-v1",
            quality_signals={},
            chunk_summary=CorpusChunkSummary(1, 100, None, None),
            audit_events=(),
            cleanup_requests=(),
        )


@dataclass
class WebHarness:
    base_url: str
    assistant: FakeAssistant
    corpus: FakeCorpus
    storage: FakeStorage
    server: uvicorn.Server
    thread: threading.Thread
    engine: object
    sessions: async_sessionmaker[AsyncSession]


class NullAudit:
    async def append(self, event: SecurityAuditEvent) -> None:
        del event


@pytest.fixture
def internal_web_server(request: pytest.FixtureRequest) -> Iterator[WebHarness]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def prepare() -> async_sessionmaker[AsyncSession]:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        report_id = UUID("11111111-1111-1111-1111-111111111111")
        if "release_workflow" in request.node.name:
            await _prepare_release_workflow(factory)
        if "without_readiness" not in request.node.name:
            async with factory() as session:
                session.add_all(
                    [
                        PilotReadinessRecord(
                            id=report_id,
                            report_version="pilot-readiness-v1",
                            release_version="spec10-test",
                            release_fingerprint="a" * 64,
                            decision="PASS",
                            generated_at=datetime.now(UTC),
                            payload={"decision": "PASS"},
                        ),
                        PilotAccessRecord(
                            id=uuid4(),
                            enabled=True,
                            readiness_report_id=report_id,
                            release_fingerprint="a" * 64,
                            approved_by="system-admin",
                            pilot_scope="内部验收组",
                            capacity="10 RPS",
                            monitoring_owner="ops-owner",
                            rollback_owner="release-owner",
                            reason="浏览器验收准备",
                            created_at=datetime.now(UTC),
                        ),
                    ]
                )
                await session.commit()
        return factory

    sessions = asyncio.run(prepare())
    is_admin_scenario = "admin_" in request.node.name
    subject_id = "system-admin" if is_admin_scenario else "employee-user"
    membership = IdentityMembership(
        tenant_id="tenant-a",
        roles=frozenset(
            {
                IdentityRole.SYSTEM_ADMIN
                if is_admin_scenario
                else IdentityRole.INTERNAL_EMPLOYEE
            }
        ),
        knowledge_scopes=frozenset(),
    )
    access = IdentityAccess(
        verifier=DeterministicIdentityVerifier(),
        directory=StaticIdentityDirectory({subject_id: membership}),
        audit=NullAudit(),
    )
    settings = Settings(
        rag_enabled=True,
        internal_web_test_subject=subject_id,
        pilot_release_fingerprint="a" * 64,
    )
    store = MemorySessionStore()
    assistant = FakeAssistant()
    corpus = FakeCorpus()
    storage = FakeStorage()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_identity_access] = lambda: access
    app.dependency_overrides[get_web_session_store] = lambda: store
    app.dependency_overrides[get_knowledge_assistant] = lambda: assistant
    app.dependency_overrides[get_web_knowledge_corpus] = lambda: corpus
    app.dependency_overrides[get_release_knowledge_corpus] = lambda: corpus
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_document_dispatcher] = lambda: (
        lambda document_id, trace_id: f"task-{document_id}-{trace_id}"
    )

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("test web server did not start")
    harness = WebHarness(
        base_url=f"http://127.0.0.1:{port}",
        assistant=assistant,
        corpus=corpus,
        storage=storage,
        server=server,
        thread=thread,
        engine=engine,
        sessions=sessions,
    )
    try:
        yield harness
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


@pytest.fixture
def browser() -> Iterator[Browser]:
    if not CHROME.exists():
        pytest.skip("system Chrome is required for the browser workflow seam")
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            executable_path=str(CHROME),
            headless=True,
            args=["--disable-gpu"],
        )
        try:
            yield instance
        finally:
            instance.close()


def test_employee_answer_refusal_failure_feedback_and_safe_citations(
    internal_web_server: WebHarness,
    browser: Browser,
) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{internal_web_server.base_url}/internal")
    page.wait_for_url("**/internal")
    page.get_by_text("employee-user · tenant-a").wait_for()
    assert page.get_by_role("button", name="知识治理").is_hidden()
    forged_status = page.evaluate(
        """async () => (await fetch('/internal/api/documents', {
          credentials: 'same-origin'
        })).status"""
    )
    assert forged_status == 403
    release_status = page.evaluate(
        """async () => (await fetch('/internal/api/releases', {
          credentials: 'same-origin'
        })).status"""
    )
    assert release_status == 403
    csrf_status = page.evaluate(
        """async () => (await fetch('/internal/api/answers', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({question: '绕过 CSRF'})
        })).status"""
    )
    assert csrf_status == 403
    cookies = page.context.cookies()
    session_cookie = next(item for item in cookies if item["name"] == "erp_internal_session")
    assert session_cookie["httpOnly"] is True

    page.get_by_label("你的业务问题").fill("欧洲仓差异复核时限是多少？")
    page.get_by_role("button", name="查询知识").click()
    page.get_by_role("heading", name="知识回答").wait_for()
    assert page.get_by_text("欧洲仓差异应在 2 个工作日内复核。").is_visible()
    assert page.evaluate("window.__injected") is None
    assert page.locator("#answer-trace").inner_text() != "—"

    page.get_by_role("button", name="打开引用 1").first.click()
    assert page.get_by_text("欧洲仓库存差异处理规范").last.is_visible()
    assert page.get_by_text("v3").is_visible()
    assert page.locator("#citation-detail").get_by_text("第 7 页").is_visible()
    page.get_by_role("button", name="关闭引用详情").click()

    page.get_by_role("radio", name="有帮助", exact=True).check()
    page.get_by_label("补充说明（可选）").fill("引用位置清楚")
    page.get_by_role("button", name="提交反馈").click()
    page.get_by_text("反馈已记录").wait_for()
    assert internal_web_server.assistant.feedback[0].reason == "引用位置清楚"

    page.get_by_label("你的业务问题").fill("资料不足的问题")
    page.get_by_role("button", name="查询知识").click()
    page.get_by_text("证据不足：").wait_for()
    assert "系统错误" not in page.locator("#answer-state").inner_text()

    page.get_by_label("你的业务问题").fill("模拟故障")
    page.get_by_role("button", name="查询知识").click()
    page.get_by_text("临时系统故障").wait_for()
    assert page.get_by_role("button", name="查询知识").is_enabled()

    page.get_by_label("你的业务问题").fill("无效问题")
    page.get_by_role("button", name="查询知识").click()
    page.get_by_text("输入未通过校验").wait_for()

    page.get_by_label("你的业务问题").fill("无权限问题")
    page.get_by_role("button", name="查询知识").click()
    page.get_by_text("无权访问").wait_for()

    page.set_viewport_size({"width": 375, "height": 812})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    page.get_by_label("你的业务问题").focus()
    page.keyboard.press("Tab")
    assert page.get_by_role("button", name="查询知识").evaluate(
        "element => element === document.activeElement"
    )


def test_admin_upload_governance_confirmation_and_pilot_controls(
    internal_web_server: WebHarness,
    browser: Browser,
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.goto(f"{internal_web_server.base_url}/internal")
    page.get_by_text("system-admin · tenant-a").wait_for()

    page.get_by_role("button", name="知识治理").click()
    page.get_by_label("选择资料").set_input_files(
        {
            "name": "pilot.md",
            "mimeType": "text/markdown",
            "buffer": "# 试点操作指南\n\n发布前必须核对解析警告和版本。".encode(),
        }
    )
    page.get_by_label("资料标题").fill("试点操作指南")
    page.get_by_label("文档编号").fill("pilot-guide")
    page.get_by_role("button", name="上传并开始处理").click()
    page.wait_for_function("document.querySelector('#upload-status').textContent.length > 0")
    assert "上传失败" not in page.locator("#upload-status").inner_text()
    page.get_by_text("已接收 试点操作指南").wait_for()
    card = page.locator(".document-card", has_text="试点操作指南")
    assert card.is_visible()
    assert card.get_by_text("UNPUBLISHED").is_visible()
    assert card.get_by_text("处理阶段").is_visible()
    assert card.get_by_text("任务结果").is_visible()
    assert card.get_by_text("QUEUED", exact=True).first.is_visible()
    assert card.get_by_text("分块数量").is_visible()
    assert internal_web_server.storage.objects

    progress_requests = 0

    def advance_document_progress(route: Route) -> None:
        nonlocal progress_requests
        upstream = route.fetch()
        documents = upstream.json()
        progress_requests += 1
        job_status, phase, chunk_count = (
            ("RUNNING", "INDEXING", None)
            if progress_requests == 1
            else ("SUCCEEDED", "READY", 4)
        )
        for document in documents:
            if document["documentKey"] == "pilot-guide":
                document["jobStatus"] = job_status
                document["phase"] = phase
                document["chunkCount"] = chunk_count
        route.fulfill(response=upstream, body=json.dumps(documents))

    page.route("**/internal/api/documents", advance_document_progress)
    page.get_by_role("button", name="刷新列表").click()
    card.get_by_text("RUNNING", exact=True).wait_for()
    card.get_by_text("SUCCEEDED", exact=True).wait_for(timeout=5_000)
    assert progress_requests >= 2
    page.unroute("**/internal/api/documents", advance_document_progress)

    for label, action in (
        ("批准入候选", LifecycleAction.PUBLISH),
        ("撤回", LifecycleAction.WITHDRAW),
        ("回滚", LifecycleAction.ROLLBACK),
    ):
        card = page.locator(".document-card", has_text="试点操作指南")
        card.get_by_role("button", name=label, exact=True).click()
        page.get_by_label("操作原因").fill(f"完成{label}验收")
        page.get_by_role("button", name="确认执行").click()
        page.get_by_text(f"{label}操作已完成并写入审计").wait_for()
        assert internal_web_server.corpus.transitions[-1][1] is action

    page.get_by_role("button", name="试点门禁").click()
    page.get_by_label("试点范围").fill("华东运营组 30 人")
    page.get_by_label("容量约束").fill("10 RPS / 30 并发")
    page.get_by_label("监控负责人").fill("ops-owner")
    page.get_by_label("回滚负责人").fill("release-owner")
    page.get_by_label("批准原因").fill("生产类证据已通过")
    page.get_by_role("button", name="关闭试点").click()
    page.get_by_text("试点已关闭，知识数据未删除").wait_for()
    assert internal_web_server.storage.objects


def test_admin_governance_works_without_readiness_while_answers_remain_gated(
    internal_web_server: WebHarness,
    browser: Browser,
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.goto(f"{internal_web_server.base_url}/internal")
    page.get_by_text("system-admin · tenant-a").wait_for()
    page.get_by_text("员工问答暂未开放，知识治理仍可使用").wait_for()

    page.get_by_role("button", name="知识治理").click()
    page.get_by_label("选择资料").set_input_files(
        {
            "name": "bootstrap.md",
            "mimeType": "text/markdown",
            "buffer": "# 启动知识\n\n管理员可在试点关闭时准备候选语料。".encode(),
        }
    )
    page.get_by_label("资料标题").fill("启动知识")
    page.get_by_label("文档编号").fill("bootstrap-guide")
    page.get_by_role("button", name="上传并开始处理").click()
    page.get_by_text("已接收 启动知识").wait_for()

    page.get_by_role("button", name="问答").click()
    page.get_by_label("你的业务问题").fill("试点关闭时仍不能问答")
    with page.expect_response(
        lambda response: response.url.endswith("/internal/api/answers")
    ) as response_info:
        page.get_by_role("button", name="查询知识").click()
    assert response_info.value.status == 503
    page.get_by_text("内部试点当前未开放，请联系试点负责人").wait_for()
    assert internal_web_server.storage.objects


def test_admin_release_workflow_activates_canaries_rolls_back_and_withdraws(
    internal_web_server: WebHarness,
    browser: Browser,
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.goto(f"{internal_web_server.base_url}/internal")
    page.get_by_text("system-admin · tenant-a").wait_for()
    csrf = page.evaluate(
        "async () => (await (await fetch('/internal/api/session')).json()).csrfToken"
    )

    bootstrap = _browser_api(
        page,
        csrf,
        "/internal/api/releases/bootstrap",
        {"profileId": str(ACTIVE_PROFILE_ID)},
    )
    assert bootstrap["status"] == "ACTIVE"

    candidate = _browser_api(
        page,
        csrf,
        "/internal/api/releases",
        {
            "releaseVersion": "spec11-candidate-v1",
            "profileId": str(CANDIDATE_PROFILE_ID),
        },
    )
    assert candidate["status"] == "BUILDING"
    candidate_id = candidate["releaseId"]

    assert _browser_api_error(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/sync-build",
        {},
    ) == (409, "CANDIDATE_BUILD_STALE")
    _complete_candidate_build_sync(internal_web_server.sessions)
    built = _browser_api(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/sync-build",
        {},
    )
    assert built["status"] == "EVALUATING"

    rejected = _browser_api_error(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/activate",
        {
            "pilotScope": "内部 QA",
            "capacity": "10 RPS",
            "monitoringOwner": "ops-owner",
            "rollbackOwner": "release-owner",
            "reason": "证据尚未绑定",
        },
    )
    assert rejected == (409, "RELEASE_NOT_READY")

    _set_candidate_report_sync(internal_web_server.sessions, stale=True)
    assert _browser_api_error(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/readiness",
        {},
    ) == (409, "READINESS_REPORT_STALE")
    _set_candidate_report_sync(internal_web_server.sessions, mismatch=True)
    assert _browser_api_error(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/readiness",
        {},
    ) == (409, "RELEASE_EVIDENCE_MISMATCH")
    _set_candidate_report_sync(internal_web_server.sessions)

    ready = _browser_api(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/readiness",
        {},
    )
    assert ready["status"] == "READY"

    _set_activation_failure_trigger_sync(internal_web_server.sessions, enabled=True)
    assert _browser_api_error(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/activate",
        {
            "pilotScope": "内部 QA",
            "capacity": "10 RPS",
            "monitoringOwner": "ops-owner",
            "rollbackOwner": "release-owner",
            "reason": "注入提交前故障",
        },
    ) == (409, "ACTIVATION_COMMIT_FAILED")
    releases_after_failure = page.evaluate(
        "async () => await (await fetch('/internal/api/releases')).json()"
    )
    assert next(
        item for item in releases_after_failure if item["releaseId"] == bootstrap["releaseId"]
    )["status"] == "ACTIVE"
    assert next(
        item for item in releases_after_failure if item["releaseId"] == candidate_id
    )["status"] == "READY"
    _set_activation_failure_trigger_sync(internal_web_server.sessions, enabled=False)

    activated = _browser_api(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/activate",
        {
            "pilotScope": "内部 QA",
            "capacity": "10 RPS",
            "monitoringOwner": "ops-owner",
            "rollbackOwner": "release-owner",
            "reason": "候选证据通过",
        },
    )
    assert activated["status"] == "ACTIVE"
    assert activated["previousReleaseId"] == bootstrap["releaseId"]
    assert activated["rolloutState"] == "DISABLED"

    excluded_canary = _browser_api(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/rollout",
        {
            "state": "CANARY",
            "subjectIds": ["another-employee"],
            "reason": "先验证未命中员工保持关闭",
        },
    )
    assert excluded_canary["rolloutState"] == "CANARY"
    excluded_pilot = page.evaluate(
        "async () => await (await fetch('/internal/api/pilot')).json()"
    )
    assert excluded_pilot["enabled"] is False
    assert excluded_pilot["reasonCode"] == "PILOT_COHORT_DENIED"

    canary = _browser_api(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/rollout",
        {
            "state": "CANARY",
            "subjectIds": ["system-admin"],
            "reason": "先对系统管理员开放",
        },
    )
    assert canary["rolloutState"] == "CANARY"
    pilot = page.evaluate("async () => await (await fetch('/internal/api/pilot')).json()")
    assert pilot["enabled"] is True
    assert pilot["releaseId"] == candidate_id
    answer = _browser_api(
        page,
        csrf,
        "/internal/api/answers",
        {"question": "当前发布版本的处理时限是什么？"},
    )
    assert answer["releaseId"] == candidate_id
    assert answer["releaseVersion"] == "spec11-candidate-v1"
    assert answer["citations"][0]["documentId"] == str(RELEASE_DOCUMENT_ID)

    paused = _browser_api(
        page,
        csrf,
        f"/internal/api/releases/{candidate_id}/rollout",
        {"state": "PAUSED", "subjectIds": [], "reason": "观测窗口暂停"},
    )
    assert paused["rolloutState"] == "PAUSED"
    paused_pilot = page.evaluate(
        "async () => await (await fetch('/internal/api/pilot')).json()"
    )
    assert paused_pilot["enabled"] is False
    assert paused_pilot["reasonCode"] == "PILOT_PAUSED"
    assert isinstance(
        page.evaluate("async () => await (await fetch('/internal/api/releases')).json()"),
        list,
    )

    rolled_back = _browser_api(
        page,
        csrf,
        f"/internal/api/releases/{bootstrap['releaseId']}/rollback",
        {"reason": "演练回滚"},
    )
    assert rolled_back["status"] == "ACTIVE"
    assert rolled_back["rolloutState"] == "PAUSED"

    unrelated_document = UUID("66666666-6666-6666-6666-666666666666")
    assert _browser_api_error(
        page,
        csrf,
        (
            f"/internal/api/releases/{bootstrap['releaseId']}/documents/"
            f"{unrelated_document}/emergency-withdraw"
        ),
        {"reason": "不应影响非成员文档"},
    ) == (409, "DOCUMENT_NOT_IN_RELEASE")
    assert internal_web_server.corpus.transitions == []

    degraded = _browser_api(
        page,
        csrf,
        (
            f"/internal/api/releases/{bootstrap['releaseId']}/documents/"
            f"{RELEASE_DOCUMENT_ID}/emergency-withdraw"
        ),
        {"reason": "发现敏感内容"},
    )
    assert degraded["status"] == "DEGRADED"
    assert degraded["rolloutState"] == "PAUSED"
    assert degraded["degradedReason"] == "EMERGENCY_WITHDRAWAL"
    assert degraded["repairPending"] is True
    assert degraded["repairExcludedDocumentIds"] == [str(RELEASE_DOCUMENT_ID)]
    requests_before_closed_answer = len(internal_web_server.assistant.requests)
    assert _browser_api_error(
        page,
        csrf,
        "/internal/api/answers",
        {"question": "撤回后不应再返回目标引用"},
    ) == (503, "RELEASE_DEGRADED")
    assert len(internal_web_server.assistant.requests) == requests_before_closed_answer

    degraded_again = _browser_api(
        page,
        csrf,
        (
            f"/internal/api/releases/{bootstrap['releaseId']}/documents/"
            f"{SECOND_RELEASE_DOCUMENT_ID}/emergency-withdraw"
        ),
        {"reason": "发现第二份风险内容"},
    )
    assert degraded_again["status"] == "DEGRADED"
    assert set(degraded_again["repairExcludedDocumentIds"]) == {
        str(RELEASE_DOCUMENT_ID),
        str(SECOND_RELEASE_DOCUMENT_ID),
    }
    assert _release_repair_request_count_sync(internal_web_server.sessions) == 2

    releases = page.evaluate("async () => await (await fetch('/internal/api/releases')).json()")
    assert {item["releaseVersion"] for item in releases} == {
        "bootstrap-current-v1",
        "spec11-candidate-v1",
    }
    audit_actions = _release_audit_actions_sync(internal_web_server.sessions)
    assert {
        "BOOTSTRAP",
        "CREATE_CANDIDATE",
        "BIND_READINESS",
        "ACTIVATE",
        "SET_ROLLOUT",
        "ROLLBACK",
        "EMERGENCY_WITHDRAWAL",
    } <= audit_actions


def _browser_api(
    page: Page, csrf: str, path: str, payload: dict[str, object]
) -> dict[str, object]:
    result = page.evaluate(
        """async ({path, csrf, payload}) => {
          const response = await fetch(path, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
            body: JSON.stringify(payload)
          });
          const body = await response.json();
          if (!response.ok) throw new Error(`${response.status}:${JSON.stringify(body)}`);
          return body;
        }""",
        {"path": path, "csrf": csrf, "payload": payload},
    )
    assert isinstance(result, dict)
    return result


def _browser_api_error(
    page: Page, csrf: str, path: str, payload: dict[str, object]
) -> tuple[int, str]:
    result = page.evaluate(
        """async ({path, csrf, payload}) => {
          const response = await fetch(path, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
            body: JSON.stringify(payload)
          });
          const body = await response.json();
          return [response.status, body.detail.code];
        }""",
        {"path": path, "csrf": csrf, "payload": payload},
    )
    return int(result[0]), str(result[1])


async def _release_audit_actions(
    factory: async_sessionmaker[AsyncSession],
) -> set[str]:
    async with factory() as session:
        return set(await session.scalars(select(CorpusReleaseAuditRecord.action)))


def _release_audit_actions_sync(
    factory: async_sessionmaker[AsyncSession],
) -> set[str]:
    results: list[set[str]] = []
    errors: list[BaseException] = []

    def load() -> None:
        try:
            results.append(asyncio.run(_release_audit_actions(factory)))
        except BaseException as exc:  # pragma: no cover - re-raised in caller
            errors.append(exc)

    thread = threading.Thread(target=load)
    thread.start()
    thread.join(timeout=10)
    if errors:
        raise errors[0]
    assert results
    return results[0]


async def _set_activation_failure_trigger(
    factory: async_sessionmaker[AsyncSession], *, enabled: bool
) -> None:
    async with factory() as session:
        if enabled:
            await session.execute(
                text(
                    """
                    CREATE TRIGGER fail_candidate_activation
                    BEFORE UPDATE OF status ON corpus_release
                    WHEN OLD.status = 'READY' AND NEW.status = 'ACTIVE'
                    BEGIN
                      SELECT RAISE(ABORT, 'injected activation failure');
                    END
                    """
                )
            )
        else:
            await session.execute(text("DROP TRIGGER fail_candidate_activation"))
        await session.commit()


def _set_activation_failure_trigger_sync(
    factory: async_sessionmaker[AsyncSession], *, enabled: bool
) -> None:
    errors: list[BaseException] = []

    def update() -> None:
        try:
            asyncio.run(_set_activation_failure_trigger(factory, enabled=enabled))
        except BaseException as exc:  # pragma: no cover - re-raised in caller
            errors.append(exc)

    thread = threading.Thread(target=update)
    thread.start()
    thread.join(timeout=10)
    if errors:
        raise errors[0]


async def _release_repair_request_count(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    async with factory() as session:
        return len((await session.scalars(select(CorpusReleaseRepairRequestRecord))).all())


def _release_repair_request_count_sync(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    results: list[int] = []
    errors: list[BaseException] = []

    def load() -> None:
        try:
            results.append(asyncio.run(_release_repair_request_count(factory)))
        except BaseException as exc:  # pragma: no cover - re-raised in caller
            errors.append(exc)

    thread = threading.Thread(target=load)
    thread.start()
    thread.join(timeout=10)
    if errors:
        raise errors[0]
    assert results
    return results[0]


async def _complete_candidate_build(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        profile = await session.get_one(RetrievalProfileRecord, CANDIDATE_PROFILE_ID)
        snapshot = await capture_corpus_snapshot(session, DataClassification.ORDINARY)
        manifest = build_manifest(profile, snapshot)
        profile.index_build_hash = manifest.build_hash
        profile.index_build_corpus_hash = manifest.corpus_hash
        profile.index_build_document_count = manifest.document_count
        profile.index_build_completed_at = datetime.now(UTC)
        await session.commit()


def _complete_candidate_build_sync(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    errors: list[BaseException] = []

    def update() -> None:
        try:
            asyncio.run(_complete_candidate_build(factory))
        except BaseException as exc:  # pragma: no cover - re-raised in caller
            errors.append(exc)

    thread = threading.Thread(target=update)
    thread.start()
    thread.join(timeout=10)
    if errors:
        raise errors[0]


async def _set_candidate_report(
    factory: async_sessionmaker[AsyncSession],
    *,
    stale: bool = False,
    mismatch: bool = False,
) -> None:
    async with factory() as session:
        report = await session.scalar(
            select(PilotReadinessRecord).where(
                PilotReadinessRecord.release_version == "spec11-candidate-v1"
            )
        )
        assert report is not None
        report.generated_at = datetime.now(UTC) - (
            timedelta(hours=25) if stale else timedelta()
        )
        payload = json.loads(json.dumps(report.payload))
        release = payload["release"]
        assert isinstance(release, dict)
        release["indexBuildHash"] = (
            "0" * 64
            if mismatch
            else build_manifest(
                await session.get_one(RetrievalProfileRecord, CANDIDATE_PROFILE_ID),
                await capture_corpus_snapshot(session, DataClassification.ORDINARY),
            ).build_hash
        )
        report.payload = payload
        await session.commit()


def _set_candidate_report_sync(
    factory: async_sessionmaker[AsyncSession],
    *,
    stale: bool = False,
    mismatch: bool = False,
) -> None:
    errors: list[BaseException] = []

    def update() -> None:
        try:
            asyncio.run(_set_candidate_report(factory, stale=stale, mismatch=mismatch))
        except BaseException as exc:  # pragma: no cover - re-raised in caller
            errors.append(exc)

    thread = threading.Thread(target=update)
    thread.start()
    thread.join(timeout=10)
    if errors:
        raise errors[0]


async def _prepare_release_workflow(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add_all(
            [
                KnowledgeDocument(
                id=RELEASE_DOCUMENT_ID,
                scope="TENANT",
                scope_id="tenant-a",
                tenant_id="tenant-a",
                document_key="release-guide",
                version=1,
                title="发布指南",
                original_filename="release.md",
                media_type="text/markdown",
                data_classification="ORDINARY",
                size_bytes=100,
                sha256="d" * 64,
                object_key="knowledge/release.md",
                status="READY",
                active=True,
                publication_status="PUBLISHED",
                retention_status="RETAINED",
                governance_revision=1,
                index_artifacts_present=True,
                created_by="system-admin",
                ),
                KnowledgeDocument(
                    id=SECOND_RELEASE_DOCUMENT_ID,
                    scope="TENANT",
                    scope_id="tenant-a",
                    tenant_id="tenant-a",
                    document_key="release-guide-secondary",
                    version=1,
                    title="发布补充指南",
                    original_filename="release-secondary.md",
                    media_type="text/markdown",
                    data_classification="ORDINARY",
                    size_bytes=80,
                    sha256="f" * 64,
                    object_key="knowledge/release-secondary.md",
                    status="READY",
                    active=True,
                    publication_status="PUBLISHED",
                    retention_status="RETAINED",
                    governance_revision=1,
                    index_artifacts_present=True,
                    created_by="system-admin",
                ),
            ]
        )
        profiles = [
            RetrievalProfileRecord(
                id=profile_id,
                profile_key=key,
                version=1,
                data_classification="ORDINARY",
                provider_boundary="INTERNAL",
                embedding_deployment="embedding-v1",
                embedding_model="embedding-model",
                embedding_dimension=3,
                rerank_deployment="rerank-v1",
                rerank_model="rerank-model",
                index_identity=index_identity,
                parser_version="parser-v1",
                chunk_version="chunk-v1",
                retrieval_config={"rrfK": 60},
                configuration_fingerprint="c" * 64,
                production_ready=True,
                status=status,
            )
            for profile_id, key, index_identity, status in (
                (ACTIVE_PROFILE_ID, "current", "milvus-v3:current", "ACTIVE"),
                (CANDIDATE_PROFILE_ID, "candidate", "milvus-v3:candidate", "STAGING"),
            )
        ]
        session.add_all(profiles)
        await session.commit()
        snapshot = await capture_corpus_snapshot(session, DataClassification.ORDINARY)
        candidate_manifest = None
        for profile in profiles:
            manifest = build_manifest(profile, snapshot)
            if profile.id == CANDIDATE_PROFILE_ID:
                candidate_manifest = manifest
                continue
            profile.index_build_hash = manifest.build_hash
            profile.index_build_corpus_hash = manifest.corpus_hash
            profile.index_build_document_count = manifest.document_count
            profile.index_build_completed_at = datetime.now(UTC)
        assert candidate_manifest is not None
        identity = ReleaseIdentity(
            release_version="spec11-candidate-v1",
            dataset_version="pilot-dataset-v1",
            dataset_hash="e" * 64,
            index_identity=profiles[1].index_identity,
            index_build_hash=candidate_manifest.build_hash,
            parser_version=profiles[1].parser_version,
            chunk_version=profiles[1].chunk_version,
            configuration_fingerprint=profiles[1].configuration_fingerprint,
            route_fingerprints=frozenset({"ORDINARY:retrieval-route-v1"}),
            answer_route_fingerprints=frozenset({"ORDINARY:answer-route-v1"}),
        )
        report_id = UUID("55555555-5555-5555-5555-555555555555")
        session.add(
            PilotReadinessRecord(
                id=report_id,
                report_version="pilot-readiness-v1",
                release_version=identity.release_version,
                release_fingerprint=identity.fingerprint,
                decision="PASS",
                generated_at=datetime.now(UTC),
                payload={
                    "decision": "PASS",
                    "release": {
                        "releaseVersion": identity.release_version,
                        "fingerprint": identity.fingerprint,
                        "datasetVersion": identity.dataset_version,
                        "datasetHash": identity.dataset_hash,
                        "indexIdentity": identity.index_identity,
                        "indexBuildHash": identity.index_build_hash,
                        "parserVersion": identity.parser_version,
                        "chunkVersion": identity.chunk_version,
                        "configurationFingerprint": identity.configuration_fingerprint,
                        "routeFingerprints": sorted(identity.route_fingerprints),
                        "answerRouteFingerprints": sorted(
                            identity.answer_route_fingerprints
                        ),
                    },
                },
            )
        )
        await session.commit()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_pilot_gate_fails_closed_until_current_matching_report_is_approved() -> None:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(pilot_release_fingerprint="a" * 64)
    report = PilotReadinessRecord(
        id=uuid4(),
        report_version="pilot-readiness-v1",
        release_version="spec10-test",
        release_fingerprint="a" * 64,
        decision="FAIL",
        generated_at=datetime.now(UTC),
        payload={"decision": "FAIL"},
    )
    async with factory() as session:
        gate = PilotAccessGate(session, settings)
        assert (await gate.state()).reason_code == "READINESS_REPORT_MISSING"
        session.add(report)
        await session.commit()
        assert (await gate.state()).reason_code == "READINESS_NOT_PASSING"

        report.decision = "PASS"
        report.generated_at = datetime.now(UTC) - timedelta(hours=25)
        await session.commit()
        assert (await gate.state()).reason_code == "READINESS_REPORT_STALE"

        report.generated_at = datetime.now(UTC)
        report.release_fingerprint = "b" * 64
        await session.commit()
        assert (await gate.state()).reason_code == "RELEASE_FINGERPRINT_MISMATCH"

        report.release_fingerprint = "a" * 64
        await session.commit()
        disabled = await gate.state()
        assert disabled.available is True
        assert disabled.reason_code == "PILOT_DISABLED"

        enabled = await gate.record(
            PilotAccessDecision(
                enabled=True,
                approved_by="system-admin",
                pilot_scope="华东运营组",
                capacity="10 RPS",
                monitoring_owner="ops-owner",
                rollback_owner="release-owner",
                reason="证据已复核",
            )
        )
        assert enabled.enabled is True
        assert enabled.rollback_owner == "release-owner"
    await engine.dispose()


def test_oidc_login_uses_state_and_pkce_without_exposing_a_client_secret() -> None:
    settings = Settings(
        identity_provider="oidc",
        oidc_authorization_url="https://identity.example.test/authorize",
        oidc_client_id="erp-assistant-web",
        oidc_client_secret="server-only-secret",
        internal_web_base_url="https://assistant.example.test",
    )
    _, transaction = new_oidc_transaction(settings)
    url = oidc_authorization_url(settings, transaction)

    assert url.startswith("https://identity.example.test/authorize?")
    assert f"state={transaction.state}" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "server-only-secret" not in url

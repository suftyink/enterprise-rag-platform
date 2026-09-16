import asyncio
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
from playwright.sync_api import Browser, sync_playwright
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
from erp_assistant.models.operations import PilotAccessRecord, PilotReadinessRecord
from erp_assistant.rag.errors import RagDependencyError, RagError
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
    DocumentStatus,
    PublicationStatus,
    RetentionStatus,
)
from erp_assistant.web.pilot import PilotAccessGate
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

    async def ask(self, request: AnswerRequest) -> AnswerOutcome:
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
                    document_id=uuid4(),
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
            publication_status=PublicationStatus.PUBLISHED,
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
    assert internal_web_server.storage.objects

    card.get_by_role("button", name="发布").click()
    page.get_by_label("操作原因").fill("完成管理员验收")
    page.get_by_role("button", name="确认执行").click()
    page.get_by_text("治理操作已完成并写入审计").wait_for()
    assert internal_web_server.corpus.transitions[0][1] is LifecycleAction.PUBLISH

    page.get_by_role("button", name="试点门禁").click()
    page.get_by_label("试点范围").fill("华东运营组 30 人")
    page.get_by_label("容量约束").fill("10 RPS / 30 并发")
    page.get_by_label("监控负责人").fill("ops-owner")
    page.get_by_label("回滚负责人").fill("release-owner")
    page.get_by_label("批准原因").fill("生产类证据已通过")
    page.get_by_role("button", name="关闭试点").click()
    page.get_by_text("试点已关闭，知识数据未删除").wait_for()
    assert internal_web_server.storage.objects


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
            enabled=True,
            approved_by="system-admin",
            pilot_scope="华东运营组",
            capacity="10 RPS",
            monitoring_owner="ops-owner",
            rollback_owner="release-owner",
            reason="证据已复核",
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

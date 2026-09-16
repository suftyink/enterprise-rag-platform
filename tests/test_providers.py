import httpx
import pytest

from erp_assistant.orchestration.fake_providers import (
    FakeChatProvider,
    FakeEmbeddingProvider,
    FakeRerankProvider,
)
from erp_assistant.orchestration.http_chat_provider import HttpChatProvider
from erp_assistant.orchestration.providers import ChatMessage, ProviderBoundary


@pytest.mark.asyncio
async def test_fake_chat_is_deterministic() -> None:
    provider = FakeChatProvider()
    result = await provider.generate([ChatMessage(role="user", content="hello")])
    assert result.content == "FAKE:hello"
    assert result.model == "fake-chat-v1"


@pytest.mark.asyncio
async def test_http_chat_returns_structured_content_with_bound_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"decision":"REFUSED","refusalReason":"NO_EVIDENCE"}'}}
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://model.example/v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        provider = HttpChatProvider(
            client,
            deployment="ordinary-chat-prod",
            model="grounded-chat",
            version="v1",
            boundary=ProviderBoundary.CLOUD,
            api_key="secret-token",
        )
        result = await provider.generate([ChatMessage(role="user", content="question")])

    assert result.model == "grounded-chat:v1"
    assert result.content == '{"decision":"REFUSED","refusalReason":"NO_EVIDENCE"}'
    assert requests[0].headers["authorization"] == "Bearer secret-token"
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].read().find(b"ordinary-chat-prod") >= 0


@pytest.mark.asyncio
async def test_fake_embedding_is_deterministic() -> None:
    provider = FakeEmbeddingProvider()
    first = await provider.embed(["sku-001"])
    second = await provider.embed(["sku-001"])
    assert first == second
    assert len(first[0]) == provider.dimension


@pytest.mark.asyncio
async def test_fake_reranker_orders_by_overlap() -> None:
    provider = FakeRerankProvider()
    ranked = await provider.rerank("amazon stock", ["purchase order", "amazon stock report"])
    assert [item.index for item in ranked] == [1, 0]
    assert provider.model == "fake-lexical-rerank-v2"
    assert all(0 <= item.score <= 1 for item in ranked)
    assert ranked[0].score == 1.0

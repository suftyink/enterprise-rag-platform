import pytest

from erp_assistant.orchestration.fake_providers import (
    FakeChatProvider,
    FakeEmbeddingProvider,
    FakeRerankProvider,
)
from erp_assistant.orchestration.providers import ChatMessage


@pytest.mark.asyncio
async def test_fake_chat_is_deterministic() -> None:
    provider = FakeChatProvider()
    result = await provider.generate([ChatMessage(role="user", content="hello")])
    assert result.content == "FAKE:hello"
    assert result.model == "fake-chat-v1"


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

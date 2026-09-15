import math
import re
from hashlib import sha256

from erp_assistant.orchestration.providers import (
    ChatMessage,
    ChatResult,
    ProviderBoundary,
    ProviderUsage,
    RankedDocument,
)


class FakeChatProvider:
    async def generate(self, messages: list[ChatMessage]) -> ChatResult:
        content = messages[-1].content if messages else ""
        return ChatResult(content=f"FAKE:{content}", model="fake-chat-v1")


class FakeEmbeddingProvider:
    """Deterministic lexical vectors for local development, not semantic embeddings."""

    dimension = 256
    model = "fake-lexical-v1"
    deployment = "local-fake"
    boundary = ProviderBoundary.INTERNAL
    production_ready = False
    usage = ProviderUsage()

    @staticmethod
    def features(value: str) -> list[str]:
        normalized = value.casefold()
        words = re.findall(r"[a-z0-9]+", normalized)
        chinese = re.findall(r"[\u3400-\u9fff]", normalized)
        bigrams = ["".join(chinese[index : index + 2]) for index in range(len(chinese) - 1)]
        return [*words, *chinese, *bigrams]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for value in texts:
            vector = [0.0] * self.dimension
            for feature in self.features(value):
                digest = sha256(feature.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimension
                vector[index] += 1.0
            norm = math.sqrt(sum(item * item for item in vector))
            if norm:
                vector = [round(item / norm, 6) for item in vector]
            vectors.append(vector)
        return vectors


class FakeRerankProvider:
    model = "fake-lexical-rerank-v2"
    deployment = "local-fake"
    boundary = ProviderBoundary.INTERNAL
    production_ready = False
    usage = ProviderUsage()

    async def rerank(self, query: str, documents: list[str]) -> list[RankedDocument]:
        query_terms = set(FakeEmbeddingProvider.features(query))
        denominator = max(len(query_terms), 1)
        ranked = [
            RankedDocument(
                index=index,
                score=round(
                    len(query_terms & set(FakeEmbeddingProvider.features(text))) / denominator,
                    6,
                ),
            )
            for index, text in enumerate(documents)
        ]
        return sorted(ranked, key=lambda item: (-item.score, item.index))

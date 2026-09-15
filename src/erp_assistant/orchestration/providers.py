import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Protocol


class ProviderBoundary(StrEnum):
    CLOUD = "CLOUD"
    INTERNAL = "INTERNAL"


@dataclass(frozen=True)
class ProviderUsage:
    requests: int = 0
    input_tokens: int = 0


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str


@dataclass(frozen=True)
class RankedDocument:
    index: int
    score: float


class ChatProvider(Protocol):
    async def generate(self, messages: list[ChatMessage]) -> ChatResult: ...


class EmbeddingProvider(Protocol):
    dimension: int
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class RerankProvider(Protocol):
    model: str

    async def rerank(self, query: str, documents: list[str]) -> list[RankedDocument]: ...


def embedding_binding_fingerprint(provider: EmbeddingProvider) -> str:
    payload = json.dumps(
        {
            "boundary": str(getattr(provider, "boundary", ProviderBoundary.INTERNAL)),
            "deployment": str(getattr(provider, "deployment", "legacy")),
            "dimension": provider.dimension,
            "model": provider.model,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()

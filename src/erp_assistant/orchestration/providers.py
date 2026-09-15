from dataclasses import dataclass
from enum import StrEnum
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

from dataclasses import dataclass
from typing import Protocol


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

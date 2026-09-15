import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from erp_assistant.rag.errors import DocumentValidationError


class BlockKind(StrEnum):
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST = "LIST"
    CODE = "CODE"
    TABLE = "TABLE"
    MIXED = "MIXED"


@dataclass(frozen=True)
class ParserLimits:
    max_file_size: int
    max_pages: int
    max_blocks: int
    max_uncompressed_size: int
    max_compression_ratio: int
    minimum_text_characters: int
    minimum_text_per_page: int


@dataclass(frozen=True)
class SourceLocator:
    block_index: int
    page_start: int | None = None
    page_end: int | None = None
    heading_path: tuple[str, ...] = ()
    anchor: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "blockIndex": self.block_index,
            "pageStart": self.page_start,
            "pageEnd": self.page_end,
            "headingPath": list(self.heading_path),
            "anchor": self.anchor,
        }


@dataclass(frozen=True)
class ParsedBlock:
    kind: BlockKind
    text: str
    locator: SourceLocator
    metadata: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "locator": self.locator.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ParsedDocument:
    parser_version: str
    blocks: tuple[ParsedBlock, ...]
    warnings: tuple[str, ...]
    quality_signals: dict[str, int | float | str]
    page_count: int | None = None


class StructuredDocumentParser(Protocol):
    version: str

    def parse(self, content: bytes, limits: ParserLimits) -> ParsedDocument: ...


def normalize_text(value: str) -> str:
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def complete_document(
    *,
    parser_version: str,
    blocks: list[ParsedBlock],
    warnings: list[str],
    limits: ParserLimits,
    page_count: int | None = None,
) -> ParsedDocument:
    if len(blocks) > limits.max_blocks:
        raise DocumentValidationError(
            "TOO_MANY_BLOCKS", f"文档不能超过 {limits.max_blocks} 个结构块"
        )
    non_whitespace = sum(len(re.sub(r"\s+", "", block.text)) for block in blocks)
    total_characters = sum(len(block.text) for block in blocks)
    if not blocks or non_whitespace == 0:
        raise DocumentValidationError("EMPTY_DOCUMENT", "文档不包含可索引正文")
    if non_whitespace < limits.minimum_text_characters:
        raise DocumentValidationError("TEXT_QUALITY_TOO_LOW", "文档有效文本不足")
    quality_score = round(non_whitespace / max(total_characters, 1), 4)
    return ParsedDocument(
        parser_version=parser_version,
        blocks=tuple(blocks),
        warnings=tuple(dict.fromkeys(warnings)),
        quality_signals={
            "blockCount": len(blocks),
            "characterCount": total_characters,
            "nonWhitespaceCharacterCount": non_whitespace,
            "qualityScore": quality_score,
        },
        page_count=page_count,
    )

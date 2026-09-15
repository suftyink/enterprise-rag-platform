import re
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID, uuid5

from erp_assistant.rag.parsed_document import ParsedBlock, ParsedDocument
from erp_assistant.rag.pdf_parser import ParsedPage

CHUNK_STRATEGY_VERSION = "structured-window-v1"


@dataclass(frozen=True)
class ChunkDraft:
    id: UUID
    index: int
    page_start: int | None
    page_end: int | None
    section_title: str | None
    content: str
    content_hash: str
    heading_path: tuple[str, ...] = ()
    anchor: str | None = None
    block_kind: str = "PARAGRAPH"
    source_locator: dict[str, object] = field(default_factory=dict)


def chunk_document(
    document_id: UUID,
    document: ParsedDocument,
    target_size: int,
    max_size: int,
    overlap: int,
) -> list[ChunkDraft]:
    max_size = max(1, max_size)
    target_size = min(max(1, target_size), max_size)
    overlap = min(max(0, overlap), max_size - 1)
    chunks: list[ChunkDraft] = []
    buffered: list[tuple[ParsedBlock, str, int]] = []

    def flush() -> None:
        if not buffered:
            return
        first = buffered[0][0]
        last = buffered[-1][0]
        content = "\n\n".join(part for _, part, _ in buffered)
        if first.locator.heading_path and not any(
            block.kind.value == "HEADING" for block, _, _ in buffered
        ):
            content = f"{' > '.join(first.locator.heading_path)}\n\n{content}"
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        index = len(chunks)
        part_indexes = [part_index for _, _, part_index in buffered]
        locator = first.locator.to_dict()
        locator["blockStart"] = first.locator.block_index
        locator["blockEnd"] = last.locator.block_index
        if any(part_indexes):
            locator["partIndex"] = part_indexes[0]
        page_values = [
            page
            for block, _, _ in buffered
            for page in (block.locator.page_start, block.locator.page_end)
            if page is not None
        ]
        page_start = min(page_values) if page_values else None
        page_end = max(page_values) if page_values else None
        locator["pageStart"] = page_start
        locator["pageEnd"] = page_end
        block_kinds = {block.kind.value for block, _, _ in buffered}
        chunks.append(
            ChunkDraft(
                id=uuid5(
                    document_id,
                    f"{first.locator.block_index}:{last.locator.block_index}:{content_hash}",
                ),
                index=index,
                page_start=page_start,
                page_end=page_end,
                section_title=(
                    first.locator.heading_path[-1] if first.locator.heading_path else None
                ),
                content=content,
                content_hash=content_hash,
                heading_path=first.locator.heading_path,
                anchor=first.locator.anchor,
                block_kind=next(iter(block_kinds)) if len(block_kinds) == 1 else "MIXED",
                source_locator=locator,
            )
        )
        buffered.clear()

    for block in document.blocks:
        for part_index, part in enumerate(_split_block(block.text, max_size, overlap)):
            same_context = not buffered or (
                buffered[0][0].locator.heading_path == block.locator.heading_path
                and buffered[0][0].locator.anchor == block.locator.anchor
                and buffered[0][0].locator.page_start == block.locator.page_start
            )
            candidate_size = sum(len(value) + 2 for _, value, _ in buffered) + len(part)
            if buffered and (not same_context or candidate_size > max_size):
                flush()
            buffered.append((block, part, part_index))
            if sum(len(value) + 2 for _, value, _ in buffered) >= target_size:
                flush()
    flush()
    return chunks


def _split_block(text: str, max_size: int, overlap: int) -> list[str]:
    if len(text) <= max_size:
        return [text]
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_size, len(text))
        if end < len(text):
            boundary = max(text.rfind(" ", start, end), text.rfind("\n", start, end))
            if boundary > start + max_size // 2:
                end = boundary
        value = text[start:end].strip()
        if value:
            parts.append(value)
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and not text[next_start - 1].isspace():
            next_start += 1
        start = next_start
    return parts


def chunk_pages(
    document_id: UUID,
    pages: list[ParsedPage],
    target_size: int,
    max_size: int,
    overlap: int,
) -> list[ChunkDraft]:
    chunks: list[ChunkDraft] = []
    for page in pages:
        page_chunks = _chunk_page(page.text, target_size, max_size, overlap)
        for section_title, content in page_chunks:
            content_hash = sha256(content.encode("utf-8")).hexdigest()
            index = len(chunks)
            chunks.append(
                ChunkDraft(
                    id=uuid5(document_id, f"{page.page_number}:{index}:{content_hash}"),
                    index=index,
                    page_start=page.page_number,
                    page_end=page.page_number,
                    section_title=section_title,
                    content=content,
                    content_hash=content_hash,
                    source_locator={
                        "blockStart": index,
                        "blockEnd": index,
                        "blockIndex": index,
                        "pageStart": page.page_number,
                        "pageEnd": page.page_number,
                        "headingPath": [section_title] if section_title else [],
                        "anchor": None,
                    },
                )
            )
    return chunks


def _chunk_page(
    text: str, target_size: int, max_size: int, overlap: int
) -> list[tuple[str | None, str]]:
    paragraphs = [value.strip() for value in re.split(r"\n\s*\n", text) if value.strip()]
    results: list[tuple[str | None, str]] = []
    buffer = ""
    buffer_title: str | None = None
    has_fresh_content = False
    section_title: str | None = None

    def flush(keep_overlap: bool) -> None:
        nonlocal buffer, has_fresh_content
        content = buffer.strip()
        if content:
            results.append((buffer_title, content))
        buffer = content[-overlap:] if keep_overlap and overlap and len(content) > overlap else ""
        has_fresh_content = False

    for paragraph in paragraphs:
        if len(paragraph) <= 80 and not re.search(r"[。！？.!?]$", paragraph):
            section_title = paragraph
        if len(paragraph) > max_size:
            if has_fresh_content:
                flush(keep_overlap=False)
            buffer = ""
            step = max(1, max_size - overlap)
            for start in range(0, len(paragraph), step):
                part = paragraph[start : start + max_size].strip()
                if part:
                    results.append((section_title, part))
                if start + max_size >= len(paragraph):
                    break
            buffer = ""
            continue
        candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if has_fresh_content and len(candidate) > max_size:
            flush(keep_overlap=True)
            candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if not has_fresh_content:
            buffer_title = section_title
        buffer = candidate
        has_fresh_content = True
        if len(buffer) >= target_size:
            flush(keep_overlap=True)
    if has_fresh_content:
        content = buffer.strip()
        if content:
            results.append((buffer_title, content))
    return results

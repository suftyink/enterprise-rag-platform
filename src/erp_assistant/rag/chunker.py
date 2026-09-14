import re
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid5

from erp_assistant.rag.pdf_parser import ParsedPage


@dataclass(frozen=True)
class ChunkDraft:
    id: UUID
    index: int
    page_start: int
    page_end: int
    section_title: str | None
    content: str
    content_hash: str


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
        buffer = (
            content[-overlap:]
            if keep_overlap and overlap and len(content) > overlap
            else ""
        )
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

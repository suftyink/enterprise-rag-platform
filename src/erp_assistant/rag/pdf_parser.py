import math
import re
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from erp_assistant.rag.errors import OcrRequiredError, PdfValidationError


@dataclass(frozen=True)
class ParsedPage:
    page_number: int
    text: str


@dataclass(frozen=True)
class ParsedPdf:
    pages: list[ParsedPage]
    warnings: list[str]


class PdfParser(Protocol):
    def inspect(self, content: bytes, max_pages: int) -> int: ...

    def parse(self, content: bytes, minimum_text_per_page: int) -> ParsedPdf: ...


class PypdfParser:
    def _reader(self, content: bytes) -> PdfReader:
        try:
            reader = PdfReader(BytesIO(content))
        except (PdfReadError, ValueError, TypeError) as exc:
            raise PdfValidationError("INVALID_PDF", "PDF 文件损坏或格式无效") from exc
        if reader.is_encrypted:
            raise PdfValidationError("ENCRYPTED_PDF", "暂不支持加密 PDF")
        return reader

    def inspect(self, content: bytes, max_pages: int) -> int:
        reader = self._reader(content)
        page_count = len(reader.pages)
        if page_count == 0:
            raise PdfValidationError("EMPTY_PDF", "PDF 不包含页面")
        if page_count > max_pages:
            raise PdfValidationError("TOO_MANY_PAGES", f"PDF 不能超过 {max_pages} 页")
        return page_count

    def parse(self, content: bytes, minimum_text_per_page: int) -> ParsedPdf:
        reader = self._reader(content)
        raw_pages: list[list[str]] = []
        warnings: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                warnings.append(f"PAGE_{index}_EXTRACT_{exc.__class__.__name__}")
                text = ""
            lines = [self._clean_line(line) for line in text.replace("\r", "\n").split("\n")]
            raw_pages.append([line for line in lines if line])

        headers, footers = self._repeated_margins(raw_pages)
        pages: list[ParsedPage] = []
        non_whitespace = 0
        for index, lines in enumerate(raw_pages, start=1):
            if lines and lines[0] in headers:
                lines = lines[1:]
            if lines and lines[-1] in footers:
                lines = lines[:-1]
            text = self._normalize_lines(lines)
            if not text:
                warnings.append(f"PAGE_{index}_NO_TEXT")
            non_whitespace += len(re.sub(r"\s+", "", text))
            pages.append(ParsedPage(page_number=index, text=text))

        required_text = max(100, len(pages) * minimum_text_per_page)
        if non_whitespace < required_text:
            raise OcrRequiredError("OCR_REQUIRED", "PDF 可提取文本过少，需要 OCR")
        return ParsedPdf(pages=pages, warnings=warnings)

    @staticmethod
    def _clean_line(value: str) -> str:
        return re.sub(r"[ \t\u00a0]+", " ", value).strip()

    @staticmethod
    def _repeated_margins(pages: list[list[str]]) -> tuple[set[str], set[str]]:
        if len(pages) < 3:
            return set(), set()
        threshold = max(2, math.ceil(len(pages) * 0.6))
        headers = Counter(lines[0] for lines in pages if lines)
        footers = Counter(lines[-1] for lines in pages if lines)
        return (
            {value for value, count in headers.items() if count >= threshold},
            {value for value, count in footers.items() if count >= threshold},
        )

    @staticmethod
    def _normalize_lines(lines: list[str]) -> str:
        if not lines:
            return ""
        text = "\n\n".join(lines)
        text = re.sub(r"(?<=[A-Za-z])-\n\n(?=[A-Za-z])", "", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

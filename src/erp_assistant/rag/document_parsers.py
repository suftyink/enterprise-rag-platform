import json
import re
import zipfile
from hashlib import sha256
from io import BytesIO

from erp_assistant.rag.docx_parser import DocxDocumentParser
from erp_assistant.rag.errors import DocumentValidationError, OcrRequiredError
from erp_assistant.rag.html_document_parser import HtmlDocumentParser
from erp_assistant.rag.parsed_document import (
    BlockKind,
    ParsedBlock,
    ParsedDocument,
    ParserLimits,
    SourceLocator,
    StructuredDocumentParser,
    complete_document,
    normalize_text,
)
from erp_assistant.rag.pdf_parser import PdfParser, PypdfParser

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MARKDOWN_MEDIA_TYPE = "text/markdown"
HTML_MEDIA_TYPE = "text/html"
SUPPORTED_MEDIA_TYPES = frozenset(
    {PDF_MEDIA_TYPE, DOCX_MEDIA_TYPE, MARKDOWN_MEDIA_TYPE, HTML_MEDIA_TYPE}
)


class PdfDocumentParser:
    def __init__(self, parser: PdfParser | None = None) -> None:
        self._parser = parser or PypdfParser()
        self.version = self._parser.version

    def parse(self, content: bytes, limits: ParserLimits) -> ParsedDocument:
        self._parser.inspect(content, limits.max_pages)
        parsed = self._parser.parse(content, limits.minimum_text_per_page)
        blocks = [
            ParsedBlock(
                kind=BlockKind.PARAGRAPH,
                text=page.text,
                locator=SourceLocator(
                    block_index=index,
                    page_start=page.page_number,
                    page_end=page.page_number,
                ),
                metadata={},
            )
            for index, page in enumerate(parsed.pages)
            if page.text.strip()
        ]
        try:
            return complete_document(
                parser_version=self.version,
                blocks=blocks,
                warnings=parsed.warnings,
                limits=limits,
                page_count=len(parsed.pages),
            )
        except DocumentValidationError as exc:
            if exc.code in {"EMPTY_DOCUMENT", "TEXT_QUALITY_TOO_LOW"}:
                raise OcrRequiredError("OCR_REQUIRED", "PDF 可提取文本过少，需要 OCR") from exc
            raise


class MarkdownDocumentParser:
    version = "markdown-v1"
    _heading = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
    _list_item = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(.+)$")

    def parse(self, content: bytes, limits: ParserLimits) -> ParsedDocument:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentValidationError(
                "INVALID_MARKDOWN_ENCODING", "Markdown 必须使用 UTF-8 编码"
            ) from exc
        blocks: list[ParsedBlock] = []
        headings: list[str] = []
        paragraph: list[str] = []
        list_items: list[str] = []
        code_lines: list[str] = []
        in_code = False

        def add(kind: BlockKind, value: str, metadata: dict[str, str] | None = None) -> None:
            normalized = normalize_text(value)
            if not normalized:
                return
            if len(blocks) >= limits.max_blocks:
                raise DocumentValidationError(
                    "TOO_MANY_BLOCKS", f"文档不能超过 {limits.max_blocks} 个结构块"
                )
            blocks.append(
                ParsedBlock(
                    kind=kind,
                    text=normalized,
                    locator=SourceLocator(
                        block_index=len(blocks),
                        heading_path=tuple(headings),
                    ),
                    metadata=metadata or {},
                )
            )

        def flush_text() -> None:
            if paragraph:
                add(BlockKind.PARAGRAPH, " ".join(paragraph))
                paragraph.clear()
            if list_items:
                add(BlockKind.LIST, "\n".join(f"- {item}" for item in list_items))
                list_items.clear()

        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if line.lstrip().startswith("```"):
                if in_code:
                    add(BlockKind.CODE, "\n".join(code_lines), {"language": line.strip()[3:]})
                    code_lines.clear()
                    in_code = False
                else:
                    flush_text()
                    in_code = True
                continue
            if in_code:
                code_lines.append(line.rstrip())
                continue
            heading = self._heading.match(line.strip())
            if heading:
                flush_text()
                level = len(heading.group(1))
                title = normalize_text(heading.group(2))
                headings[level - 1 :] = [title]
                add(BlockKind.HEADING, title, {"level": str(level)})
                continue
            item = self._list_item.match(line)
            if item:
                if paragraph:
                    add(BlockKind.PARAGRAPH, " ".join(paragraph))
                    paragraph.clear()
                list_items.append(item.group(1).strip())
                continue
            if not line.strip():
                flush_text()
            else:
                if list_items:
                    add(BlockKind.LIST, "\n".join(f"- {item}" for item in list_items))
                    list_items.clear()
                paragraph.append(line.strip())
        flush_text()
        warnings: list[str] = []
        if in_code:
            add(BlockKind.CODE, "\n".join(code_lines))
            warnings.append("MARKDOWN_UNCLOSED_CODE_FENCE")
        return complete_document(
            parser_version=self.version,
            blocks=blocks,
            warnings=warnings,
            limits=limits,
        )


class DocumentParserRouter:
    version = "document-parser-router-v1"

    def __init__(self, pdf_parser: PdfParser | None = None) -> None:
        self._parsers: dict[str, StructuredDocumentParser] = {
            PDF_MEDIA_TYPE: PdfDocumentParser(pdf_parser),
            DOCX_MEDIA_TYPE: DocxDocumentParser(),
            MARKDOWN_MEDIA_TYPE: MarkdownDocumentParser(),
            HTML_MEDIA_TYPE: HtmlDocumentParser(),
        }

    @property
    def identity(self) -> str:
        versions = {media_type: parser.version for media_type, parser in self._parsers.items()}
        digest = sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()[:16]
        return f"{self.version}:{digest}"

    def parser_version(self, media_type: str) -> str:
        parser = self._parsers.get(normalize_media_type(media_type))
        return parser.version if parser is not None else self.version

    def parse(self, media_type: str, content: bytes, limits: ParserLimits) -> ParsedDocument:
        normalized = normalize_media_type(media_type)
        parser = self._parsers.get(normalized)
        if parser is None:
            raise DocumentValidationError(
                "UNSUPPORTED_MEDIA_TYPE", f"暂不支持媒体类型 {normalized}"
            )
        if len(content) > limits.max_file_size:
            raise DocumentValidationError("FILE_TOO_LARGE", "文档超过允许的大小限制")
        detected = sniff_media_type(content)
        if detected != normalized:
            if normalized == DOCX_MEDIA_TYPE and detected == "application/x-ole-storage":
                raise DocumentValidationError("ENCRYPTED_DOCX", "暂不支持加密 DOCX")
            if normalized == DOCX_MEDIA_TYPE and detected in {
                "application/zip",
                "application/octet-stream",
            }:
                raise DocumentValidationError("INVALID_DOCX", "DOCX 文件损坏或格式无效")
            raise DocumentValidationError(
                "MEDIA_TYPE_MISMATCH", f"声明类型 {normalized} 与实际内容 {detected} 不一致"
            )
        return parser.parse(content, limits)


def normalize_media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def sniff_media_type(content: bytes) -> str:
    if content.startswith(b"%PDF-"):
        return PDF_MEDIA_TYPE
    if content.startswith(b"\xd0\xcf\x11\xe0"):
        return "application/x-ole-storage"
    if content.startswith(b"PK"):
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError):
            return "application/zip"
        if "[Content_Types].xml" in names and "word/document.xml" in names:
            return DOCX_MEDIA_TYPE
        return "application/zip"
    try:
        text = content.decode("utf-8-sig").lstrip()
    except UnicodeDecodeError:
        return "application/octet-stream"
    if re.search(r"(?is)^<!doctype\s+html\b|^<html\b|<body\b|<head\b", text):
        return HTML_MEDIA_TYPE
    return MARKDOWN_MEDIA_TYPE

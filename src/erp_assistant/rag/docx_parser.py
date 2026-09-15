import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

from erp_assistant.rag.errors import DocumentValidationError
from erp_assistant.rag.parsed_document import (
    BlockKind,
    ParsedBlock,
    ParsedDocument,
    ParserLimits,
    SourceLocator,
    complete_document,
    normalize_text,
)

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_WORD_NS}}}"


class DocxDocumentParser:
    version = "docx-v1"

    def parse(self, content: bytes, limits: ParserLimits) -> ParsedDocument:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                self._validate_archive(archive, limits)
                xml = archive.read("word/document.xml")
        except DocumentValidationError:
            raise
        except (KeyError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise DocumentValidationError("INVALID_DOCX", "DOCX 文件损坏或格式无效") from exc
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise DocumentValidationError("INVALID_DOCX", "DOCX 主文档 XML 损坏") from exc
        body = root.find(f"{_W}body")
        if body is None:
            raise DocumentValidationError("INVALID_DOCX", "DOCX 不包含正文")

        blocks: list[ParsedBlock] = []
        headings: list[str] = []
        warnings: list[str] = []

        def add(kind: BlockKind, text: str, metadata: dict[str, str] | None = None) -> None:
            value = normalize_text(text)
            if not value:
                return
            if len(blocks) >= limits.max_blocks:
                raise DocumentValidationError(
                    "TOO_MANY_BLOCKS", f"文档不能超过 {limits.max_blocks} 个结构块"
                )
            blocks.append(
                ParsedBlock(
                    kind=kind,
                    text=value,
                    locator=SourceLocator(
                        block_index=len(blocks),
                        heading_path=tuple(headings),
                    ),
                    metadata=metadata or {},
                )
            )

        for child in body:
            if child.tag == f"{_W}p":
                text = self._text(child)
                style = child.find(f"{_W}pPr/{_W}pStyle")
                style_name = style.get(f"{_W}val", "") if style is not None else ""
                heading_match = re.fullmatch(r"Heading([1-6])", style_name, re.IGNORECASE)
                if heading_match and text.strip():
                    level = int(heading_match.group(1))
                    title = normalize_text(text)
                    headings[level - 1 :] = [title]
                    add(BlockKind.HEADING, title, {"level": str(level)})
                elif child.find(f"{_W}pPr/{_W}numPr") is not None:
                    add(BlockKind.LIST, f"- {text}")
                else:
                    add(BlockKind.PARAGRAPH, text)
            elif child.tag == f"{_W}tbl":
                rows: list[str] = []
                for row in child.findall(f"{_W}tr"):
                    cells = [normalize_text(self._text(cell)) for cell in row.findall(f"{_W}tc")]
                    if any(cells):
                        rows.append(" | ".join(cells))
                if rows:
                    add(BlockKind.TABLE, "\n".join(rows))
                    warnings.append("DOCX_TABLE_LAYOUT_FLATTENED")
        return complete_document(
            parser_version=self.version,
            blocks=blocks,
            warnings=warnings,
            limits=limits,
        )

    @staticmethod
    def _text(element: ElementTree.Element) -> str:
        parts: list[str] = []
        for node in element.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag in {f"{_W}tab", f"{_W}br"}:
                parts.append("\n")
        return "".join(parts)

    @staticmethod
    def _validate_archive(archive: zipfile.ZipFile, limits: ParserLimits) -> None:
        infos = archive.infolist()
        if any(info.flag_bits & 0x1 for info in infos):
            raise DocumentValidationError("ENCRYPTED_DOCX", "暂不支持加密 DOCX")
        if len(infos) > limits.max_blocks * 4:
            raise DocumentValidationError("ZIP_BOMB_RISK", "DOCX 压缩包条目过多")
        total_size = sum(info.file_size for info in infos)
        if total_size > limits.max_uncompressed_size:
            raise DocumentValidationError("DECOMPRESSION_LIMIT_EXCEEDED", "DOCX 解压后超过限制")
        for info in infos:
            if info.file_size == 0:
                continue
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > limits.max_compression_ratio:
                raise DocumentValidationError("ZIP_BOMB_RISK", "DOCX 压缩比超过安全限制")

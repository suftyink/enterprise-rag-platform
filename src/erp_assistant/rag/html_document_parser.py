import re
from html.parser import HTMLParser

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

_ACTIVE_TAGS = {"script", "style", "noscript", "iframe", "object", "embed", "svg", "canvas"}
_NAVIGATION_TAGS = {"nav", "header", "footer", "aside"}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_BLOCK_TAGS = {
    "p": BlockKind.PARAGRAPH,
    "li": BlockKind.LIST,
    "pre": BlockKind.CODE,
    "td": BlockKind.TABLE,
    "th": BlockKind.TABLE,
    **{f"h{level}": BlockKind.HEADING for level in range(1, 7)},
}


class _StructuredHtmlParser(HTMLParser):
    def __init__(self, max_blocks: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max_blocks = max_blocks
        self.blocks: list[ParsedBlock] = []
        self.warnings: list[str] = []
        self._headings: list[str] = []
        self._heading_anchor: str | None = None
        self._skip_depth = 0
        self._active_tag: str | None = None
        self._active_kind: BlockKind | None = None
        self._active_level: int | None = None
        self._active_anchor: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value for name, value in attrs}
        if self._skip_depth:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in _ACTIVE_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth = 1
            self.warnings.append("HTML_ACTIVE_CONTENT_REMOVED")
            return
        if tag in _NAVIGATION_TAGS:
            self._skip_depth = 1
            self.warnings.append("HTML_NAVIGATION_REMOVED")
            return
        style = (attributes.get("style") or "").replace(" ", "").lower()
        hidden = (
            "hidden" in attributes
            or (attributes.get("aria-hidden") or "").lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if hidden:
            if tag not in _VOID_TAGS:
                self._skip_depth = 1
            self.warnings.append("HTML_HIDDEN_CONTENT_REMOVED")
            return
        if tag in _BLOCK_TAGS:
            self._finish_block()
            self._active_tag = tag
            self._active_kind = _BLOCK_TAGS[tag]
            self._active_level = int(tag[1]) if tag.startswith("h") else None
            self._active_anchor = attributes.get("id") or attributes.get("name")
        elif tag == "br" and self._active_tag is not None:
            self._text.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == self._active_tag:
            self._finish_block()

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and self._active_tag is not None:
            self._text.append(data)

    def close(self) -> None:
        super().close()
        self._finish_block()

    def _finish_block(self) -> None:
        if self._active_kind is None:
            return
        text = normalize_text("".join(self._text))
        if text:
            if len(self.blocks) >= self._max_blocks:
                raise DocumentValidationError(
                    "TOO_MANY_BLOCKS", f"文档不能超过 {self._max_blocks} 个结构块"
                )
            if self._active_kind is BlockKind.HEADING and self._active_level is not None:
                self._headings[self._active_level - 1 :] = [text]
                if self._active_anchor:
                    self._heading_anchor = self._active_anchor
            anchor = self._active_anchor or self._heading_anchor
            self.blocks.append(
                ParsedBlock(
                    kind=self._active_kind,
                    text=(f"- {text}" if self._active_kind is BlockKind.LIST else text),
                    locator=SourceLocator(
                        block_index=len(self.blocks),
                        heading_path=tuple(self._headings),
                        anchor=anchor,
                    ),
                    metadata=(
                        {"level": str(self._active_level)} if self._active_level is not None else {}
                    ),
                )
            )
        self._active_tag = None
        self._active_kind = None
        self._active_level = None
        self._active_anchor = None
        self._text.clear()


class HtmlDocumentParser:
    version = "html-v1"

    def parse(self, content: bytes, limits: ParserLimits) -> ParsedDocument:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentValidationError(
                "INVALID_HTML_ENCODING", "HTML 必须使用 UTF-8 编码"
            ) from exc
        if not re.search(r"(?is)<!doctype\s+html\b|<html\b|<body\b", text):
            raise DocumentValidationError("INVALID_HTML", "HTML 文档结构无效")
        parser = _StructuredHtmlParser(limits.max_blocks)
        try:
            parser.feed(text)
            parser.close()
        except DocumentValidationError:
            raise
        except Exception as exc:
            raise DocumentValidationError("INVALID_HTML", "HTML 文档损坏或格式无效") from exc
        return complete_document(
            parser_version=self.version,
            blocks=parser.blocks,
            warnings=parser.warnings,
            limits=limits,
        )

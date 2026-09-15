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
_IGNORED_TAGS = {"head"}
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
_CONTAINER_TAGS = {
    "article",
    "blockquote",
    "body",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "main",
    "section",
}
_OPTIONAL_END_TAGS = {
    "body",
    "colgroup",
    "dd",
    "dt",
    "head",
    "html",
    "li",
    "optgroup",
    "option",
    "p",
    "rp",
    "rt",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
}
_P_CLOSE_START_TAGS = _CONTAINER_TAGS | {
    "address",
    "details",
    "dialog",
    "fieldset",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "menu",
    "ol",
    "p",
    "pre",
    "table",
    "ul",
}
_AUTO_CLOSE_ON_START = {
    "dd": {"dd", "dt"},
    "dt": {"dd", "dt"},
    "li": {"li"},
    "option": {"option"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr"},
}
_PERSISTENT_ANCHOR_TAGS = {"a", *(f"h{level}" for level in range(1, 7))}


class _StructuredHtmlParser(HTMLParser):
    def __init__(self, max_blocks: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max_blocks = max_blocks
        self.blocks: list[ParsedBlock] = []
        self.warnings: list[str] = []
        self._headings: list[str] = []
        self._heading_anchor: str | None = None
        self._current_anchor: str | None = None
        self._skip_depth = 0
        self._skip_root: str | None = None
        self._open_tags: list[str] = []
        self._anchor_restore: list[tuple[bool, str | None]] = []
        self._malformed = False
        self._active_tag: str | None = None
        self._active_kind: BlockKind | None = None
        self._active_level: int | None = None
        self._active_anchor: str | None = None
        self._text: list[str] = []
        self._implicit_text: list[str] = []
        self._implicit_anchor: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value for name, value in attrs}
        if self._skip_depth:
            if self._skip_root == "head" and self._skip_depth == 1 and tag == "body":
                self._skip_depth = 0
                self._skip_root = None
            else:
                if tag not in _VOID_TAGS:
                    self._skip_depth += 1
                return
        if tag in _ACTIVE_TAGS | _IGNORED_TAGS:
            self._finish_implicit_block()
            if tag not in _VOID_TAGS:
                self._skip_depth = 1
                self._skip_root = tag
            if tag in _ACTIVE_TAGS:
                self.warnings.append("HTML_ACTIVE_CONTENT_REMOVED")
            return
        if tag in _NAVIGATION_TAGS:
            self._finish_implicit_block()
            self._skip_depth = 1
            self._skip_root = tag
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
            self._finish_implicit_block()
            if tag not in _VOID_TAGS:
                self._skip_depth = 1
                self._skip_root = tag
            self.warnings.append("HTML_HIDDEN_CONTENT_REMOVED")
            return
        anchor = attributes.get("id") or attributes.get("name")
        self._implicitly_close_for_start(tag)
        if tag not in _VOID_TAGS:
            should_restore = bool(anchor and tag not in _PERSISTENT_ANCHOR_TAGS)
            self._open_tags.append(tag)
            self._anchor_restore.append((should_restore, self._current_anchor))
        if anchor:
            if self._active_tag is None:
                self._finish_implicit_block()
            self._current_anchor = anchor
            if self._active_tag is not None:
                self._active_anchor = anchor
        if tag in _BLOCK_TAGS:
            self._finish_implicit_block()
            self._finish_block()
            self._active_tag = tag
            self._active_kind = _BLOCK_TAGS[tag]
            self._active_level = int(tag[1]) if tag.startswith("h") else None
            self._active_anchor = anchor or self._current_anchor
        elif tag in _CONTAINER_TAGS:
            self._finish_implicit_block()
        elif tag == "br" and self._active_tag is not None:
            self._text.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            self._skip_depth -= 1
            if not self._skip_depth:
                self._skip_root = None
            return
        if tag in _VOID_TAGS:
            return
        while self._open_tags and self._open_tags[-1] != tag:
            if self._open_tags[-1] not in _OPTIONAL_END_TAGS:
                break
            self._pop_open_tag()
        if not self._open_tags or self._open_tags[-1] != tag:
            self._malformed = True
            return
        self._pop_open_tag()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._active_tag is not None:
            self._text.append(data)
            return
        if not self._implicit_text:
            self._implicit_anchor = self._current_anchor or self._heading_anchor
        self._implicit_text.append(data)

    def close(self) -> None:
        super().close()
        self._finish_block()
        self._finish_implicit_block()
        if self._skip_root == "head" and self._skip_depth == 1:
            self._skip_depth = 0
            self._skip_root = None
        while self._open_tags and self._open_tags[-1] in _OPTIONAL_END_TAGS:
            self._pop_open_tag()
        if self._malformed or self._skip_depth or self._open_tags:
            raise DocumentValidationError("INVALID_HTML", "HTML 文档损坏或格式无效")

    def _implicitly_close_for_start(self, tag: str) -> None:
        auto_close = _AUTO_CLOSE_ON_START.get(tag, set())
        while self._open_tags:
            current = self._open_tags[-1]
            if current in auto_close or (current == "p" and tag in _P_CLOSE_START_TAGS):
                self._pop_open_tag()
                continue
            break

    def _pop_open_tag(self) -> None:
        tag = self._open_tags.pop()
        should_restore, previous_anchor = self._anchor_restore.pop()
        if tag == self._active_tag:
            self._finish_block()
        elif tag in _CONTAINER_TAGS:
            self._finish_implicit_block()
        if should_restore:
            self._current_anchor = previous_anchor

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
            self._add_block(
                self._active_kind,
                f"- {text}" if self._active_kind is BlockKind.LIST else text,
                self._active_anchor or self._heading_anchor,
                {"level": str(self._active_level)} if self._active_level is not None else {},
            )
        self._active_tag = None
        self._active_kind = None
        self._active_level = None
        self._active_anchor = None
        self._text.clear()

    def _finish_implicit_block(self) -> None:
        text = normalize_text("".join(self._implicit_text))
        if text:
            self._add_block(BlockKind.PARAGRAPH, text, self._implicit_anchor)
        self._implicit_text.clear()
        self._implicit_anchor = None

    def _add_block(
        self,
        kind: BlockKind,
        text: str,
        anchor: str | None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        if len(self.blocks) >= self._max_blocks:
            raise DocumentValidationError(
                "TOO_MANY_BLOCKS", f"文档不能超过 {self._max_blocks} 个结构块"
            )
        self.blocks.append(
            ParsedBlock(
                kind=kind,
                text=text,
                locator=SourceLocator(
                    block_index=len(self.blocks),
                    heading_path=tuple(self._headings),
                    anchor=anchor,
                ),
                metadata=metadata or {},
            )
        )


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

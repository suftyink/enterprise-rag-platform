# Spec 05：支持首批多格式解析与可引用 Chunk

Type: spec
Status: resolved
Blocked by: rag-03-document-indexer, rag-04-knowledge-governance
Primary test seam: `DocumentIndexer.index()`

## Problem Statement

当前上传和解析仅支持可复制文字的 PDF，解析产物主要围绕页码，Chunk 缺少统一章节定位和结构元数据。生产试点首批范围还要求 DOCX、Markdown 和 HTML，并需要为后续引用回答保留页码、标题路径、锚点和表格/标识符上下文。

## Solution

在 `DocumentIndexer` 内建立基于媒体类型的 `DocumentParser` Adapter 路由。PDF、DOCX、Markdown 和 HTML 都输出统一的结构化文档块及来源定位，再由可配置的 Chunk 策略生成可检索、可引用的证据单元。

## User Stories

1. As a knowledge administrator, I want to upload text PDF, DOCX, Markdown and HTML, so that the pilot covers common internal documentation.
2. As a reviewer, I want the detected format to match the actual content, so that renamed or deceptive files are rejected safely.
3. As an employee, I want headings and nearby context preserved, so that retrieved fragments remain understandable.
4. As an employee, I want page numbers or document anchors preserved, so that I can verify evidence in the source.
5. As a product specialist, I want SKU, abbreviations and numeric units kept intact, so that exact identifiers remain searchable.
6. As a reviewer, I want parsing warnings and quality scores, so that damaged or low-quality documents are not published blindly.
7. As an operator, I want unsupported or encrypted inputs classified explicitly, so that failures are actionable.
8. As a retrieval engineer, I want parser and Chunk strategy versions recorded, so that evaluation results are reproducible.
9. As a maintainer, I want each parser replaceable without changing indexing callers, so that formats can evolve independently.
10. As a maintainer, I want bounded memory and batch sizes, so that large accepted documents do not destabilize workers.

## Implementation Decisions

- The primary application behavior remains `DocumentIndexer.index()`; parser interfaces are internal Adapter seams and are not separate acceptance-test entry points.
- Supported initial media types are text PDF, DOCX, Markdown and HTML. MIME/content validation takes precedence over filename extension.
- All parsers emit a common parsed-document representation containing ordered blocks, block kind, normalized text, structural metadata and a source locator.
- Source locators support PDF pages, heading paths and HTML anchors without forcing every format into a page model.
- Cleaning removes navigation/noise conservatively and never silently drops numeric values, units, SKU-like identifiers or headings.
- The first implementation preserves fixed-window/page-aware Chunking while adding structure metadata; Parent-child Chunking is introduced only when the evaluation baseline proves benefit.
- Parser version, Chunk strategy version and warnings are persisted with indexed output.
- Encrypted, malformed, unsupported and text-insufficient inputs produce distinct outcomes. OCR remains explicitly deferred.
- File-size, page/block-count and decompression limits are enforced before expensive processing.
- Publication remains governed by Spec 04; successful parsing only produces a reviewable version.

## Testing Decisions

- The sole primary testing Seam is `DocumentIndexer.index()`.
- Acceptance fixtures cover each supported format, misleading MIME/extension combinations, headings, links, page/anchor locations, repeated margins, numeric/SKU content, malformed input and size limits through that Seam.
- Tests assert indexed chunks, source locators, warnings and processing outcomes, not parser-library calls or internal helper functions.
- Golden fixtures should be small, reviewable and derived from synthetic or cleared content.
- Existing PDF pipeline cases are prior art and should be preserved at the higher indexer Seam where feasible.

## Out of Scope

- Scanned PDF OCR, image understanding, Excel and complex table reconstruction.
- Automatic external-source synchronization.
- Parent-child Chunking unless justified by Spec 02 evaluation data.
- Model-vendor selection and answer generation.
- Full document preview UI.

## Further Notes

Parser choice and cleanup rules must be evaluated against the approved pilot corpus. New formats beyond the initial four require a separate scope decision rather than being silently added here.

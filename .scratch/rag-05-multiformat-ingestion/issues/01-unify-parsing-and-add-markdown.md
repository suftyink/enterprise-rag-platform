# 01: 统一解析产物并贯通 Markdown 入库

**What to build:** 让现有 PDF 和新增 Markdown 都通过媒体类型路由输出统一的结构化块与来源定位，并经 `DocumentIndexer.index()`生成待审核、可检索和可引用的 Chunk。

**Blocked by:** rag-03-document-indexer/03 / 恢复部分索引并统一重建路径; rag-04-knowledge-governance/03 / 支持回滚、清理补偿与并发审计.

**Status:** resolved

- [x] PDF 现有行为通过统一 parsed-document 表达保持兼容。
- [x] Markdown 标题层级、段落、列表和代码文本按安全规则解析。
- [x] 两种格式均输出 ordered blocks、block kind、结构元数据和 source locator。
- [x] Markdown Chunk 保留 heading path，PDF Chunk 保留页码。
- [x] 实际内容类型与声明格式不一致时被安全拒绝。
- [x] 成功结果进入 REVIEW_PENDING，不自动发布。
- [x] Parser 和 Chunk 版本随索引产物持久化。

## Answer

已建立统一 ParsedDocument/ParsedBlock/SourceLocator 表达与 MIME 内容路由，PDF 和 Markdown 均从 `DocumentIndexer.index()` 生成结构化、可引用 Chunk；处理成功进入 REVIEW_PENDING，并持久化 Parser、Chunk 版本和质量信号。

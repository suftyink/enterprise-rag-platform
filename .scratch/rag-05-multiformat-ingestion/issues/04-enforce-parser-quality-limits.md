# 04: 统一解析质量、安全限制与版本信息

**What to build:** 让四种已支持格式接受统一的大小、页数或块数、解压和文本质量限制，并输出一致的警告与解析版本信息，使低质量或高风险内容在审核前可识别。

**Blocked by:** 02 / 贯通 DOCX 结构化入库; 03 / 贯通 HTML 清洗与锚点入库.

**Status:** resolved

- [x] PDF、DOCX、Markdown 和 HTML 使用明确且可配置的资源上限。
- [x] 超限、压缩炸弹风险、空正文和质量不足具有不同错误代码。
- [x] 所有格式的结果都包含 Parser、Chunk 版本、质量信号和警告。
- [x] source locator 在切分后仍能定位原文位置。
- [x] 清洗规则不会静默移除数字、单位、SKU 或标题。
- [x] 四格式验收夹具均通过同一 `DocumentIndexer.index()` 测试入口。
- [x] OCR、Excel 和复杂表格明确保持不支持状态。

## Answer

已统一文件大小、页数、块数、解压大小、压缩比、文本质量和 Embedding 批次上限；解析结果记录版本、质量分和警告，Chunk 保存页码、heading path、anchor 与结构块范围。OCR、Excel 和复杂表格仍明确在范围外。

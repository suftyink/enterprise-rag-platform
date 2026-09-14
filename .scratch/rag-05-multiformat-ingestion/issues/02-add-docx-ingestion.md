# 02: 贯通 DOCX 结构化入库

**What to build:** 让知识管理员上传 DOCX，并在保留标题层级、段落、列表、表格文本和来源定位的前提下生成待审核 Chunk，使员工发布后能检索其中的业务事实和标识符。

**Blocked by:** 01 / 统一解析产物并贯通 Markdown 入库.

**Status:** ready-for-agent

- [ ] 合法 DOCX 通过同一 Indexer Seam 完成解析和索引。
- [ ] 标题、段落、列表和表格单元文本按文档顺序保留。
- [ ] Chunk 包含可显示的 heading path 或等效结构定位。
- [ ] SKU、数字、单位和英文缩写不会被清洗规则破坏。
- [ ] 加密、损坏或伪装的 DOCX 返回明确处理结果。
- [ ] 解析警告可供审核者查看且文档不会自动发布。
- [ ] PDF 和 Markdown 回归行为保持通过。

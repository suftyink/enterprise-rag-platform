# 03: 贯通 HTML 清洗与锚点入库

**What to build:** 让知识管理员上传 HTML，在移除脚本、样式、导航和重复噪声的同时保留正文、标题、链接文本和锚点，生成可审核且能回到原位置的知识 Chunk。

**Blocked by:** 01 / 统一解析产物并贯通 Markdown 入库.

**Status:** ready-for-agent

- [ ] 合法 HTML 通过同一 Indexer Seam 完成解析和索引。
- [ ] 脚本、样式、隐藏内容和危险活动内容不进入 Chunk。
- [ ] 标题层级、正文顺序、链接文本和有效锚点得到保留。
- [ ] 每个 Chunk 包含 heading path、anchor 或等效来源定位。
- [ ] 无正文、损坏或伪装输入返回明确处理结果。
- [ ] 解析警告可供审核者查看且文档不会自动发布。
- [ ] PDF 和 Markdown 回归行为保持通过。

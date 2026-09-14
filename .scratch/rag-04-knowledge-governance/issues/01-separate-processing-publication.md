# 01: 分离文档处理状态与发布状态

**What to build:** 让新文档在上传和索引后进入可审核但不可检索的状态，并迁移现有记录，使处理成功不再自动公开新版本，同时不破坏当前已发布知识的可用性。

**Blocked by:** rag-03-document-indexer/03 / 恢复部分索引并统一重建路径.

**Status:** ready-for-agent

- [ ] 处理状态与发布状态可独立表达且具有合法转换规则。
- [ ] 新上传文档处理成功后进入 REVIEW_PENDING 而非 PUBLISHED。
- [ ] REVIEW_PENDING 文档不会被 Retriever 返回。
- [ ] 当前 active READY 数据迁移为已处理且已发布。
- [ ] 历史非活动或已取代版本保留可解释的历史状态。
- [ ] 状态 API 能区分处理进度、审核状态和发布状态。

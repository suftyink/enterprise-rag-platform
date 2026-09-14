# 01: 通过 DocumentIndexer 完成 PDF 索引

**What to build:** 让一个已上传的文本 PDF 通过注入式 `DocumentIndexer.index()`完成读取、解析、Chunk、Embedding、数据库和向量写入，并让 Celery 只负责调用该 Seam、返回真实处理结果。

**Blocked by:** rag-01-knowledge-retriever/03 / 固化检索排序与可复现运行信息.

**Status:** ready-for-agent

- [ ] 文本 PDF 通过单一 Indexer 入口生成可由 Retriever 使用的 Chunk 和向量。
- [ ] Session factory、Blob Store、Parser、Embedding 和 Vector Writer 在组合根注入。
- [ ] 处理结果包含文档、决定、Chunk 数、警告和管线身份。
- [ ] 处理阶段和成功结果持久化后 Celery 才确认任务。
- [ ] Celery 返回 Indexer 的实际结果，不再固定报告 processed。
- [ ] 测试不需要 monkeypatch 全局 SessionFactory。

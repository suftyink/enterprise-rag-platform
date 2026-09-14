# 01: 建立 KnowledgeRetriever 并贯通兼容搜索

**What to build:** 让现有搜索请求从 FastAPI 进入单一的 `KnowledgeRetriever.retrieve()`，完成当前 Dense + BM25 + RRF + Fake Rerank 成功路径，并继续返回兼容的搜索响应，使调用方不再组装数据库、向量和模型依赖。

**Blocked by:** None (can start immediately).

**Status:** resolved

- [x] 合法租户查询通过 `KnowledgeRetriever.retrieve()` 返回与当前 API 等价的有序结果。
- [x] 检索请求和结果使用领域类型，不向调用方暴露 ORM、Milvus 或 Provider 响应对象。
- [x] FastAPI 只完成输入、输出和领域错误映射。
- [x] 候选召回、RRF、二次查询和 Rerank 仍保持现有可观察行为。
- [x] 当前搜索 API 契约和已有成功路径测试保持通过。
- [x] 新的主要行为测试只从 `KnowledgeRetriever.retrieve()` 进入。

## Answer

Added the typed Retriever Seam and composition dependency, moved search orchestration behind it, and preserved the public search response. The Retriever test suite now exercises the successful path through `retrieve()`, including one Dense request, one BM25 request and the configured RRF ranker in the production Milvus adapter.

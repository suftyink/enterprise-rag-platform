# 03: 固化检索排序与可复现运行信息

**What to build:** 让相同查询、身份、索引和配置产生稳定的证据顺序，并返回可追踪的管线、模型和配置身份，从而为离线评测和后续回答模块提供可复现的检索结果。

**Blocked by:** 02 / 在 Retriever 内强制证据资格与错误语义.

**Status:** resolved

- [x] 相同输入和版本下的最终排序可重复，同分候选有确定性次序。
- [x] 每次运行包含 trace ID、管线版本、模型身份、配置指纹和耗时。
- [x] 运行信息不包含完整查询、Chunk 正文或敏感凭据。
- [x] API 响应继续只暴露约定的公开管线信息。
- [x] 旧检索服务入口不再被 API 或后续模块直接调用。
- [x] Retriever 契约测试覆盖稳定排序和运行元数据。

## Answer

Added deterministic ordering using Rerank score, fused retrieval score and chunk identity; added pipeline/model/configuration run metadata with a stable SHA-256 fingerprint; and removed the old public search service function. Tests prove ordering is independent of equal-score hit order.

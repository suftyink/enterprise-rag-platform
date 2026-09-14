# 01: 接入普通知识的真实 Embedding 与 Rerank

**What to build:** 让普通敏感级别的知识使用批准的真实 Embedding 和 Rerank profile 建立可识别版本的索引，并通过正式检索与评测链路返回语义和精确标识符证据。

**Blocked by:** rag-02-retrieval-evaluation/03 / 发布可复现评测报告与 HTTP 验收 Adapter; rag-05-multiformat-ingestion/04 / 统一解析质量、安全限制与版本信息.

**Status:** ready-for-agent

- [ ] 普通知识可通过批准的真实 Embedding Adapter 建立 staging 索引。
- [ ] 查询使用与索引一致的模型版本和向量维度。
- [ ] 真实 Rerank Adapter 返回稳定的候选身份和分数。
- [ ] Provider 超时、限流、认证和无效响应转换为明确错误。
- [ ] 索引记录模型 deployment、版本、维度和检索 profile。
- [ ] Fake Provider 仍可用于本地确定性测试但不能标记为生产 profile。
- [ ] 语义改写问题和精确 SKU 问题都能进入同一评测报告。

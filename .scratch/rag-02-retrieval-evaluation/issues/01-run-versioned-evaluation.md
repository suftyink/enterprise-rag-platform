# 01: 运行版本化检索评测

**What to build:** 让 `RetrievalEvaluator.run()`读取经过校验的版本化 JSONL 数据集，通过正式 `KnowledgeRetriever` 执行全部问题，并生成 Recall@5、MRR@5、nDCG@5 和延迟等基础结果。

**Blocked by:** rag-01-knowledge-retriever/03 / 固化检索排序与可复现运行信息.

**Status:** ready-for-agent

- [ ] 数据集要求不可变 case ID、问题、评测主体、期望来源、期望决策和分类标签。
- [ ] 重复 ID、非法身份、缺失来源和未知字段组合在执行前被拒绝。
- [ ] 每个案例调用正式 Retriever，而不是复制检索逻辑。
- [ ] 报告正确计算 Recall@5、MRR@5、nDCG@5、P50 和 P95 延迟。
- [ ] 依赖失败记录为执行失败，不计作未召回。
- [ ] 合成数据仅用于契约测试，不被标记为生产基线。

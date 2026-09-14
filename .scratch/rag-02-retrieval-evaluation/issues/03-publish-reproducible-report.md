# 03: 发布可复现评测报告与 HTTP 验收 Adapter

**What to build:** 让每次评测输出绑定数据集、索引、解析、Chunk、模型和检索配置版本的可持久化报告，同时保留通过同一 Evaluator 运行的 HTTP Adapter 用于部署后端到端验收。

**Blocked by:** 02 / 增加分类指标与检索质量门禁.

**Status:** resolved

- [x] 报告包含数据集哈希、运行时间、索引身份、管线版本、模型身份和配置指纹。
- [x] 相同输入可关联和比较，配置不一致不会被误认为同一基线。
- [x] 默认执行路径为进程内 Retriever。
- [x] HTTP Adapter 通过同一个 `RetrievalEvaluator.run()` 入口运行。
- [x] HTTP 传输或认证失败被归类为部署执行失败。
- [x] 报告可供后续发布门禁机器读取且不包含敏感正文。

## Answer

已实现稳定比较键、JSON 报告持久化、默认进程内执行和显式 HTTP 验收 Adapter；报告仅包含非敏感运行身份、指标、案例 ID 与错误码。

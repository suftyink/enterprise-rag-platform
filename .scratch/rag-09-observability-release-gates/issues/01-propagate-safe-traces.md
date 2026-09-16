# 01: 贯通安全 Trace 与关键运行信号

**What to build:** 让一次上传、异步处理、检索或回答在 HTTP、Celery 和主要 RAG 阶段共享 trace 上下文，并记录可运营的耗时、数量、决定和错误码，同时通过字段白名单阻止敏感正文进入遥测。

**Blocked by:** rag-02-retrieval-evaluation/03 / 发布可复现评测报告与 HTTP 验收 Adapter; rag-04-knowledge-governance/03 / 支持回滚、清理补偿与并发审计; rag-07-grounded-answer/03 / 建立回答反馈与质量门禁; rag-08-identity-rbac-audit/03 / 强制管理员治理权限并完成安全审计.

**Status:** resolved

- [x] Trace context 从 HTTP 传播至 Celery 并覆盖解析、索引和状态转换。
- [x] 查询 Trace 覆盖 Embedding、召回、过滤、Rerank、门禁和回答。
- [x] 记录阶段耗时、候选数、Chunk 数、版本、决定和安全错误码。
- [x] 队列深度、重试、失败和卡住任务具有可采集信号。
- [x] 遥测字段使用白名单，完整问题、Chunk、Prompt、Token 和凭据不会出现。
- [x] Exporter 失败不会改变业务结果，但会产生可见运行告警。
- [x] 审计事件与运行 Trace 可通过 trace ID 关联。

## Answer

已贯通 HTTP、Celery、解析、索引、检索、模型和回答阶段的 trace ID，并加入白名单化运行遥测、队列快照信号和不影响业务结果的 Exporter 失败告警；安全审计与运行事件可按同一 trace ID 关联。

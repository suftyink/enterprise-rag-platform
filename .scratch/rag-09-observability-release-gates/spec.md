# Spec 09：建立可观测性、运行韧性与发布门禁

Type: spec
Status: resolved
Blocked by: rag-02-retrieval-evaluation, rag-04-knowledge-governance, rag-07-grounded-answer, rag-08-identity-rbac-audit
Primary test seam: `PilotReadiness.evaluate()`

## Problem Statement

当前系统只有基础健康检查、搜索 traceId 和单次耗时日志，缺少跨 FastAPI/Celery/解析/检索/模型/回答的结构化 Trace、审计关联、队列监控、错误预算、真实基础设施验证以及自动发布/回退门禁。单元测试通过不能证明生产试点可安全运行。

## Solution

建立 `PilotReadiness` 模块，以 `evaluate()` 汇总配置校验、依赖健康、评测报告、关键运行指标、安全审计完整性和恢复演练结果，产生可机器判定的发布结论。各模块发出白名单化遥测，Trace 在 HTTP 与异步任务间传播。

## User Stories

1. As an operator, I want one trace across upload, queue, parsing, indexing and publication, so that failures can be located quickly.
2. As an operator, I want retrieval and answer stages timed separately, so that latency regressions have an owner.
3. As a security reviewer, I want telemetry fields allowlisted, so that questions, chunks, prompts and tokens do not leak into logs.
4. As an operator, I want queue depth, retry count and stuck-job signals, so that indexing backlogs are visible.
5. As a release owner, I want quality, security and dependency checks combined into a release decision, so that no critical gate is forgotten.
6. As a release owner, I want any tenant leakage or invalid citation to block release, so that averages cannot hide critical failures.
7. As an operator, I want startup configuration validated, so that model, index, OIDC and storage mismatches fail early.
8. As an operator, I want backup, restore, rollback and orphan-cleanup procedures exercised, so that recoverability is demonstrated.
9. As a maintainer, I want real PostgreSQL, Redis, MinIO, Milvus and Celery integration checks, so that in-memory tests are not the only evidence.
10. As a pilot owner, I want capacity and latency evidence for expected load, so that employee access is intentionally bounded.
11. As an auditor, I want release evidence retained with version identities, so that every pilot deployment is explainable.

## Implementation Decisions

- `PilotReadiness.evaluate()` is the sole release-readiness Seam and returns a structured report with pass, fail or indeterminate status per gate.
- Trace context propagates through HTTP, Celery and all major RAG stages; safe attributes include trace ID, stage, versions, counts, latency and reason/error codes.
- Full questions, document text, chunks, prompts, tokens and credentials are forbidden telemetry attributes by default.
- Required runtime signals include request outcome/latency, retrieval candidates, rerank/refusal reasons, model failures, parsing quality, indexing duration, queue depth/retries, lifecycle transitions and authorization denials.
- Readiness verifies production configuration coherence: OIDC mappings, model-route policy, embedding/index dimensions, storage, database migrations and required worker availability.
- Release input includes the latest approved retrieval and answer evaluation reports and rejects stale dataset/index/model combinations.
- Hard failures include tenant leakage above zero, citation support below 95%, retrieval gates below target, refusal accuracy below 90%, missing critical audit events or unavailable mandatory model routes.
- Real-infrastructure integration and a bounded load/capacity run are required before production-pilot approval.
- Recovery evidence covers database backup/restore, index rebuild, previous-profile rollback, failed cleanup retry and old published-version continuity.
- Gate reports are versioned and immutable; secrets and content are excluded.

## Testing Decisions

- The sole primary testing Seam is `PilotReadiness.evaluate()`.
- Tests supply controlled health, evaluation, telemetry and recovery evidence and assert gate aggregation, stale-evidence rejection, hard-failure precedence and safe report content through that Seam.
- Recording telemetry exporters may be used to prove allowlisting and trace continuity as evidence consumed by the readiness evaluation.
- Live infrastructure and load runs produce readiness inputs but do not create parallel acceptance semantics.
- Existing health and evaluation checks are prior art and should feed the unified report rather than remain unrelated release signals.

## Out of Scope

- Public-SaaS scale, multi-region disaster recovery and 24/7 commercial SLOs.
- Choosing a specific observability vendor where standard telemetry is sufficient.
- Automatic production deployment or rollback without human authorization.
- Storing user content for analytics.
- Building the employee Web UI.

## Further Notes

Final capacity numbers and alert thresholds must be calibrated against the pilot population and deployment environment. An unavailable measurement is `indeterminate`, not silently passing.

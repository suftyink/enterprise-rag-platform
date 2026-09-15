# Spec 04：分离知识处理与审核发布生命周期

Type: spec
Status: resolved
Blocked by: rag-03-document-indexer
Primary test seam: `KnowledgeCorpus`

## Problem Statement

当前索引成功会立即把新版本设为 `READY + active` 并替换旧版本，处理完成等同于正式发布。管理员无法先复核解析质量、敏感范围和版本差异，也缺少撤回、回滚、停用、删除、审计和跨存储清理边界。这与“审核后发布、未发布不参与检索”的生产规划直接冲突。

## Solution

建立 `KnowledgeCorpus` 模块，以 submit、inspect 和 transition 组成一个治理 Seam。处理状态与发布状态分离，新版本索引成功后进入待审核，只有合法角色执行 publish 后才成为正式检索版本；撤回和回滚立即影响检索可见性，物理清理由可补偿任务完成。

## User Stories

1. As a knowledge administrator, I want new documents to wait for review after indexing, so that unverified content is not searchable.
2. As a reviewer, I want to inspect processing warnings and version metadata, so that publication decisions are evidence-based.
3. As a reviewer, I want to publish a reviewed version, so that employees receive only approved knowledge.
4. As a reviewer, I want to reject or withdraw unsafe content, so that it immediately stops participating in retrieval.
5. As a reviewer, I want to roll back to a previously published version, so that a bad update can be reversed safely.
6. As an employee, I want publication switches to preserve an available version, so that updates do not create avoidable outages.
7. As an auditor, I want every lifecycle action attributed with actor, time, reason and revision, so that changes are accountable.
8. As an operator, I want failed vector cleanup recorded and retried, so that orphaned data is eventually removed.
9. As a maintainer, I want concurrent transitions protected by expected revision, so that reviewers cannot overwrite each other silently.
10. As a data owner, I want explicit retention and purge states, so that deletion is controlled and recoverability is known.

## Implementation Decisions

- `KnowledgeCorpus` is the only public governance Seam; its use cases are submit, inspect and transition.
- Processing state and publication state are separate fields with independent legal transitions.
- Processing success produces `REVIEW_PENDING`, never `PUBLISHED`.
- Publication atomically selects one current published version per knowledge scope and document key; the previous version becomes superseded only after the new publication succeeds.
- Withdrawal removes the version from retrieval immediately through PostgreSQL truth; vector deletion is asynchronous and idempotent.
- Rollback selects a previously valid indexed version through the same publication transition rather than rewriting document contents.
- Every transition requires an actor, reason and expected revision and writes an immutable audit event.
- Hash deduplication semantics are scoped to a document identity/version policy, not silently shared across unrelated document keys.
- Retrieval continues to use PostgreSQL as the final authority for publication and active-version checks.
- Existing records are migrated deterministically: current active READY records become published; inactive/superseded records retain historical meaning.

## Testing Decisions

- The sole primary testing Seam is `KnowledgeCorpus`.
- Tests exercise complete lifecycle scenarios through submit, inspect and transition: processing completion, review, publish, reject, withdraw, republish, rollback, purge request and invalid transition.
- Tests assert externally observable corpus state, retrieval eligibility, audit events and compensation requests, not internal state-machine helper calls.
- Concurrency tests use expected revision through the same Seam.
- `DocumentIndexer` is represented by controlled outcomes; its behavior is not retested here.

## Out of Scope

- OIDC token verification; this spec accepts a trusted actor abstraction that Spec 08 will populate.
- Designing the final Web review interface.
- OCR and new document formats.
- Physical immediate deletion guarantees across all stores.
- Public SaaS retention, legal hold and regional residency features.

## Further Notes

Authorization hooks must exist at the module boundary, but production identity wiring remains blocked until Spec 08. No governance endpoint should be exposed to pilot users before that wiring is complete.

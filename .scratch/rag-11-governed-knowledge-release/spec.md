# Spec 11：解耦知识治理、版本发布与员工试点门禁

Type: spec
Status: claimed
Blocked by: rag-04-knowledge-governance, rag-06-real-retrieval-quality, rag-09-observability-release-gates, rag-10-internal-web-pilot
Primary test seam: internal Web knowledge-release workflow

## Problem Statement

当前内部 Web 使用同一个试点门禁保护员工问答和管理员知识治理。缺少通过且有效的 `PilotReadiness` 报告时，员工不能问答是正确的，但知识管理员也不能上传、审核、发布、撤回或回滚文档。与此同时，生成可信 readiness 证据又依赖已准备好的语料、索引和评测结果，因此首次部署和后续修复都会陷入“没有知识就不能通过门禁、没有通过门禁又不能治理知识”的循环依赖。

当前文档 `PUBLISHED` 还同时表达“内容已获治理批准”和“内容立即进入员工检索范围”。常规发布会直接改变活动语料资格，而增量索引写入活动 Profile，使正在提供服务的知识版本和先前通过评测的发布证据可能发生漂移。系统虽然已经具备 corpus generation、候选 Profile、构建清单、评测、release fingerprint、readiness 和回滚能力，但缺少一个把这些能力串成不可变候选版本并受控激活的发布工作流。

生产试点需要让管理员在员工入口关闭时仍能安全准备和修复知识，让当前活动版本在下一候选构建期间继续稳定服务，并保证只有与准确语料、索引、配置和评测证据绑定的版本才能向指定员工开放。

## Solution

把系统明确拆成控制面、发布面和服务面：知识治理控制面始终可供已授权管理员使用；发布面把获批内容冻结为不可变 `CorpusRelease` 候选，完成索引构建、离线评测、readiness 和人工批准后原子激活；员工问答服务面只访问当前活动 Release，并继续受试点范围和运行门禁保护。

内容治理状态、发布版本状态和试点开放状态分别建模。文档获批只代表它可以进入下一候选语料，不直接改变当前员工服务结果；新候选在构建和评测期间不影响当前活动 Release。激活操作绑定完整版本指纹并保留上一版本供回滚。敏感或错误内容可走紧急撤回路径，通过数据库资格检查立即停止作为证据，随后异步清理物理向量并生成修复 Release。

先以最小改动解除治理 API 与试点门禁的循环依赖，再引入 Release 聚合和原子激活，最后增加按员工组灰度、持续监控和暂停/回滚能力。

## User Stories

1. As a knowledge administrator, I want to open the governance workspace while the employee pilot is disabled, so that I can prepare the knowledge required to make the system ready.
2. As a knowledge administrator, I want to upload documents before a readiness report exists, so that a first production corpus can be created without bypassing release controls.
3. As a knowledge administrator, I want to inspect parsing status, warnings and source metadata while serving is unavailable, so that ingestion defects can be corrected before evaluation.
4. As a knowledge administrator, I want to approve content for a future release without immediately exposing it to employees, so that content review and service activation remain separate decisions.
5. As a knowledge administrator, I want to withdraw or supersede content within my authorized scope, so that obsolete knowledge does not enter future releases.
6. As a knowledge administrator, I want governance failures to identify authentication, authorization, validation or lifecycle causes separately, so that I know how to correct them.
7. As a system administrator, I want governance actions protected by SSO, RBAC, resource scope, CSRF and audit even when the pilot gate is closed, so that removing the circular dependency does not weaken security.
8. As a system administrator, I want global-scope changes to remain restricted and explicitly confirmed, so that broad corpus changes are deliberate.
9. As a release owner, I want to freeze the approved documents into an identified candidate corpus generation, so that every release has a reproducible source snapshot.
10. As a release owner, I want the candidate to record corpus hash and document count, so that later evidence can prove exactly what was evaluated.
11. As a release owner, I want candidate indexing to use a staging Profile independent from the current active Profile, so that building a new release cannot corrupt current service.
12. As a release owner, I want candidate construction to fail if its corpus changes during the build, so that a mixed or incomplete snapshot cannot be activated.
13. As a release owner, I want unchanged vector artifacts to be reused when their document and processing fingerprints match, so that routine releases avoid unnecessary embedding cost.
14. As a release owner, I want changed or new document versions to be parsed, chunked and embedded incrementally, so that normal knowledge updates do not require full-corpus reprocessing.
15. As a release owner, I want parser, chunking, embedding-dimension or index-contract changes to force an appropriate rebuild, so that incompatible artifacts are never silently reused.
16. As an evaluator, I want retrieval and answer evaluation to run against the exact candidate Release, so that quality results cannot be borrowed from another corpus or index.
17. As an evaluator, I want evaluation failures and indeterminate evidence to prevent activation, so that uncertainty never silently passes a production gate.
18. As a pilot owner, I want `PilotReadiness` to bind the release version, corpus hash, index build hash and route fingerprints, so that approval applies to one exact deployable artifact.
19. As a pilot owner, I want stale or mismatched readiness evidence rejected, so that an old PASS cannot open a changed release.
20. As a system administrator, I want to review the candidate identity, evaluation result, capacity, monitoring owner and rollback owner before activation, so that the release decision is accountable.
21. As a system administrator, I want activation to switch one atomic active-release pointer, so that employees never observe a partially switched corpus.
22. As an operator, I want the previous active Release retained as a rollback target, so that recovery does not require re-uploading or re-embedding documents.
23. As an operator, I want a failed activation to leave the previous Release active, so that release errors do not interrupt a healthy pilot.
24. As an employee, I want questions to use only the current active Release, so that answers correspond to the release that was evaluated and approved.
25. As an employee, I want a clear unavailable response when no approved Release or pilot rollout applies to me, so that a release gate is not confused with an application crash.
26. As a pilot owner, I want access enabled first for an explicit canary group, so that production behavior can be observed with limited impact.
27. As a pilot owner, I want to expand, pause or disable a rollout without rebuilding the Release, so that exposure can respond quickly to operational evidence.
28. As an operator, I want pilot access decisions evaluated against the current Release and user context, so that a rollout approval cannot accidentally carry over to a different version or audience.
29. As an operator, I want candidate builds to continue while the current Release serves employees, so that routine governance does not create avoidable downtime.
30. As a security responder, I want an emergency withdrawal to make a risky document immediately ineligible at retrieval time, so that physical vector deletion need not finish before containment.
31. As a security responder, I want emergency withdrawal to mark the active Release degraded or its readiness stale, so that follow-up evaluation and release repair cannot be forgotten.
32. As an operator, I want vector cleanup and replacement-release construction to run asynchronously after emergency withdrawal, so that immediate containment remains fast and recoverable.
33. As an auditor, I want content approval, candidate creation, evaluation, activation, rollout changes, emergency withdrawal and rollback recorded with actor and trace identity, so that the complete release chain is explainable.
34. As an auditor, I want activity records to reference immutable Release and evidence identifiers without storing document text, questions, prompts or credentials, so that auditability does not leak sensitive content.
35. As a maintainer, I want existing valid active Profile and manifest data migratable into an initial Release without unnecessary re-embedding, so that adoption does not disrupt the current pilot.
36. As a maintainer, I want invalid or incomplete legacy manifests to require a fresh candidate build, so that migration cannot manufacture missing evidence.

## Implementation Decisions

- The architecture has three explicit planes:
  - The governance control plane handles upload, processing visibility, content review, approval, withdrawal and rollback preparation.
  - The release plane handles corpus snapshotting, candidate indexing, evaluation, readiness evidence, human approval and activation.
  - The serving plane handles employee retrieval, cited answers and feedback against the current active Release.
- Governance endpoints require an authenticated actor and retain server-side administrator role checks, resource-scope authorization, CSRF protection, optimistic concurrency, lifecycle validation, audit events and trace correlation. They do not require the employee pilot gate to be enabled.
- Employee answer and feedback endpoints continue to fail closed when no applicable active Release and rollout approval exist. Pilot status remains available to authenticated users, while pilot-management operations remain restricted to system administrators.
- The Web UI presents governance availability, candidate release status and employee pilot availability separately. A closed employee pilot must not disable or describe the governance workspace as unavailable.
- Content governance, release and rollout use independent state dimensions:
  - Content governance expresses processing and approval eligibility such as processing, review pending, approved, rejected, superseded and withdrawn.
  - Release state expresses building, evaluating, ready, active, rollback and failed.
  - Rollout state expresses disabled, canary, enabled and paused.
- Existing document publication terminology is treated as content approval for candidate inclusion, not proof that the document is live for employees. Compatibility aliases may be retained at API boundaries during migration, but the domain model and UI must use unambiguous language.
- Introduce a `CorpusRelease` aggregate as the authoritative deployable identity. It records at least a release identifier and version, corpus generation and hash, document count, Profile identifier, index identity and build hash, parser/chunk/embedding/configuration fingerprints, retrieval and answer route fingerprints, evaluation report identifiers, readiness report identifier, approval metadata, lifecycle status and timestamps.
- Candidate creation freezes the approved content set and its authorization-relevant metadata. Candidate build and evaluation always use this frozen snapshot, not a moving query over current document rows.
- Routine ingestion and indexing must not mutate the physical index serving the current active Release. New and changed artifacts are prepared for a candidate staging Profile or reusable artifact store.
- Artifact reuse is content- and configuration-addressed. An artifact is reusable only when document version/content hash and parser, chunk, embedding model, vector dimension and relevant index contract fingerprints match.
- New documents and changed document bodies are processed incrementally. A publication/approval state change for already prepared content does not by itself recalculate embeddings.
- Embedding model or dimension changes, incompatible parser/chunk changes, index schema/analyzer changes, or another retrieval-contract incompatibility require a full candidate rebuild. A normal application deployment does not require rebuilding knowledge when all release fingerprints remain compatible.
- Candidate build records a manifest and verifies corpus generation/hash and document count before and after construction. Drift makes the candidate failed or obsolete; it is never silently repaired into the same identity.
- Existing retrieval and answer evaluation runs are bound to the candidate identity. `PilotReadiness.evaluate()` remains the single readiness aggregation authority and consumes only evidence matching that identity.
- Activation requires a current PASS readiness report and explicit system-administrator approval containing pilot scope, capacity, monitoring owner, rollback owner and reason. Approval and readiness must reference the same `CorpusRelease` fingerprint.
- Activation changes the active Release/Profile binding atomically. The prior active Release becomes the rollback target and remains intact for a defined rollback window. Failure before commit leaves the old Release active.
- `PilotAccessGate` evaluates the current active Release, rollout state and actor context. An approval for one Release or audience cannot authorize another Release or audience.
- Rollout control is independent of Release construction and activation. Pausing or narrowing employee access does not delete knowledge, disable governance or destroy the rollback target.
- Retrieval requires membership in the active Release, actor scope authorization and absence of an emergency withdrawal. Current mutable `PUBLISHED` state alone is insufficient to introduce new content into an active Release.
- Emergency withdrawal is an explicit fail-closed exception. It immediately adds a database-backed retrieval exclusion, records a security/governance audit event, marks the affected Release degraded or readiness stale, and schedules physical vector cleanup plus a corrected candidate Release.
- Retrieval retains a database-backed eligibility/exclusion check after vector search so that emergency withdrawal takes effect before asynchronous vector deletion finishes.
- Bootstrap migration creates an initial `CorpusRelease` from the current active Profile only when its manifest, corpus snapshot and build identity validate. Otherwise the system requires a fresh candidate build and does not fabricate a PASS or release fingerprint.
- Stable machine-readable error codes distinguish authentication failure, authorization denial, pilot unavailable, no active Release, stale or mismatched evidence, candidate drift, activation conflict and emergency withdrawal.
- Every release-changing command is idempotent or concurrency-protected. Conflicting activation, approval or lifecycle revisions fail explicitly rather than using last-write-wins behavior.
- Implementation is delivered in four ordered increments:
  1. Unblock governance while preserving authorization and keep employee serving gated.
  2. Add independent content approval and `CorpusRelease` candidate identities.
  3. Bind build, evaluation, readiness and approval evidence and activate atomically with rollback.
  4. Add cohort-aware canary rollout, monitoring-driven pause and emergency repair workflow.

## Testing Decisions

- The sole primary testing Seam is the internal Web knowledge-release workflow, exercised through the same HTTP and browser boundaries used by knowledge administrators, system administrators and employees.
- Tests assert externally visible state, authorization decisions, release identities, answers and audit outcomes. They do not assert private helper calls, SQL query shape or internal class structure.
- The existing internal Web browser workflow is the primary prior art. Existing lifecycle, retrieval, Profile activation and `PilotReadiness.evaluate()` tests remain supporting evidence and are extended only where a lower-level contract cannot be observed reliably through the primary Seam.
- The primary workflow covers an empty bootstrap environment: with no readiness report, an authorized knowledge administrator can upload, inspect and approve content while an employee answer request remains unavailable.
- The workflow proves that unauthenticated users, ordinary employees and out-of-scope administrators cannot use governance capabilities after the pilot dependency is removed.
- The workflow creates a candidate from approved content, observes build and evaluation progress, and verifies that current employee answers remain bound to the old active Release until activation.
- The workflow attempts activation with missing, failed, stale and fingerprint-mismatched evidence and observes fail-closed outcomes with stable reason codes.
- The workflow activates a passing candidate and verifies one coherent switch: subsequent answers identify/use the new Release while the prior Release remains available for rollback.
- The workflow injects a failure before activation commit and verifies that the old Release remains active and available.
- The workflow rolls back and verifies restoration of the prior Release without re-uploading, reparsing or re-embedding its documents.
- The workflow verifies that adding a document or changing its body processes only changed content, while compatible unchanged artifacts are reused.
- The workflow verifies that embedding dimension, parser/chunk contract or incompatible index changes reject reuse and require a complete candidate build.
- The workflow verifies canary targeting: eligible pilot members can ask against the active Release while nonmembers receive the closed-gate response.
- The workflow pauses a rollout and verifies that employee traffic stops while governance and release repair remain operational.
- The workflow performs an emergency withdrawal and verifies that the document immediately disappears from citations even while its physical vector cleanup remains pending.
- The workflow verifies that every governance, release, rollout, withdrawal and rollback action produces an actor- and trace-linked audit record without raw document or question content.
- Production-like acceptance still requires real OIDC identities, PostgreSQL, Redis, object storage, vector infrastructure, workers, model routes and representative evaluation data; controlled doubles may be used for deterministic automated coverage but cannot replace final pilot evidence.

## Out of Scope

- Replacing the existing parser, chunker, embedding provider, vector database or answer-generation architecture solely for this release workflow.
- Building public self-service document management, public registration, billing or external customer tenancy.
- Implementing automatic content approval or allowing model output and employee feedback to become published knowledge without human review.
- Automatically promoting a candidate to production without explicit human authorization.
- Multi-region active-active serving, cross-region disaster recovery or zero-RPO guarantees.
- A general-purpose feature-flag platform; rollout targeting is limited to the internal RAG pilot.
- Re-embedding the entire corpus on every application deployment or ordinary document publication.
- Removing historical release, audit or evaluation evidence before its defined retention window.

## Further Notes

The first implementation increment is a safety and operability correction, not a readiness bypass: it moves the existing gate from the governance control plane to the employee serving boundary while keeping all administrator security controls intact.

For a local demonstration, the first increment removes the `READINESS_REPORT_MISSING` governance deadlock. For an internal production pilot, the feature is not complete until candidate identity, evidence binding, atomic activation and rollback are implemented and exercised against production-like infrastructure.

The recommended operational sequence is:

`upload/process -> review/approve content -> freeze candidate corpus -> build staging index -> evaluate -> release_check -> human approval -> atomic activation -> canary -> monitor -> expand, pause or rollback`.

The accompanying research is retained under the same production-pilot effort and provides the official-source basis for control-plane separation, readiness semantics, versioned index switching, incremental processing, pre-deployment evaluation, canary rollout and least-privilege authorization.

## Answer

代码侧发布工作流与自动化验收已实现，01–09 已完成；生产类真实基础设施与代表性身份/语料验收仍由票 10 跟踪。在该验收和同一 Release 的最终 PASS/人工批准完成前，Spec 11 保持 claimed，试点不得视为生产批准。

# Spec 12：建立可恢复的 RAG 首次初始化流程

Type: spec
Status: claimed
Label: ready-for-agent
Blocked by: rag-11-governed-knowledge-release
Primary test seam: internal Web first-run knowledge initialization workflow

## Problem Statement

全新部署完成数据库迁移后，系统可能没有任何 `RetrievalProfile` 或 `CorpusRelease`。内部 Web 此时却只提示管理员“迁移当前活动 Profile”，并要求手工输入一个 Profile UUID。迁移入口只接受已有、已激活且构建清单完整的 Profile；候选创建入口又只接受已有 STAGING Profile；生产代码没有创建首个 Profile 的入口，因此管理员无法从空数据库建立第一个可发布知识版本。

首次安装死路还暴露出两个相邻问题。第一，API 和 Worker 不会在启动时确认数据库 Alembic revision 与代码兼容，数据库落后时进程仍能启动，随后在业务请求中因缺表或缺列返回 HTTP 500。第二，Milvus Collection 的物理名称没有覆盖完整 Embedding binding 与构建身份，发现同名 Collection 时也不核对实际 schema 和索引配置，可能把历史遗留或并发创建的冲突 Collection 当成成功产物。

现有系统已经具备文档治理、候选 Release、构建清单、评测、Readiness、人工批准、原子激活、灰度和回滚能力，但这些能力缺少全新安装的起点。管理员需要一条从空数据库开始、可观察、可恢复且不绕过现有发布门禁的正式初始化路径，而不是手工写数据库、伪造 Profile UUID 或直接开放试点。

## Solution

在内部 Web 增加“初始化知识库”向导，并在后端建立一个深的首次初始化 Module。该 Module 以一个幂等初始化命令隐藏 Profile UUID、物理 Collection、配置指纹、构建清单和并发协调细节，持久化每次初始化运行的阶段、Attempt、结果和稳定错误码。

全新安装可以先创建经过验证的 STAGING Profile 和空的独立物理 Collection。空 Collection 只表示基础设施已 Provision，不代表存在可发布语料，也不能成为 ACTIVE Release。管理员上传并批准文档后，初始化流程冻结语料快照、构建候选索引并建立候选 `CorpusRelease`；此后复用现有评测、Readiness、管理员批准、激活和 rollout 流程。员工问答在完整发布链完成前继续失败关闭。

初始化入口根据实际状态选择模式：无 Profile 时创建全新初始化；存在有效 ACTIVE Profile 但没有 Release 时执行兼容迁移；存在未完成初始化运行或 STAGING Profile 时恢复原运行；存在 ACTIVE Release 时进入日常治理和候选发布，不再显示首次初始化操作。

同时增加启动期数据库 Schema 兼容门禁，并强化物理索引 Provision：物理名称与身份覆盖数据分类、schema 版本、Embedding binding、维度和 Profile/build token；已存在资源必须完整比对后才能视为幂等成功。PostgreSQL 的 `CorpusRelease ACTIVE` 继续作为唯一发布真相，不再引入 Milvus Alias 作为第二个活动指针。

## User Stories

1. As a system administrator, I want a clear “初始化知识库” action on a fresh installation, so that I do not need a pre-existing Profile UUID.
2. As a system administrator, I want the UI to distinguish fresh initialization from legacy Profile migration, so that I do not choose an impossible operation.
3. As a system administrator, I want internal identifiers hidden by the initialization workflow, so that I cannot accidentally bind the wrong Profile or Collection.
4. As a system administrator, I want initialization to preflight PostgreSQL, Redis, object storage, Milvus, workers and model routes, so that configuration failures are found before an expensive build begins.
5. As a local developer, I want a safe Fake Provider default to be preselected, so that I can initialize the development environment without configuring production models.
6. As a production operator, I want initialization choices restricted to validated Runtime Deployments, so that an arbitrary model or index cannot enter the release pipeline.
7. As a production operator, I want multiple valid Runtime Deployments presented as explicit choices, so that the system does not silently choose the wrong security boundary or data classification.
8. As a system administrator, I want an empty STAGING Profile and physical Collection to be provisionable before documents exist, so that infrastructure preparation is not confused with content publication.
9. As an employee, I want an empty or partially initialized knowledge base to remain unavailable for answers, so that setup progress is never mistaken for an approved production corpus.
10. As a knowledge administrator, I want governance and upload capabilities available while initialization is waiting for content, so that I can prepare the first approved corpus.
11. As a knowledge administrator, I want uploaded content to remain outside the active employee Release until explicitly frozen and activated, so that ingestion does not silently change production answers.
12. As a release owner, I want initialization to freeze the first approved corpus into an immutable candidate, so that the initial evaluation is reproducible.
13. As a release owner, I want the first candidate to use an independent physical index, so that later retries or migrations cannot corrupt another version.
14. As a release owner, I want the initialization run to expose its current stage and progress, so that I can distinguish preflight, provisioning, waiting for content, building and evaluation preparation.
15. As a release owner, I want initialization to continue after closing the browser, so that a UI session is not the owner of a long-running operation.
16. As a release owner, I want repeated clicks with the same operation identity to return the same initialization run, so that retries do not create duplicate Profiles or Releases.
17. As a release owner, I want concurrent initialization attempts for the same data classification to resolve deterministically, so that only one nonterminal run owns the first Profile.
18. As an operator, I want a failed stage to record a stable reason code and retryability, so that remediation can be automated and explained.
19. As an operator, I want to retry only the failed stage when immutable inputs have not changed, so that completed parsing or index work is not repeated unnecessarily.
20. As an operator, I want changed configuration or corpus inputs to create a new Attempt or candidate identity, so that previous evidence is not silently overwritten.
21. As an operator, I want to cancel a nonterminal initialization safely, so that abandoned provisioning work does not remain indefinitely active.
22. As an operator, I want Worker heartbeat loss or an expired lease to converge to a visible failed state, so that initialization cannot remain permanently “处理中”.
23. As an operator, I want incomplete physical index artifacts isolated from active retrieval, so that a failed build cannot leak partial results.
24. As an operator, I want an existing physical Collection accepted only when its full schema and binding identity match, so that name reuse does not hide incompatibility.
25. As an operator, I want an index identity collision reported explicitly, so that the system never repairs a conflicting Collection by destructive overwrite.
26. As an operator, I want a successful old ACTIVE Profile with a valid manifest migratable without re-embedding, so that upgrades remain efficient.
27. As an operator, I want an incomplete or stale old Profile rejected from migration, so that bootstrap cannot fabricate build evidence.
28. As an operator, I want rejected legacy migration to offer fresh initialization as the next action, so that the UI does not leave me at another dead end.
29. As an evaluator, I want the first candidate to use the existing retrieval and answer evaluation contracts, so that first installation is not exempt from quality requirements.
30. As a pilot owner, I want the first Release to require a current matching PASS Readiness report, so that initialization cannot manufacture production approval.
31. As a system administrator, I want explicit approval and rollout controls after initialization, so that provisioning alone never opens employee traffic.
32. As an employee, I want the first successful answer to identify the activated Release, so that it is auditable against the approved corpus.
33. As an auditor, I want every initialization, retry, cancellation, migration and collision recorded with actor and trace identity, so that the origin of the first production Release is explainable.
34. As a security reviewer, I want initialization telemetry and audit data to omit document text, prompts, credentials and provider secrets, so that operability does not create a disclosure path.
35. As a developer, I want an outdated database rejected before serving business requests, so that schema incompatibility cannot surface as an unrelated HTTP 500.
36. As a developer, I want the startup error to name the current and expected revisions and the remediation command, so that local recovery is immediate.
37. As a production operator, I want API and Worker processes to enforce the same Schema compatibility rule, so that mixed revisions cannot process incompatible records.
38. As a production operator, I want a database revision ahead of or divergent from the code rejected distinctly from a merely outdated database, so that rollback and deployment mistakes are diagnosable.
39. As a maintainer, I want one initialization interface shared by the Web UI, tests and future CLI Adapter, so that lifecycle rules remain local to one Module.
40. As a maintainer, I want current Release activation and rollback semantics preserved, so that fixing first-run setup does not introduce a second publishing mechanism.

## Implementation Decisions

- Introduce a `KnowledgeInitialization` Module as the sole orchestration interface for first-run status, start, retry and cancellation. Callers provide intent and an idempotency key; they do not construct or select raw Profile IDs.
- Persist a `KnowledgeInitializationRun` aggregate containing at least run identity, data classification, mode, stage, status, Attempt, idempotency key, configuration fingerprint, Profile identity, candidate Release identity, actor, trace, stable failure code, retryability, revision and timestamps.
- Initialization modes are `FRESH`, `MIGRATE_ACTIVE` and `RESUME`. The server derives the allowed mode from persisted state; the client cannot force migration when no ACTIVE Profile exists.
- Initialization stages are preflight, Profile Provision, physical index Provision, waiting for approved content, corpus freeze, candidate build and handoff to evaluation. Status is independently represented as queued, running, waiting, succeeded, failed or cancelled.
- Only one nonterminal initialization run per data classification is permitted. Starting with the same idempotency key returns the existing run; a competing key receives a stable conflict containing the resumable run identity.
- An empty physical Collection and STAGING Profile are valid Provision results. A zero-document corpus remains invalid for candidate build completion, Readiness and activation.
- Keep `RetrievalProfile` lifecycle focused on STAGING, ACTIVE and ROLLBACK. Provisioning progress and failure belong to `KnowledgeInitializationRun`, avoiding a second overlapping Profile state machine.
- Extend the persisted Profile identity with the physical index locator, index schema fingerprint and Embedding binding fingerprint needed to verify an existing Collection. Existing logical `index_identity` remains the deployable lookup identity.
- The physical Collection name is derived from immutable inputs: base name, data classification, index schema version, vector dimension, Embedding binding hash and deterministic Profile/build token. Retrying the same operation resolves to the same name; a new incompatible Profile resolves to a different name.
- Introduce an index Provision seam with two concrete Adapters: the Milvus Adapter for deployed infrastructure and an in-memory/fake Adapter for deterministic tests. Its interface creates or reconciles a physical index and returns an observed identity; it never activates a Release.
- Provision checks existence, then describes and compares fields, vector dimensions, metrics, analyzer/BM25 function, scalar filter fields, index definitions and stored fingerprints. A concurrent already-exists result triggers describe-and-compare, not unconditional success.
- A completely matching existing physical index is an idempotent success. Any mismatch returns `INDEX_IDENTITY_COLLISION`; initialization must not drop, rename or overwrite the resource automatically.
- Continue using the PostgreSQL `CorpusRelease ACTIVE` row as the only serving pointer. Milvus aliases are not introduced because their state cannot participate atomically in the PostgreSQL activation transaction.
- Preserve the existing legacy bootstrap behavior behind the unified initialization Module. It may create an initial ACTIVE CorpusRelease only from an existing ACTIVE Profile whose build manifest, corpus snapshot and physical identity all validate.
- If legacy validation fails, the UI presents the stable cause and offers FRESH initialization. It does not mutate the legacy Profile or invent missing hashes.
- For development with Fake providers and no explicit Runtime Deployment, expose one clearly labelled local default using the current Fake Embedding dimension, Fake Rerank binding, parser/chunk versions and a dedicated versioned physical Collection.
- Production forbids the development default. It requires a validated Runtime Deployment with an allowed provider boundary, reachable model routes, consistent vector dimension and a dedicated physical index location.
- If exactly one production runtime option is valid, the UI may preselect it but still requires administrator confirmation. If multiple options are valid, the administrator must select one. Secrets and API keys are never returned to the browser.
- Initialization Provision may run before documents exist and then enter `WAITING_FOR_CONTENT`. Upload, parsing and governance continue through their existing control-plane interfaces.
- Continuing from `WAITING_FOR_CONTENT` freezes only approved, retained and correctly classified documents. The resulting candidate uses the existing CorpusRelease membership snapshot and build manifest rules.
- The initialization Module schedules long-running build work through the existing Worker infrastructure and persists ownership/lease information. Browser requests return the run view immediately rather than holding an HTTP transaction open.
- Retry increments Attempt and resumes from the earliest invalid or failed stage. Completed immutable stages may be reused only when their configuration fingerprint, corpus hash and physical identity still match.
- Cancellation prevents new stages from starting, records an audit event and leaves incomplete staging artifacts ineligible for serving. Physical cleanup is a separate, retryable and auditable action.
- Reuse the existing evaluation, Readiness, approval, activation and rollout interfaces after the first candidate reaches the evaluation handoff. Initialization does not create PASS evidence, approve a Release or enable rollout.
- Add a startup Schema compatibility Module that compares the database Alembic revision with the code head before API or Worker startup completes.
- An outdated database fails startup with `DATABASE_SCHEMA_OUTDATED`; a database ahead of the binary uses `DATABASE_SCHEMA_AHEAD`; an unknown/divergent revision uses `DATABASE_SCHEMA_DIVERGED`. The diagnostic includes safe revision identifiers and the appropriate operator action, never connection credentials.
- API and Worker use the same Schema compatibility interface. Migration commands and isolated migration tests do not import the running application and remain able to perform upgrades.
- The internal Web status response includes initialization availability, allowed mode, resumable run and safe runtime options. It never returns Profile secrets, provider keys or raw infrastructure credentials.
- Add authenticated, CSRF-protected system-administrator operations to start, retry and cancel initialization. Read access follows existing internal administrator authorization; mutations produce audit records and use optimistic revision checks.
- Replace the raw Profile UUID form in the Web UI with state-driven actions: “初始化知识库”, “迁移当前版本”, “继续初始化” or the normal Release workflow. Internal IDs may remain visible in diagnostic detail but are not required input.
- The UI displays stage, Attempt, progress, last safe error, retryability and next action. It distinguishes `WAITING_FOR_CONTENT` from failure and links the administrator to upload/governance when no approved documents exist.
- Employee answers and feedback continue to require the existing active Release and rollout gate throughout initialization. No temporary preview route shares the production answer endpoint.
- Any optional local retrieval preview is explicitly non-production, uses the candidate identity and cannot emit Readiness or approval evidence.
- Implementation proceeds in three ordered increments: Schema startup compatibility; initialization aggregate plus safe Profile/index Provision; internal Web orchestration and recovery integrated with the existing Release workflow.

## Testing Decisions

- The sole primary testing Seam is the internal Web first-run knowledge initialization workflow. It exercises the same browser and HTTP surfaces used by system administrators, knowledge administrators and employees.
- Tests assert externally visible status, authorization, idempotency, Release identity, retrieval availability, audit outcomes and stable error codes. They do not assert helper calls or SQL query shapes.
- The existing internal Web browser workflow is the primary prior art. Existing Profile activation, staging build, vector store and production startup tests provide supporting lower-level coverage where infrastructure observations cannot be expressed reliably through the browser.
- The primary workflow starts from a current, empty database with no RetrievalProfile and no CorpusRelease. A system administrator sees “初始化知识库”, never a required Profile UUID.
- Starting initialization creates one resumable run and one STAGING Profile/physical index identity. Repeating the same action or idempotency key returns the same resources.
- A concurrent start with another key cannot create a second nonterminal run for the same classification and returns a stable conflict with the existing run identity.
- With no approved documents, initialization reaches `WAITING_FOR_CONTENT`; governance remains usable and employee answers remain closed.
- After upload, processing and approval, continuing initialization freezes the correct corpus snapshot and starts the candidate build without mutating an ACTIVE Release.
- The workflow injects a Provision failure, observes a safe failure code, retries from the UI and completes without duplicating Profile, Collection or Release records.
- The workflow simulates an existing matching physical Collection and verifies idempotent reconciliation. It then simulates a schema or binding mismatch and observes `INDEX_IDENTITY_COLLISION` without destructive changes.
- The workflow closes and recreates the browser session while work is running and verifies that persisted progress and recovery actions remain available.
- The workflow cancels a nonterminal run and verifies that it cannot become active, while a later new run can proceed without reusing incompatible artifacts.
- The workflow completes evaluation handoff and then uses the existing Readiness, approval, activation and rollout operations. Only after those operations does employee answering become available against the exact activated Release.
- The workflow verifies that local Fake defaults appear only outside production and that production exposes only validated Runtime Deployment choices.
- The workflow covers the legacy branch: a valid ACTIVE Profile is migrated without re-embedding, while stale or incomplete build evidence rejects migration and offers fresh initialization.
- A supporting application-lifespan test boots against an outdated, ahead and divergent Alembic revision and verifies fail-fast stable diagnostics for both API and Worker startup.
- Supporting index Provision contract tests run against the fake Adapter for deterministic edge cases and, when Milvus is available, against a real Collection for schema description, concurrent create reconciliation and collision detection.
- Supporting tests verify that audit and telemetry contain run/Profile/Release identities, stages, reason codes and trace IDs but no document body, prompt, question, credential or provider secret.
- Production-like acceptance still requires real OIDC, PostgreSQL, Redis, object storage, Milvus, workers, real model routes and representative documents. Automated Fake coverage cannot authorize the production pilot.

## Out of Scope

- Automatically generating PASS evaluation or Readiness evidence.
- Automatically approving or activating the first Release without a system administrator.
- Automatically enabling employee rollout after initialization.
- Replacing PostgreSQL CorpusRelease state with Milvus, Qdrant or Elasticsearch aliases.
- Supporting more vector databases solely for first-run initialization.
- Public self-service knowledge-base creation or external customer onboarding.
- OCR, GraphRAG, complex-table extraction, business Connector or ERP write operations.
- Deleting legacy or failed physical indexes inside the initialization transaction.
- Rebuilding compatible existing document artifacts when their immutable fingerprints already match.
- Redesigning the established governance, evaluation, Readiness, activation, rollout, emergency withdrawal or rollback rules from Spec 11.

## Further Notes

主流 RAG 产品普遍允许先创建空知识库，再通过显式状态推进文档解析和索引；主流向量检索系统普遍采用独立物理版本、后台构建和逻辑指针切换。本规格借鉴它们的首次配置、异步状态和失败恢复，但保留本项目针对内部生产试点的更严格 Release 证据链。

“默认初始化”仅表示提供经过验证的配置默认值并创建未激活的 STAGING 资源，不表示在应用启动时静默写数据库，也不表示默认开放问答。这样既改善首次使用体验，又避免多实例启动竞态和生产配置误选。

该范围适合用一个 Spec 完成，因为三个修复点共同服务于同一个首次初始化工作流和同一个主要测试 Seam。实施时可以按三个增量拆成依赖 tickets，但任何中间版本都不得恢复手工 Profile UUID 或绕过现有 Release 门禁。

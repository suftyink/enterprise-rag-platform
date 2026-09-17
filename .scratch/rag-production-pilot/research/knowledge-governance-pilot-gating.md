# 知识治理与试点门禁流程调研

日期：2026-09-17  
范围：`crossborder-erp-ai-assistant` 内部员工生产试点  
方法：仅采用官方规范、厂商文档及项目源代码作为证据。

## 结论

当前“试点门禁同时保护员工问答和知识治理”的实现应调整。主流做法不是取消门禁，而是把它放到正确的边界：

- **控制面**：知识管理员上传、解析、审核、批准、撤回、构建候选版本；由 SSO、RBAC、资源范围、CSRF、审计和状态机保护，不依赖员工试点是否开放。
- **发布面**：把已批准内容固化为候选语料/索引版本，运行离线评测和生产证据检查，人工批准后激活；门禁证据绑定具体版本指纹。
- **服务面**：员工问答、引用和反馈；只有当前活动版本通过 readiness、获得发布批准且命中试点人群时才接收流量。

因此，当前最优先动作是解除管理员知识治理 API 对 `PilotAccessGate` 的依赖，同时保留并加强管理员授权；员工问答仍受试点门禁保护。随后再把“文档内容批准”和“对员工生效”拆成两个状态/动作，形成可回滚的候选版本发布流程。

## 官方资料所体现的主流模式

### 1. 控制面和服务面是不同职责

AWS 把控制面定义为资源的创建、读取、更新、删除和列举等管理操作，把数据面定义为服务的主要功能，并明确二者是不同组件；分离可带来性能和可用性收益。[AWS：Control planes and data planes](https://docs.aws.amazon.com/whitepapers/latest/aws-fault-isolation-boundaries/control-planes-and-data-planes.html)

映射到本项目：

- 上传、解析、审核、发布准备、撤回、回滚是控制面。
- 检索、生成引用回答、收集员工反馈是服务面。
- “员工试点未开放”不等于“管理员不能准备或修复知识”。

### 2. Readiness 的职责是接收流量，不是关闭管理能力

Kubernetes 官方文档将 readiness 定义为容器是否准备好接收流量；readiness 失败时，容器仍运行，但不再从匹配 Service 接收流量。[Kubernetes：Liveness, Readiness, and Startup Probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)

对本项目的直接启示是：`READINESS_REPORT_MISSING` 应阻止员工问答流量，而不是阻止管理员进入治理后台生成使系统达到 ready 所需的内容和证据。

### 3. 版本化索引先构建和验证，再切换活动指针

Elastic 官方文档说明索引别名可实时改变应用使用的索引，多个别名动作可在一次原子操作中完成；其示例在切换期间无停机，且不会同时指向两个单一目标。[Elastic：Aliases](https://www.elastic.co/docs/manage-data/data-store/aliases)

Azure AI Search 同样建议用稳定别名指向索引的新版本，替换底层索引时只更新别名映射；旧索引应在切换传播完成后再删除。[Azure AI Search：Create an index alias](https://learn.microsoft.com/en-us/azure/search/search-how-to-alias)

这对应本项目已有的 `STAGING -> ACTIVE -> ROLLBACK` Profile 方向：候选索引应独立构建、评测并固化 manifest，最终通过一个受控激活动作切换服务版本，旧版本保留到回滚窗口结束。

### 4. 普通文档变更不要求每次全量重建

Azure AI Search 的索引器可通过变更检测只同步新增和更新文档；删除孤立文档可发送 delete 动作。只有需要重新处理全部文档时才 reset/full reindex。[Azure AI Search：Run or reset indexers](https://learn.microsoft.com/en-us/azure/search/search-howto-run-reset-indexers)

其索引更新文档进一步区分了两类变化：新增字段等部分变化可以不重建；修改字段定义、分析器等会破坏物理结构的变化才需要完整重建。[Azure AI Search：Update or rebuild an index](https://learn.microsoft.com/en-us/azure/search/search-howto-reindex)

因此，RAG 语料操作应按影响分类：

| 变更 | 是否需要重新向量化 | 是否需要全量重建 |
|---|---:|---:|
| 新增文档或新版本 | 仅新内容 | 否 |
| 修改文档正文 | 仅新版本 | 否 |
| 发布已完成索引的文档 | 否，只改变候选语料资格 | 否 |
| 撤回文档 | 否，立即改变检索资格并异步清理 | 否 |
| Embedding 模型/维度变化 | 是 | 是 |
| Parser/Chunk 策略变化 | 通常是 | 是 |
| 索引结构、分析器、关键检索契约变化 | 通常是 | 是 |

若采用“每个发布版本一个不可变物理索引”的严格模式，新候选仍需形成完整索引，但实现可以复用未变化文档的向量，只计算增量，而不必重新调用 Embedding 处理全部正文。

### 5. 部署、发布和逐步开放应解耦

Azure Feature Management 明确把功能发布与代码部署分离，并支持开关、指定用户/群组和按比例逐步开放；同时提供快速关闭的 kill switch。[Azure：Feature management overview](https://learn.microsoft.com/en-us/azure/azure-app-configuration/concept-feature-management)

OpenFeature 的 evaluation context 支持按用户、应用或其他上下文做规则匹配和按比例评估，适合作为试点人群决策的标准化思路。[OpenFeature：Evaluation Context](https://openfeature.dev/specification/sections/evaluation-context/)

对应本项目应分成三个动作：

1. 部署代码和基础设施；
2. 准备并激活一个通过验证的知识发布版本；
3. 向指定员工组开放该活动版本。

三者不应由一个全局布尔值同时控制。

### 6. 上线前门禁和上线后监控缺一不可

NIST AI RMF 要求 AI 系统在部署前测试，并在运行中定期评估；还要求生产监控、用户反馈、事件响应、恢复和变更管理机制。[NIST：AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/)

Google SRE 将 canary 定义为对小范围、限时发布进行评估，再决定是否扩大；要求能把候选版本只交给部分人群，并把评估纳入发布流程。[Google SRE：Canarying Releases](https://sre.google/workbook/canarying-releases/)

因此 `release_check -> PASS -> 人工批准` 是开放条件，但不是终点。试点期仍要监控错误率、延迟、拒答、引用支持、权限泄漏和用户负反馈，并支持暂停试点或回切上一活动版本。

### 7. 解除试点门禁不等于降低管理员安全性

OWASP 要求默认拒绝、最小权限，并在每个请求上验证授权。[OWASP：Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)

因此治理 API 不再依赖 `PilotAccessGate` 后，仍必须具备：

- 企业身份认证；
- `KNOWLEDGE_ADMIN` / `SYSTEM_ADMIN` 角色和资源范围授权；
- 服务端逐请求鉴权；
- CSRF 防护；
- 乐观并发/状态机检查；
- 生命周期审计及 trace 关联。

## 当前实现与目标模式的差距

### 已有的正确基础

项目已经具备继续演进所需的大部分积木：

- 文档存在 `READY`、`REVIEW_PENDING`、`PUBLISHED`、`WITHDRAWN` 等处理和发布状态；检索资格要求文档 active、ready、published、retained（`src/erp_assistant/rag/knowledge_eligibility.py:8-24`）。
- 发布、撤回和回滚会记录治理修订，并更新 corpus generation（`src/erp_assistant/rag/knowledge_corpus.py:232-279`）。
- 候选 Profile 使用 `STAGING`、活动 Profile 使用 `ACTIVE`、前一版本使用 `ROLLBACK`（`src/erp_assistant/evaluation/profiles.py:21-24`）。
- 候选索引要求与活动索引独立，并核对构建前后 corpus snapshot（`src/erp_assistant/rag/staging_index.py:20-95`）。
- 激活前会核对评测 PASS、证据时效、index build hash、corpus hash、document count 和运行时绑定（`src/erp_assistant/evaluation/profiles.py:120-217`）。
- release identity 已包含 dataset、index build、parser、chunk、retrieval/answer route 等指纹（`src/erp_assistant/operations/readiness_models.py:14-62`）。
- `PilotAccessGate` 会检查最近 PASS 报告、24 小时时效、配置指纹和人工批准记录（`src/erp_assistant/web/pilot.py:61-146`）。

### 关键问题

1. **控制面被服务面门禁锁死。** `web_documents`、`web_upload_document`、`web_lifecycle` 与问答接口一样依赖 `get_pilot_actor`（`src/erp_assistant/web/routes.py:75-240`）。没有 readiness 就不能上传/发布知识，而没有可用知识和发布证据又无法形成 readiness，产生引导循环。
2. **内容批准与服务生效耦合。** 当前 `PUBLISH` 会立即设置 `active=True` 和 `PUBLISHED`；撤回会立即改为不可检索并排队清理（`src/erp_assistant/rag/_knowledge_lifecycle.py:19-66, 140-166`）。对于生产发布，“管理员批准内容”与“这个版本已通过评测并向员工生效”应是两个动作。
3. **活动索引和不可变发布证据存在张力。** 新文档通过当前 ACTIVE Profile 做增量索引（`src/erp_assistant/rag/classified_indexer.py:64-116`），但发布导致 corpus snapshot 改变；生产配置探针要求活动 Profile 的 build manifest 与当前 snapshot 完全一致（`src/erp_assistant/operations/production_probes.py:157-190`）。因此内容发布后，原 readiness 证据理应失效，但系统缺少清晰的“新候选发布版本”工作流。
4. **一个全局门禁承担了过多语义。** 当前门禁混合了证据有效性、发布批准和用户开放状态。它不能清楚表达“控制面可操作、当前活动版本继续服务、下一候选尚未通过”这种正常状态。

## 推荐的目标流程

```text
上传/解析/增量向量化
        ↓
管理员审核内容与权限范围
        ↓
内容批准（APPROVED，不立即影响员工服务）
        ↓
冻结候选 CorpusGeneration + corpus hash
        ↓
构建/校验候选索引 + index build hash
        ↓
离线检索与回答评测
        ↓
release_check 生成绑定该版本的 PASS 报告
        ↓
系统管理员发布批准
        ↓
原子激活 Profile/Release，旧版本进入 ROLLBACK
        ↓
指定员工组小范围试点
        ↓
持续监控 → 扩大 / 暂停 / 回滚
```

推荐把状态拆成三个独立维度，而不是继续增加一个总开关：

| 维度 | 典型状态 | 回答的问题 |
|---|---|---|
| 内容治理 | DRAFT / PROCESSING / REVIEW_PENDING / APPROVED / REJECTED / WITHDRAWN | 这份内容是否获准进入候选语料？ |
| 发布版本 | BUILDING / EVALUATING / READY / ACTIVE / ROLLBACK / FAILED | 哪个 corpus/index 版本在提供服务？ |
| 试点开放 | DISABLED / CANARY / ENABLED / PAUSED | 哪些员工可以访问当前活动版本？ |

## 分阶段处理建议

### P0：先解除循环依赖

目标：管理员在试点关闭、readiness 缺失或失败时仍能准备知识。

- `GET/POST /internal/api/documents` 和 `POST /internal/api/documents/{id}/lifecycle` 改用已认证 actor，不再调用 `gate.require_enabled()`。
- 继续执行 `_require_admin`，并保留现有资源范围授权、CSRF、审计和状态机检查。
- `/internal/api/answers` 和员工反馈继续依赖 `get_pilot_actor`。
- 页面分别呈现“员工试点未开放”和“知识治理可用”，不要用同一前端状态禁用整个页面。

这一步是最小修复，不需要立刻重做索引模型。

### P1：拆开内容批准与服务激活

目标：管理员可以持续治理内容，而当前活动版本保持稳定。

- 将现有 `PUBLISH` 的业务含义改为“批准进入下一候选语料”，或新增 `APPROVE`，避免直接改变当前员工服务版本。
- 新增明确的 `ReleaseCandidate`/`CorpusRelease` 实体，至少记录：release id、corpus generation/hash、profile id、index identity/build hash、评测报告 id、readiness report id、审批人、状态和时间。
- 当前 `ACTIVE` Release 在新候选构建和评测期间继续提供服务。
- `PilotAccessRecord` 绑定活动 release id/fingerprint，而不是只依赖“数据库里最新一份报告”。

### P2：建立候选构建和原子激活

目标：发布可重现、切换无中间态、可以快速回滚。

- 冻结候选语料快照后，构建独立候选索引；构建期间 corpus 变化则候选失败或重新生成。
- 评测报告、readiness 报告和人工审批必须绑定相同的 corpus hash、index build hash 和 route fingerprints。
- 激活在单一事务/原子指针切换中完成，旧 ACTIVE 进入 ROLLBACK 并保留到回滚窗口结束。
- 初期小语料可以全量构建候选索引以降低复杂度；规模增长后，再加入“复用未变化向量 + 只嵌入增量”的优化。

### P3：灰度、监控与回滚

目标：把风险限制在小范围并形成运营闭环。

- 试点开关按用户组或明确名单评估，不用单一全局布尔值。
- 建议开放顺序：系统管理员/知识管理员 → 内部 QA → 小范围业务员工 → 扩大试点。
- 每个阶段观察固定窗口内的错误率、p95 延迟、无证据拒答率、引用支持率、权限拒绝/泄漏、差评和依赖健康。
- 超阈值时自动暂停继续扩大，由负责人决定恢复、回滚或修复候选。

## 紧急撤回的例外路径

敏感、错误或合规风险内容不能等待下一次完整发布。建议保留 fail-closed 的即时撤回：

1. 立即在数据库检索资格层把文档设为不可用；
2. 检索结果在返回前继续做数据库资格二次过滤；
3. 记录高优先级审计事件并标记活动 Release 为 `DEGRADED` 或 readiness stale；
4. 视风险暂停员工试点；
5. 异步删除向量并构建不包含该文档的新候选版本；
6. 重跑必要评测和发布证据。

即时撤回不需要重新向量化；物理向量清理可以异步完成，资格过滤负责立即止血。

## 建议验收用例

1. readiness 缺失时，知识管理员可以登录、上传、查看处理状态和执行授权范围内的治理动作。
2. readiness 缺失时，普通员工问答返回明确的 503 门禁原因。
3. 普通员工不能调用任何治理 API；知识管理员不能越过自己的 tenant/scope。
4. 候选内容变化不会改变当前活动 Release 的问答结果。
5. 候选 corpus hash、index build hash 或评测指纹任一不匹配时不能激活。
6. PASS 报告过期或不属于候选 Release 时不能批准开放。
7. 激活后新 Release 对指定试点组可见，其他员工仍被拒绝。
8. 暂停试点只关闭员工流量，不影响管理员治理和故障处置。
9. 回滚恢复上一 Release，不重新上传或重新解析原始文档。
10. 紧急撤回后，目标文档立即不再作为证据，即使物理向量仍在异步清理。

## 决策建议

建议采用“**P0 立即解耦 + P1/P2 版本化发布 + P3 灰度运营**”路线。

- 如果当前目标只是本地演示，P0 即可解除 `READINESS_REPORT_MISSING` 的治理死锁。
- 如果目标是可供内部员工使用的生产试点，P0 只是止血，至少应完成 P1 和 P2 后再把门禁视为完整。
- 不建议为了暂时开放页面而伪造 PASS 报告、绕过 release fingerprint 或把门禁整体删除；那会丢失已有的版本证据和回滚保障。

## 来源

- [AWS Fault Isolation Boundaries — Control planes and data planes](https://docs.aws.amazon.com/whitepapers/latest/aws-fault-isolation-boundaries/control-planes-and-data-planes.html)
- [Kubernetes — Liveness, Readiness, and Startup Probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)
- [Elastic — Aliases](https://www.elastic.co/docs/manage-data/data-store/aliases)
- [Azure AI Search — Create an index alias](https://learn.microsoft.com/en-us/azure/search/search-how-to-alias)
- [Azure AI Search — Run or reset indexers](https://learn.microsoft.com/en-us/azure/search/search-howto-run-reset-indexers)
- [Azure AI Search — Update or rebuild an index](https://learn.microsoft.com/en-us/azure/search/search-howto-reindex)
- [Azure App Configuration — Feature management overview](https://learn.microsoft.com/en-us/azure/azure-app-configuration/concept-feature-management)
- [OpenFeature — Evaluation Context](https://openfeature.dev/specification/sections/evaluation-context/)
- [NIST — AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/)
- [Google SRE Workbook — Canarying Releases](https://sre.google/workbook/canarying-releases/)
- [OWASP — Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)

# 跨境 ERP 智能业务助手

面向外贸、采购、物流、Amazon 运营和 ERP 的统一智能业务入口。项目采用 Python 模块化单体、LangGraph 编排和独立 Celery Worker；第一阶段只建立受控、可测试的技术基线。

## 目标与边界

- RAG：回答制度、SOP、平台政策、产品说明和 ERP 手册，并返回可定位来源。
- Function：查询实时库存、订单、采购、物流和 Amazon 数据；后续受控执行建单或修改。
- 实时业务事实只来自白名单 Function，不能由模型或知识 Chunk 猜测。
- 写操作必须经过服务端权限检查、幂等控制以及确认或审批。
- 当前版本已包含四格式知识入库、混合检索、引用回答、身份与审计、发布门禁及内部 Web 试点入口；不包含 OCR、业务连接器和 ERP 写操作。

## 架构

```text
客户端 -> FastAPI -> LangGraph 编排 -> RAG / Function Gateway
                    |                -> 人工确认与审批（后续）
                    +-> PostgreSQL：任务、工作流、权限、审计
                    +-> Redis：Celery、锁、限流、短期缓存
                    +-> Milvus：知识检索（rag Profile）
                    +-> MinIO：知识原文与解析文件（rag Profile）

Celery Worker -> 解析、Embedding、索引、同步与评测任务
```

系统默认使用 Fake Provider。`ChatProvider`、`EmbeddingProvider` 和 `RerankProvider` 均为协议接口，检索链路通过 Provider 工厂装配，后续增加模型适配器时不修改领域逻辑。

## 目录

```text
src/erp_assistant/
├── api/             # FastAPI 路由与请求/响应模型
├── core/            # 配置与公共基础能力
├── domains/         # 外贸、采购、物流、Amazon、ERP 领域服务
├── orchestration/   # LangGraph 与模型 Provider
├── rag/             # 知识入库和检索
├── functions/       # 白名单 Function
├── workflows/       # 确认、审批和恢复流程
├── connectors/      # ERP、Amazon、物流 Adapter
├── infrastructure/  # PostgreSQL、Redis、Milvus、MinIO
├── workers/         # Celery 任务
├── evaluation/      # 评测与反馈
└── web/             # 内部员工问答、知识治理与试点门禁
```

## 环境初始化

uv 管理 Python 3.12 解释器及应用依赖，并根据 `pyproject.toml` 和 `uv.lock` 创建项目级 `.venv`。

```powershell
uv sync --frozen
Copy-Item .env.example .env
```

## 基础设施

启动 PostgreSQL 和 Redis：

```powershell
docker compose up -d
```

需要开发知识库时，额外启动 Milvus、MinIO 和 etcd：

```powershell
docker compose --profile rag up -d
```

默认配置不启用 RAG，因此 `/health/ready` 不依赖 MinIO、Milvus 和 etcd。

## 数据库与运行

```powershell
uv run python -m alembic upgrade head
uv run python -m uvicorn erp_assistant.main:app --reload
```

API 文档：`http://127.0.0.1:8000/docs`

内部 Web：`http://127.0.0.1:8000/internal`

Windows 开发环境启动 Worker：

```powershell
uv run python -m celery -A erp_assistant.workers.celery_app:celery_app worker --loglevel=INFO --pool=solo --without-gossip --without-mingle --without-heartbeat
```

另开进程启动 Beat，以每分钟采集索引队列深度、重试、失败和卡住任务：

```powershell
uv run python -m celery -A erp_assistant.workers.celery_app:celery_app beat --loglevel=INFO
```

## 接口

- `GET /health/live`：进程存活检查。
- `GET /health/ready`：PostgreSQL、Redis 及可选 RAG 依赖检查。
- `POST /api/assistant/v1/tasks`：创建 `READY` 状态的文本任务。
- `POST /api/assistant/v1/knowledge/documents`：上传文本型 PDF 并创建索引任务。
- `GET /api/assistant/v1/knowledge/documents/{documentId}`：查询解析与索引状态。
- `POST /api/assistant/v1/knowledge/documents/{documentId}/retry`：重试失败任务。
- `POST /api/assistant/v1/knowledge/search`：检索租户私有和全局共享知识 Chunk。
- `GET /internal`：内部员工引用问答、知识治理与试点门禁入口。

### 内部 Web 试点

本地开发可使用确定性身份适配器，但必须显式配置测试主体及其服务端权限映射：

```dotenv
IDENTITY_PROVIDER=test
INTERNAL_WEB_TEST_SUBJECT=employee-a
IDENTITY_MEMBERSHIPS_JSON={"employee-a":{"tenantId":"tenant-a","roles":["INTERNAL_EMPLOYEE"],"knowledgeScopes":[{"scope":"GLOBAL","scopeId":"GLOBAL"}]}}
```

生产环境禁止测试身份适配器，必须配置 OIDC Authorization Code + PKCE 所需的 issuer、audience、JWKS、authorization/token URL、client ID 和准确的 HTTPS 回调基址：

```dotenv
IDENTITY_PROVIDER=oidc
OIDC_ISSUER=https://identity.example.com
OIDC_AUDIENCE=erp-assistant
OIDC_JWKS_URL=https://identity.example.com/.well-known/jwks.json
OIDC_AUTHORIZATION_URL=https://identity.example.com/authorize
OIDC_TOKEN_URL=https://identity.example.com/token
OIDC_CLIENT_ID=erp-assistant-web
INTERNAL_WEB_BASE_URL=https://assistant.example.com
```

浏览器会话使用 Redis 中的服务端凭据、`HttpOnly` 会话 Cookie、同站策略和 CSRF 令牌。问答、上传和治理接口还受试点门禁保护：只有最近 24 小时内、与 `PILOT_RELEASE_FINGERPRINT` 一致的 `PASS` Readiness 报告，连同系统管理员记录的范围、容量、监控负责人和回滚负责人，才能开放。关闭试点只追加关闭记录，不删除知识数据。

代码和自动化浏览器流程通过不代表生产试点已获批准；仍需代表性员工与知识管理员使用真实身份、真实知识、真实模型和生产类基础设施完成验收。

任务示例：

```json
{
  "tenantId": "tenant-1",
  "operatorId": "user-1",
  "departmentId": "AMAZON_OPS",
  "storeIds": ["US-01"],
  "content": "查询美国站库存"
}
```

### PDF 入库与检索

在 `.env` 中启用 RAG：

```dotenv
RAG_ENABLED=true
PILOT_RELEASE_FINGERPRINT=<PilotReadiness ReleaseIdentity fingerprint>
```

启动完整基础设施、升级数据库，并分别启动 API 和 Worker：

```powershell
docker compose --profile rag up -d --wait
uv run python -m alembic upgrade head
uv run python -m uvicorn erp_assistant.main:app --reload
uv run python -m celery -A erp_assistant.workers.celery_app:celery_app worker --loglevel=INFO --pool=solo --without-gossip --without-mingle --without-heartbeat
```

上传文本型 PDF：

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/assistant/v1/knowledge/documents `
  -F "file=@D:\docs\inventory-sop.pdf;type=application/pdf" `
  -F "tenantId=tenant-1" `
  -F "operatorId=user-1" `
  -F "scope=TENANT" `
  -F "documentKey=inventory-sop"
```

检索知识：

```powershell
$body = @{
  tenantId = "tenant-1"
  operatorId = "user-1"
  query = "库存盘点出现差异如何处理"
  topK = 5
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/assistant/v1/knowledge/search `
  -ContentType application/json `
  -Body $body
```

检索使用 Dense + BM25 双路召回、RRF 融合和 Rerank。低于配置阈值的候选不会返回；没有合格结果时，响应中的 `decision` 为 `INSUFFICIENT_KNOWLEDGE`。

首次切换到新的混合检索 Collection，或修改 Embedding 模型、维度和索引策略后，重建所有活动文档索引：

```powershell
uv run python -m erp_assistant.rag.reindex
```

RAG 基础设施运行后，默认通过进程内 `KnowledgeRetriever` 执行版本化 JSONL 检索评测，并持久化机器可读报告：

```powershell
uv run python -m erp_assistant.evaluation.retrieval `
  --dataset tests/fixtures/retrieval_eval.example.jsonl `
  --report retrieval-evaluation-report.json `
  --index-identity knowledge_chunks_hybrid_v2:local
```

部署后端到端验收时，显式增加 HTTP Adapter：

```powershell
uv run python -m erp_assistant.evaluation.retrieval `
  --dataset tests/fixtures/retrieval_eval.example.jsonl `
  --report retrieval-evaluation-report.json `
  --index-identity knowledge_chunks_hybrid_v2:staging `
  --base-url http://127.0.0.1:8000
```

仓库内示例集是合成契约夹具，只能验证评测框架；生产基线必须包含至少 100 条由业务方人工复核、覆盖全部必需分类的真实案例。

生产试点发布前，使用统一门禁采集当前数据库迁移、PostgreSQL、Redis、MinIO、Milvus、Celery、模型路由和最近 24 小时运行遥测，并合并已批准评测、容量、安全、基础设施与恢复演练证据：

```powershell
uv run python -m erp_assistant.operations.release_check pilot-release-manifest.json
```

Manifest 的 `release` 固定数据集、索引构建、解析/切分版本、配置及检索/回答路由指纹；`evaluationReport` 指向同目录下的评测报告；`security`、`infrastructure`、`capacity` 和 `recovery` 提供现场运行产生的时间、原始 `identityFingerprint`、无敏感信息证据引用和指标。命令只在 `APP_ENV=production` 下运行，`PILOT_RELEASE_FINGERPRINT`、活动 Profile/索引构建和每份现场证据的指纹必须等于 `release` 计算出的 SHA-256 指纹；仅 `PASS` 返回退出码 0，其他结果返回 2，并将完整报告追加写入数据库且不可覆盖。

当前限制为单文件 20MB、最多 200 页，仅支持可复制文字的 PDF。扫描件会进入 `OCR_REQUIRED`，不会生成错误索引。Fake Embedding 只用于验证流程和基础词项检索，不能代表真实语义准确率。

## 检查

```powershell
uv run ruff check .
uv run mypy src
uv run pytest
uv pip check
docker compose config --quiet
docker compose --profile rag config --quiet
```

## 当前完成状态

技术基线已经完成并通过验证：

- uv 使用 Python 3.12，并根据锁文件同步项目级 `.venv`。
- PostgreSQL、Redis 默认基础设施以及 Milvus、MinIO、etcd 可选 RAG Profile 已完成启动和连接验证。
- Alembic 初始迁移已经创建 `ai_task` 和 `ai_task_message`，可从空数据库升级到最新版本。
- `GET /health/live`、`GET /health/ready` 和 `POST /api/assistant/v1/tasks` 已实现。
- 文本任务可以持久化到 PostgreSQL，并返回唯一的 `taskId`、`traceId` 和 `READY` 状态。
- `ChatProvider`、`EmbeddingProvider`、`RerankProvider` 及其确定性 Fake 实现已经建立。
- Celery Worker 和无业务副作用的健康任务已经建立。
- `knowledge_document`、`knowledge_chunk` 和 `knowledge_index_job` 数据模型及迁移已经建立。
- PDF、DOCX、Markdown 和 HTML 可以保存到 MinIO，经 Celery 解析、结构化切分后写入 PostgreSQL 和 Milvus。
- 已实现哈希去重、安全版本切换、失败重试、扫描件识别和带页码检索。
- 检索同时支持租户私有知识和全局共享知识，并执行 Milvus 与 PostgreSQL 双层租户过滤。
- Fake Embedding 已升级为确定性的 256 维词项及中文字符特征向量 `fake-lexical-v1`。
- 检索已升级为 Dense + BM25、RRF、Rerank 和低置信度门禁，并提供可重建的新版本 Collection。
- 检索响应包含 `traceId`、检索管线版本、最终归一化分数和 `MATCHED`/`INSUFFICIENT_KNOWLEDGE` 决策。
- 内部 Web 已串联企业 SSO、引用回答、拒答/错误区分、反馈、四格式上传、警告审核、发布/撤回/回滚和试点开放/关闭；所有权限决策仍由服务端执行。
- 已建立版本化检索评测框架、分类指标、可复现报告和机器可判定质量门禁；真实业务基线仍待业务方复核批准。
- Ruff、Mypy、Pytest 和依赖兼容性检查已经通过。

OCR、复杂表格、业务连接器、IM 集成、GraphRAG、Agent 和 ERP 写操作仍属于后续范围；真实模型、真实身份和生产类基础设施验收仍是开放生产试点前的外部门槛。

## 后续实施路线

后续阶段按以下顺序实施。每个阶段验收完成后再进入下一阶段；这里不预设固定工期，实际排期取决于团队人数以及外部系统接口的可用性。

### 1. RAG MVP（进行中：检索评测框架已完成）

**目标**

第一阶段已完成文本型 PDF 入库和带页码检索；下一阶段建立带来源回答，并在知识不足时明确拒答。

**主要任务**

- 已建立知识文档、Chunk 和索引任务的数据模型，记录租户、文件版本、处理状态和错误原因。
- 已使用 MinIO 保存知识原文和分页解析结果。
- 已使用 Celery 实现文本型 PDF 解析、切分、Fake Embedding、Milvus 入库及失败重试。
- 已实现租户与全局范围过滤、哈希去重、版本切换和带页码 Chunk 检索。
- 已实现 Dense 与 BM25 混合检索、RRF、Rerank、相似度阈值和可量化评测。
- 回答返回可定位的文档、页码或段落来源；低置信度结果不进入回答上下文。
- 先使用 Fake Provider 建立确定性测试，再通过现有协议接入真实模型 Provider。

**交付物**

- 可追踪状态的知识入库流程。
- 可独立测试的解析、切分、检索和引用模块。
- 最小 RAG 评测集及检索、引用、拒答测试。

**验收条件**

- 文件能够上传到 MinIO，并完成解析、切分和 Milvus 索引。
- 可以查询每个文档和索引任务的处理状态及失败原因。
- 回答包含有效来源；删除或停用文档后不再被检索。
- 无匹配知识或置信度不足时明确拒答，不编造业务事实。

**前置依赖**

- 启用 `rag` Profile，并设置 `RAG_ENABLED=true`。
- 确定首批知识文件格式、来源、访问范围和更新规则。

### 2. 只读 Function（待实施）

**目标**

通过受控接口查询实时业务数据，并与静态知识回答严格分离。

**主要任务**

- 建立白名单 Function 注册、参数校验、统一结果和错误模型。
- 每次调用强制传递租户、操作人、部门和店铺范围，并在服务端校验权限。
- 优先实现库存、订单、采购、物流、FBA 和销量查询。
- 先提供 Fake Connector 和契约测试，再逐步接入 ERP、Amazon 和物流系统。
- 增加超时、有限重试、限流、脱敏和完整调用审计。

**交付物**

- Function 注册表、统一调用网关和权限检查入口。
- Fake Connector 及外部系统 Connector 契约。
- 只读业务查询测试集和审计记录。

**验收条件**

- 实时业务事实只来自白名单 Function，不由模型或知识 Chunk 推测。
- 越权的租户、店铺和操作人请求被拒绝。
- 外部系统超时或异常时返回可识别的错误，不生成虚假结果。
- 每次 Function 调用都能通过 `traceId` 查询输入范围、结果状态和耗时。

**前置依赖**

- 确认外部系统的只读接口、认证方式、限流规则和测试环境。
- 明确租户、部门、操作人和店铺的权限映射规则。

### 3. 智能编排与跨环节流程（待实施）

**目标**

统一编排 RAG 与 Function，并完成首个可暂停、恢复和追踪的跨环节业务流程。

**主要任务**

- 使用 LangGraph 实现意图识别、RAG/Function 路由、执行和结果汇总。
- 持久化流程检查点，支持超时、失败恢复、人工确认和继续执行。
- 统一任务、消息、步骤、工具调用和错误状态，并保持 `traceId` 全链路传递。
- 首个完整场景实现“补货建议 → 采购需求 → 审批 → 履约跟踪”。
- 为重复请求和 Worker 重试增加幂等控制。

**交付物**

- 可测试的 LangGraph 工作流和状态模型。
- 流程暂停、恢复、取消及查询能力。
- 补货到履约的端到端演示流程。

**验收条件**

- RAG 与 Function 能按意图正确路由，失败步骤不会污染已完成结果。
- 流程可以暂停、恢复和追踪，重启 API 或 Worker 后仍能继续。
- 同一幂等请求不会产生重复任务、重复审批或重复业务动作。

**前置依赖**

- RAG MVP 和相关只读 Function 已达到验收条件。
- 业务方确认补货、采购、审批和履约的状态及责任边界。

### 4. 受控写操作（待实施）

**目标**

在明确授权、确认和审计的前提下，逐项开放外部系统写操作。

**主要任务**

- 建立操作风险等级、参数预览、人工确认、审批和执行状态模型。
- 所有写操作默认关闭，通过功能开关按租户和操作类型逐项开放。
- 服务端执行权限检查、幂等校验和审计记录，不信任模型生成的权限结论。
- Connector 返回标准化执行结果，并保留失败补偿和人工处理入口。
- 按风险逐步开放报价、退款、Listing 和广告操作。

**交付物**

- 写操作策略、审批流、幂等记录和审计日志。
- 支持预览但不执行的 Dry Run 能力。
- 首批受控写操作 Connector 和回归测试。

**验收条件**

- 未授权或功能开关关闭的操作不能执行。
- 高风险操作必须经过人工确认或审批，执行参数与确认内容一致。
- 重复请求不会重复写入，失败操作可以安全重试或转人工处理。
- 每次写操作都可追溯到租户、操作人、审批人、输入、结果和 `traceId`。

**前置依赖**

- 只读 Connector 和跨环节工作流稳定运行。
- 业务方确认风险分级、审批人、操作范围和失败补偿规则。

### 5. 生产化完善（待实施）

**目标**

建立可持续交付、监控、评测和恢复能力，使系统满足生产运行要求。

**主要任务**

- 增加结构化日志、Trace、指标、告警和模型及基础设施成本统计。
- 建立 RAG、Function、工作流、权限和幂等回归测试集。
- 增加 CI 质量门禁，自动执行 Ruff、Mypy、Pytest、迁移和依赖检查。
- 建立开发、测试和生产配置分层，真实密钥只通过密钥管理服务注入。
- 编写 PostgreSQL、MinIO、Milvus 的备份、恢复和升级流程。
- 完成容量测试、故障演练、部署说明和运行手册。

**交付物**

- 监控仪表盘、告警规则、评测报告和成本统计。
- 自动化 CI 流程及环境配置规范。
- 部署、备份恢复、故障处理和版本升级说明。

**验收条件**

- 代码和数据库变更必须通过自动质量门禁后才能发布。
- 关键依赖异常、任务积压和错误率上升能够被发现并告警。
- 核心数据能够按文档完成备份恢复，恢复结果经过验证。
- 主要业务场景具备稳定的回归评测结果和可追踪的性能基线。

**前置依赖**

- 目标部署环境、可用性目标、数据保留周期和安全要求已经确定。
- 核心业务流程和首批真实 Provider、Connector 已稳定运行。

## 跨阶段实施要求

- **租户隔离**：数据库查询、知识检索、缓存键、对象路径和外部调用都必须包含租户范围。
- **权限控制**：权限判断由服务端完成；模型只能提出调用建议，不能授予权限。
- **审计与追踪**：任务、模型调用、检索、Function、审批和写操作统一使用 `traceId` 串联。
- **可观测性**：记录请求耗时、Provider 耗时、Token、检索命中、Worker 队列和依赖状态。
- **评测规范**：新增能力必须同时增加确定性单元测试、集成测试和对应的业务评测用例。
- **失败处理**：外部依赖失败时返回明确状态；自动重试必须有限且满足幂等要求。
- **配置安全**：仓库只保留配置示例，任何真实密钥、Token 和生产连接信息都不得提交到 Git。

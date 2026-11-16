# 企业级 RAG 知识平台

`enterprise-rag-platform` 是面向内部员工的受治理 RAG 平台，覆盖文档入库、解析切分、混合检索、引用回答、权限控制、质量评测、语料发布和生产试点门禁。

项目不包含跨境业务、ERP 交易处理或业务系统写操作。回答只能基于当前发布且有权限访问的知识；证据不足时明确拒答。

## 核心能力

- **知识入库**：支持 PDF、DOCX、Markdown 和 HTML，保留文档版本、处理状态、解析警告和失败原因。
- **结构化解析**：保存页码、标题路径和来源定位信息，扫描 PDF 会进入 `OCR_REQUIRED`，不会生成错误索引。
- **混合检索**：Dense + BM25 双路召回、RRF 融合、Rerank 和低置信度门禁。
- **引用回答**：回答携带可定位证据；无合格证据时返回 `INSUFFICIENT_KNOWLEDGE`。
- **访问治理**：支持租户、共享和全局知识范围，数据库与向量检索执行双层权限过滤。
- **知识治理**：文档审核、发布、撤回、回滚、清理以及基于状态和权限的合法操作提示。
- **语料发布**：冻结语料快照，构建候选索引，绑定评测与运行证据，再激活或回滚 Release。
- **质量评测**：版本化 JSONL 数据集、分类指标、检索与回答质量门禁、可复现机器报告。
- **生产试点门禁**：通过 Readiness 报告、发布指纹、证据时效和管理员批准控制试点开放。
- **审计与可观测性**：统一 `traceId`，记录身份、治理、发布、反馈和运行证据。

## 架构

```text
内部员工 / 知识管理员
          |
          v
FastAPI + Internal Web
          |
          +--> 身份、权限、审计 --------> PostgreSQL
          +--> 文档与解析产物 ----------> MinIO
          +--> 解析、切分、索引任务 ----> Celery + Redis
          +--> Dense / BM25 / Rerank ---> Milvus + PostgreSQL
          +--> 引用回答 ----------------> Chat Provider
          +--> 评测、Release、门禁 ------> PostgreSQL
```

默认 Provider 为确定性 Fake，仅用于本地流程和自动化测试。生产环境需要配置真实的 Embedding、Rerank、Chat、OIDC 和基础设施服务。

## 当前状态

当前版本已完成可供内部员工试用的完整 RAG MVP：

- 四格式文档入库、解析、结构化切分、版本管理与失败重试。
- PostgreSQL、MinIO 和 Milvus 持久化，以及 Celery 异步任务。
- Dense + BM25、RRF、Rerank、置信度门禁和可重建索引。
- 企业身份、范围权限、审计、引用回答和反馈。
- 文档治理、Corpus Release、Readiness 报告、试点开放与回滚。
- 检索和回答评测框架、机器可判定质量门禁和运行证据采集。
- 内部 Web 的问答、上传、治理、发布和试点操作流程。

真实模型、真实身份、代表性知识集、容量测试和生产基础设施演练仍需在正式开放前完成。

## 项目结构

```text
src/erp_assistant/
├── api/             # FastAPI 路由与请求/响应契约
├── core/            # 配置
├── evaluation/      # 检索与回答评测
├── identity/        # 身份、权限与安全审计
├── infrastructure/  # PostgreSQL、MinIO 等基础设施
├── models/          # 持久化模型
├── operations/      # Release、Readiness、初始化和运行治理
├── orchestration/   # 模型 Provider 与回答编排
├── rag/             # 解析、切分、索引、检索和引用回答
├── web/             # 内部员工入口与知识治理 UI
└── workers/         # Celery Worker 与 Beat

migrations/          # Alembic 数据库迁移
tests/               # 单元、集成和浏览器测试
.scratch/            # 本地规格与 ticket（项目过程资料）
```

> `erp_assistant` 是第一阶段保留的兼容性 Python 包名，不代表当前产品领域。数据库名、Redis key、对象桶、Docker 数据卷和 OIDC 标识同样暂时保留，后续应通过独立迁移再统一为 `rag_platform`。

## 环境要求

- Python `3.12`
- [uv](https://docs.astral.sh/uv/)
- Docker Desktop / Docker Compose
- Windows、Linux 或 macOS；以下命令以 PowerShell 为例

## 快速启动

### 1. 安装依赖

```powershell
uv sync --frozen
Copy-Item .env.example .env
```

`.env` 已加入 `.gitignore`。不要提交密码、Token、私钥或生产连接信息。

### 2. 启动基础设施

仅启动 PostgreSQL 和 Redis：

```powershell
docker compose up -d --wait
```

启动完整 RAG 基础设施（额外包含 Milvus、MinIO 和 etcd）：

```powershell
docker compose --profile rag up -d --wait
```

### 3. 升级数据库

```powershell
uv run python -m alembic upgrade head
uv run python -m alembic current
```

### 4. 启动 API

```powershell
uv run python -m uvicorn erp_assistant.main:app --reload
```

- API 文档：<http://127.0.0.1:8000/docs>
- 内部 Web：<http://127.0.0.1:8000/internal>
- 存活检查：<http://127.0.0.1:8000/health/live>
- 就绪检查：<http://127.0.0.1:8000/health/ready>

### 5. 启动 Worker 与 Beat

Windows 开发环境建议使用 `solo` Worker：

```powershell
uv run python -m celery -A erp_assistant.workers.celery_app:celery_app worker --loglevel=INFO --pool=solo --without-gossip --without-mingle --without-heartbeat
```

另开终端启动 Beat：

```powershell
uv run python -m celery -A erp_assistant.workers.celery_app:celery_app beat --loglevel=INFO
```

## 本地身份配置

开发环境可使用确定性测试身份：

```dotenv
IDENTITY_PROVIDER=test
INTERNAL_WEB_TEST_SUBJECT=employee-a
IDENTITY_MEMBERSHIPS_JSON={"employee-a":{"tenantId":"tenant-a","roles":["INTERNAL_EMPLOYEE"],"knowledgeScopes":[{"scope":"GLOBAL","scopeId":"GLOBAL"}]}}
```

如需上传和治理文档，将角色改为 `KNOWLEDGE_ADMIN`；发布全局知识、管理 Release 和开放试点需要 `SYSTEM_ADMIN`。

生产环境必须使用 OIDC Authorization Code + PKCE，并配置 issuer、audience、JWKS、授权端点、Token 端点、client ID 和 HTTPS 回调地址。仓库中的 `erp-assistant` OIDC 示例值属于兼容标识，可在后续身份迁移时更换。

## RAG 配置

在 `.env` 中启用 RAG：

```dotenv
RAG_ENABLED=true
MILVUS_URI=http://localhost:19530
MINIO_ENDPOINT=http://localhost:9000
PILOT_RELEASE_FINGERPRINT=
```

真实检索 Provider 需要分别为普通和受限知识配置已批准的 Embedding 与 Rerank 部署，并通过 `RETRIEVAL_RUNTIME_DEPLOYMENTS` 声明模型、维度、解析版本和切分版本绑定。API Key 只能通过运行环境或密钥管理系统注入。

## 文档入库与治理

推荐通过内部 Web 操作：

1. 使用知识管理员身份登录 `/internal`。
2. 在“知识治理”中选择作用域并上传文档。
3. 等待解析、切分和索引任务完成。
4. 检查解析警告和文档状态。
5. 对 `READY / REVIEW_PENDING` 文档执行“批准入候选”。
6. 由系统管理员冻结候选、构建索引、绑定 Readiness 报告并激活 Release。

公共 API 仍可用于自动化上传和检索：

- `POST /api/assistant/v1/knowledge/documents`
- `GET /api/assistant/v1/knowledge/documents/{documentId}`
- `POST /api/assistant/v1/knowledge/documents/{documentId}/retry`
- `POST /api/assistant/v1/knowledge/search`

`/api/assistant/v1` 是当前兼容 API 前缀，后续版本化迁移时再调整，不应在本次产品重命名中直接破坏现有调用方。

## 重新索引

修改 Embedding 模型、向量维度、Collection schema、解析版本或切分策略后，需要为目标 Profile 重建索引。仅发布、撤回或调整文档检索资格通常不需要重新向量化。

```powershell
uv run python -m erp_assistant.rag.reindex
```

候选构建会校验语料快照、文档清单、语料哈希和索引构建产物，避免构建期间内容变化导致错误激活。

## 检索评测

运行进程内评测：

```powershell
uv run python -m erp_assistant.evaluation.retrieval `
  --dataset tests/fixtures/retrieval_eval.example.jsonl `
  --report retrieval-evaluation-report.json `
  --index-identity knowledge_chunks_hybrid_v2:local
```

部署后端到端评测：

```powershell
uv run python -m erp_assistant.evaluation.retrieval `
  --dataset tests/fixtures/retrieval_eval.example.jsonl `
  --report retrieval-evaluation-report.json `
  --index-identity knowledge_chunks_hybrid_v2:staging `
  --base-url http://127.0.0.1:8000
```

仓库内数据集是合成契约夹具。生产基线应使用经过业务负责人复核、覆盖必要知识分类和拒答场景的真实评测集。

## 发布与试点门禁

生产试点发布前执行：

```powershell
uv run python -m erp_assistant.operations.release_check pilot-release-manifest.json
```

开放试点要求：

- Readiness 报告为 `PASS`。
- 报告在允许的时效窗口内。
- 报告、活动 Release、索引构建和运行证据的发布指纹一致。
- 安全、基础设施、容量和恢复演练证据完整。
- 系统管理员明确记录试点范围、容量、监控负责人和回滚负责人。

关闭试点只改变访问门禁，不删除知识、向量或发布记录。

## 质量检查

```powershell
uv run ruff check .
uv run mypy src
uv run pytest
uv pip check
docker compose config --quiet
docker compose --profile rag config --quiet
```

## 当前限制与后续方向

- 扫描 PDF 暂不执行 OCR，仅标记 `OCR_REQUIRED`。
- 复杂表格、图片和版面语义解析仍需增强。
- 示例 Fake Embedding 只验证流程和词项检索，不代表真实语义召回率。
- 真实评测集、阈值基线、容量和恢复目标需要按部署环境建立。
- 可继续增加 RAG 相关能力：内容源同步、增量解析、查询改写、多阶段召回、GraphRAG、评测看板和治理工作台。

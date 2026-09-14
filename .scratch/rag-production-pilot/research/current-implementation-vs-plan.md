# 当前代码实现与生产试点规划差异核查

日期：2026-09-15  
范围：仅核查本仓库当前代码、测试、README、依赖/迁移和 `.scratch/rag-production-pilot` 规划；不把旧会话摘要当作事实来源。

## 结论

当前代码已经是一个**可测试的 RAG 检索技术基线**，但还不是规划中的“可供内部员工使用的生产试点”。完整链路大致停在“文本 PDF 入库 → Fake 模型混合检索 → Chunk 结果/阈值拒答”，而规划要求的真实模型、四格式解析、引用回答、OIDC/RBAC、审核发布、完整评测门禁、可观测性和最小 Web 入口仍未形成闭环。规划本身也尚未完成：路线图下 9 个决策/设计票据仍全部为 `open`，最终规格票据依赖其余票据关闭后才能汇总。[路线图目标](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:8) [路线图质量目标](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:22) [最终规格依赖](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/09-finalize-pilot-spec.md:4)

## 对照结果

| 能力 | 当前实现 | 与规划差异 | 判断 |
|---|---|---|---|
| 基础设施与运行基线 | FastAPI、PostgreSQL、Redis、MinIO、Milvus、etcd、Celery 已有配置；RAG 基础设施通过 Compose profile 启用。[Compose 服务](/D:/Project/Python/crossborder-erp-ai-assistant/docker-compose.yml:3) [RAG profile](/D:/Project/Python/crossborder-erp-ai-assistant/docker-compose.yml:32) | 符合保留现有技术栈的决定；尚未验证生产部署、容量、备份恢复和告警。[规划技术栈](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:23) | 已实现基线 |
| 文档入库 | 有上传、状态查询、失败重试；原文写入 MinIO，Celery 解析、Chunk、Embedding、Milvus 入库；数据库记录文档、Chunk 和索引任务。[上传路由](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/routes/knowledge.py:122) [入库状态流转](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:211) [知识迁移](/D:/Project/Python/crossborder-erp-ai-assistant/migrations/versions/20260803_0002_knowledge.py:14) | 只有单文档 API，没有知识库/批次、列表、删除、停用、重解析和清理 API。 | 部分实现 |
| 解析格式 | 上传端严格限定扩展名和 MIME 均为 PDF；解析器固定为 `PypdfParser`。[PDF 限制](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/routes/knowledge.py:75) [固定解析器](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:223) | 规划首批还包含 DOCX、Markdown、HTML；扫描 PDF/OCR 明确尚未实现。[规划格式](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:17) [OCR 状态](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/pdf_parser.py:78) | 部分实现 |
| 解析质量与 Chunk | 去除高频页眉页脚、保留页码；按页分段、标题启发式、目标/最大长度和字符重叠切分。[解析清洗](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/pdf_parser.py:51) [Chunk 策略](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/chunker.py:20) | 尚无解析器路由、表格/SKU/结构元数据、跨页或 Parent-child Chunk；这些仍是开放设计题。[Chunk 设计票据](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/06-design-parsing-and-chunking.md:9) | 部分实现 |
| 检索 | Milvus 同时执行 Dense 和 BM25，使用 RRF 融合；之后在 PostgreSQL 再过滤活动/READY/租户文档并 Rerank、阈值裁剪。[Milvus 混合检索](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/vector_store.py:137) [二次过滤与重排](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:450) | 管线成立，但 Dense/Rerank 都是词项型 Fake 实现，不能证明真实语义召回率。[Fake Embedding](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/orchestration/fake_providers.py:14) [Fake Rerank](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/orchestration/fake_providers.py:43) | 已实现管线，质量未达标 |
| 模型与敏感分级路由 | 已定义 Chat、Embedding、Rerank 协议。[Provider 协议](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/orchestration/providers.py:23) | Provider factory 只支持 `fake`，且没有 Chat factory；规划的合规云/内网本地分级路由尚无配置、策略或适配器。[Provider factory](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/orchestration/provider_factory.py:11) [分级路由规划](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:18) | 未实现生产能力 |
| 检索拒答 | 无结果或全部低于阈值时返回 `INSUFFICIENT_KNOWLEDGE`；响应包含管线版本和分数。[搜索响应](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/routes/knowledge.py:245) | 只是“不给 Chunk”的检索决策，不是生成回答后的事实级拒答；阈值 `0.10` 还是 Fake Rerank 分数，未经过真实评测校准。[阈值配置](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/core/config.py:26) | 部分实现 |
| 引用回答 | 检索结果已有 `documentId`、`documentKey`、版本、页码、章节和 Chunk 内容，可作为证据定位基础。[结果契约](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/knowledge_schemas.py:36) | 没有 `ask/answer` 接口、上下文组装、LLM 生成、事实—引用绑定、冲突/过期证据处理、流式输出、反馈或引用支持率计算；回答证据契约仍为开放票据。[证据契约票据](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/07-define-answer-evidence-contract.md:9) | 未实现闭环 |
| 身份、权限与租户隔离 | Milvus 和 PostgreSQL 都按客户端传入的 `tenantId` 过滤；全局上传另有静态 operator 白名单。[向量过滤](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/vector_store.py:149) [全局上传门禁](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/routes/knowledge.py:141) | 没有 OIDC/SSO、令牌验证或 RBAC。查询和上传继续信任客户端 `tenantId`/`operatorId`；状态查询甚至直接丢弃 `operatorId`。[请求模型](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/knowledge_schemas.py:24) [忽略 operator](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/routes/knowledge.py:170) 这与“不继续信任客户端 operatorId”的决定冲突。[身份规划](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:19) | 明确缺口/冲突 |
| 知识治理 | 有哈希去重、文档版本、`active`、`READY/SUPERSEDED`，重建活动索引也已实现。[去重与版本](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:84) [重建索引](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:377) | 索引成功后自动激活新版本并下线旧版本，没有审核/发布边界；与“管理员上传或审核后发布，未发布不进入正式检索”直接冲突。[自动激活](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:302) [发布规划](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:20) 也没有回滚、过期、删除、审计和跨存储一致性协议。[治理票据](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/03-define-knowledge-governance.md:8) | 部分实现且需改模 |
| 评测 | 有 JSONL 读取、HTTP 执行，以及 Recall@5、MRR@5、nDCG@5、拒答准确率、租户泄漏数、P50/P95 计算。[评测指标](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/evaluation/retrieval.py:87) [HTTP 执行](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/evaluation/retrieval.py:143) | 示例集只有 2 条；没有 ≥100 条真实人工标注、困难类型/版本治理、引用支持率、自动阈值判定和发布门禁。[示例集](/D:/Project/Python/crossborder-erp-ai-assistant/tests/fixtures/retrieval_eval.example.jsonl:1) [评测规划](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/02-define-retrieval-evaluation.md:9) | 工具部分实现，基线未建立 |
| 可观测性与审计 | 搜索生成 `traceId` 并记录一次耗时日志；健康检查覆盖 PostgreSQL/Redis，启用 RAG 后覆盖 Milvus/MinIO。[检索日志](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/routes/knowledge.py:221) [就绪检查](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/infrastructure/health.py:62) | 没有持久化检索/模型/权限/知识变更审计，没有分阶段 Trace、指标、队列监控、告警、成本和发布门禁；相关定义仍开放。[可观测性票据](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/08-define-observability-release-gates.md:9) | 部分实现 |
| 内部 Web | 当前应用仅注册健康、任务、知识 API。[路由注册](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/router.py:3) | 没有前端资源、Web 页面或员工交互入口；最小 Web 原型票据仍被身份和证据契约阻塞。[Web 票据](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/05-design-internal-web-entry.md:4) | 未实现 |

## 规划与代码的关键漂移

1. **处理完成被等同于发布完成。** `process_document()` 在向量写入成功后直接将文档设为 `active=True/READY` 并淘汰旧版，没有 `DRAFT/REVIEWED/PUBLISHED/DISABLED` 之类的治理状态。这不仅是“少一个 API”，而是状态机和职责边界需要调整。[自动发布代码](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:302)
2. **租户过滤存在，但身份边界不存在。** 当前过滤只能防止一次查询返回不同 `scope_id` 的记录，不能阻止调用者伪造另一个 `tenantId`；`operatorId` 也没有参与检索授权。[搜索请求](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/knowledge_schemas.py:31) [检索调用](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/api/routes/knowledge.py:224)
3. **README 的“后续路线”比当前生产试点地图更宽。** README 仍把 Function、LangGraph 跨流程和受控写操作列为连续后续阶段，但生产试点地图明确把 ERP 实时 Function、工作流和写操作排除在本轮范围外。[README 后续路线](/D:/Project/Python/crossborder-erp-ai-assistant/README.md:243) [本轮范围外](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:39) 后续实现应以地图为本轮范围基准，避免提前进入 Function/Agent 工作。
4. **“有指标计算代码”不等于“达到质量目标”。** 现有单测用手工构造的命中结果验证公式，示例数据只有两行；它不证明当前 Fake 检索达到 Recall@5 ≥ 90% 或 MRR@5 ≥ 0.80。[指标单测](/D:/Project/Python/crossborder-erp-ai-assistant/tests/test_retrieval_evaluation.py:37) [目标值](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:22)
5. **规划仍是路线图，不是可直接编码的完整规格。** 语料、评测、治理、身份、Web、解析、证据和上线门禁票据均为 `open`；其中评测/解析依赖语料，回答依赖评测与解析，Web 依赖身份与回答契约。[票据依赖](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/02-define-retrieval-evaluation.md:4) [回答依赖](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/07-define-answer-evidence-contract.md:4)

## 主要风险

- **越权风险（最高优先级）：** 在接入 OIDC 并由服务端派生 tenant/operator/role 之前，任何能访问 API 的调用者都可以声明目标租户；现有“双层过滤”不能弥补身份来源不可信。
- **误发布风险：** 解析成功即公开检索，无法让管理员先复核解析质量、敏感范围和版本差异。
- **Worker 状态误报：** `process_document()` 捕获处理异常并写数据库失败状态，但不重新抛出；Celery 任务随后无条件返回 `status: processed`。队列层会把 OCR_REQUIRED/FAILED 也视为任务成功，影响重试、告警和运营判断。[异常吞并](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:333) [Worker 返回](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/workers/tasks.py:21)
- **跨存储一致性风险：** 数据库先提交新版本为活动状态，随后才尽力删除旧 Milvus 数据，并吞掉删除异常；虽然 PostgreSQL 二次过滤会阻止旧 Chunk 返回，但会积累孤儿向量，且当前没有删除/清理作业。[提交与清理](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:315)
- **去重语义风险：** 哈希唯一键只包含 scope/scope_id/sha256，同一文件内容即使以不同 `documentKey` 上传也会返回第一次文档；是否符合知识治理语义尚未决策。[哈希去重](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/rag/service.py:84) [唯一约束](/D:/Project/Python/crossborder-erp-ai-assistant/src/erp_assistant/models/knowledge.py:28)
- **测试置信度边界：** 当前 API/处理测试使用 SQLite、内存 Storage/VectorStore 和 Fake Provider，并未覆盖真实 PostgreSQL、MinIO、Milvus、Celery 与真实模型的组合。[测试替身](/D:/Project/Python/crossborder-erp-ai-assistant/tests/test_knowledge_processing.py:22) [API 依赖覆盖](/D:/Project/Python/crossborder-erp-ai-assistant/tests/test_knowledge_api.py:85)
- **回滚风险：** 本次核查时 `git status --short --untracked-files=all` 显示仓库文件全部未跟踪，没有可比较的提交基线；正式实施前应先由用户确认并建立可回滚基线。

## 建议实施顺序

建议拆成多个可独立验收的 spec，不把生产试点一次性写成一个大 spec：

1. **先完成决策输入：** 关闭首批语料票据，再确定真实评测协议；同时完成身份声明来源、角色矩阵和知识发布状态机。没有这些输入，模型、Chunk 和阈值选择都只能靠猜测。[语料票据](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/issues/01-define-pilot-corpus.md:8)
2. **收敛现有检索模块：** 把当前 `search_knowledge()` 封装为单一 `KnowledgeRetriever.retrieve()` 用例，让 FastAPI 和评测共用同一入口，保持现有检索行为不变；再用真实语料建立 ≥100 条可版本化评测基线。
3. **分离索引与发布：** 把“解析/索引成功”和“正式可检索”拆为不同状态与用例，补齐 review/publish/disable/rollback/delete，以及 PostgreSQL、MinIO、Milvus 的补偿/清理任务。
4. **扩展入库：** 建立 `DocumentParser` 路由，依次支持 DOCX、Markdown、HTML；基于失败分布再决定 OCR、表格和 Parent-child Chunk，不提前扩大范围。[后置决策](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:32)
5. **接入真实模型并调优检索：** 实现真实 Embedding/Rerank/Chat adapter 和敏感级别路由，用评测集决定候选数、融合参数、阈值、查询扩展及 Parent-child 策略，不直接沿用 Fake 分数阈值。[调优前置](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:34)
6. **实现引用回答闭环：** 建立 `KnowledgeAssistant.ask()`、证据上下文、事实—引用绑定校验、证据不足拒答和用户反馈；把引用支持率、拒答准确率纳入自动门禁。
7. **接入 OIDC/RBAC 与审计：** 由认证上下文派生 tenant/operator/role，替换所有客户端声明式授权；覆盖员工、知识管理员和系统管理员的资源权限。
8. **补齐运行门禁与最小 Web：** 增加结构化 Trace、解析/检索/模型/队列指标、告警和真实基础设施集成测试；最后在稳定 API 上构建员工问答、引用、拒答、上传/审核和反馈页面。
9. **完成试点规格与验收：** 汇总依赖、容量、发布/回退条件，只有在质量、安全和治理门禁通过后开放内部员工试点；Function、GraphRAG、Agent 和写操作保持在本轮范围外。[范围外项目](/D:/Project/Python/crossborder-erp-ai-assistant/.scratch/rag-production-pilot/map.md:39)

## 验证记录

- `.venv\\Scripts\\ruff.exe check --no-cache .`：通过。
- `.venv\\Scripts\\mypy.exe --no-incremental src`：通过，41 个源文件无问题。
- `.venv\\Scripts\\pytest.exe -p no:cacheprovider --basetemp <项目内临时目录>`：22 项通过，总代码覆盖率 78%；临时目录已清理。
- 首次 Pytest 使用默认用户临时目录时因沙箱权限失败；改用项目内一次性临时目录后全部通过，因此不是代码测试失败。
- 未运行真实 PostgreSQL/Redis/MinIO/Milvus/Celery/模型的端到端验证；当前结论不应解读为生产环境已通过。


# 内部生产试点级 RAG MVP 路线图

Type: wayfinder:map
Status: open

## Destination

形成一份可直接交付实施的生产试点级 RAG MVP 规格：覆盖文档入库、真实检索、引用回答、身份权限、知识治理、评测和基础可观测性，并明确阶段顺序与上线门禁。

## Notes

- 当前后端已实现文本型 PDF 入库、Dense + BM25 + RRF、Fake Rerank、阈值拒答、租户过滤和离线检索指标；真实语义质量、引用回答、认证和治理尚未完成。
- 当前 Ruff、Mypy 与 22 项 Pytest 均通过；仓库文件仍全部未提交，开始实施前需要建立可回滚的 Git 基线。
- 目标用户是内部员工；试点使用真实身份、真实知识和真实模型。
- WeKnora 仅作为解析、检索、评测和治理设计参考，不作为依赖，也不追求功能对齐。
- 使用入口为最小内部 Web 页面；企业 IM 接入后置。
- 首批格式默认包含文本型 PDF、DOCX、Markdown 和 HTML。
- 数据按敏感级别路由：普通知识可使用合规云模型，敏感知识使用内网或本地模型。
- 身份优先接入现有 OIDC/SSO，不继续信任客户端声明的 `operatorId`。
- 知识采用管理员上传或审核后发布；未发布内容不进入正式检索。
- 业务事实必须有代码绑定的引用；证据不足时拒答。
- 初始质量目标：至少 100 条人工标注问题，Recall@5 ≥ 90%，MRR@5 ≥ 0.80，引用支持率 ≥ 95%，拒答准确率 ≥ 90%，跨租户泄漏为 0。
- 保留 FastAPI、PostgreSQL、Redis、MinIO、Milvus 和 Celery 技术栈。
- 决策会话应使用 `grilling` 和 `domain-modeling`；外部事实调查使用 `research`。

## Decisions so far

<!-- 已关闭票据的结论索引；开放票据不在此处重复列出。 -->

## Not yet specified

- 在首批语料和评测协议明确后，确定云端与本地 Embedding、Rerank、LLM 候选及分级路由规则。
- 在解析失败分布明确后，决定扫描 PDF、图片、Excel 和复杂表格何时进入范围。
- 在真实评测数据形成后，确定 Parent-child Chunk、查询扩展、候选数量、融合参数和模型阈值。
- 在使用入口和运行门禁明确后，确定试点容量、部署规格和发布节奏。

## Out of scope

- 对齐 WeKnora 的完整功能集合或直接引入 WeKnora 服务。
- Wiki 自动生成、知识图谱、GraphRAG、ReAct Agent、长期记忆和技能沙箱。
- 企业微信、飞书等 IM 渠道以及外部数据源自动同步。
- ERP 实时 Function、跨环节工作流和受控写操作；这些应单独建立后续地图。
- 面向公网 SaaS 的大规模高可用、多地域容灾和完整商业化能力。

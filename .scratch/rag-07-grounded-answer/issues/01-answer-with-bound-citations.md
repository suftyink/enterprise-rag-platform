# 01: 生成带代码绑定引用的回答

**What to build:** 让员工问题通过 `KnowledgeAssistant.ask()`先获得已授权、已发布证据，再生成简洁回答；回答中的证据标识由系统预分配，文档版本和页码或章节由代码绑定，证据不足时不调用模型并直接拒答。

**Blocked by:** rag-04-knowledge-governance/03 / 支持回滚、清理补偿与并发审计; rag-05-multiformat-ingestion/04 / 统一解析质量、安全限制与版本信息; rag-06-real-retrieval-quality/03 / 评测调优并激活合格索引 Profile.

**Status:** resolved

- [x] 有合格证据的问题返回 ANSWERED、答案单元和至少一个有效引用。
- [x] 引用包含真实 document identity、version、title、source locator 和 excerpt。
- [x] 模型只能引用请求上下文中预分配的 evidence ID。
- [x] 文档、版本、页码、章节和摘录全部由代码从证据映射。
- [x] Retriever 返回证据不足时不调用 Chat Provider，并返回明确拒答。
- [x] 只有当前已发布、仍有权限访问的证据可以进入答案。
- [x] Chat 模型按证据敏感级别选择允许的运行位置。

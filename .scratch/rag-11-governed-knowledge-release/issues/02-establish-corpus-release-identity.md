# 02: 建立并迁移 CorpusRelease 身份

**What to build:** 建立可持久化、可查询的 `CorpusRelease` 发布身份，让管理员看到当前活动版本及其完整指纹，并在证据完整时把现有 ACTIVE Profile 安全迁移为初始 Release，而无需重新向量化。

**Blocked by:** None (can start immediately).

**Status:** resolved

- [x] `CorpusRelease` 记录发布版本、corpus generation/hash、文档数量、Profile、索引 identity/build hash、处理与路由指纹、状态和时间信息。
- [x] 系统能够明确查询当前 ACTIVE、上一 ROLLBACK 和正在准备的候选 Release，且同一时刻最多只有一个 ACTIVE。
- [x] 内部 Web 向授权管理员展示活动 Release 的身份和状态，而不暴露秘密或原始文档内容。
- [x] 现有 ACTIVE Profile 只有在 manifest、corpus snapshot 和构建身份一致时才迁移为初始 Release。
- [x] 旧数据证据缺失或不匹配时迁移明确失败并要求新建候选，不伪造 PASS、hash 或构建记录。
- [x] 数据迁移可重复执行且不会创建重复活动 Release；迁移结果具有审计记录。

## Answer

已新增持久化 `CorpusRelease`、冻结成员、rollout 和动作审计模型及迁移；bootstrap 仅在活动 Profile 的 manifest 与当前快照完全一致时幂等建立初始 Release，否则明确拒绝。



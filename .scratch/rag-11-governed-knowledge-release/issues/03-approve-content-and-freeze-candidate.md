# 03: 批准内容并冻结候选语料版本

**What to build:** 让知识管理员把审核通过的内容批准进入下一候选语料，并由发布负责人冻结一个具有确定文档集合和 corpus hash 的候选 Release，而不改变当前员工正在使用的活动版本。

**Blocked by:** 01 / 试点关闭时开放受控知识治理; 02 / 建立并迁移 CorpusRelease 身份.

**Status:** resolved

- [x] 文档内容批准与员工服务生效是两个独立动作，界面和 API 使用不会混淆两者的术语。
- [x] 批准内容不会立即改变当前 ACTIVE Release 的检索结果或员工引用。
- [x] 发布负责人可以从当时已批准且作用域合法的内容冻结一个候选 Release。
- [x] 候选快照固化文档版本、授权相关元数据、corpus generation/hash 和文档数量，后续治理变化不会静默改写它。
- [x] 并发批准或重复创建候选使用幂等或修订检查，冲突以稳定错误明确返回。
- [x] 内容批准、候选创建和快照身份均产生 actor、scope、reason、trace 和 revision 可关联的审计记录。
- [x] 内部 Web 工作流证明候选创建成功而当前活动问答结果保持不变。

## Answer

内容治理在 UI 中明确为“批准入候选”，不会直接切换服务；候选创建冻结文档集合、版本摘要、治理 revision、corpus hash、generation 和文档数，并记录 actor、reason code 与 trace。


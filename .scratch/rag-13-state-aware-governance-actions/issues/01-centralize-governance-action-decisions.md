# 01: 集中治理动作决策规则

**What to build:** 建立一个服务端领域决策 Seam，根据文档、Corpus Head、清理状态、活动 Release 成员关系和操作者能力生成当前允许的治理动作；生命周期写入校验和界面能力展示共享同一组谓词，避免前后端或读写路径各自维护状态机。

**Blocked by:** None (can start immediately).

**Status:** resolved

- [x] 定义类型明确的治理动作决策输入与结果，结果至少包含允许的动作标识，并可携带稳定的不可用原因码。
- [x] `PUBLISH` 仅在文档为 `READY / REVIEW_PENDING / RETAINED` 且索引产物有效时允许；`PUBLISHED` 不得再次允许 `PUBLISH`。
- [x] `WITHDRAW` 仅对当前 Corpus Head 发布的文档允许；`ROLLBACK` 仅在现有生命周期规则允许的历史或撤回状态下出现。
- [x] `QUEUED / UNPUBLISHED` 及其他非 `READY` 处理状态不产生 publish、withdraw 或 rollback 能力。
- [x] 清理状态、保留状态或索引产物不满足前置条件时，决策结果与写入校验保持一致。
- [x] 重复 `PUBLISH` 仍返回 `INVALID_CORPUS_TRANSITION`，不得改成静默幂等成功。
- [x] 生命周期执行路径复用该决策规则并继续在事务内校验 revision、权限和最新持久化状态。
- [x] 领域决策矩阵测试覆盖所有 `PublicationStatus`、关键处理/保留/索引条件、Corpus Head 关系和清理状态。

## Answer

已新增共享治理动作决策策略，并让生命周期写入在事务内复用同一规则；状态、Corpus Head、清理、保留和索引前置条件由领域矩阵测试锁定，重复发布仍按非法转换拒绝。

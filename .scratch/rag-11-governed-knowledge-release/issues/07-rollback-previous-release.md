# 07: 回滚到上一 Release

**What to build:** 让授权系统管理员在新版本异常时把活动服务原子恢复到上一 Release，复用其已保留的语料和索引产物，并保持 rollout、审计和后续修复状态清晰。

**Blocked by:** 06 / 原子激活候选 Release.

**Status:** resolved

- [x] 只有满足回滚条件且产物完整的上一 Release 可以成为回滚目标。
- [x] 回滚原子切换活动 Release/Profile，不要求重新上传、解析、切块或向量化上一版本文档。
- [x] 回滚失败时当前活动版本保持一致，不出现无 ACTIVE、双 ACTIVE 或服务绑定错配。
- [x] 回滚记录操作者、原因、源/目标 Release、关联 readiness 和 trace，并使被替换版本进入明确状态。
- [x] 回滚后员工问答和引用恢复到上一 Release，候选治理和问题修复仍可继续。
- [x] 内部 Web 工作流展示回滚目标、确认影响、执行结果，并验证恢复后的可观察答案版本。

## Answer

回滚只接受当前活动或降级版本的直接前序，原子恢复保留的 Release/Profile，并将 rollout 置为 PAUSED；无需重新上传、解析、切块或向量化，动作与原因写入发布审计。



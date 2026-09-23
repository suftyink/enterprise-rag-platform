# 02: 在内部 API 返回操作者可用动作

**What to build:** 为内部知识治理列表和单文档响应增加由服务端派生的 `allowedActions`，批量组装领域决策所需状态并与当前操作者的角色、范围权限及活动 Release 成员关系求交集；公共知识接口不暴露管理员能力。

**Blocked by:** 01 / 集中治理动作决策规则.

**Status:** resolved

- [x] 增加专用的内部文档响应契约并返回 `allowedActions`，不改变员工或公共知识响应的管理员语义。
- [x] 内部列表以批量查询取得 Corpus Head、相关清理状态和活动 Release 成员关系，不为每个文档执行一次完整 inspect 流程。
- [x] 返回动作同时满足生命周期条件和当前 actor 权限；租户知识管理员不会收到仅系统管理员可执行的全局动作。
- [x] `EMERGENCY_WITHDRAW` 只对当前活动 Release 的实际成员、符合现有紧急撤回前置条件且 actor 为 `SYSTEM_ADMIN` 时返回。
- [x] `allowedActions` 只包含固定动作标识和安全原因码，不包含正文、凭据、Prompt 或底层基础设施秘密。
- [x] 生命周期与紧急撤回写入端点保持最终权威，不能因为响应曾返回某动作而跳过事务内校验。
- [x] API 合约测试覆盖 `UNPUBLISHED`、`REVIEW_PENDING`、`PUBLISHED`、`SUPERSEDED`、`WITHDRAWN`、权限差异和活动 Release 成员/非成员。
- [x] 现有上传、列表和单文档调用方在新增内部契约后保持兼容。

## Answer

已增加内部文档响应契约，通过批量查询组装 Corpus Head、清理任务、索引任务和活动 Release 成员关系，再与 actor 角色及作用域求交集生成 `allowedActions`；公共知识响应保持不变，写入端仍执行最终权限与状态校验。

# 03: 按能力渲染治理操作并恢复陈旧状态

**What to build:** 让内部 Web 只根据 `allowedActions` 渲染固定注册的治理按钮，移除无条件展示逻辑；当 revision 或生命周期发生并发冲突时刷新列表并提示管理员按最新状态操作，同时用真实状态约束替换会接受任意动作的浏览器测试 Fake。

**Blocked by:** 02 / 在内部 API 返回操作者可用动作.

**Status:** resolved

- [x] 文档卡只渲染 `allowedActions` 中由客户端固定注册表认识的按钮；未知动作被安全忽略且不能生成任意请求。
- [x] `QUEUED / UNPUBLISHED` 文档不显示发布、撤回或回滚，并展示“处理完成后可审核”等非交互说明。
- [x] `READY / REVIEW_PENDING` 文档只显示允许的发布动作；发布成功并刷新后，`PUBLISH` 消失且合法的后续动作出现。
- [x] `PUBLISHED` 文档不显示或提交 `PUBLISH`；活动 Release 非成员不显示紧急撤回。
- [x] 收到 revision 冲突或 `INVALID_CORPUS_TRANSITION` 时自动刷新文档列表，显示“文档状态已变化，请按最新可用操作重试”，并保留服务端 trace 供排查。
- [x] 浏览器测试 Fake 拒绝非法转换，或改用执行真实决策契约的测试服务，不能继续把任意动作统一返回为 `PUBLISHED`。
- [x] 主浏览器工作流覆盖 `UNPUBLISHED → REVIEW_PENDING → PUBLISHED → WITHDRAWN / ROLLBACK` 的按钮变化，且不会触发当前重复发布报错。
- [x] 并发浏览器场景覆盖两个管理员持有同一 revision，其中一个先完成转换后另一个收到冲突并收敛到最新按钮。
- [x] 浏览器回归明确断言 `PUBLISHED` 卡片中的“批准入候选”按钮数量为零，并覆盖紧急撤回的成员与权限边界。

## Answer

前端现仅按固定动作注册表渲染服务端返回的能力，未知动作安全忽略；409 revision/状态冲突会刷新列表并显示带 trace 的恢复提示。浏览器测试已改用真实生命周期约束，覆盖完整按钮变化、权限和 Release 成员边界及并发冲突收敛。

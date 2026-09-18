# 02: 贯通空库首次初始化最小路径

**What to build:** 让系统管理员在没有 RetrievalProfile 和 CorpusRelease 的全新环境中，从内部 Web 直接初始化知识库；系统幂等建立可恢复的初始化运行、STAGING Profile 和隔离的开发索引，并进入等待首批审核内容的状态，无需管理员输入 Profile UUID，也不会提前开放员工问答。

**Blocked by:** 01 / 启动期阻止不兼容数据库运行.

**Status:** resolved

- [x] 空数据库的内部 Web 显示“初始化知识库”，不显示无法执行的迁移动作，也不要求输入 Profile UUID。
- [x] 已认证系统管理员可以启动初始化；未认证用户、普通员工和越权管理员均被现有身份、RBAC 与 CSRF 控制拒绝。
- [x] 一次启动持久化初始化运行及其模式、阶段、状态、Attempt、幂等键、配置指纹、actor、trace、revision 和关联资源身份。
- [x] 开发环境使用明确标记的 Fake 配置，创建一个 STAGING Profile 和隔离的物理索引身份，然后进入 `WAITING_FOR_CONTENT`。
- [x] 使用同一幂等键重复提交返回同一个运行、Profile 和物理索引身份，不创建重复资源。
- [x] 页面显示当前阶段、Attempt、等待原因和“上传并治理文档”的下一步操作；关闭并重新打开页面仍能看到同一运行。
- [x] 初始化期间知识治理入口可用，但员工问答与反馈仍受活动 Release 和 rollout 门禁拒绝。
- [x] 内部 Web 测试从空数据库贯通初始化到 `WAITING_FOR_CONTENT`，并验证授权、幂等性、审计和员工门禁。

## Answer

内部 Web 已改为状态驱动的“初始化知识库”向导，隐藏 Profile UUID；幂等建立运行、STAGING Profile 与隔离索引后进入 `WAITING_FOR_CONTENT`。浏览器回归覆盖 CSRF、员工拒绝和跨页面恢复。

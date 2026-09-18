# 08: 验收完整首次发布工作流

**What to build:** 使用唯一的内部 Web 首次初始化测试 Seam，贯通从空数据库或合格历史 Profile 到首个受治理活动 Release 的正常和失败路径，证明首次配置、索引安全、恢复能力与既有评测发布门禁共同满足内部生产试点要求。

**Blocked by:** 05 / 迁移有效的历史 ACTIVE Profile; 06 / 从首批审核内容构建候选 Release; 07 / 约束生产 Runtime 选择和预检.

**Status:** claimed

- [ ] 自动化内部 Web 工作流从当前空数据库贯通初始化、首批内容治理、候选构建、评测、Readiness、人工批准、激活和 rollout。
- [ ] 验收证明管理员全程不需要输入 Profile UUID，初始化可跨会话恢复，且相同或竞争请求不会创建重复 Profile、Collection 或 Release。
- [ ] 失败路径覆盖数据库 revision 不兼容、依赖预检失败、Worker 失联、Provision 重试、索引身份冲突、语料漂移、取消和陈旧 revision。
- [ ] 历史升级路径同时覆盖有效 ACTIVE Profile 无重嵌入迁移，以及证据缺失时拒绝迁移并转入全新初始化。
- [ ] 只有与候选指纹匹配且在有效期内的 PASS Readiness、人工批准、原子激活和 rollout 全部完成后，员工问答才可用并标识精确 Release。
- [ ] 初始化、重试、取消、迁移、碰撞、候选和激活动作可通过 actor、scope、reason、trace、Profile 与 Release 身份追溯，且记录中不存在敏感正文或凭据。
- [ ] 使用真实 OIDC、PostgreSQL、Redis、对象存储、Milvus、Worker、模型路由和代表性文档完成生产类人工验收；Fake 自动化结果不得单独授权生产试点。
- [ ] 验收报告如实记录通过、失败、未完成和不适用项；任何必需证据缺失时不得开放内部生产试点。

## Answer

本地自动化已覆盖空库初始化、幂等/并发、索引创建与碰撞、跨会话恢复、CSRF/RBAC、Worker lease、重试/取消、有效与无效历史迁移、候选冻结/构建移交，以及既有评测、Readiness、人工激活、rollout、引用回答、回滚与紧急撤回链路。真实 OIDC、PostgreSQL、Redis、对象存储、Milvus、Worker、模型路由和代表性语料的生产类人工验收尚未执行，因此本票与 Spec12 保持 claimed，不能据此开放生产试点。

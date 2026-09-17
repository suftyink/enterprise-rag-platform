# 08: 按员工组灰度并支持暂停

**What to build:** 让试点负责人针对当前活动 Release 按明确员工组逐步开放问答，并能在不重建 Release、不删除知识和不关闭治理面的情况下暂停、缩小或扩大访问范围。

**Blocked by:** 06 / 原子激活候选 Release.

**Status:** resolved

- [x] rollout 独立表达 DISABLED、CANARY、ENABLED 和 PAUSED，并明确绑定一个 ACTIVE Release。
- [x] 试点负责人可以按身份组或明确名单设置 canary 人群，系统根据已认证 actor context 做服务端判定。
- [x] 命中 canary 的员工可以问答，未命中的员工得到稳定且不泄漏内部配置的门禁响应。
- [x] 扩大、缩小、暂停或重新开放 rollout 不触发语料重建、重新向量化或 Release 激活。
- [x] 切换到另一 ACTIVE Release 后，旧 Release 的 rollout 批准不会自动授权新版本。
- [x] 暂停员工流量时，知识治理、候选构建、故障调查和回滚仍可使用。
- [x] rollout 变更记录 Release、范围、容量、负责人、原因、actor 和 trace；内部 Web 工作流覆盖 canary、扩大与暂停。

## Answer

rollout 已作为 Release 绑定的独立追加记录实现 DISABLED、CANARY、ENABLED、PAUSED；服务端按已认证 subject 判定 cohort，切换、暂停或扩大范围不重建知识，也不会继承旧 Release 的开放状态。

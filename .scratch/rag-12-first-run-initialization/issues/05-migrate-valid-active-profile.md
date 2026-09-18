# 05: 迁移有效的历史 ACTIVE Profile

**What to build:** 让升级环境从统一初始化入口识别并迁移证据完整的历史 ACTIVE Profile，无需重新解析或向量化；当历史证据过期、不完整或物理身份冲突时明确拒绝，并把管理员引导到安全的全新初始化路径。

**Blocked by:** 03 / 安全 Provision 并核验 Milvus 物理索引.

**Status:** resolved

- [x] 存在 ACTIVE Profile 但没有 CorpusRelease 时，内部 Web 显示“迁移当前版本”；客户端不能在没有有效 Profile 时强制选择迁移模式。
- [x] 迁移仅接受 manifest、corpus snapshot、build hash、处理指纹和物理索引身份均完整且一致的 ACTIVE Profile。
- [x] 有效迁移幂等建立初始 ACTIVE CorpusRelease，复用既有文档和索引产物，不重新调用解析、切块或 Embedding。
- [x] 缺失或不匹配证据、陈旧 Profile 或索引身份冲突以稳定原因拒绝迁移，不伪造 hash、manifest、评测或 Readiness 证据。
- [x] 拒绝迁移后页面提供“全新初始化”动作，且不会修改原 Profile、删除物理索引或影响现有安全门禁。
- [x] 迁移、重复迁移和拒绝结果记录 actor、trace、关联 Profile/Release 和稳定原因，且不包含文档正文或凭据。
- [x] 内部 Web 测试覆盖有效迁移无重嵌入、重复提交幂等以及无效迁移回退到全新初始化。

## Answer

统一初始化 Module 已封装既有 bootstrap：只有 ACTIVE Profile 的构建 manifest 与当前语料快照一致时才幂等迁移为初始 ACTIVE Release；无效证据保留原 Profile 并返回全新初始化动作，全程不重新 Embedding。

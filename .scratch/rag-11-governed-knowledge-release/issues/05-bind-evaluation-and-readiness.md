# 05: 将评测和 Readiness 绑定候选版本

**What to build:** 对已构建候选 Release 运行检索评测、回答评测和统一 readiness 检查，使所有质量、安全、依赖与运行证据精确绑定同一个可部署版本，并让管理员看到阻止发布的原因。

**Blocked by:** 04 / 增量构建独立候选索引.

**Status:** resolved

- [x] 检索评测和回答评测使用候选 Release 的冻结语料和 STAGING 索引，不读取移动中的活动治理状态。
- [x] 评测报告绑定候选的 corpus hash、index build hash、处理配置和检索/回答路由指纹。
- [x] `PilotReadiness` 只接受与候选 Release 完全匹配且仍在有效期内的评测、依赖、遥测和恢复证据。
- [x] FAIL、INDETERMINATE、过期、版本不匹配或缺少必需证据时，候选不能进入可批准状态。
- [x] PASS 报告是不可变记录，并可从候选 Release 追溯到使用的数据集、评测和生产探针证据。
- [x] 管理界面显示候选 readiness 状态、报告时间、发布指纹和稳定 reason code，不暴露问题文本、文档正文、Prompt 或凭据。
- [x] 内部 Web 工作流覆盖 PASS、FAIL、过期和指纹不匹配的可观察发布结果。

## Answer

候选评测 CLI 可绑定 `candidate-release-id`，Readiness 绑定严格核对发布版本、索引 build hash、处理配置和路由指纹；缺失、非 PASS、过期或错配证据均以稳定 reason code 阻止 READY/激活。


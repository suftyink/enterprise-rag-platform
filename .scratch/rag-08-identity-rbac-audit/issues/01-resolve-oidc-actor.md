# 01: 从 OIDC 解析可信 ActorContext

**What to build:** 让内部请求使用企业 OIDC/SSO 令牌完成签名、issuer、audience、expiry 和必需 claim 验证，并结合内部映射生成不可由请求体覆盖的 ActorContext。

**Blocked by:** rag-04-knowledge-governance/03 / 支持回滚、清理补偿与并发审计; rag-07-grounded-answer/03 / 建立回答反馈与质量门禁.

**Status:** ready-for-agent

- [ ] 合法令牌解析为 subject、tenant、roles 和知识范围信息。
- [ ] 过期、签名错误、issuer 错误、audience 错误和缺少 claim 均被拒绝。
- [ ] JWKS 使用有界缓存，并在轮换后可安全刷新。
- [ ] 身份提供方暂时不可用时采用明确的失败策略。
- [ ] 请求中的 tenantId 或 operatorId 不能覆盖已验证身份。
- [ ] 生产配置缺少必需 OIDC 映射时失败关闭。
- [ ] 测试身份 Adapter 无法被生产配置意外启用。

# 02: 强制员工检索与问答范围授权

**What to build:** 让内部员工只能检索和询问其租户及被授予的知识范围，所有模块使用可信 ActorContext 进行授权，并让无权限资源表现为不存在而不泄漏元数据。

**Blocked by:** 01 / 从 OIDC 解析可信 ActorContext.

**Status:** claimed

- [ ] Retriever 和 KnowledgeAssistant 只接受可信 ActorContext。
- [ ] 员工只能访问所属租户和显式允许的全局或共享知识。
- [ ] 伪造 tenantId、operatorId 或知识范围不会扩大权限。
- [ ] 无权限文档、版本和知识范围不泄漏存在性或状态。
- [ ] 检索和回答在权限失效时立即停止返回相关证据。
- [ ] 授权拒绝产生安全原因码和 trace 关联。
- [ ] 跨租户隔离场景通过同一 IdentityAccess 行为验证。

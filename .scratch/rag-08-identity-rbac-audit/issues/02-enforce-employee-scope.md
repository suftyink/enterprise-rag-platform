# 02: 强制员工检索与问答范围授权

**What to build:** 让内部员工只能检索和询问其租户及被授予的知识范围，所有模块使用可信 ActorContext 进行授权，并让无权限资源表现为不存在而不泄漏元数据。

**Blocked by:** 01 / 从 OIDC 解析可信 ActorContext.

**Status:** resolved

- [x] Retriever 和 KnowledgeAssistant 只接受可信 ActorContext。
- [x] 员工只能访问所属租户和显式允许的全局或共享知识。
- [x] 伪造 tenantId、operatorId 或知识范围不会扩大权限。
- [x] 无权限文档、版本和知识范围不泄漏存在性或状态。
- [x] 检索和回答在权限失效时立即停止返回相关证据。
- [x] 授权拒绝产生安全原因码和 trace 关联。
- [x] 跨租户隔离场景通过同一 IdentityAccess 行为验证。

## Answer

Retriever 与 KnowledgeAssistant 在执行前后均通过 `IdentityAccess` 复核最新成员关系；租户、全局和显式分配的共享范围逐一授权、审计，并作为 SQL/Milvus 的共同过滤条件。共享范围支持经授权入库；撤权、跨租户拒绝和不存在资源统一返回非泄漏错误并记录 trace 审计。

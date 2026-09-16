# 03: 强制管理员治理权限并完成安全审计

**What to build:** 让知识管理员只在分配范围内上传、审核、发布和撤回，让系统管理员承担受控的全局权限，同时移除静态 operator 白名单和客户端身份信任，并完整记录特权操作审计。

**Blocked by:** 02 / 强制员工检索与问答范围授权.

**Status:** resolved

- [x] 员工、知识管理员和系统管理员具有明确的 action/resource 权限矩阵。
- [x] 知识管理员不能治理未分配租户或知识范围。
- [x] 全局发布需要独立的系统管理员权限。
- [x] 静态 global operator ID 白名单不再决定生产权限。
- [x] API 不再把客户端 tenantId/operatorId 当作授权依据。
- [x] 登录失败、授权拒绝和所有治理转换生成不可变审计事件。
- [x] 审计包含 actor、action、resource、result、reason、trace 和时间，但不包含令牌或正文。
- [x] 不可访问资源统一使用非泄漏响应。

## Answer

已用 action/resource 权限矩阵替换静态 operator 白名单，知识治理统一经过 `IdentityAccess`；认证、拒绝和特权操作写入脱敏、不可变的安全审计记录。

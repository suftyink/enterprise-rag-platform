# 01: 交付员工登录、提问、答案与引用主路径

**What to build:** 让内部员工在浏览器中通过企业 SSO 登录、提交问题、查看回答，并打开包含文档、版本和页码或章节的代码绑定引用，形成首个可演示的员工问答纵向路径。

**Blocked by:** rag-04-knowledge-governance/03 / 支持回滚、清理补偿与并发审计; rag-07-grounded-answer/03 / 建立回答反馈与质量门禁; rag-08-identity-rbac-audit/03 / 强制管理员治理权限并完成安全审计; rag-09-observability-release-gates/03 / 纳入真实基础设施、容量与恢复证据.

**Status:** resolved

- [x] 未登录用户进入受保护页面时启动企业 SSO 流程。
- [x] 已登录员工可提交有效问题并看到加载状态。
- [x] ANSWERED 结果以清晰答案和可识别引用标记呈现。
- [x] 引用详情显示来源标题、版本、页码或章节、锚点和摘录。
- [x] 页面不信任或执行模型输出中的 HTML 或脚本。
- [x] 员工无法通过浏览器参数扩大租户或知识范围。
- [x] 浏览器主路径与服务端 trace 可关联。

## Answer

已交付 OIDC Authorization Code + PKCE 登录、Redis 服务端会话、员工问答与代码绑定引用详情；浏览器端仅以 `textContent`/文本节点渲染不可信内容，租户和知识范围由服务端身份确定，响应与操作均显示或返回 trace。

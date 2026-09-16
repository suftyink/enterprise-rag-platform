# Spec 10：交付内部员工 Web 试点入口与验收

Type: spec
Status: claimed
Blocked by: rag-04-knowledge-governance, rag-07-grounded-answer, rag-08-identity-rbac-audit, rag-09-observability-release-gates
Primary test seam: internal Web user workflow

## Problem Statement

当前项目只有后端 API，没有内部员工可使用的页面，也没有把登录、问答、引用、拒答、反馈和知识审核串成可验收工作流。直接开放 API 无法验证员工体验、权限边界和运营闭环。

## Solution

构建最小内部 Web：员工通过企业 SSO 提问、查看引用和提交反馈；知识管理员上传、查看处理状态、审核、发布、撤回和回滚。界面只调用前述稳定应用 Seam，并在 `PilotReadiness` 全部必需门禁通过后开放试点。

## User Stories

1. As an employee, I want to sign in with company SSO, so that access uses my existing identity.
2. As an employee, I want a simple question box and clear progress state, so that I can ask without understanding RAG internals.
3. As an employee, I want answers rendered with visible citation markers, so that supporting evidence is obvious.
4. As an employee, I want to open citation details showing document, version and page/section, so that I can verify the source.
5. As an employee, I want an explicit insufficient-knowledge message, so that refusal is not confused with a system error.
6. As an employee, I want retryable system failures distinguished from refusals, so that I know when to try again.
7. As an employee, I want to rate an answer and provide a short reason, so that incorrect cases can be reviewed.
8. As a knowledge administrator, I want to upload an approved format and see processing progress, so that ingestion is operable without API tools.
9. As a knowledge administrator, I want to review warnings and source metadata before publishing, so that bad parsing is caught.
10. As a knowledge administrator, I want to publish, withdraw or roll back only within my authorized scope, so that governance remains enforced.
11. As a system administrator, I want global-scope actions visually separated and confirmed, so that broad changes are deliberate.
12. As an auditor, I want UI actions linked to server-side audit traces, so that browser activity remains accountable.
13. As a pilot owner, I want a documented acceptance run covering employee and administrator journeys, so that launch approval is evidence-based.
14. As a user with accessibility needs, I want keyboard operation, focus visibility and semantic labels, so that the pilot is usable without a mouse.

## Implementation Decisions

- The UI is a minimal internal application, not a WeKnora feature clone and not a public SaaS portal.
- Employee navigation contains sign-in/session state, ask, answer/refusal, citation details and feedback.
- Administrator navigation contains document upload, processing status, review warnings, publish, withdraw and rollback for authorized scopes.
- All identity and authorization decisions remain server-side; hiding a control is usability, not security enforcement.
- Answer rendering treats model output as untrusted content and prevents script/HTML injection.
- Citations are rendered only from code-bound citation objects and retain document version and source locator.
- The UI distinguishes normal refusal, validation failure, authorization denial, processing state and transient system failure.
- Upload supports only the four approved formats and displays file/size limitations before submission.
- Destructive or broad lifecycle actions require explicit confirmation and show the affected document/version/scope.
- Feedback is linked to the answer trace and never becomes published knowledge automatically.
- Accessibility baseline includes keyboard flow, visible focus, semantic controls, readable contrast and status announcements.
- Pilot access is feature-gated and enabled only with a passing, current `PilotReadiness` report and an approved rollback owner.

## Testing Decisions

- The sole primary testing Seam is the internal Web user workflow exercised in a browser.
- End-to-end scenarios cover employee sign-in/question/answer/citation/refusal/feedback and administrator upload/review/publish/withdraw/rollback through the visible UI.
- Tests verify authorization by observable outcomes and server responses without directly testing component internals.
- Accessibility checks, content-injection fixtures, failure-state rendering and responsive layout are included in the same browser workflow suite.
- API/module tests remain owned by earlier specs and are not duplicated here.

## Out of Scope

- Enterprise WeChat, Feishu or other IM integrations.
- Wiki generation, knowledge graphs, GraphRAG, agents and long-term memory.
- ERP real-time Function calls, workflows and write operations.
- Public registration, billing, multi-region support and mobile-native apps.
- A full document-management or analytics platform.

## Further Notes

The pilot is complete only when representative employees and knowledge administrators finish the defined browser workflows against passing production-like readiness evidence. UI completion alone is not launch approval.

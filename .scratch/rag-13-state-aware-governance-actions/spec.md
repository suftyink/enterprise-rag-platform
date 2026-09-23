# Spec 13：由服务端驱动合法的知识治理动作

Type: spec
Status: resolved
Label: ready-for-agent
Blocked by: rag-04-knowledge-governance, rag-11-governed-knowledge-release
Primary test seam: internal Web state-aware governance workflow

## Problem Statement

内部知识治理页面对每一张文档卡都固定显示“批准入候选”“撤回”“回滚”，没有根据处理状态、发布状态、保留状态、当前 Corpus Head、活动 Release 成员关系或操作者权限筛选动作。因此已经是 `PUBLISHED` 的文档仍可再次点击 `PUBLISH`，后端正确返回 `INVALID_CORPUS_TRANSITION: 不能从 PUBLISHED 执行 PUBLISH`；`QUEUED / UNPUBLISHED` 文档同样会显示当前不能执行的按钮。

“紧急撤回并暂停”也只根据系统是否存在活动 Release 和文档是否为 `WITHDRAWN` 决定显示，可能出现在不属于活动 Release 的文档上。前端由此暗示管理员可以执行实际上不合法或不适用的操作，而真正的状态机只存在于后端。

现有浏览器测试使用的 `FakeCorpus` 会把任意治理动作都返回为 `PUBLISHED`，测试又按固定顺序执行发布、撤回和回滚，因此没有验证每个状态实际允许哪些动作，反而掩盖了前后端契约漂移。

## Solution

建立一个服务端治理动作决策接口，作为“哪些动作当前可用”的唯一来源。它综合文档处理状态、发布状态、保留状态、索引产物、Corpus Head、清理任务、活动 Release 成员关系以及操作者权限，返回面向当前内部管理员的 `allowedActions`。内部文档列表和单文档响应携带该字段，前端只渲染返回的动作，不自行重新实现生命周期规则。

后端执行接口继续在事务内重新校验动作、预期 revision 和权限。`allowedActions` 是当前快照下的操作能力，不是绕过并发控制的授权令牌；如果其他管理员已改变状态，服务端仍返回稳定的 409 冲突，前端刷新文档并提示状态已变化。

本修复不把 `PUBLISH` 改为幂等成功。重复发布通常意味着客户端状态过期或操作意图错误，静默接受会掩盖审计和并发问题。当前已发布文档保持可检索资格，不需要再次“批准入候选”。

## User Stories

1. As a knowledge administrator, I want each document to show only actions that are currently legal, so that I do not trigger avoidable lifecycle errors.
2. As a knowledge administrator, I want processing documents to explain that governance actions are waiting for indexing, so that absence of buttons is not mistaken for a UI failure.
3. As a system administrator, I want emergency withdrawal to appear only for a document that belongs to the active Release and can actually be withdrawn, so that an emergency control never targets unrelated content.
4. As an administrator, I want a stale action conflict to refresh the document and explain that its state changed, so that concurrent governance is recoverable.
5. As a security reviewer, I want action visibility to respect the current actor's role and scope, so that the UI does not advertise unauthorized operations.
6. As a maintainer, I want display decisions and transition validation to share one domain policy, so that adding a lifecycle state cannot silently desynchronize the UI.

## Implementation Decisions

- Introduce one domain-level lifecycle decision seam that derives allowed actions from persisted state. Transition execution and internal Web presentation must consume the same predicates; JavaScript must not maintain an independent transition table.
- The decision input includes the document, matching `KnowledgeCorpusHead`, latest relevant cleanup state, active Release membership where required, and actor capability. The result contains only safe action identifiers and optional stable unavailability reason codes; it contains no document content or credentials.
- Add a dedicated internal document response shape with `allowedActions`. Keep the employee/public knowledge response free of administrator-specific capabilities.
- Compute `allowedActions` in a batched service/query path for the internal list. Do not issue one full corpus inspection query per document.
- The server returns only actions that both the lifecycle state and the current actor permit. The mutation endpoint remains authoritative and repeats authorization and transition checks inside the write transaction.
- For the currently exposed normal controls, the minimum state mapping is:
  - `UNPUBLISHED` or a non-`READY` processing state: no publish, withdraw or rollback action.
  - `REVIEW_PENDING + READY + RETAINED` with valid index artifacts: `PUBLISH` when the actor may publish that scope.
  - The document currently published by its Corpus Head: `WITHDRAW`.
  - `SUPERSEDED` or `WITHDRAWN + READY + RETAINED` with valid index artifacts: `ROLLBACK` when the existing domain rules permit it.
- If later UI work exposes `REJECT`, `RESUBMIT` or `REQUEST_PURGE`, it must use the same server decision result rather than adding client-side state checks.
- Treat `EMERGENCY_WITHDRAW` as a release operation. Return it only for a system administrator when the document is a member of the current active Release and the release workflow can execute the emergency withdrawal.
- Render buttons from `allowedActions` using a fixed presentation registry that maps known identifiers to labels and visual severity. Ignore unknown action identifiers safely and record a client diagnostic; never synthesize a request from arbitrary server text.
- When no action is available because processing is incomplete, show a non-interactive explanation such as“处理完成后可审核”。对于终态或历史版本，显示简洁的状态说明，不提供非法按钮。
- Keep optimistic concurrency through `expectedRevision`. On `INVALID_CORPUS_TRANSITION` or revision conflict, reload the document list and show“文档状态已变化，请按最新可用操作重试”，同时保留 trace identifier 供问题排查。
- Do not make repeated `PUBLISH` idempotent. Idempotency belongs to retried transport commands with the same operation identity, not to a new governance command against an already published revision.
- Replace the permissive browser-test fake with a stateful contract fake or real test service that rejects illegal transitions and returns `allowedActions` from the same decision policy.

## Testing Decisions

- The sole primary test Seam is the internal Web state-aware governance workflow.
- Start with a `QUEUED / UNPUBLISHED` document and assert that no lifecycle mutation button is rendered and the waiting explanation is visible.
- Advance the document to `READY / REVIEW_PENDING` with retained index artifacts and assert that an authorized administrator sees `PUBLISH`, but not `WITHDRAW` or `ROLLBACK`.
- Publish the document through the UI, reload the list, and assert that `PUBLISH` disappears while `WITHDRAW` becomes available. Clicking the visible action must not produce `INVALID_CORPUS_TRANSITION`.
- Withdraw the document and assert that the newly returned allowed actions are reflected without retaining stale buttons. Where domain prerequisites hold, `ROLLBACK` is available; otherwise it is absent with a stable explanation.
- Add a `SUPERSEDED` historical version and assert that only the valid rollback path is offered.
- Add an active Release containing one document and another unrelated document. Assert that `EMERGENCY_WITHDRAW` appears only on the active member and only for `SYSTEM_ADMIN`.
- Simulate two administrators reading the same revision, let one transition first, then submit the stale action from the other session. Assert a 409 response, automatic list refresh, updated buttons and the state-changed message.
- Verify a tenant knowledge administrator cannot see global publication actions that require system-admin authority.
- Add a domain decision matrix test covering every `PublicationStatus`, the relevant processing/retention/index conditions, current-head membership and cleanup states. The matrix locks the shared policy without testing SQL query shape.
- Preserve direct transition tests proving that hand-crafted or stale HTTP requests are rejected even when a button would not be rendered.

## Acceptance Criteria

- A `PUBLISHED` document never displays or submits `PUBLISH` in the internal Web UI.
- A `QUEUED / UNPUBLISHED` document does not advertise publish, withdraw or rollback.
- Every rendered lifecycle action is present in the server-provided `allowedActions` for that exact document response and actor.
- Emergency withdrawal is limited to eligible members of the active Release and system administrators.
- Stale UI state converges by refreshing after a conflict; backend transition and authorization enforcement are unchanged.
- The browser regression test fails against the current unconditional-button implementation and passes after the fix.

## Out of Scope

- Changing the meaning of `PUBLISHED`, Corpus Release activation or employee retrieval eligibility.
- Making invalid lifecycle transitions silently idempotent.
- Redesigning the complete knowledge-governance page or visual system.
- Adding new lifecycle operations beyond those already supported by the domain.
- Changing document parsing, embedding, candidate build, evaluation, readiness or pilot rollout behavior.

## Further Notes

短期只在 JavaScript 中按 `publicationStatus` 隐藏按钮可以消除眼前报错，但会复制后端状态机，且无法可靠判断 Corpus Head、索引产物、清理任务、活动 Release 成员关系和权限，因此不作为正式方案。服务端派生能力、前端按能力渲染、写入时再次校验，能够同时解决当前缺陷和同类治理按钮漂移问题。

## Answer

已完成服务端驱动的治理动作能力：生命周期写入与内部展示复用共享决策策略，内部 API 批量组装 actor-aware `allowedActions`、稳定原因码及紧急撤回 Release 标识；前端仅渲染固定白名单动作，并在 revision 或状态冲突时刷新列表。领域矩阵、API 与真实状态约束的浏览器工作流已覆盖关键状态、权限、Release 成员及并发恢复场景。

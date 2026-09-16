# Spec 08：接入 OIDC、RBAC 与安全审计

Type: spec
Status: claimed
Blocked by: rag-04-knowledge-governance, rag-07-grounded-answer
Primary test seam: `IdentityAccess`

## Problem Statement

当前上传、状态和检索接口信任客户端提交的 `tenantId` 与 `operatorId`；现有租户过滤只能隔离给定 scope，不能阻止调用者伪造另一个租户。全局上传依赖静态 operator 白名单，也没有员工、知识管理员和系统管理员的资源权限矩阵。

## Solution

建立 `IdentityAccess` 模块，通过现有企业 OIDC/SSO 验证令牌并生成不可由客户端覆盖的 `ActorContext`。RBAC 在知识问答和治理模块边界执行，所有安全相关访问与状态变更产生脱敏审计事件。

## User Stories

1. As an employee, I want to use my existing company identity, so that I do not manage another password.
2. As an employee, I want access limited to my tenant and assigned knowledge spaces, so that confidential knowledge stays isolated.
3. As a knowledge administrator, I want permission to upload, review, publish and withdraw knowledge in assigned scopes, so that governance duties are controlled.
4. As a system administrator, I want separately governed global-knowledge privileges, so that broad publication is exceptional and auditable.
5. As a security reviewer, I want forged tenant/operator request fields ignored or rejected, so that identity always comes from verified claims.
6. As an operator, I want cached JWKS and identity mappings, so that normal requests do not depend on a live OIDC round trip.
7. As an auditor, I want authentication failures, denied actions and privileged transitions recorded safely, so that investigations have evidence.
8. As an employee, I want inaccessible resources to appear absent, so that their existence is not leaked.
9. As a maintainer, I want local tests to use a deterministic identity adapter, so that CI does not require enterprise SSO.
10. As a security owner, I want expired, wrong-issuer and wrong-audience tokens rejected, so that only intended identities enter the system.

## Implementation Decisions

- `IdentityAccess` is the sole identity and authorization Seam. It verifies credentials, resolves membership/roles and evaluates actions against resources.
- `ActorContext` contains subject ID, tenant ID, role set and authorized knowledge-scope references derived only from verified identity plus internal mappings.
- OIDC verification checks signature, issuer, audience, expiry and required claims. JWKS is cached with bounded refresh and fail-safe behavior.
- Request bodies and query strings no longer provide trusted `tenantId` or `operatorId`; transitional fields are ignored only during a documented compatibility window, then removed.
- Initial roles are internal employee, knowledge administrator and system administrator. Permissions are expressed as actions over knowledge scopes, documents and lifecycle transitions.
- `KnowledgeRetriever`, `KnowledgeAssistant` and `KnowledgeCorpus` accept only trusted actor context and enforce permissions inside their module boundaries.
- Unauthorized and nonexistent resources use non-leaking responses.
- Global publication requires an explicit privileged action; the static operator-ID list is removed.
- Audit events include actor, action, resource reference, outcome, reason code, trace ID and time, while excluding tokens, questions, prompts and document content.
- A deterministic test adapter and actor fixtures are retained; they cannot be enabled by production configuration.

## Testing Decisions

- The sole primary testing Seam is `IdentityAccess`.
- Tests cover valid identity resolution, expiry, signature/issuer/audience failures, missing claims, tenant membership, role/action matrix, global privilege, non-leaking denial and audit-event emission through that Seam.
- Recording adapters validate JWKS caching and safe audit fields without creating a second module-level behavioral suite.
- Existing knowledge scenarios consume resulting `ActorContext` fixtures; they do not repeat token-verification tests.
- A minimal deployment smoke check may call protected APIs, but acceptance behavior remains owned by the identity Seam.

## Out of Scope

- Provisioning or changing the enterprise identity provider.
- Fine-grained attribute-based policy beyond the initial role and knowledge-scope model.
- External customer identities and public SaaS tenancy.
- Storing raw ID tokens or access tokens.
- Building the Web login experience, which belongs to Spec 10.

## Further Notes

OIDC issuer, audience and claim mappings are deployment inputs. The implementation must refuse startup or protected traffic when required production mappings are absent; it must not fall back to caller-supplied identity.

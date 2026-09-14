# Spec 07：实现引用回答与证据拒答闭环

Type: spec
Status: ready-for-agent
Blocked by: rag-04-knowledge-governance, rag-05-multiformat-ingestion, rag-06-real-retrieval-quality
Primary test seam: `KnowledgeAssistant.ask()`

## Problem Statement

当前系统只能返回检索 Chunk 或 `INSUFFICIENT_KNOWLEDGE`，没有面向员工的答案、证据上下文、事实与引用绑定、冲突/过期证据处理或反馈。即使接入 Chat Provider，也不能允许模型自行编造文档、页码和引用。

## Solution

建立 `KnowledgeAssistant` 深模块。`ask()` 先调用 `KnowledgeRetriever`，通过证据门禁后再调用符合数据分级策略的 Chat Adapter；模型只能使用系统分配的 evidence ID，最终引用由代码绑定和校验。任何证据不足、引用无效或事实缺少支持的结果都失败关闭为拒答。

## User Stories

1. As an employee, I want a concise answer to my business question, so that I do not have to read every retrieved fragment.
2. As an employee, I want each factual statement linked to source evidence, so that I can verify it quickly.
3. As an employee, I want citations to show title, version and page or section location, so that the original source is findable.
4. As an employee, I want the assistant to refuse when evidence is insufficient, so that uncertainty is not hidden.
5. As an employee, I want conflicting sources called out rather than silently merged, so that I can seek clarification.
6. As a reviewer, I want unpublished, withdrawn and superseded versions excluded, so that answers use governed knowledge.
7. As a security reviewer, I want the model limited to authorized evidence, so that prompts cannot reveal inaccessible knowledge.
8. As a security owner, I want sensitive evidence routed only to an approved model location, so that policy is enforced during generation.
9. As an operator, I want malformed model output and unknown evidence IDs rejected, so that fabricated citations never reach users.
10. As a product owner, I want citation support and refusal accuracy measured, so that answer quality has a release gate.
11. As an employee, I want to submit useful/not-useful feedback with a reason, so that failed cases can enter the evaluation backlog.

## Implementation Decisions

- `KnowledgeAssistant.ask()` is the only public answer use case; callers provide a question and trusted actor context.
- The module calls `KnowledgeRetriever` and does not duplicate retrieval logic.
- Insufficient retrieval evidence returns a refusal without invoking the Chat provider.
- Evidence receives deterministic IDs before prompt construction. The model returns structured answer units referencing only those IDs.
- Citation metadata—document identity, version, title, page/section/anchor and excerpt—is always mapped by code from evidence, never accepted from model text.
- Non-refusal factual answer units require at least one valid evidence ID. Unknown IDs, invalid structured output or unsupported claims fail closed.
- Conflicting authoritative evidence produces either a qualified answer displaying the conflict or an explicit refusal according to the evidence contract.
- Only current published versions may be cited; publication is rechecked before returning an answer when the request crosses a version change.
- Context selection obeys a configured token budget while preserving source locators and evidence identity.
- The answer outcome includes trace ID, decision, answer units, citations and a safe refusal/error reason.
- Feedback stores question/answer trace reference, rating and reason without treating free-form feedback as trusted knowledge.
- Spec 02 evaluation is extended with citation support rate at least 95% and answer refusal accuracy at least 90%.

## Testing Decisions

- The sole primary testing Seam is `KnowledgeAssistant.ask()`.
- Scenario tests cover answered, insufficient evidence, conflicting evidence, withdrawn evidence, unknown citation ID, missing citation, malformed model output, provider timeout, sensitive routing and feedback linkage through that Seam.
- Deterministic Retriever and Chat adapters supply controlled evidence and structured outputs; tests assert only answer outcomes, citations and observable audit/feedback references.
- Citation support is checked against code-bound evidence, not string similarity to a model-generated bibliography.
- Retrieval ranking and provider internals are not retested in this spec.

## Out of Scope

- OIDC token verification and final HTTP authorization wiring.
- Conversational memory, multi-turn agents, tools and ERP Function calls.
- Public-web search and external sources.
- Rich document preview UI.
- Streaming unless the structured evidence contract can be preserved without exposing unvalidated claims.

## Further Notes

Correctness takes priority over fluency. A well-formed refusal is a valid business outcome; a fluent answer with an invalid citation is not.

# Spec 02：建立真实检索评测基线

Type: spec
Status: resolved
Blocked by: rag-01-knowledge-retriever
Primary test seam: `RetrievalEvaluator.run()`

## Problem Statement

当前已有 Recall@5、MRR@5、nDCG@5、拒答、租户泄漏和延迟公式，但示例集只有少量合成数据，执行器主要通过 HTTP 和松散字典工作，也没有数据集版本、分类统计、配置指纹或自动质量门禁。因此现有测试只能证明公式可计算，不能证明检索达到生产试点目标。

## Solution

建立 `RetrievalEvaluator` 模块，以版本化 JSONL 评测集驱动 Spec 01 的同一个 `KnowledgeRetriever`。评测报告记录数据集和检索配置身份，输出总体与分类指标，并对既定试点阈值给出通过或失败结论。

## User Stories

1. As a product owner, I want at least 100 reviewed business questions, so that the pilot is evaluated on representative work rather than demos.
2. As a domain reviewer, I want each question linked to expected evidence, so that relevance judgments can be audited.
3. As a retrieval engineer, I want overall and category-level metrics, so that improvements do not hide failures on SKU, numbers or abbreviations.
4. As a security reviewer, I want explicit cross-tenant leakage cases, so that any leakage fails the run.
5. As an operator, I want refusal cases measured separately, so that raising recall does not cause unsupported answers.
6. As a maintainer, I want every report tied to dataset, index, model and configuration versions, so that results are reproducible.
7. As a release owner, I want machine-readable pass/fail gates, so that unqualified retrieval changes cannot reach the pilot.
8. As a developer, I want fast in-process evaluation, so that retrieval experiments do not require an HTTP deployment.
9. As a deployment tester, I want the existing HTTP runner retained as an optional end-to-end adapter, so that deployed wiring can still be checked.
10. As a reviewer, I want invalid or incomplete cases rejected before execution, so that bad labels do not silently distort metrics.

## Implementation Decisions

- `RetrievalEvaluator.run()` is the public evaluation interface and calls `KnowledgeRetriever` for every case.
- The dataset schema includes immutable case ID, question, trusted evaluation actor, expected relevant sources, expected decision and category tags.
- Required categories include Chinese natural language, English abbreviations, SKU/product identifiers, numeric/table facts, ambiguous questions, no-answer questions and tenant-isolation probes.
- The production baseline contains at least 100 human-reviewed cases drawn from the approved pilot corpus; synthetic examples remain test fixtures only.
- Reports record dataset hash/version, execution time, retrieval pipeline version, index identity, model identities and configuration fingerprint.
- Required metrics are Recall@5, MRR@5, nDCG@5, refusal accuracy, tenant leakage count and latency percentiles.
- Initial gates are Recall@5 at least 90%, MRR@5 at least 0.80, refusal accuracy at least 90% and tenant leakage count exactly zero. Citation support is deferred until the answer spec.
- Dependency errors are reported as execution failures, not converted into missed retrievals.
- In-process execution is the default; HTTP execution is retained only as an adapter for deployment verification.

## Testing Decisions

- The sole primary testing Seam is `RetrievalEvaluator.run()`.
- Tests feed valid and invalid datasets through the Seam and assert metrics, category breakdowns, reproducibility metadata, leakage failure and quality-gate decisions.
- A deterministic `KnowledgeRetriever` test adapter supplies controlled outcomes; metric helper functions are not separate test targets.
- One compatibility test may exercise the optional HTTP adapter through the same evaluator Seam.
- Existing retrieval-evaluation formula tests are prior art and should be migrated upward where practical.

## Out of Scope

- Choosing production model vendors.
- Answer generation and citation support scoring.
- Building the business corpus itself without domain-owner review.
- Online A/B testing, dashboards and continuous scheduling.
- Changing retrieval parameters before a reviewed baseline exists.

## Further Notes

The implementation framework is agent-ready. Completion of the production gate additionally requires business owners to approve the corpus and relevance labels; the report must distinguish “framework complete” from “production baseline approved.”

## Answer

评测框架、分类指标、质量门禁、可复现报告和 HTTP 验收 Adapter 已完成。框架会把合成、未复核、分类不全或少于 100 条的评测标记为 `INDETERMINATE`；真实生产基线仍需业务方提供并批准语料与相关性标签。

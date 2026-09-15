import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from erp_assistant.evaluation.models import (
    DatasetProvenance,
    EvaluationCase,
    EvaluationCategory,
    EvaluationDataset,
    EvaluationDatasetError,
    RelevantSource,
)
from erp_assistant.rag.retriever import ActorContext
from erp_assistant.rag.types import DataClassification, SearchDecision

_CASE_FIELDS = {
    "actor",
    "categories",
    "datasetVersion",
    "dataClassification",
    "expectedDecision",
    "expectedSources",
    "forbiddenDocumentKeys",
    "id",
    "provenance",
    "question",
    "reviewed",
}


def load_dataset(path: Path) -> EvaluationDataset:
    dataset_bytes = path.read_bytes()
    try:
        lines = dataset_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise EvaluationDatasetError(
            "INVALID_DATASET_ENCODING", "Evaluation dataset must be UTF-8"
        ) from exc

    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    dataset_version: str | None = None
    for line_number, raw_line in enumerate(lines, 1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise EvaluationDatasetError(
                "INVALID_CASE_SCHEMA",
                f"Invalid JSON at line {line_number}",
                line_number,
            ) from exc
        case = _parse_case(value, line_number)
        if case.case_id in seen_ids:
            raise EvaluationDatasetError(
                "DUPLICATE_CASE_ID",
                f"Duplicate evaluation case ID at line {line_number}",
                line_number,
            )
        if dataset_version is not None and case.dataset_version != dataset_version:
            raise EvaluationDatasetError(
                "MIXED_DATASET_VERSION",
                f"Mixed dataset versions at line {line_number}",
                line_number,
            )
        seen_ids.add(case.case_id)
        dataset_version = case.dataset_version
        cases.append(case)

    if not cases or dataset_version is None:
        raise EvaluationDatasetError("EMPTY_DATASET", "Evaluation dataset is empty")
    return EvaluationDataset(
        version=dataset_version,
        content_hash=sha256(dataset_bytes).hexdigest(),
        cases=tuple(cases),
    )


def _parse_case(value: Any, line_number: int) -> EvaluationCase:
    if (
        not isinstance(value, dict)
        or not set(value).issubset(_CASE_FIELDS)
        or not (_CASE_FIELDS - {"dataClassification"}).issubset(value)
    ):
        raise _invalid("Invalid evaluation case fields", line_number)
    actor = value["actor"]
    if not isinstance(actor, dict) or set(actor) != {"subjectId", "tenantId"}:
        raise _invalid_actor(line_number)
    subject_id = actor["subjectId"]
    tenant_id = actor["tenantId"]
    if not _is_bounded_text(subject_id, 64) or not _is_bounded_text(tenant_id, 64):
        raise _invalid_actor(line_number)

    case_id = value["id"]
    version = value["datasetVersion"]
    question = value["question"]
    if not all(
        (
            _is_bounded_text(case_id, 128),
            _is_bounded_text(version, 64),
            _is_bounded_text(question, 2_000),
        )
    ):
        raise _invalid("Invalid evaluation case values", line_number)

    expected_sources = value["expectedSources"]
    categories = value["categories"]
    forbidden_keys = value["forbiddenDocumentKeys"]
    if (
        not isinstance(expected_sources, list)
        or not isinstance(categories, list)
        or not categories
        or not isinstance(forbidden_keys, list)
        or not isinstance(value["reviewed"], bool)
    ):
        raise _invalid("Invalid evaluation labels", line_number)
    try:
        provenance = DatasetProvenance(value["provenance"])
        expected_decision = SearchDecision(value["expectedDecision"])
        data_classification = DataClassification(
            value.get("dataClassification", DataClassification.ORDINARY.value)
        )
        category_set = frozenset(EvaluationCategory(category) for category in categories)
        sources = tuple(_parse_source(source, line_number) for source in expected_sources)
    except (TypeError, ValueError) as exc:
        raise _invalid("Invalid evaluation labels", line_number) from exc
    if len(category_set) != len(categories) or any(
        not _is_bounded_text(key, 256) for key in forbidden_keys
    ):
        raise _invalid("Invalid evaluation labels", line_number)

    normalized_forbidden_keys = tuple(key.strip() for key in forbidden_keys)
    if len(normalized_forbidden_keys) != len(set(normalized_forbidden_keys)):
        raise _invalid("Invalid evaluation labels", line_number)
    forbidden_key_set = frozenset(normalized_forbidden_keys)
    source_keys = {source.document_key for source in sources}
    if (
        (expected_decision == SearchDecision.MATCHED) != bool(sources)
        or bool(source_keys.intersection(forbidden_key_set))
        or _has_overlapping_sources(sources)
        or (
            (EvaluationCategory.NO_ANSWER in category_set)
            != (expected_decision == SearchDecision.INSUFFICIENT_KNOWLEDGE)
        )
        or ((EvaluationCategory.TENANT_ISOLATION in category_set) != bool(forbidden_key_set))
    ):
        raise _invalid("Inconsistent evaluation labels", line_number)
    return EvaluationCase(
        case_id=case_id.strip(),
        dataset_version=version.strip(),
        question=question.strip(),
        actor=ActorContext(subject_id=subject_id.strip(), tenant_id=tenant_id.strip()),
        expected_sources=sources,
        expected_decision=expected_decision,
        categories=category_set,
        forbidden_document_keys=forbidden_key_set,
        provenance=provenance,
        reviewed=value["reviewed"],
        data_classification=data_classification,
    )


def _parse_source(value: Any, line_number: int) -> RelevantSource:
    if not isinstance(value, dict) or set(value) != {"documentKey", "pageEnd", "pageStart"}:
        raise _invalid("Invalid expected source", line_number)
    document_key = value["documentKey"]
    page_start = value["pageStart"]
    page_end = value["pageEnd"]
    if (
        not _is_bounded_text(document_key, 256)
        or type(page_start) is not int
        or type(page_end) is not int
        or page_start < 1
        or page_end < page_start
    ):
        raise _invalid("Invalid expected source", line_number)
    return RelevantSource(document_key.strip(), page_start, page_end)


def _is_bounded_text(value: Any, maximum: int) -> bool:
    return type(value) is str and bool(value.strip()) and len(value.strip()) <= maximum


def _has_overlapping_sources(sources: tuple[RelevantSource, ...]) -> bool:
    return any(
        left.document_key == right.document_key
        and left.page_start <= right.page_end
        and right.page_start <= left.page_end
        for index, left in enumerate(sources)
        for right in sources[index + 1 :]
    )


def _invalid(message: str, line_number: int) -> EvaluationDatasetError:
    return EvaluationDatasetError(
        "INVALID_CASE_SCHEMA", f"{message} at line {line_number}", line_number
    )


def _invalid_actor(line_number: int) -> EvaluationDatasetError:
    return EvaluationDatasetError(
        "INVALID_EVALUATION_ACTOR",
        f"Invalid evaluation actor at line {line_number}",
        line_number,
    )

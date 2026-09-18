from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import erp_assistant.main as main_module
import erp_assistant.operations.evidence as evidence_module
import erp_assistant.operations.production_probes as probes_module
import erp_assistant.operations.schema_compatibility as schema_module
import erp_assistant.workers.celery_app as worker_module
from erp_assistant.core.config import Settings
from erp_assistant.evaluation.models import GateDecision
from erp_assistant.operations.production_probes import ProductionRuntimeStartupError
from erp_assistant.operations.schema_compatibility import (
    DatabaseSchemaCompatibilityError,
    SchemaRevisionGraph,
    ensure_schema_compatible,
)


def test_current_database_revision_is_accepted() -> None:
    ensure_schema_compatible(
        current_heads=("20260922_0014",),
        graph=SchemaRevisionGraph(
            heads=("20260922_0014",),
            ancestors=frozenset({"20260921_0013", "20260922_0014"}),
        ),
    )


@pytest.mark.parametrize(
    ("current_heads", "expected_code"),
    [
        (("20260921_0013",), "DATABASE_SCHEMA_OUTDATED"),
        (("20260923_0015",), "DATABASE_SCHEMA_AHEAD"),
        (("unknown-branch",), "DATABASE_SCHEMA_DIVERGED"),
        (("20260921_0013", "other-head"), "DATABASE_SCHEMA_DIVERGED"),
    ],
)
def test_incompatible_database_revision_has_stable_diagnostic(
    current_heads: tuple[str, ...], expected_code: str
) -> None:
    graph = SchemaRevisionGraph(
        heads=("20260922_0014",),
        ancestors=frozenset({"20260921_0013", "20260922_0014"}),
    )

    with pytest.raises(DatabaseSchemaCompatibilityError) as raised:
        ensure_schema_compatible(current_heads=current_heads, graph=graph)

    error = raised.value
    assert error.code == expected_code
    assert error.current_revisions == current_heads
    assert error.expected_revisions == ("20260922_0014",)
    assert "alembic" in error.remediation.casefold()
    assert "password" not in str(error).casefold()


@pytest.mark.asyncio
async def test_api_lifespan_checks_schema_before_other_runtime_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def reject_schema() -> None:
        calls.append("schema")
        raise DatabaseSchemaCompatibilityError(
            code="DATABASE_SCHEMA_OUTDATED",
            current_revisions=("20260921_0013",),
            expected_revisions=("20260922_0014",),
            remediation="Run alembic upgrade head.",
        )

    async def runtime_probe(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("runtime")

    monkeypatch.setattr(main_module, "validate_database_schema", reject_schema)
    monkeypatch.setattr(main_module, "validate_production_runtime_startup", runtime_probe)

    with pytest.raises(DatabaseSchemaCompatibilityError):
        async with main_module.lifespan(FastAPI()):
            pass

    assert calls == ["schema"]


def test_worker_checks_schema_before_other_runtime_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def reject_schema(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("schema")
        raise DatabaseSchemaCompatibilityError(
            code="DATABASE_SCHEMA_DIVERGED",
            current_revisions=("other-head",),
            expected_revisions=("20260922_0014",),
            remediation="Inspect `python -m alembic heads`.",
        )

    async def runtime_probe(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("runtime")

    monkeypatch.setattr(schema_module, "validate_database_schema", reject_schema)
    monkeypatch.setattr(probes_module, "validate_production_runtime_startup", runtime_probe)
    monkeypatch.setattr(
        evidence_module,
        "validate_production_startup",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(DatabaseSchemaCompatibilityError):
        worker_module.validate_worker_startup()

    assert calls == ["schema"]


@pytest.mark.asyncio
async def test_production_startup_reports_unhealthy_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def passing_configuration(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(
            checks=(SimpleNamespace(name="configuration", decision=GateDecision.PASS),)
        )

    async def unhealthy_dependencies(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(
            components={
                "postgres": SimpleNamespace(status="up"),
                "redis": SimpleNamespace(status="down"),
                "minio": SimpleNamespace(status="down"),
                "milvus": SimpleNamespace(status="up"),
            }
        )

    monkeypatch.setattr(
        probes_module,
        "_collect_production_configuration",
        passing_configuration,
    )
    monkeypatch.setattr(probes_module, "check_readiness", unhealthy_dependencies)

    with pytest.raises(ProductionRuntimeStartupError) as raised:
        await probes_module.validate_production_runtime_startup(
            Settings(app_env="production"),
            observed_at=datetime.now(UTC),
        )

    assert raised.value.failed_checks == ("redis", "minio")
    assert "password" not in str(raised.value).casefold()

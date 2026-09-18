from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

_ORDERED_REVISION = re.compile(r"^(?P<date>\d{8})_(?P<sequence>\d+)$")


@dataclass(frozen=True, slots=True)
class SchemaRevisionGraph:
    heads: tuple[str, ...]
    ancestors: frozenset[str]


class DatabaseSchemaCompatibilityError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        current_revisions: tuple[str, ...],
        expected_revisions: tuple[str, ...],
        remediation: str,
    ) -> None:
        self.code = code
        self.current_revisions = current_revisions
        self.expected_revisions = expected_revisions
        self.remediation = remediation
        current = ", ".join(current_revisions) or "<empty>"
        expected = ", ".join(expected_revisions)
        super().__init__(
            f"{code}: database revisions [{current}], expected [{expected}]. {remediation}"
        )


def ensure_schema_compatible(
    *,
    current_heads: tuple[str, ...],
    graph: SchemaRevisionGraph,
) -> None:
    if current_heads == graph.heads:
        return

    if len(current_heads) != 1:
        _raise_incompatible("DATABASE_SCHEMA_DIVERGED", current_heads, graph.heads)

    current = current_heads[0] if current_heads else ""
    if not current or current in graph.ancestors:
        _raise_incompatible("DATABASE_SCHEMA_OUTDATED", current_heads, graph.heads)

    if _is_revision_ahead(current, graph.heads):
        _raise_incompatible("DATABASE_SCHEMA_AHEAD", current_heads, graph.heads)

    _raise_incompatible("DATABASE_SCHEMA_DIVERGED", current_heads, graph.heads)


async def validate_database_schema(
    database_engine: AsyncEngine | None = None,
    *,
    project_root: Path | None = None,
) -> None:
    if database_engine is None:
        from erp_assistant.infrastructure.database import engine

        database_engine = engine
    graph = load_schema_revision_graph(project_root=project_root)
    async with database_engine.connect() as connection:
        current_heads = await connection.run_sync(_read_current_heads)
    ensure_schema_compatible(current_heads=current_heads, graph=graph)


def load_schema_revision_graph(*, project_root: Path | None = None) -> SchemaRevisionGraph:
    root = project_root or Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    script = ScriptDirectory.from_config(config)
    heads = tuple(sorted(script.get_heads()))
    ancestors = frozenset(revision.revision for revision in script.walk_revisions())
    return SchemaRevisionGraph(heads=heads, ancestors=ancestors)


def _read_current_heads(connection: Connection) -> tuple[str, ...]:
    context = MigrationContext.configure(connection)
    return tuple(sorted(context.get_current_heads()))


def _is_revision_ahead(current: str, expected_heads: tuple[str, ...]) -> bool:
    current_match = _ORDERED_REVISION.fullmatch(current)
    expected_matches = [_ORDERED_REVISION.fullmatch(value) for value in expected_heads]
    if current_match is None or any(match is None for match in expected_matches):
        return False
    current_key = (
        int(current_match.group("date")),
        int(current_match.group("sequence")),
    )
    expected_keys = [
        (int(match.group("date")), int(match.group("sequence")))
        for match in expected_matches
        if match is not None
    ]
    return bool(expected_keys) and current_key > max(expected_keys)


def _raise_incompatible(
    code: str,
    current_revisions: tuple[str, ...],
    expected_revisions: tuple[str, ...],
) -> None:
    if code == "DATABASE_SCHEMA_OUTDATED":
        remediation = "Run `python -m alembic upgrade head` before starting this process."
    elif code == "DATABASE_SCHEMA_AHEAD":
        remediation = (
            "Verify with `python -m alembic current`, then deploy matching application code; "
            "do not downgrade with a running process."
        )
    else:
        remediation = "Inspect `python -m alembic heads` and reconcile the migration graph."
    raise DatabaseSchemaCompatibilityError(
        code=code,
        current_revisions=current_revisions,
        expected_revisions=expected_revisions,
        remediation=remediation,
    )

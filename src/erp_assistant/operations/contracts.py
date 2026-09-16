from collections.abc import Mapping
from typing import Protocol
from uuid import UUID


class Telemetry(Protocol):
    def emit(
        self,
        *,
        trace_id: UUID,
        signal: str,
        stage: str,
        outcome: str,
        attributes: Mapping[str, object] | None = None,
    ) -> None: ...


class NullTelemetry:
    def emit(
        self,
        *,
        trace_id: UUID,
        signal: str,
        stage: str,
        outcome: str,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        del trace_id, signal, stage, outcome, attributes

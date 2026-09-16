from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.models.operations import PilotReadinessRecord
from erp_assistant.operations.readiness_models import PilotReadinessReport


class ReadinessReportStore(Protocol):
    async def append(self, report: PilotReadinessReport) -> None: ...


class SqlReadinessReportStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(self, report: PilotReadinessReport) -> None:
        payload = report.to_dict()
        async with self._sessions() as session:
            session.add(
                PilotReadinessRecord(
                    id=report.report_id,
                    report_version=str(payload["reportVersion"]),
                    release_version=report.identity.release_version,
                    release_fingerprint=report.identity.fingerprint,
                    decision=report.decision.value,
                    generated_at=report.generated_at,
                    payload=payload,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("Readiness reports are immutable") from exc

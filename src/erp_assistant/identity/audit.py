from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.identity.models import SecurityAuditEvent
from erp_assistant.models.security import SecurityAuditRecord


class SqlSecurityAuditSink:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: SecurityAuditEvent) -> None:
        self._session.add(
            SecurityAuditRecord(
                actor_id=event.actor_id,
                action=event.action.value,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                outcome=event.outcome,
                reason_code=event.reason_code,
                trace_id=event.trace_id,
                created_at=event.created_at,
            )
        )
        await self._session.commit()


class SessionSecurityAuditSink:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(self, event: SecurityAuditEvent) -> None:
        async with self._sessions() as session:
            await SqlSecurityAuditSink(session).append(event)

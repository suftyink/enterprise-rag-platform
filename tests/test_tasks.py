from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.infrastructure.database import Base, get_session
from erp_assistant.main import app
from erp_assistant.models.task import AiTask


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    test_engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    yield factory
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_create_task_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/assistant/v1/tasks",
                json={
                    "tenantId": "tenant-1",
                    "operatorId": "user-1",
                    "storeIds": ["US-01"],
                    "content": "查询库存",
                },
            )
        assert response.status_code == 202
        assert response.json()["status"] == "READY"
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(AiTask))
        assert count == 1
    finally:
        app.dependency_overrides.clear()


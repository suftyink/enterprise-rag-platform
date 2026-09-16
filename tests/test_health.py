from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from erp_assistant.main import app


@pytest.mark.asyncio
async def test_liveness() -> None:
    trace_id = UUID("00000000-0000-0000-0000-000000000123")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/health/live",
            headers={"X-Request-ID": str(trace_id)},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert response.headers["X-Trace-ID"] == str(trace_id)

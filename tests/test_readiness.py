import pytest

from erp_assistant.core.config import Settings
from erp_assistant.infrastructure import health


@pytest.mark.asyncio
async def test_readiness_skips_rag_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok() -> None:
        return None

    async def ok_redis(settings: Settings) -> None:
        return None

    monkeypatch.setattr(health, "check_postgres", ok)
    monkeypatch.setattr(health, "check_redis", ok_redis)
    result = await health.check_readiness(Settings(rag_enabled=False))
    assert result.status == "ready"
    assert set(result.components) == {"postgres", "redis"}


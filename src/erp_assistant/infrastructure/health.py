import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import boto3
from pymilvus import MilvusClient
from redis.asyncio import Redis
from sqlalchemy import text

from erp_assistant.api.schemas import ComponentHealth, ReadinessResponse
from erp_assistant.core.config import Settings
from erp_assistant.infrastructure.database import engine

HealthCheck = Callable[[], Awaitable[None]]


async def check_postgres() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_redis(settings: Settings) -> None:
    client = Redis.from_url(settings.redis_url)
    try:
        await client.ping()
    finally:
        await client.aclose()


async def check_milvus(settings: Settings) -> None:
    def probe() -> None:
        client = MilvusClient(uri=settings.milvus_uri)
        try:
            client.list_collections()
        finally:
            client.close()

    await asyncio.to_thread(probe)


async def check_minio(settings: Settings) -> None:
    def probe() -> None:
        client = boto3.client(
            "s3",
            endpoint_url=settings.minio_endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
        )
        client.list_buckets()

    await asyncio.to_thread(probe)


async def _run_check(check: HealthCheck) -> ComponentHealth:
    try:
        await check()
        return ComponentHealth(status="up")
    except Exception as exc:
        return ComponentHealth(status="down", detail=exc.__class__.__name__)


async def check_readiness(settings: Settings) -> ReadinessResponse:
    checks: dict[str, HealthCheck] = {
        "postgres": check_postgres,
        "redis": lambda: check_redis(settings),
    }
    if settings.rag_enabled:
        checks.update(
            {
                "milvus": lambda: check_milvus(settings),
                "minio": lambda: check_minio(settings),
            }
        )
    names = list(checks)
    results = await asyncio.gather(*(_run_check(checks[name]) for name in names))
    components = dict(zip(names, results, strict=True))
    overall: Literal["ready", "not_ready"] = (
        "ready" if all(item.status == "up" for item in results) else "not_ready"
    )
    return ReadinessResponse(status=overall, components=components)

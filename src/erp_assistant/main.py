import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import perf_counter

from fastapi import FastAPI, Request, Response

from erp_assistant.api.dependencies import request_trace_id
from erp_assistant.api.router import api_router
from erp_assistant.core.config import get_settings
from erp_assistant.operations.evidence import validate_production_startup
from erp_assistant.operations.production_probes import (
    validate_production_runtime_startup,
)
from erp_assistant.operations.telemetry import get_telemetry
from erp_assistant.orchestration.provider_factory import close_retrieval_provider_clients


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    del application
    settings = get_settings()
    observed_at = datetime.now(UTC)
    validate_production_startup(settings, observed_at=observed_at)
    await validate_production_runtime_startup(settings, observed_at=observed_at)
    try:
        yield
    finally:
        await close_retrieval_provider_clients()
        await asyncio.to_thread(get_telemetry().close)


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    @application.middleware("http")
    async def trace_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = request_trace_id(request)
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            get_telemetry().emit(
                trace_id=trace_id,
                signal="http_request",
                stage="http",
                outcome="FAILED",
                attributes={
                    "duration_ms": round((perf_counter() - started) * 1_000, 2),
                    "error_code": exc.__class__.__name__,
                },
            )
            raise
        response.headers["X-Trace-ID"] = str(trace_id)
        get_telemetry().emit(
            trace_id=trace_id,
            signal="http_request",
            stage="http",
            outcome="SUCCEEDED" if response.status_code < 500 else "FAILED",
            attributes={
                "duration_ms": round((perf_counter() - started) * 1_000, 2),
                "status": response.status_code,
            },
        )
        return response

    application.include_router(api_router)
    return application


app = create_app()

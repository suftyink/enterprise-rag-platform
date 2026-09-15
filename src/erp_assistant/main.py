from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from erp_assistant.api.router import api_router
from erp_assistant.core.config import get_settings
from erp_assistant.orchestration.provider_factory import close_retrieval_provider_clients


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    del application
    try:
        yield
    finally:
        await close_retrieval_provider_clients()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    application.include_router(api_router)
    return application


app = create_app()

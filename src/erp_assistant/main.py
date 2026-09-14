from fastapi import FastAPI

from erp_assistant.api.router import api_router
from erp_assistant.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title=settings.app_name, version="0.1.0")
    application.include_router(api_router)
    return application


app = create_app()


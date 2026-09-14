from fastapi import APIRouter

from erp_assistant.api.routes import health, knowledge, tasks

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(tasks.router, prefix="/api/assistant/v1", tags=["assistant"])
api_router.include_router(knowledge.router, prefix="/api/assistant/v1", tags=["knowledge"])

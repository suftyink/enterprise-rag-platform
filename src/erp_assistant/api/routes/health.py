from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from erp_assistant.api.schemas import ReadinessResponse
from erp_assistant.core.config import Settings, get_settings
from erp_assistant.infrastructure.health import check_readiness

router = APIRouter()


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReadinessResponse:
    result = await check_readiness(settings)
    if result.status == "not_ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result

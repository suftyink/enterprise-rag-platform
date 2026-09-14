from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tenant_id: str = Field(alias="tenantId", min_length=1, max_length=64)
    operator_id: str = Field(alias="operatorId", min_length=1, max_length=64)
    department_id: str | None = Field(default=None, alias="departmentId", max_length=64)
    store_ids: list[str] = Field(default_factory=list, alias="storeIds")
    content: str = Field(min_length=1, max_length=20_000)


class TaskAccepted(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: UUID = Field(alias="taskId")
    trace_id: UUID = Field(alias="traceId")
    status: Literal["READY"] = "READY"


class ComponentHealth(BaseModel):
    status: Literal["up", "down"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    components: dict[str, ComponentHealth]


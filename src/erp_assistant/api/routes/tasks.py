from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.api.schemas import TaskAccepted, TaskCreate
from erp_assistant.infrastructure.database import get_session
from erp_assistant.models.task import AiTask, AiTaskMessage

router = APIRouter()


@router.post("/tasks", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_task(
    payload: TaskCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskAccepted:
    task_id = uuid4()
    trace_id = uuid4()
    task = AiTask(
        id=task_id,
        trace_id=trace_id,
        tenant_id=payload.tenant_id,
        operator_id=payload.operator_id,
        department_id=payload.department_id,
        store_ids=payload.store_ids,
        status="READY",
    )
    message = AiTaskMessage(task_id=task_id, role="user", content=payload.content)
    session.add_all([task, message])
    await session.commit()
    return TaskAccepted(taskId=task_id, traceId=trace_id)

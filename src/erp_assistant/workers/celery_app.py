from celery import Celery

from erp_assistant.core.config import get_settings

settings = get_settings()
celery_app = Celery(
    "erp_assistant",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["erp_assistant.workers.tasks"],
)
celery_app.conf.update(task_serializer="json", result_serializer="json", accept_content=["json"])

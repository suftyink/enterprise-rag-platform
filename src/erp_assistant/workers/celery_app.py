from celery import Celery, bootsteps
from celery.signals import worker_process_shutdown

from erp_assistant.core.config import get_settings
from erp_assistant.operations.telemetry import get_telemetry

settings = get_settings()
celery_app = Celery(
    "erp_assistant",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["erp_assistant.workers.tasks"],
)
celery_app.conf.update(task_serializer="json", result_serializer="json", accept_content=["json"])
celery_app.conf.beat_schedule = {
    "observe-index-queue-every-minute": {
        "task": "erp_assistant.observe_index_queue",
        "schedule": 60.0,
    }
}


def flush_worker_telemetry(**kwargs: object) -> None:
    del kwargs
    get_telemetry().close()


worker_process_shutdown.connect(flush_worker_telemetry)


def validate_worker_startup(**kwargs: object) -> None:
    del kwargs
    from asyncio import run
    from datetime import UTC, datetime

    from erp_assistant.infrastructure.database import engine
    from erp_assistant.operations.evidence import validate_production_startup
    from erp_assistant.operations.production_probes import (
        validate_production_runtime_startup,
    )

    observed_at = datetime.now(UTC)
    validate_production_startup(settings, observed_at=observed_at)

    async def validate() -> None:
        try:
            await validate_production_runtime_startup(
                settings,
                observed_at=observed_at,
                worker_self=True,
            )
        finally:
            await engine.dispose()

    run(validate())


class ProductionStartupStep(bootsteps.StartStopStep):  # type: ignore[misc]
    """Abort Worker boot when the production runtime is incoherent."""

    def start(self, worker: object) -> None:
        del worker
        validate_worker_startup()


celery_app.steps["worker"].add(ProductionStartupStep)

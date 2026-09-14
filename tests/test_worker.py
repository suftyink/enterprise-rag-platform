from erp_assistant.workers.celery_app import celery_app
from erp_assistant.workers.tasks import health


def test_health_task_is_registered_and_side_effect_free() -> None:
    celery_app.finalize()
    assert "erp_assistant.health" in celery_app.tasks
    assert health.run() == {"status": "ok"}

from celery import Celery
from celery.schedules import crontab
from src.config import settings

app = Celery(
    "indexer",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["src.workers.index_worker", "src.workers.coupling_worker"],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    task_routes={
        "src.workers.index_worker.*": {"queue": "indexing"},
        "src.workers.coupling_worker.*": {"queue": "coupling"},
    },
    # Daily coupling recomputation at 2 AM
    beat_schedule={
        "coupling-daily": {
            "task": "src.workers.coupling_worker.recompute_all_coupling",
            "schedule": crontab(hour=2, minute=0),
        },
    },
)

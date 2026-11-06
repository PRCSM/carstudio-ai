from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery = Celery("carstudio", broker=settings.redis_url, backend=None, include=["app.workers.tasks"])
celery.conf.update(
    task_acks_late=True,              # redeliver if a worker dies mid-job
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=900,
    task_soft_time_limit=840,
    broker_transport_options={"visibility_timeout": 1800},
    beat_schedule={
        "dispatch-outbox": {"task": "app.workers.tasks.dispatch_outbox", "schedule": 15.0},
        "requeue-stuck": {"task": "app.workers.tasks.requeue_stuck", "schedule": 120.0},
        "cleanup-expired": {"task": "app.workers.tasks.cleanup_expired", "schedule": 6 * 3600.0},
    },
    timezone="UTC",
)

if settings.sentry_dsn:
    import sentry_sdk

    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env, traces_sample_rate=0.1,
                    send_default_pii=False)

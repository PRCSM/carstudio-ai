import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import redis
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import sign_payload
from app.db.models import GeneratedImage, GenerationJob, OutboxEvent, Upload
from app.db.session import session_scope
from app.providers.chain import ProviderChain, RedisCircuits, build_providers
from app.services.storage import get_storage
from app.services.vision import VisionClient
from app.workers.celery_app import celery
from app.workers.pipeline import RetryLater, _finish, run_job

configure_logging()
log = logging.getLogger(__name__)
settings = get_settings()
_redis = redis.Redis.from_url(settings.redis_url)
_chain: ProviderChain | None = None
OUTAGE_RETRIES = 5


def get_chain() -> ProviderChain:
    global _chain
    if _chain is None:
        _chain = ProviderChain(build_providers(settings), RedisCircuits(_redis),
                               settings.provider_circuit_seconds)
    return _chain


def get_vision() -> VisionClient | None:
    # Vision only runs on the billed key, so customer photos never reach a free tier by default.
    if settings.vision_enabled and settings.gemini_paid_api_key:
        return VisionClient(settings.gemini_paid_api_key, settings.gemini_vision_model)
    return None


@celery.task(bind=True, max_retries=OUTAGE_RETRIES, name="app.workers.tasks.process_job")
def process_job(self, job_id: str):
    # Lock TTL slightly above the task hard limit, so a killed worker's lock expires on its own.
    lock = _redis.lock(f"lock:job:{job_id}", timeout=960, blocking=False)
    if not lock.acquire(blocking=False):
        log.info("job %s is locked by another run; checking again later", job_id)
        raise self.retry(countdown=120, max_retries=OUTAGE_RETRIES + 10)
    try:
        with session_scope() as db:
            status = run_job(db, uuid.UUID(job_id), settings=settings, storage=get_storage(),
                             chain=get_chain(), vision=get_vision(),
                             final_retry=self.request.retries >= OUTAGE_RETRIES)
            log.info("job finished", extra={"job_id": job_id, "status": status})
    except RetryLater:
        countdown = min(60 * 2 ** self.request.retries, 900)
        log.warning("providers unavailable; retrying job %s in %ss", job_id, countdown)
        raise self.retry(countdown=countdown) from None
    finally:
        try:
            lock.release()
        except Exception:
            pass


@celery.task(name="app.workers.tasks.dispatch_outbox")
def dispatch_outbox():
    """Deliver committed events to n8n with HMAC signatures; exponential backoff on failure."""
    if not settings.n8n_callback_url:
        return
    with session_scope() as db:
        now = datetime.now(timezone.utc)
        events = db.scalars(select(OutboxEvent).where(OutboxEvent.published_at.is_(None),
                                                      OutboxEvent.next_attempt_at <= now)
                            .order_by(OutboxEvent.created_at).limit(20).with_for_update(skip_locked=True)).all()
        with httpx.Client(timeout=15) as client:
            for ev in events:
                body = json.dumps({"event_id": str(ev.id), "type": ev.event_type, "data": ev.payload},
                                  separators=(",", ":")).encode()
                ts, sig = sign_payload(body, settings.n8n_callback_hmac_secret)
                try:
                    r = client.post(settings.n8n_callback_url, content=body, headers={
                        "Content-Type": "application/json", "X-CarStudio-Timestamp": ts,
                        "X-CarStudio-Signature": sig, "X-CarStudio-Event-Id": str(ev.id)})
                    ok = r.status_code < 300
                    err = None if ok else f"http {r.status_code}"
                except httpx.HTTPError as e:
                    ok, err = False, type(e).__name__
                if ok:
                    ev.published_at = now
                else:
                    ev.attempt_count += 1
                    ev.last_error = err
                    ev.next_attempt_at = now + timedelta(seconds=min(30 * 2 ** ev.attempt_count, 3600))


@celery.task(name="app.workers.tasks.requeue_stuck")
def requeue_stuck():
    """Re-enqueue jobs that were never picked up (broker down at enqueue time) or whose worker died
    mid-run (OOM kill, redeploy). In-progress jobs are only touched after the run lock has expired."""
    now = datetime.now(timezone.utc)
    in_progress = ("validating", "analyzing", "preparing", "generating", "verifying")
    with session_scope() as db:
        queued = db.scalars(select(GenerationJob.id).where(
            GenerationJob.status == "queued", GenerationJob.created_at < now - timedelta(minutes=3)).limit(50)).all()
        orphaned = db.scalars(select(GenerationJob.id).where(
            GenerationJob.status.in_(in_progress),
            GenerationJob.started_at < now - timedelta(minutes=17)).limit(50)).all()
        ids = [i for i in [*queued, *orphaned] if not _redis.exists(f"lock:job:{i}")]
        # give up on jobs that keep dying (e.g. repeatedly killed for memory) instead of looping forever
        hopeless = db.scalars(select(GenerationJob).where(
            GenerationJob.status.in_(in_progress), GenerationJob.started_at < now - timedelta(hours=2))).all()
        for job in hopeless:
            if not _redis.exists(f"lock:job:{job.id}"):
                _finish(db, job, "failed", error_code="worker_lost",
                        message="Something went wrong while processing your photo. Please try again.")
                ids = [i for i in ids if i != job.id]
    for job_id in ids:
        process_job.delay(str(job_id))  # the per-job Redis lock prevents double processing


@celery.task(name="app.workers.tasks.cleanup_expired")
def cleanup_expired():
    """Delete originals and results past the retention window (DB rows cascade from uploads)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.image_retention_days)
    storage = get_storage()
    with session_scope() as db:
        uploads = db.scalars(select(Upload).where(Upload.created_at < cutoff).limit(200)).all()
        for up in uploads:
            job_ids = db.scalars(select(GenerationJob.id).where(GenerationJob.source_upload_id == up.id)).all()
            keys = db.scalars(select(GeneratedImage.storage_key).where(
                GeneratedImage.job_id.in_(job_ids), GeneratedImage.storage_key.is_not(None))).all() if job_ids else []
            storage.delete(settings.storage_outputs_bucket, list(keys))
            storage.delete(settings.storage_originals_bucket, [up.storage_key])
            db.delete(up)

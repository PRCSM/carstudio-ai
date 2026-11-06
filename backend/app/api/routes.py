import uuid

from fastapi import APIRouter, Depends, File, Header, Response, UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_principal, require_admin, utc_day_start
from app.core.config import Settings, get_settings
from app.core.errors import Conflict, NotFound, QuotaExceeded, ValidationFailed
from app.db.models import (Customer, GeneratedImage, GenerationJob, OutboxEvent, Upload)
from app.db.session import get_db
from app.schemas.jobs import (JobAccepted, JobCreate, JobOut, JobResults, ResultImage, ReviewDecision,
                              UploadOut)
from app.services.job_state import InvalidTransition, transition
from app.services.preprocess import prepare_image
from app.services.storage import get_storage

router = APIRouter()


# ---------- health ----------
@router.get("/health/live")
def live():
    return {"ok": True}


@router.get("/health/ready")
def ready(db: Session = Depends(get_db)):
    import redis

    db.execute(text("select 1"))
    redis.Redis.from_url(get_settings().redis_url, socket_timeout=2).ping()
    return {"ok": True}


# ---------- uploads ----------
@router.post("/v1/uploads", response_model=UploadOut, status_code=201)
def create_upload(file: UploadFile = File(...), p: Principal = Depends(get_principal),
                  db: Session = Depends(get_db), s: Settings = Depends(get_settings)):
    max_bytes = s.max_upload_mb * 1024 * 1024
    data = file.file.read(max_bytes + 1)  # bounded read
    prepared = prepare_image(data, max_bytes=max_bytes, max_pixels=s.max_image_pixels, min_side=s.min_image_side)

    key = f"{p.customer.id}/{uuid.uuid4()}.jpg"
    get_storage().upload(s.storage_originals_bucket, key, prepared.data, prepared.mime_type)
    up = Upload(customer_id=p.customer.id, storage_key=key, sha256=prepared.sha256, mime_type=prepared.mime_type,
                width=prepared.width, height=prepared.height, byte_size=len(prepared.data),
                quality=prepared.quality)
    db.add(up)
    db.commit()
    warnings = [k for k in ("is_blurry", "too_dark", "too_bright") if prepared.quality.get(k)]
    return UploadOut(upload_id=up.id, width=up.width, height=up.height, quality_warnings=warnings)


# ---------- jobs ----------
def _enforce_quota(db: Session, p: Principal, s: Settings) -> None:
    today = db.scalar(select(func.count()).select_from(GenerationJob).where(
        GenerationJob.customer_id == p.customer.id, GenerationJob.created_at >= utc_day_start()))
    limit = s.daily_customer_quota if p.kind == "service" else s.daily_anonymous_quota
    if today >= limit:
        raise QuotaExceeded("Daily limit reached. Please try again tomorrow.")
    if p.kind == "web" and p.client_ip:
        import redis

        r = redis.Redis.from_url(s.redis_url)
        key = f"quota:ip:{p.client_ip}:{utc_day_start().date()}"
        n = r.incr(key)
        r.expire(key, 90000)
        if n > s.daily_anonymous_quota * 3:  # a few sessions per IP, not unlimited
            raise QuotaExceeded("Daily limit reached for this network.")


@router.post("/v1/jobs", response_model=JobAccepted, status_code=202)
def create_job(body: JobCreate, idempotency_key: str = Header(..., min_length=8, max_length=128),
               p: Principal = Depends(get_principal), db: Session = Depends(get_db),
               s: Settings = Depends(get_settings)):
    scoped_key = f"{p.customer.id}:{idempotency_key}"
    existing = db.scalar(select(GenerationJob).where(GenerationJob.idempotency_key == scoped_key))
    if existing:  # webhook retry / double click: same answer, no new job
        return JobAccepted(job_id=existing.id, status=existing.status, status_url=f"/v1/jobs/{existing.id}")

    if body.style not in s.style_list:
        raise ValidationFailed(f"Unknown style. Choose one of: {', '.join(s.style_list)}.", code="invalid_style")
    upload = db.get(Upload, body.source_upload_id)
    if upload is None or upload.customer_id != p.customer.id:
        raise NotFound("Upload not found.")
    _enforce_quota(db, p, s)

    customer = db.get(Customer, p.customer.id)
    if customer.consent_version != body.consent_version:
        from datetime import datetime, timezone
        customer.consent_version = body.consent_version
        customer.consent_at = datetime.now(timezone.utc)

    job = GenerationJob(customer_id=p.customer.id, source_upload_id=upload.id, channel=p.customer.channel,
                        style=body.style, mode=body.mode, idempotency_key=scoped_key)
    db.add(job)
    db.commit()

    from app.workers.tasks import process_job
    process_job.delay(str(job.id))  # if this fails the job stays 'queued' and the sweeper re-enqueues it
    return JobAccepted(job_id=job.id, status=job.status, status_url=f"/v1/jobs/{job.id}")


def _owned_job(db: Session, job_id: uuid.UUID, p: Principal) -> GenerationJob:
    job = db.get(GenerationJob, job_id)
    if job is None or job.customer_id != p.customer.id:
        raise NotFound("Job not found.")  # same error for 'not yours' to avoid leaking ids
    return job


@router.get("/v1/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    job = _owned_job(db, job_id, p)
    return JobOut(job_id=job.id, status=job.status, style=job.style, mode=job.mode,
                  attempt_count=job.attempt_count, error_code=job.error_code, message=job.error_message,
                  created_at=job.created_at, finished_at=job.finished_at)


@router.get("/v1/jobs/{job_id}/results", response_model=JobResults)
def get_results(job_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db),
                s: Settings = Depends(get_settings)):
    job = _owned_job(db, job_id, p)
    images: list[ResultImage] = []
    if job.status == "completed":
        gen = db.scalar(select(GeneratedImage).where(GeneratedImage.job_id == job.id,
                                                     GeneratedImage.qa_status == "approved")
                        .order_by(GeneratedImage.attempt.desc()))
        if gen and gen.storage_key:
            url = get_storage().signed_url(s.storage_outputs_bucket, gen.storage_key)
            images.append(ResultImage(url=url, expires_in=s.signed_url_ttl_seconds, provider=gen.provider))
    return JobResults(job_id=job.id, status=job.status, images=images)


@router.post("/v1/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    job = _owned_job(db, job_id, p)
    # Only queued jobs: a running worker owns the job's state until it finishes.
    db.refresh(job, with_for_update=True)
    if job.status != "queued":
        raise Conflict("This job is already being processed and can't be cancelled.")
    try:
        transition(db, job, "cancelled", "customer_cancelled")
    except InvalidTransition:
        raise Conflict("This job can no longer be cancelled.") from None
    db.commit()
    return get_job(job_id, p, db)


@router.delete("/v1/me/data", status_code=204)
def delete_my_data(p: Principal = Depends(get_principal), db: Session = Depends(get_db),
                   s: Settings = Depends(get_settings)):
    storage = get_storage()
    originals = db.scalars(select(Upload.storage_key).where(Upload.customer_id == p.customer.id)).all()
    results = db.scalars(select(GeneratedImage.storage_key).join(GenerationJob).where(
        GenerationJob.customer_id == p.customer.id, GeneratedImage.storage_key.is_not(None))).all()
    storage.delete(s.storage_outputs_bucket, list(results))
    storage.delete(s.storage_originals_bucket, list(originals))
    db.delete(db.get(Customer, p.customer.id))  # cascades to all rows
    db.commit()
    return Response(status_code=204)


# ---------- admin review ----------
@router.get("/v1/admin/review", dependencies=[Depends(require_admin)])
def review_queue(db: Session = Depends(get_db), s: Settings = Depends(get_settings)):
    jobs = db.scalars(select(GenerationJob).where(GenerationJob.status == "review_required")
                      .order_by(GenerationJob.created_at).limit(50)).all()
    storage, out = get_storage(), []
    for j in jobs:
        up = db.get(Upload, j.source_upload_id)
        gens = db.scalars(select(GeneratedImage).where(GeneratedImage.job_id == j.id)
                          .order_by(GeneratedImage.attempt)).all()
        out.append({
            "job_id": str(j.id), "style": j.style, "created_at": j.created_at.isoformat(),
            "source_url": storage.signed_url(s.storage_originals_bucket, up.storage_key),
            "attempts": [{"id": str(g.id), "provider": g.provider, "qa": g.qa, "qa_status": g.qa_status,
                          "url": storage.signed_url(s.storage_outputs_bucket, g.storage_key) if g.storage_key
                          else None} for g in gens],
        })
    return out


@router.post("/v1/admin/review/{job_id}", dependencies=[Depends(require_admin)])
def review_decide(job_id: uuid.UUID, body: ReviewDecision, db: Session = Depends(get_db)):
    from app.db.models import DeliveryEvent, ReviewAction

    job = db.get(GenerationJob, job_id)
    if job is None or job.status != "review_required":
        raise NotFound("No job awaiting review with that id.")
    target = {"approve": "completed", "reject": "failed", "request_more_images": "needs_more_images"}[body.decision]
    if body.decision == "approve":
        gen = db.scalar(select(GeneratedImage).where(GeneratedImage.job_id == job.id,
                                                     GeneratedImage.qa_status == "review_required")
                        .order_by(GeneratedImage.attempt))
        if gen is None:
            raise Conflict("No reviewable image on this job.")
        gen.qa_status = "approved"
    transition(db, job, target, f"review:{body.decision}")
    db.add(ReviewAction(job_id=job.id, reviewer_id=body.reviewer_id, decision=body.decision,
                        reason=body.reason))
    db.add(OutboxEvent(event_type=f"job.{target}", payload={
        "job_id": str(job.id), "status": target, "channel": job.channel, "customer_id": str(job.customer_id),
        "has_result": target == "completed", "reviewed": True}))
    if job.channel == "whatsapp":
        db.add(DeliveryEvent(job_id=job.id, channel=job.channel))
    db.commit()
    return {"job_id": str(job.id), "status": job.status}

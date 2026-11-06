import uuid

from fastapi import APIRouter, Depends, File, Header, Response, UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_principal, require_admin, utc_day_start
from app.core.config import Settings, get_settings
from app.core.errors import Conflict, NotFound, QuotaExceeded, ValidationFailed
from app.db.models import (Customer, GeneratedImage, GenerationJob, GenerationSet, OutboxEvent, PhotoGroup, Upload)
from app.db.session import get_db
from app.schemas.jobs import (JobAccepted, JobCreate, JobOut, JobResults, ResultImage, ReviewDecision,
                              SetAccepted, SetCreate, SetItem, SetOut, UploadOut)
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
def _join_group(db: Session, customer_id: uuid.UUID, external_ref: str) -> tuple[uuid.UUID, bool]:
    """Find-or-create the photo group; the request that creates it is the 'first' photo of the album.
    Uses INSERT ... ON CONFLICT so concurrent album uploads agree on a single winner."""
    from sqlalchemy.dialects.postgresql import insert

    created = db.scalar(insert(PhotoGroup).values(customer_id=customer_id, external_ref=external_ref)
                        .on_conflict_do_nothing(index_elements=["customer_id", "external_ref"])
                        .returning(PhotoGroup.id))
    if created is not None:
        return created, True
    existing = db.scalar(select(PhotoGroup.id).where(PhotoGroup.customer_id == customer_id,
                                                     PhotoGroup.external_ref == external_ref))
    return existing, False


@router.post("/v1/uploads", response_model=UploadOut, status_code=201)
def create_upload(file: UploadFile = File(...), x_media_group: str | None = Header(default=None, max_length=64),
                  p: Principal = Depends(get_principal), db: Session = Depends(get_db),
                  s: Settings = Depends(get_settings)):
    max_bytes = s.max_upload_mb * 1024 * 1024
    data = file.file.read(max_bytes + 1)  # bounded read
    prepared = prepare_image(data, max_bytes=max_bytes, max_pixels=s.max_image_pixels, min_side=s.min_image_side)

    upload_id = uuid.uuid4()
    external_ref = f"album:{x_media_group.strip()}" if x_media_group else f"single:{upload_id}"
    group_id, is_first = _join_group(db, p.customer.id, external_ref)
    in_group = db.scalar(select(func.count()).select_from(Upload).where(Upload.group_id == group_id))
    if in_group >= s.max_set_photos:
        db.rollback()
        raise ValidationFailed(f"Albums can have up to {s.max_set_photos} photos.", code="album_too_large")

    key = f"{p.customer.id}/{upload_id}.jpg"
    get_storage().upload(s.storage_originals_bucket, key, prepared.data, prepared.mime_type)
    up = Upload(id=upload_id, customer_id=p.customer.id, storage_key=key, sha256=prepared.sha256,
                mime_type=prepared.mime_type, width=prepared.width, height=prepared.height,
                byte_size=len(prepared.data), quality=prepared.quality, group_id=group_id)
    db.add(up)
    db.commit()
    warnings = [k for k in ("is_blurry", "too_dark", "too_bright") if prepared.quality.get(k)]
    return UploadOut(upload_id=up.id, width=up.width, height=up.height, quality_warnings=warnings,
                     group_id=group_id, is_first_in_group=is_first, group_size=in_group + 1)


# ---------- jobs ----------
def _enforce_quota(db: Session, p: Principal, s: Settings, new_jobs: int = 1) -> None:
    today = db.scalar(select(func.count()).select_from(GenerationJob).where(
        GenerationJob.customer_id == p.customer.id, GenerationJob.created_at >= utc_day_start()))
    limit = s.daily_customer_quota if p.kind == "service" else s.daily_anonymous_quota
    if today + new_jobs > limit:
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

    if body.hide_plate and not s.feature_plate_blur:
        raise ValidationFailed("Hiding the number plate isn't available yet.", code="feature_disabled")
    job = GenerationJob(customer_id=p.customer.id, source_upload_id=upload.id, channel=p.customer.channel,
                        style=body.style, mode=body.mode, idempotency_key=scoped_key, reply_ref=p.reply_ref,
                        hide_plate=body.hide_plate)
    db.add(job)
    db.commit()

    from app.workers.tasks import process_job
    process_job.delay(str(job.id))  # if this fails the job stays 'queued' and the sweeper re-enqueues it
    return JobAccepted(job_id=job.id, status=job.status, status_url=f"/v1/jobs/{job.id}")


# ---------- sets (albums and generated angles) ----------
def _record_consent(db: Session, p: Principal, consent_version: str) -> None:
    from datetime import datetime, timezone

    customer = db.get(Customer, p.customer.id)
    if customer.consent_version != consent_version:
        customer.consent_version = consent_version
        customer.consent_at = datetime.now(timezone.utc)


@router.post("/v1/sets", response_model=SetAccepted, status_code=202)
def create_set(body: SetCreate, idempotency_key: str = Header(..., min_length=8, max_length=128),
               p: Principal = Depends(get_principal), db: Session = Depends(get_db),
               s: Settings = Depends(get_settings)):
    scoped_key = f"{p.customer.id}:{idempotency_key}"
    existing = db.scalar(select(GenerationSet).where(GenerationSet.idempotency_key == scoped_key))
    if existing:  # double tap / webhook retry: same answer, no new jobs
        return SetAccepted(set_id=existing.id, status=existing.status, total_jobs=existing.total_jobs,
                           status_url=f"/v1/sets/{existing.id}")

    if body.style not in s.style_list:
        raise ValidationFailed(f"Unknown style. Choose one of: {', '.join(s.style_list)}.", code="invalid_style")
    group = db.get(PhotoGroup, body.group_id)
    if group is None or group.customer_id != p.customer.id:
        raise NotFound("Photos not found. Please send them again.")
    uploads = db.scalars(select(Upload).where(Upload.group_id == group.id).order_by(Upload.created_at)).all()
    if not uploads:
        raise NotFound("Photos not found. Please send them again.")
    if body.hide_plate and not s.feature_plate_blur:
        raise ValidationFailed("Hiding the number plate isn't available yet.", code="feature_disabled")
    if body.mode == "angles" and not s.feature_novel_angles:
        raise ValidationFailed("Generating extra angles isn't available yet.", code="feature_disabled")
    if body.mode == "enhance" and len(uploads) > 1 and not s.feature_sets:
        raise ValidationFailed("Please send one photo at a time for now.", code="feature_disabled")

    if body.mode == "enhance":
        plan = [dict(source_upload_id=u.id, mode="same_angle", target_angle=None, reference_upload_ids=None)
                for u in uploads[:s.max_set_photos]]
    else:
        refs = [u.id for u in uploads[:s.max_angle_references]]
        plan = [dict(source_upload_id=refs[0], mode="novel_angle", target_angle=a, reference_upload_ids=refs)
                for a in s.angle_target_list]
    _enforce_quota(db, p, s, new_jobs=len(plan))
    _record_consent(db, p, body.consent_version)

    gset = GenerationSet(customer_id=p.customer.id, group_id=group.id, channel=p.customer.channel, mode=body.mode,
                         style=body.style, hide_plate=body.hide_plate, reply_ref=p.reply_ref,
                         total_jobs=len(plan), idempotency_key=scoped_key)
    db.add(gset)
    db.flush()
    jobs = [GenerationJob(customer_id=p.customer.id, channel=p.customer.channel, style=body.style,
                          idempotency_key=f"{scoped_key}:{i}", reply_ref=p.reply_ref, set_id=gset.id,
                          hide_plate=body.hide_plate, **item) for i, item in enumerate(plan)]
    db.add_all(jobs)
    db.commit()

    from app.workers.tasks import process_job
    for job in jobs:
        process_job.delay(str(job.id))  # the sweeper re-enqueues any that fail to enqueue
    return SetAccepted(set_id=gset.id, status=gset.status, total_jobs=len(jobs), status_url=f"/v1/sets/{gset.id}")


@router.get("/v1/sets/{set_id}", response_model=SetOut)
def get_set(set_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db),
            s: Settings = Depends(get_settings)):
    """Set status plus a fresh short-lived link for every finished image."""
    gset = db.get(GenerationSet, set_id)
    if gset is None or gset.customer_id != p.customer.id:
        raise NotFound("Set not found.")
    jobs = db.scalars(select(GenerationJob).where(GenerationJob.set_id == gset.id)
                      .order_by(GenerationJob.idempotency_key)).all()
    storage, items = get_storage(), []
    for j in jobs:
        url = None
        if j.status == "completed":
            gen = db.scalar(select(GeneratedImage).where(GeneratedImage.job_id == j.id,
                                                         GeneratedImage.qa_status == "approved")
                            .order_by(GeneratedImage.attempt.desc()))
            if gen and gen.storage_key:
                url = storage.signed_url(s.storage_outputs_bucket, gen.storage_key)
        items.append(SetItem(job_id=j.id, angle=j.target_angle, status=j.status, message=j.error_message,
                             url=url, ai_visualization=j.mode == "novel_angle"))
    return SetOut(set_id=gset.id, mode=gset.mode, style=gset.style, status=gset.status, hide_plate=gset.hide_plate,
                  total_jobs=gset.total_jobs, items=items, expires_in=s.signed_url_ttl_seconds)


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


@router.get("/v1/me/jobs/latest", response_model=JobOut)
def latest_job(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    job = db.scalar(select(GenerationJob).where(GenerationJob.customer_id == p.customer.id)
                    .order_by(GenerationJob.created_at.desc()))
    if job is None:
        raise NotFound("No jobs yet.")
    return get_job(job.id, p, db)


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
    from app.workers.pipeline import event_payload

    messages = {"failed": "We couldn't produce a faithful studio image from this photo. Please try a clearer one.",
                "needs_more_images": "Please send a clearer photo showing the whole car."}
    db.add(OutboxEvent(event_type=f"job.{target}", payload=event_payload(
        job, target, message=messages.get(target), has_result=target == "completed", reviewed=True)))
    if job.channel in ("whatsapp", "telegram"):
        db.add(DeliveryEvent(job_id=job.id, channel=job.channel))
    db.commit()
    return {"job_id": str(job.id), "status": job.status}

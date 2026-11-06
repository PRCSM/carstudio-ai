"""The per-job pipeline: validate -> analyze -> prepare -> generate (provider chain) -> verify.
Status is committed at every transition so the API always shows real progress."""
import logging
import uuid
from datetime import datetime, timezone

from app.core.config import Settings
from app.db.models import DeliveryEvent, GeneratedImage, GenerationJob, OutboxEvent, Upload, Vehicle
from app.providers.base import EditRequest, PolicyRejected
from app.providers.chain import AllProvidersFailed, ProviderChain
from app.services import qa as qa_mod
from app.services.budget import DbBudget
from app.services.job_state import can_transition, transition
from app.services.prompts import PROMPT_VERSION, same_angle_prompt
from app.services.vision import PROFILE_VERSION, VisionClient

log = logging.getLogger(__name__)

ORDER = ["queued", "validating", "analyzing", "preparing", "generating", "verifying"]


class RetryLater(Exception):
    """Every provider is temporarily unavailable; re-queue the job."""


def _advance(db, job: GenerationJob, target: str, reason: str | None = None) -> None:
    """Idempotent forward move: skip if a crashed run already got this far."""
    if job.status == target or (job.status in ORDER and target in ORDER
                                and ORDER.index(job.status) > ORDER.index(target)):
        return
    transition(db, job, target, reason)
    db.commit()


def _finish(db, job: GenerationJob, status: str, *, error_code: str | None = None,
            message: str | None = None, result_key: str | None = None) -> None:
    if not can_transition(job.status, status):
        # e.g. failing straight from 'generating' to 'review_required' goes via verifying
        if can_transition(job.status, "verifying"):
            transition(db, job, "verifying")
    transition(db, job, status, error_code or message)
    job.error_code, job.error_message = error_code, message
    db.add(OutboxEvent(event_type=f"job.{status}", payload=event_payload(
        job, status, error_code=error_code, message=message,
        has_result=status == "completed" and result_key is not None)))
    if job.channel in ("whatsapp", "telegram"):
        db.add(DeliveryEvent(job_id=job.id, channel=job.channel))
    db.commit()


def event_payload(job: GenerationJob, status: str, *, error_code=None, message=None, has_result=False,
                  **extra) -> dict:
    """Completion event for n8n. reply_ref tells it which chat to answer; no image URLs are included
    (n8n fetches a fresh short-lived link from /v1/jobs/{id}/results)."""
    return {"job_id": str(job.id), "status": status, "channel": job.channel, "style": job.style,
            "customer_id": str(job.customer_id), "reply_ref": job.reply_ref, "error_code": error_code,
            "message": message, "has_result": has_result, **extra}


def allow_training_providers(settings: Settings, consent_version: str | None) -> bool:
    return bool(consent_version) and consent_version in settings.free_tier_consents


TEMPORARY_ERRORS = {"QuotaExhausted", "TransientProviderError", "CreditExhausted"}


def is_temporary_outage(attempts: list[dict]) -> bool:
    """True when some remote provider was down/limited (so waiting may help), as opposed to the
    photo itself being unusable for every layer."""
    return any(a.get("provider") != "local_composite" and
               (a.get("skipped") == "circuit_open" or a.get("error") in TEMPORARY_ERRORS)
               for a in attempts)


def run_job(db, job_id: uuid.UUID, *, settings: Settings, storage, chain: ProviderChain,
            vision: VisionClient | None, final_retry: bool = False) -> str:
    job = db.get(GenerationJob, job_id)
    if job is None:
        log.warning("job %s not found", job_id)
        return "missing"
    if job.status in ("completed", "failed", "cancelled", "review_required", "needs_more_images"):
        return job.status

    from app.db.models import Customer
    customer = db.get(Customer, job.customer_id)
    upload = db.get(Upload, job.source_upload_id)
    source = storage.download(settings.storage_originals_bucket, upload.storage_key)

    # 1. validate
    _advance(db, job, "validating")
    q = upload.quality or {}
    if q.get("is_blurry") or q.get("too_dark"):
        reason = "blurry" if q.get("is_blurry") else "too_dark"
        _finish(db, job, "needs_more_images", error_code=f"source_{reason}",
                message="Please send a sharper, well-lit photo of the whole car.")
        return job.status

    # 2. analyze (advisory)
    _advance(db, job, "analyzing")
    if vision and job.vehicle_id is None:  # skip on re-runs after an outage retry
        profile = vision.profile(source)
        DbBudget(db, settings.monthly_spend_limit_usd).record(
            provider="gemini_vision", kind="vision", cost_usd=settings.price_gemini_vision,
            job_id=job.id, customer_id=job.customer_id)
        if profile:
            vehicle = Vehicle(customer_id=job.customer_id, profile=profile, profile_version=PROFILE_VERSION)
            db.add(vehicle)
            db.flush()
            job.vehicle_id = vehicle.id
            confident = profile.get("confidence") in ("high", "medium")
            if confident and (profile.get("is_car_photo") is False or profile.get("car_clearly_visible") is False):
                _finish(db, job, "needs_more_images", error_code="no_clear_car",
                        message=profile.get("retake_reason") or "We couldn't see a car clearly. "
                                "Please send a photo showing the whole vehicle.")
                return job.status
        db.commit()

    # 3. prepare
    _advance(db, job, "preparing")
    signed = storage.signed_url(settings.storage_originals_bucket, upload.storage_key, ttl=600)
    req = EditRequest(image=source, prompt=same_angle_prompt(job.style), style=job.style,
                      width=upload.width, height=upload.height, image_url=signed)
    allow_training = allow_training_providers(settings, customer.consent_version if customer else None)

    # 4/5. generate + verify, bounded attempts
    best_review: GeneratedImage | None = None
    while job.attempt_count < settings.max_generation_attempts:
        if job.status == "preparing":
            transition(db, job, "generating")
        elif job.status == "verifying":  # coming back for another attempt
            transition(db, job, "generating", "qa_retry")
        db.commit()

        budget = DbBudget(db, settings.monthly_spend_limit_usd)
        try:
            outcome = chain.run(req, budget=budget, allow_training_providers=allow_training)
        except PolicyRejected:
            _finish(db, job, "failed", error_code="policy_rejected",
                    message="This image can't be processed.")
            return job.status
        except AllProvidersFailed as e:
            log.error("all providers failed for job %s: %s", job.id, e.attempts)
            if is_temporary_outage(e.attempts) and not final_retry:
                raise RetryLater() from e  # outage: don't burn an attempt, re-queue
            _finish(db, job, "failed", error_code="providers_unavailable",
                    message="Our studio is busy right now. Please try again shortly.")
            return job.status

        job.attempt_count += 1
        res = outcome.result
        if res.cost_usd:
            budget.record(provider=res.provider, kind="generation", cost_usd=res.cost_usd,
                          job_id=job.id, customer_id=job.customer_id)
        key = f"{job.customer_id}/{job.id}/attempt-{job.attempt_count}.jpg"
        storage.upload(settings.storage_outputs_bucket, key, res.image, "image/jpeg")

        transition(db, job, "verifying")
        pixel_preserving = res.provider == "local_composite"
        result = qa_mod.technical_qa(source, res.image, pixel_preserving=pixel_preserving)
        if vision and not pixel_preserving and result.decision != "retry":
            identity = vision.compare(source, res.image)
            budget.record(provider="gemini_vision", kind="qa", cost_usd=settings.price_gemini_vision,
                          job_id=job.id, customer_id=job.customer_id)
            result = qa_mod.merge_identity(result, identity)

        gen = GeneratedImage(job_id=job.id, attempt=job.attempt_count, storage_key=key, angle="source_view",
                             provider=res.provider, model_id=res.model_id, prompt_version=PROMPT_VERSION,
                             qa_status=result.decision,
                             qa={**result.as_dict(), "chain": outcome.attempts, "provider_meta": res.meta})
        db.add(gen)
        db.commit()

        if result.decision == "approved":
            _finish(db, job, "completed", result_key=key)
            return job.status
        if result.decision == "review_required" and best_review is None:
            best_review = gen
        if result.decision == "rejected":
            break

    if best_review is not None:
        _finish(db, job, "review_required", error_code="needs_review",
                message="Your photo is being checked by our team.", result_key=best_review.storage_key)
    else:
        _finish(db, job, "failed", error_code="qa_failed",
                message="We couldn't produce a faithful studio image from this photo. "
                        "Try a clearer photo of the whole car.")
    return job.status


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

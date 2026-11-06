"""The per-job pipeline: validate -> analyze -> prepare -> generate (provider chain) -> verify.
Status is committed at every transition so the API always shows real progress.

Two job kinds:
- same_angle  (default): restage the customer's real photo in a studio.
- novel_angle (set mode 'angles'): draw a new viewpoint from 1-3 reference photos. Labelled as an AI
  visualization and never auto-approved without an identity check.
Feature switches in Settings keep the v0.1 behaviour when off."""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import Settings
from app.db.models import (DeliveryEvent, GeneratedImage, GenerationJob, GenerationSet, OutboxEvent, Upload,
                           Vehicle)
from app.providers.base import EditRequest, PolicyRejected
from app.providers.chain import AllProvidersFailed, ProviderChain
from app.services import qa as qa_mod
from app.services.budget import DbBudget
from app.services.job_state import TERMINAL_STATUSES, can_transition, transition
from app.services.plate import blur_plates
from app.services.prompts import (PROMPT_VERSION, PROMPT_VERSION_ANGLE, PROMPT_VERSION_CLEAN,
                                  PROMPT_VERSION_RELIGHT, novel_angle_prompt, relight_prompt, same_angle_prompt)
from app.services.vision import PROFILE_VERSION, VisionClient

log = logging.getLogger(__name__)

UNUSABLE_BLUR = 15.0   # Laplacian variance; below this almost no edges survive
UNUSABLE_DARK = 20.0   # mean brightness 0-255

ORDER = ["queued", "validating", "analyzing", "preparing", "generating", "verifying"]
WATERMARK_MESSAGE = ("This looks like a screenshot or a photo with another website's watermark. "
                     "Please send the original photo from your camera for the best result.")


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
    if job.set_id is None:
        db.add(OutboxEvent(event_type=f"job.{status}", payload=event_payload(
            job, status, error_code=error_code, message=message,
            has_result=status == "completed" and result_key is not None)))
        if job.channel in ("whatsapp", "telegram"):
            db.add(DeliveryEvent(job_id=job.id, channel=job.channel))
    db.commit()
    if job.set_id is not None:
        maybe_complete_set(db, job.set_id)


def maybe_complete_set(db, set_id: uuid.UUID) -> str | None:
    """When the last job of a set finishes, mark the set and emit ONE event for the whole set,
    so the customer gets a single album instead of one message per photo."""
    gset = db.execute(select(GenerationSet).where(GenerationSet.id == set_id).with_for_update()).scalar_one_or_none()
    if gset is None or gset.status != "processing":
        db.rollback()
        return None
    statuses = db.scalars(select(GenerationJob.status).where(GenerationJob.set_id == set_id)).all()
    if any(s not in TERMINAL_STATUSES for s in statuses):
        db.rollback()
        return None
    done = sum(s == "completed" for s in statuses)
    review = sum(s == "review_required" for s in statuses)
    retake = sum(s == "needs_more_images" for s in statuses)
    gset.status = "completed" if done == len(statuses) else ("partial" if done or review else "failed")
    # Distinct customer-facing reasons, so the summary says *why* (e.g. a watermark), not a generic failure.
    rows = db.execute(select(GenerationJob.error_code, GenerationJob.error_message)
                      .where(GenerationJob.set_id == set_id,
                             GenerationJob.status.in_(["needs_more_images", "failed"]))).all()
    reasons: dict[str, dict] = {}
    for code, message in rows:
        r = reasons.setdefault(code or "unknown", {"code": code, "message": message, "count": 0})
        r["count"] += 1
    gset.finished_at = datetime.now(timezone.utc)
    db.add(OutboxEvent(event_type=f"set.{gset.status}", payload={
        "set_id": str(gset.id), "status": gset.status, "mode": gset.mode, "style": gset.style,
        "channel": gset.channel, "reply_ref": gset.reply_ref, "customer_id": str(gset.customer_id),
        "total": len(statuses), "completed": done, "review_required": review, "needs_more_images": retake,
        "failed": len(statuses) - done - review - retake, "hide_plate": gset.hide_plate,
        "reasons": list(reasons.values()),
    }))
    db.commit()
    return gset.status


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
    return any(a.get("provider") not in ("local_composite",) and
               (a.get("skipped") == "circuit_open" or a.get("error") in TEMPORARY_ERRORS)
               for a in attempts)


def has_foreign_watermark(profile: dict | None) -> bool:
    return any(isinstance(o, dict) and o.get("type") == "watermark_or_logo"
               for o in (profile or {}).get("overlays") or [])


def _profile_of(db, job: GenerationJob) -> dict | None:
    if job.vehicle_id is None:
        return None
    vehicle = db.get(Vehicle, job.vehicle_id)
    return vehicle.profile if vehicle else None


def watermark_message(profile: dict | None) -> str:
    found = next((o.get("description") for o in (profile or {}).get("overlays") or []
                  if isinstance(o, dict) and o.get("type") == "watermark_or_logo" and o.get("description")), None)
    if not found:
        return WATERMARK_MESSAGE
    found = found.strip().rstrip(".")[:120]
    return (f"I spotted another website's watermark on this photo ({found}). "
            "Please send the original photo from your own camera instead.")


def apply_plate_blur(image: bytes, vision: VisionClient | None, budget: DbBudget, settings: Settings,
                     job: GenerationJob) -> tuple[bytes, dict]:
    if vision is None:
        return image, {"plate_blur": "unavailable"}
    boxes = vision.locate_plates(image)
    budget.record(provider="gemini_vision", kind="plate", cost_usd=settings.price_gemini_vision,
                  job_id=job.id, customer_id=job.customer_id)
    if not boxes:
        return image, {"plate_blur": "no_plate_found" if boxes == [] else "locate_failed"}
    blurred, n = blur_plates(image, boxes)
    return blurred, {"plate_blur": "applied" if n else "no_valid_box", "plates_blurred": n}


def run_job(db, job_id: uuid.UUID, *, settings: Settings, storage, chain: ProviderChain,
            vision: VisionClient | None, final_retry: bool = False) -> str:
    job = db.get(GenerationJob, job_id)
    if job is None:
        log.warning("job %s not found", job_id)
        return "missing"
    if job.status in TERMINAL_STATUSES:
        return job.status

    from app.db.models import Customer
    customer = db.get(Customer, job.customer_id)
    novel = job.mode == "novel_angle"
    cleanup = settings.feature_overlay_cleanup
    upload = db.get(Upload, job.source_upload_id)
    source = storage.download(settings.storage_originals_bucket, upload.storage_key)
    references: list[bytes] = [source]
    if novel:
        for ref_id in (job.reference_upload_ids or [])[:settings.max_angle_references]:
            if ref_id != upload.id:
                ref = db.get(Upload, ref_id)
                if ref is not None:
                    references.append(storage.download(settings.storage_originals_bucket, ref.storage_key))

    # 1. validate
    _advance(db, job, "validating")
    q = upload.quality or {}
    unusable = (q.get("blur_variance", 999) < UNUSABLE_BLUR) or (q.get("mean_brightness", 128) < UNUSABLE_DARK)
    if unusable:
        reason = "blurry" if q.get("blur_variance", 999) < UNUSABLE_BLUR else "too_dark"
        _finish(db, job, "needs_more_images", error_code=f"source_{reason}",
                message="This photo is too blurry or dark to work with. Please send a sharper, well-lit photo.")
        return job.status
    # Mildly blurry / dark photos are processed anyway; the customer was already warned at upload.

    # 2. analyze (advisory)
    _advance(db, job, "analyzing")
    if job.set_id is not None and job.vehicle_id is None:
        sibling = db.scalar(select(GenerationJob).where(
            GenerationJob.set_id == job.set_id, GenerationJob.source_upload_id == job.source_upload_id,
            GenerationJob.vehicle_id.is_not(None), GenerationJob.id != job.id))
        if sibling is not None:
            if sibling.error_code == "no_clear_car" or (sibling.error_code == "watermarked_source"
                                                       and settings.watermark_policy != "remove"):
                _finish(db, job, "needs_more_images", error_code=sibling.error_code, message=sibling.error_message)
                return job.status
            job.vehicle_id = sibling.vehicle_id  # same photo, same answer: don't pay to analyse it again
            db.commit()
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
            if cleanup and has_foreign_watermark(profile) and settings.watermark_policy != "remove":
                # Default: don't strip other sites' watermarks; ask for the customer's own original.
                _finish(db, job, "needs_more_images", error_code="watermarked_source",
                        message=watermark_message(profile))
                return job.status
        db.commit()

    # 3. prepare
    _advance(db, job, "preparing")
    signed = storage.signed_url(settings.storage_originals_bucket, upload.storage_key, ttl=600)
    if novel:
        prompt, prompt_version = novel_angle_prompt(job.style, job.target_angle), PROMPT_VERSION_ANGLE
    elif cleanup:
        prompt, prompt_version = same_angle_prompt(job.style, cleanup=True), PROMPT_VERSION_CLEAN
    else:
        prompt, prompt_version = same_angle_prompt(job.style), PROMPT_VERSION
    model_input, input_url = source, signed
    if (not novel and cleanup and settings.watermark_policy == "remove"
            and has_foreign_watermark(_profile_of(db, job))):
        # The image model refuses requests to remove watermarks, and we don't disguise such requests.
        # Instead our own cut-out puts the real car on a studio background (a watermark over the old
        # background goes with it), and the model is only asked to polish the lighting of that image.
        staged = _stage_on_studio(source, job.style)
        if staged is not None:
            model_input, input_url = staged, None
            prompt, prompt_version = relight_prompt(job.style), PROMPT_VERSION_RELIGHT
    req = EditRequest(image=model_input, prompt=prompt, style=job.style, width=upload.width, height=upload.height,
                      image_url=input_url, extra_images=references[1:], novel_view=novel)
    allow_training = allow_training_providers(settings, customer.consent_version if customer else None)
    blur_plate = job.hide_plate and settings.feature_plate_blur and not novel  # novel angles render plates blank

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
            if settings.composite_fallback and not novel and _try_composite_fallback(
                    db, job, source, settings, storage, vision, blur_plate):
                return job.status
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

        transition(db, job, "verifying")
        pixel_preserving = res.provider == "local_composite"
        result = qa_mod.technical_qa(source, res.image, pixel_preserving=pixel_preserving, novel_view=novel)
        if novel:
            identity = None
            if vision and result.decision != "retry":
                identity = vision.compare_angle(references, res.image, job.target_angle or "new view")
                budget.record(provider="gemini_vision", kind="qa", cost_usd=settings.price_gemini_vision,
                              job_id=job.id, customer_id=job.customer_id)
            result = qa_mod.merge_angle_identity(result, identity)
        elif vision and not pixel_preserving and result.decision != "retry":
            identity = vision.compare(source, res.image)
            budget.record(provider="gemini_vision", kind="qa", cost_usd=settings.price_gemini_vision,
                          job_id=job.id, customer_id=job.customer_id)
            result = qa_mod.merge_identity(result, identity, require_clean=cleanup)

        final_image, extra_meta = res.image, {}
        if blur_plate and result.decision in ("approved", "review_required"):
            # QA ran on the unblurred image, so a blurred plate never counts as an identity change.
            final_image, extra_meta = apply_plate_blur(res.image, vision, budget, settings, job)
        key = f"{job.customer_id}/{job.id}/attempt-{job.attempt_count}.jpg"
        storage.upload(settings.storage_outputs_bucket, key, final_image, "image/jpeg")

        gen = GeneratedImage(job_id=job.id, attempt=job.attempt_count, storage_key=key,
                             angle=job.target_angle or "source_view", provider=res.provider, model_id=res.model_id,
                             prompt_version=prompt_version, qa_status=result.decision,
                             qa={**result.as_dict(), "chain": outcome.attempts, "provider_meta": res.meta,
                                 "ai_visualization": novel, **extra_meta})
        db.add(gen)
        db.commit()

        if result.decision == "approved":
            _finish(db, job, "completed", result_key=key)
            return job.status
        if result.decision == "review_required" and best_review is None:
            best_review = gen
        if result.decision == "rejected":
            break

    if settings.composite_fallback and not novel:
        done = _try_composite_fallback(db, job, source, settings, storage, vision, blur_plate)
        if done:
            return job.status

    if best_review is not None:
        _finish(db, job, "review_required", error_code="needs_review",
                message="Your photo is being checked by our team.", result_key=best_review.storage_key)
    else:
        _finish(db, job, "failed", error_code="qa_failed",
                message="We couldn't produce a faithful studio image from this photo. "
                        "Try a clearer photo of the whole car.")
    return job.status


def _stage_on_studio(source: bytes, style: str) -> bytes | None:
    from app.providers.base import ProviderError
    from app.providers.local_composite import composite

    try:
        image, _ = composite(source, style)
        return image
    except ProviderError as e:
        log.warning("could not stage watermarked photo on studio background: %s", e)
        return None


def _try_composite_fallback(db, job: GenerationJob, source: bytes, settings: Settings, storage,
                            vision: VisionClient | None, blur_plate: bool) -> bool:
    """Last resort so the customer still gets a faithful result: cut the real car out and place it in the
    studio. Car pixels are never regenerated, so identity is preserved by construction."""
    from app.providers.base import ProviderError
    from app.providers.local_composite import LocalComposite

    try:
        res = LocalComposite().edit(EditRequest(image=source, prompt="", style=job.style, width=0, height=0))
    except ProviderError as e:
        log.warning("composite fallback failed for job %s: %s", job.id, e)
        return False
    result = qa_mod.technical_qa(source, res.image, pixel_preserving=True)
    if result.decision != "approved":
        return False
    final_image, extra_meta = res.image, {}
    if blur_plate:
        final_image, extra_meta = apply_plate_blur(res.image, vision, DbBudget(db, settings.monthly_spend_limit_usd),
                                                   settings, job)
    job.attempt_count += 1
    key = f"{job.customer_id}/{job.id}/fallback.jpg"
    storage.upload(settings.storage_outputs_bucket, key, final_image, "image/jpeg")
    db.add(GeneratedImage(job_id=job.id, attempt=job.attempt_count, storage_key=key, angle="source_view",
                          provider=res.provider, model_id=res.model_id, prompt_version="composite_fallback",
                          qa_status="approved",
                          qa={**result.as_dict(), "fallback": True, "provider_meta": res.meta, **extra_meta}))
    db.commit()
    _finish(db, job, "completed", result_key=key)
    return True


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

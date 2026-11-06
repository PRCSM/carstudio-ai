"""Job state machine. Every transition is validated and recorded in job_events."""
from datetime import datetime, timezone

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "review_required", "needs_more_images"}

ALLOWED = {
    "queued": {"validating", "cancelled", "failed"},
    "validating": {"analyzing", "failed", "needs_more_images", "cancelled"},
    "analyzing": {"preparing", "failed", "needs_more_images", "cancelled"},
    "preparing": {"generating", "failed", "cancelled"},
    "generating": {"verifying", "failed", "cancelled", "generating"},
    "verifying": {"completed", "generating", "review_required", "failed", "needs_more_images"},
    "review_required": {"completed", "failed", "needs_more_images"},
}


class InvalidTransition(RuntimeError):
    pass


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, set())


def transition(db, job, target: str, reason: str | None = None) -> None:
    from app.db.models import JobEvent

    if not can_transition(job.status, target):
        raise InvalidTransition(f"{job.status} -> {target}")
    db.add(JobEvent(job_id=job.id, from_status=job.status, to_status=target, reason=reason))
    now = datetime.now(timezone.utc)
    if job.status == "queued" and job.started_at is None:
        job.started_at = now
    job.status = target
    if target in TERMINAL_STATUSES:
        job.finished_at = now

"""Spend tracking and the 'stop paying before the card does' guard."""
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db.models import UsageEvent


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend_usd(db) -> float:
    total = db.scalar(select(func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0))
                      .where(UsageEvent.created_at >= month_start()))
    return float(total or 0)


class DbBudget:
    """Budget view used by the provider chain."""

    def __init__(self, db, limit_usd: float):
        self.db = db
        self.limit = limit_usd

    def can_spend(self, amount_usd: float) -> bool:
        if amount_usd <= 0:
            return True
        return month_spend_usd(self.db) + amount_usd <= self.limit

    def record(self, *, provider: str, kind: str, cost_usd: float, job_id=None, customer_id=None) -> None:
        self.db.add(UsageEvent(provider=provider, kind=kind, estimated_cost_usd=cost_usd,
                               job_id=job_id, customer_id=customer_id))
        self.db.flush()

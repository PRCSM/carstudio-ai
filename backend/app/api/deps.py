"""Who is calling: n8n (service token + customer ref) or a web visitor (client session + IP)."""
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import Unauthorized, ValidationFailed
from app.core.security import constant_time_equals, hash_external_ref
from app.db.models import Customer
from app.db.session import get_db


SERVICE_CHANNELS = {"whatsapp", "telegram"}


@dataclass
class Principal:
    kind: str            # "service" | "web"
    customer: Customer
    client_ip: str | None
    reply_ref: str | None = None  # where results are delivered (Telegram chat id)


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


def _get_or_create_customer(db: Session, channel: str, ref_hash: str) -> Customer:
    c = db.scalar(select(Customer).where(Customer.channel == channel, Customer.external_ref_hash == ref_hash))
    if c:
        return c
    c = Customer(channel=channel, external_ref_hash=ref_hash)
    db.add(c)
    try:
        db.commit()
    except IntegrityError:  # concurrent first request
        db.rollback()
        c = db.scalar(select(Customer).where(Customer.channel == channel,
                                             Customer.external_ref_hash == ref_hash))
    return c


def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_customer_ref: str | None = Header(default=None),
    x_client_session: str | None = Header(default=None),
    x_channel: str = Header(default="whatsapp"),
    db: Session = Depends(get_db),
    s: Settings = Depends(get_settings),
) -> Principal:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if token:
        if not constant_time_equals(token, s.n8n_internal_api_token):
            raise Unauthorized("Invalid service token.")
        if not x_customer_ref:
            raise ValidationFailed("X-Customer-Ref header is required.", code="missing_customer")
        channel = x_channel.strip().lower()
        if channel not in SERVICE_CHANNELS:
            raise ValidationFailed("X-Channel must be whatsapp or telegram.", code="invalid_channel")
        customer = _get_or_create_customer(db, channel, hash_external_ref(x_customer_ref, s.customer_hash_salt))
        # Telegram chat ids are needed to deliver results; phone numbers are never stored.
        reply_ref = x_customer_ref.strip() if channel == "telegram" else None
        return Principal("service", customer, None, reply_ref)

    if not x_client_session or not 16 <= len(x_client_session) <= 64:
        raise Unauthorized("Missing client session.")
    customer = _get_or_create_customer(db, "web", hash_external_ref(x_client_session, s.customer_hash_salt))
    return Principal("web", customer, _client_ip(request))


def require_admin(authorization: str | None = Header(default=None), s: Settings = Depends(get_settings)) -> None:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not constant_time_equals(token, s.admin_api_token):
        raise Unauthorized("Admin token required.")


def utc_day_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

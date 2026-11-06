import hashlib
import hmac
import secrets
import time


def hash_external_ref(value: str, salt: str) -> str:
    """Hash phone numbers / session ids so raw identifiers never hit the database."""
    return hashlib.sha256(f"{salt}:{value.strip()}".encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def sign_payload(body: bytes, secret: str, timestamp: int | None = None) -> tuple[str, str]:
    """Return (timestamp, signature) for an outbound webhook. Signature covers 'ts.body'."""
    ts = str(timestamp if timestamp is not None else int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return ts, f"sha256={sig}"


def verify_signature(body: bytes, secret: str, timestamp: str, signature: str,
                     tolerance_seconds: int = 300, now: int | None = None) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else int(time.time())
    if abs(current - ts) > tolerance_seconds:
        return False  # replay protection
    _, expected = sign_payload(body, secret, ts)
    return hmac.compare_digest(expected, signature or "")


def verify_meta_signature(body: bytes, app_secret: str, header_value: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 header."""
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value or "")


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)

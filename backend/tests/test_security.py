import hashlib
import hmac

from app.core.logging import redact
from app.core.security import (constant_time_equals, hash_external_ref, sign_payload, verify_meta_signature,
                               verify_signature)


def test_hash_is_stable_salted_and_not_raw():
    a = hash_external_ref("+919876543210", "salt1")
    assert a == hash_external_ref(" +919876543210 ", "salt1")
    assert a != hash_external_ref("+919876543210", "salt2")
    assert "9876543210" not in a


def test_signature_roundtrip_and_tamper():
    body = b'{"job_id":"x"}'
    ts, sig = sign_payload(body, "secret", timestamp=1000)
    assert verify_signature(body, "secret", ts, sig, now=1000)
    assert not verify_signature(body + b" ", "secret", ts, sig, now=1000)
    assert not verify_signature(body, "other", ts, sig, now=1000)


def test_signature_replay_window():
    body = b"{}"
    ts, sig = sign_payload(body, "secret", timestamp=1000)
    assert not verify_signature(body, "secret", ts, sig, now=1000 + 301)
    assert not verify_signature(body, "secret", "not-a-number", sig, now=1000)


def test_meta_signature():
    body = b'{"entry":[]}'
    header = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(body, "appsecret", header)
    assert not verify_meta_signature(body, "appsecret", "sha256=deadbeef")


def test_constant_time_equals_rejects_empty():
    assert not constant_time_equals("", "")
    assert constant_time_equals("abc", "abc")


def test_redaction():
    out = redact("customer +91 98765 43210 url https://x/y?token=abc123&a=1")
    assert "98765" not in out and "abc123" not in out

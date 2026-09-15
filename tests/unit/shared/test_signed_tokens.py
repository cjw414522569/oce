"""signed_tokens：签名往返、防篡改、过期、错误密钥。"""

from __future__ import annotations

from freezegun import freeze_time

from oce.shared.signed_tokens import new_state, sign_payload, verify_payload


def test_roundtrip_and_wrong_secret():
    token = sign_payload("u:42", 60, "s3cret")
    assert verify_payload(token, "s3cret") == "u:42"
    assert verify_payload(token, "other") is None


def test_tampered_payload_rejected():
    token = sign_payload("u:42", 60, "s3cret")
    payload, exp, sig = token.split(".")
    assert verify_payload(f"u:43.{exp}.{sig}", "s3cret") is None
    assert verify_payload(f"{token}x", "s3cret") is None


@freeze_time("2026-09-15 00:00:00")
def test_expired_token_rejected():
    token = sign_payload("u:42", 60, "s3cret")
    assert verify_payload(token, "s3cret") == "u:42"
    with freeze_time("2026-09-15 00:01:01"):
        assert verify_payload(token, "s3cret") is None


def test_malformed_tokens_rejected():
    for bad in ("", "u:42", "u.42.sig", "a.b.c.d", "u:42.notanumber.sig"):
        assert verify_payload(bad, "s3cret") is None


def test_new_state_unique():
    assert len({new_state() for _ in range(50)}) == 50

"""HMAC signature verification — happy path and every refusal."""

from __future__ import annotations

import pytest

from ring_assistant.verify import SignatureError, sign_payload, verify_signature

BODY = b'{"id": "evt-0001"}'


def test_sign_payload_format():
    value = sign_payload(BODY, "key")
    scheme, _, digest = value.partition("=")
    assert scheme == "sha256"
    assert len(digest) == 64
    int(digest, 16)  # hex


def test_valid_signature_passes():
    verify_signature(BODY, sign_payload(BODY, "key"), "key")


def test_missing_header_rejected():
    with pytest.raises(SignatureError, match="missing signature header"):
        verify_signature(BODY, None, "key")


def test_empty_header_rejected():
    with pytest.raises(SignatureError, match="missing signature header"):
        verify_signature(BODY, "   ", "key")


def test_wrong_scheme_rejected():
    with pytest.raises(SignatureError, match="unsupported signature format"):
        verify_signature(BODY, "hmac-sha256=" + "a" * 64, "key")


def test_bare_digest_rejected():
    with pytest.raises(SignatureError, match="unsupported signature format"):
        verify_signature(BODY, "a" * 64, "key")


def test_wrong_key_rejected():
    with pytest.raises(SignatureError, match="signature mismatch"):
        verify_signature(BODY, sign_payload(BODY, "right-key"), "other-key")


def test_tampered_body_rejected():
    with pytest.raises(SignatureError, match="signature mismatch"):
        verify_signature(BODY + b" ", sign_payload(BODY, "key"), "key")


def test_empty_secret_refuses_to_verify():
    with pytest.raises(SignatureError, match="empty webhook secret"):
        verify_signature(BODY, sign_payload(BODY, "key"), "")


def test_empty_secret_refuses_to_sign():
    with pytest.raises(SignatureError, match="empty webhook secret"):
        sign_payload(BODY, "")

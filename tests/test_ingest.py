"""Webhook ingestion: signature gate + JSON gate + normalization, end to end."""

from __future__ import annotations

import json

import pytest

from ring_assistant.ingest import WebhookError, receive_webhook
from ring_assistant.schema import RingEvent, SchemaError
from ring_assistant.verify import SignatureError, sign_payload


def test_happy_path(make_payload, body_of, signed_headers, secret):
    body = body_of(make_payload())
    event = receive_webhook(body, signed_headers(body), secret)
    assert isinstance(event, RingEvent)
    assert event.event_id == "evt-0001"


def test_unsigned_request_rejected(make_payload, body_of, secret):
    with pytest.raises(SignatureError):
        receive_webhook(body_of(make_payload()), {}, secret)


def test_wrong_secret_rejected(make_payload, body_of, signed_headers):
    body = body_of(make_payload())
    with pytest.raises(SignatureError):
        receive_webhook(body, signed_headers(body, "other-secret"), "secret")


def test_header_lookup_case_insensitive(make_payload, body_of, secret):
    body = body_of(make_payload())
    headers = {"x-ring-signature": sign_payload(body, secret)}
    receive_webhook(body, headers, secret)


def test_custom_header_name(make_payload, body_of, secret):
    body = body_of(make_payload())
    headers = {"x-webhook-hmac": sign_payload(body, secret)}
    event = receive_webhook(
        body, headers, secret, signature_header="x-webhook-hmac"
    )
    assert event.event_id == "evt-0001"


def test_signed_but_invalid_json_rejected(secret):
    body = b"this is not json"
    headers = {"X-Ring-Signature": sign_payload(body, secret)}
    with pytest.raises(WebhookError, match="not valid UTF-8 JSON"):
        receive_webhook(body, headers, secret)


def test_signed_json_array_rejected(make_payload, body_of, signed_headers, secret):
    body = json.dumps([make_payload()]).encode("utf-8")
    with pytest.raises(WebhookError, match="JSON object"):
        receive_webhook(body, signed_headers(body), secret)


def test_signed_but_schema_violating_body_rejected(
    make_payload, body_of, signed_headers, secret
):
    body = body_of(make_payload(type="doorbell_exploded"))
    with pytest.raises(SchemaError):
        receive_webhook(body, signed_headers(body), secret)


def test_error_layering_is_distinct():
    """401-material vs 400-material failures never masquerade as each other."""
    assert issubclass(WebhookError, Exception)
    assert issubclass(SchemaError, ValueError)
    assert not issubclass(WebhookError, SignatureError)
    assert not issubclass(SchemaError, SignatureError)

"""Webhook-shaped entry point: verify -> parse -> normalize.

This is deliberately a PLAIN FUNCTION, not a framework handler:

- the entire core stays stdlib-only and offline-testable (no server,
  no sockets, no event loop needed to exercise the real code path);
- any HTTP framework can host it in ~15 lines — ``server.py`` ships a
  FastAPI adapter behind the optional ``[server]`` extra;
- the demo CLI replays synthetic payloads through this exact function,
  so what tests cover is what production runs.

Deployment note (from the Ring webhook documentation): a real receiver
must acknowledge with HTTP 200 within ~5 seconds. The synchronous
ack-after-verify flow here is fast; heavier work (snapshot fetch,
LLM classification) belongs downstream of the ack — see the honest
caveats in VALIDATION.md.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from .schema import RingEvent, SchemaError, parse_event_payload
from .settings import DEFAULT_SIGNATURE_HEADER
from .verify import SignatureError, verify_signature


class WebhookError(Exception):
    """The webhook body is not valid UTF-8 JSON (signature was fine)."""


def receive_webhook(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    signature_header: str = DEFAULT_SIGNATURE_HEADER,
    received_at: datetime | None = None,
) -> RingEvent:
    """Verify the HMAC signature, then normalize the payload to a RingEvent.

    Header lookup is case-insensitive (HTTP headers are). Raises
    SignatureError for auth failures, WebhookError for malformed JSON,
    SchemaError for a well-signed body that violates the event
    contract — three distinct types so an HTTP adapter can map 401 vs
    400 precisely.
    """
    lowered = {str(name).lower(): value for name, value in headers.items()}
    header_value = lowered.get(signature_header.lower())
    verify_signature(body, header_value, secret)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError(f"webhook body is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookError("webhook body must be a JSON object")

    return parse_event_payload(payload, received_at=received_at)

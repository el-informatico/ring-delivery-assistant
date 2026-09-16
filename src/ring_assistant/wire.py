"""Ring webhook v1.1 wire adapter — the live edge of the pipeline.

Ring's production webhooks (Partner API documentation:
https://developer.amazon.com/docs/ring/api-documentation.html#webhook-v11-payload-structure)
are JSON:API envelopes, not the flat internal contract in ``schema.py``:

    {
      "meta": {"version": "1.1", "time": "...Z", "request_id": "...",
               "account_id": "ava1.ring.account.XXXYYY"},
      "data": {"id": "<device_id>_<event_type>_<timestamp>",
               "type": "motion_detected" | "button_press" | ...,
               "attributes": {"source": "<device_id>",
                              "source_type": "devices",
                              "timestamp": 1770989995231,  # epoch ms, UTC
                              "sub_type": "human" | "motion" | ...,
                              "component_ids": ["0"]},
               "relationships": {"devices": {"links": {...}}}}
    }

Live-contract facts this adapter encodes (all from the docs above):

* Signature: ``X-Signature: sha256=<hex HMAC-SHA256 of the RAW body>`` —
  the same scheme ``verify.py`` already implements; only the header NAME
  differs from the internal default (``x-ring-signature``), so it stays
  configurable instead of being hardcoded twice.
* ``data.attributes.timestamp`` (epoch milliseconds, UTC) is when the
  event HAPPENED — the docs say to use it "for any time calculation";
  ``meta.time`` is when Ring sent the webhook (kept verbatim, never
  parsed — it can carry nanosecond precision).
* Idempotency: retries redeliver the same ``meta.request_id`` /
  ``data.id``; the state layer already dedupes on event id.
* Event types beyond motion/button (device lifecycle, app-integration,
  subscription) are documented but not intent-bearing for this
  pipeline: ``adapt_v1_1`` raises ``UnsupportedEvent`` for them so a
  host can ack HTTP 200 and ignore (a 4xx would make Ring treat the
  delivery as a PERMANENT failure, which misrepresents "handled on
  purpose").

Adaptation mapping (documented values only, nothing invented):

    type:     button_press -> ding ; motion_detected -> motion_detected
    sub_type: human -> human ; vehicle -> vehicle
              motion / other_motion / absent / unknown -> None
              (unclassified motion is noise; the burst-suppression
              routing rule owns it)
    time:     epoch ms -> timezone-aware UTC datetime
    device:   attributes.source is the device id; v1.1 carries no
              display name, so the schema's id-fallback applies

The full original envelope rides along under the ``wire`` key of the
internal payload, so every field (``request_id``, ``component_ids``,
future additions) survives in ``RingEvent.raw`` — carried, never
interpreted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .ingest import WebhookError
from .schema import EventKind, RingEvent, SchemaError, SubType, parse_event_payload
from .verify import sign_payload, verify_signature

LIVE_SIGNATURE_HEADER = "x-signature"

_TYPE_MAP: dict[str, EventKind] = {
    "button_press": EventKind.DING,
    "motion_detected": EventKind.MOTION,
}
_SUB_TYPE_MAP: dict[str, SubType] = {
    "human": SubType.HUMAN,
    "vehicle": SubType.VEHICLE,
}


class UnsupportedEvent(ValueError):
    """A v1.1 event type this pipeline deliberately does not consume.

    Distinct from :class:`SchemaError` on purpose: a malformed payload is
    a 400 (permanent failure), an ignored-but-valid event type is a 200.
    """

    def __init__(self, event_type: str):
        super().__init__(f"event type {event_type!r} is not consumed by this pipeline")
        self.event_type = event_type


def _epoch_ms_to_utc(value: Any) -> datetime:
    """Documented timestamp field: epoch milliseconds, UTC."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError("'data.attributes.timestamp' must be epoch milliseconds (number)")
    seconds, millis = divmod(int(value), 1000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=millis * 1000)


def adapt_v1_1(payload: Any) -> dict:
    """Convert a Ring webhook v1.1 envelope to the internal payload.

    Raises ``UnsupportedEvent`` for documented-but-ignored event types
    and ``SchemaError`` for structural violations; field-level checks
    (id, device) stay with ``parse_event_payload`` — one validation
    owner, no duplication.
    """
    if not isinstance(payload, Mapping):
        raise SchemaError("v1.1 webhook payload must be a JSON object")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise SchemaError("v1.1 payload is missing the 'data' envelope")
    event_type = data.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise SchemaError("v1.1 'data.type' is required")
    kind = _TYPE_MAP.get(event_type)
    if kind is None:
        raise UnsupportedEvent(event_type)

    attributes = data.get("attributes")
    if not isinstance(attributes, Mapping):
        raise SchemaError("v1.1 payload is missing 'data.attributes'")

    raw_sub_type = attributes.get("sub_type")
    sub_type = _SUB_TYPE_MAP.get(raw_sub_type) if isinstance(raw_sub_type, str) else None

    return {
        "id": data.get("id"),
        "type": kind.value,
        "sub_type": sub_type.value if sub_type is not None else None,
        "device_id": attributes.get("source"),
        "occurred_at": _epoch_ms_to_utc(attributes.get("timestamp")).isoformat(),
        "wire": dict(payload),
    }


def receive_v1_1(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    signature_header: str = LIVE_SIGNATURE_HEADER,
    received_at: datetime | None = None,
) -> RingEvent:
    """Verify a live Ring webhook (raw bytes) and adapt it to a RingEvent.

    Same order as production: authenticate first (signature over the
    raw bytes, before any parsing), then adapt, then validate — so even
    an ignored event type must carry a valid signature.
    """
    lowered = {str(name).lower(): value for name, value in headers.items()}
    verify_signature(body, lowered.get(signature_header.lower()), secret)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError(f"webhook body is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookError("webhook body must be a JSON object")
    return parse_event_payload(adapt_v1_1(payload), received_at=received_at)


def _ms_to_iso_z(timestamp_ms: int) -> str:
    seconds, millis = divmod(int(timestamp_ms), 1000)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=millis * 1000)
    return moment.isoformat().replace("+00:00", "Z")


def build_v1_1(
    *,
    event_type: str,
    device_id: str,
    timestamp_ms: int,
    request_id: str,
    sub_type: str | None = None,
    component_ids: list[str] | None = None,
    account_id: str = "ava1.ring.account.XXXYYY",
    sent_at: str | None = None,
    event_id: str | None = None,
) -> dict:
    """Build a documented-shape v1.1 envelope (fixtures, live-window probes).

    Ids default to the generic documented pattern
    ``<device_id>_<event_type>_<timestamp>`` (the motion example in the
    docs shows the shorthand ``..._motion_...``; both appear there).
    """
    occurred = _ms_to_iso_z(timestamp_ms)
    attributes: dict[str, Any] = {
        "source": device_id,
        "source_type": "devices",
        "timestamp": timestamp_ms,
        "timestamp_readable": occurred[:19].replace("T", " "),
    }
    if sub_type is not None:
        attributes["sub_type"] = sub_type
    if component_ids is not None:
        attributes["component_ids"] = list(component_ids)
    return {
        "meta": {
            "version": "1.1",
            "time": sent_at or occurred,
            "request_id": request_id,
            "account_id": account_id,
        },
        "data": {
            "id": event_id or f"{device_id}_{event_type}_{timestamp_ms}",
            "type": event_type,
            "attributes": attributes,
            "relationships": {"devices": {"links": {"self": f"/v1/devices/{device_id}"}}},
        },
    }


def encode_v1_1(payload: Mapping) -> bytes:
    """Canonical bytes for a v1_1 payload (compact JSON, stable key order)."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def signed_v1_1(
    payload: Mapping, secret: str
) -> tuple[bytes, dict[str, str]]:
    """(body, headers) exactly as Ring would send them: canonical casing."""
    body = encode_v1_1(payload)
    return body, {"X-Signature": sign_payload(body, secret)}

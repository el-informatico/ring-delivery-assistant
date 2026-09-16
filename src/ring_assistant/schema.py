"""Typed event and intent schema.

The webhook payload contract (synthetic, Ring-shaped):

.. code-block:: json

    {
      "id": "evt-001",
      "type": "ding" | "motion_detected",
      "sub_type": "human" | "vehicle" | "package_delivery"
                | "package_pickup" | null,
      "device_id": "front-door",
      "device_name": "Front Door",
      "occurred_at": "2026-09-14T09:14:02+00:00",
      "snapshot_path": "snapshots/package-mat.png",
      "received_at": "2026-09-14T09:14:06+00:00"
    }

Semantics and honest boundaries:

- ``type`` mirrors the documented Ring webhook event pair
  (``ding`` = button press / ring, ``motion_detected`` = motion) and
  ``sub_type`` mirrors the per-event classification the platform
  already performs (e.g. ``human``). ``package_delivery`` /
  ``package_pickup`` represent a device whose native package alerts are
  forwarded into the stream. Detection is an INPUT to this layer; the
  intent vocabulary above it is what this project adds.
- ``occurred_at`` is when it happened at the door; ``received_at`` is
  when the pipeline saw it. They differ under network delay, and the
  state machine relies on that distinction to survive out-of-order
  delivery. Production webhooks do not carry ``received_at`` (the
  ingester stamps it); synthetic replay payloads MAY pin it so demos
  and tests are deterministic.
- ``snapshot_path`` is an opaque reference to the event snapshot
  (a media URL in production, a local fixture path offline). It may be
  null — the pipeline degrades to event-only classification.
- Unknown extra fields are tolerated and preserved in ``RingEvent.raw``
  (forward compatibility), never interpreted.

All datetimes are normalized to timezone-aware UTC. Naive timestamps
are ASSUMED UTC (documented assumption, not detected truth).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    """What kind of trigger fired at the door."""

    DING = "ding"
    MOTION = "motion_detected"


class SubType(str, Enum):
    """Platform-provided classification carried alongside the trigger."""

    HUMAN = "human"
    VEHICLE = "vehicle"
    PACKAGE_DELIVERY = "package_delivery"
    PACKAGE_PICKUP = "package_pickup"


class Intent(str, Enum):
    """What the event MEANS — the vocabulary this layer adds.

    The platform API exposes triggers and object classes, not intent:
    "a person" is a class, "the deposited package was picked up" is an
    intent derived across events.
    """

    PACKAGE_DEPOSITED = "package_deposited"
    PACKAGE_PICKED_UP = "package_picked_up"
    PERSON_AT_DOOR = "person_at_door"
    VEHICLE_AT_DOOR = "vehicle_at_door"
    MOTION_NOISE = "motion_noise"


class SchemaError(ValueError):
    """A webhook payload does not match the event contract."""


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"'{field_name}' must be an ISO-8601 timestamp string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SchemaError(f"'{field_name}' is not a valid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class RingEvent:
    """A normalized, verified doorbell event."""

    event_id: str
    kind: EventKind
    sub_type: SubType | None
    device_id: str
    device_name: str
    occurred_at: datetime
    received_at: datetime
    snapshot_path: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Compact ``kind/sub_type`` label for timelines and logs."""
        if self.sub_type is None:
            return self.kind.value
        return f"{self.kind.value}/{self.sub_type.value}"


@dataclass(frozen=True)
class IntentResult:
    """The outcome of classifying one event into an intent."""

    intent: Intent
    confidence: float  # 0.0..1.0 — stub confidences are heuristic, not calibrated
    rationale: str  # human-readable WHY (feeds the timeline)
    source: str  # provenance tag, e.g. "rules" or "llm:<model>"
    detail: str = ""  # optional free-text scene description


def parse_event_payload(
    payload: Any,
    *,
    received_at: datetime | None = None,
) -> RingEvent:
    """Validate and normalize a webhook payload mapping into a RingEvent.

    ``received_at`` stamps the event when the payload does not carry
    one; when both are given the payload's value wins (synthetic
    replays pin it for determinism).
    """
    if not isinstance(payload, Mapping):
        raise SchemaError("event payload must be a JSON object")

    event_id = payload.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise SchemaError("'id' is required and must be a non-empty string")

    kind_raw = payload.get("type")
    try:
        kind = EventKind(kind_raw)
    except ValueError:
        expected = ", ".join(k.value for k in EventKind)
        raise SchemaError(f"unknown 'type' {kind_raw!r} (expected one of: {expected})") from None

    sub_raw = payload.get("sub_type")
    if sub_raw in (None, ""):
        sub_type: SubType | None = None
    else:
        try:
            sub_type = SubType(sub_raw)
        except ValueError:
            expected = ", ".join(s.value for s in SubType)
            raise SchemaError(
                f"unknown 'sub_type' {sub_raw!r} (expected null or one of: {expected})"
            ) from None

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        raise SchemaError("'device_id' is required and must be a non-empty string")

    device_name = payload.get("device_name")
    if device_name in (None, ""):
        device_name = device_id
    if not isinstance(device_name, str):
        raise SchemaError("'device_name' must be a string when present")

    occurred_at = _parse_timestamp(payload.get("occurred_at"), "occurred_at")

    received_raw = payload.get("received_at")
    if received_raw in (None, ""):
        stamped = received_at or datetime.now(timezone.utc)
    else:
        stamped = _parse_timestamp(received_raw, "received_at")

    snapshot_path = payload.get("snapshot_path")
    if snapshot_path == "":
        snapshot_path = None
    if snapshot_path is not None and not isinstance(snapshot_path, str):
        raise SchemaError("'snapshot_path' must be a string reference or null")

    return RingEvent(
        event_id=event_id,
        kind=kind,
        sub_type=sub_type,
        device_id=device_id,
        device_name=device_name,
        occurred_at=occurred_at,
        received_at=stamped,
        snapshot_path=snapshot_path,
        raw=dict(payload),
    )

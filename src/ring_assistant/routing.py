"""Intent-conditioned notification routing.

Classification says WHAT happened; this module decides WHO hears about
it and how loudly. The rules encode the product's judgment calls:

  duplicate            -> suppress (idempotent pipeline)
  deposit opened/      -> notify primary ("a package is at the door")
    refreshed
  deposit superseded   -> suppress (late webhook; track already closed)
  pickup closed        -> notify primary ("package picked up")
  pickup orphan        -> notify with a caveat (no deposit on record)
  person, night        -> ESCALATE every sink at CRITICAL
  person, day          -> notify primary
  vehicle              -> notify secondary (courier van &c.)
  motion noise         -> suppress UNLESS repeated >= N inside the
                          window, then one digest notification
  anything else        -> suppress

Sinks are pluggable (``Sink`` protocol): a recording ``LogSink`` for
tests/demo, and a generic ``WebhookSink`` that POSTs JSON anywhere
(slack/discord/own API shape) — enabled only when the environment
configures a URL. Nothing here does I/O unless a configured sink does.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Mapping, Protocol

from .schema import Intent, IntentResult, RingEvent
from .state import ApplyResult, Transition


class Severity(Enum):
    INFO = "info"
    CRITICAL = "critical"


class Action(Enum):
    NOTIFY = "notify"
    ESCALATE = "escalate"
    SUPPRESS = "suppress"


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    severity: Severity
    event_id: str
    intent: str
    at: datetime

    def to_payload(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "severity": self.severity.value,
            "event_id": self.event_id,
            "intent": self.intent,
            "at": self.at.isoformat(),
        }


@dataclass(frozen=True)
class RoutingDecision:
    action: Action
    sinks: tuple[str, ...]  # sink names the notification targets
    notification: Notification | None
    reason: str


@dataclass(frozen=True)
class RoutingContext:
    event: RingEvent
    intent: IntentResult
    apply_result: ApplyResult
    burst_count: int  # motion-noise events in window, INCLUDING this one


class Sink(Protocol):
    name: str

    def deliver(self, notification: Notification) -> None: ...


@dataclass
class LogSink:
    """Records notifications in memory (tests, demo summary)."""

    name: str = "log"
    received: list[Notification] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.received is None:
            self.received = []

    def deliver(self, notification: Notification) -> None:
        self.received.append(notification)


WebhookTransport = Callable[[str, bytes, Mapping[str, str], float], bytes]


def _urllib_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


@dataclass
class WebhookSink:
    """POST notifications as JSON to a generic webhook endpoint."""

    url: str
    timeout_s: float = 10.0
    transport: WebhookTransport = _urllib_transport
    name: str = "webhook"
    sent: list[Notification] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.sent is None:
            self.sent = []

    def deliver(self, notification: Notification) -> None:
        body = json.dumps(notification.to_payload()).encode("utf-8")
        self.transport(
            self.url, body, {"Content-Type": "application/json"}, self.timeout_s
        )
        self.sent.append(notification)


def is_night(moment: datetime, start_hour: int, end_hour: int) -> bool:
    """True inside the [start_hour, end_hour) night window (wraps midnight)."""
    hour = moment.hour
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour  # e.g. 21:00-06:00


@dataclass
class Router:
    """Ordered intent-conditioned rules over primary/secondary sinks."""

    primary: tuple[Sink, ...]
    secondary: tuple[Sink, ...]
    night_start_hour: int = 21
    night_end_hour: int = 6
    motion_burst_count: int = 3
    motion_burst_window_min: int = 10

    @classmethod
    def from_settings(cls, settings, *, extra_primary: Sink | None = None) -> "Router":
        """Default wiring: log sink everywhere; webhook/Telegram when configured.

        The recording ``LogSink`` joins both groups so the demo can print
        a faithful "what would have gone out" summary offline. The
        Telegram sink joins the same groups when the environment carries
        ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``; absent credentials
        leave it out entirely (offline runs substitute the mock sink at
        the composition point instead — see ``telegram.py``).
        """
        # local import: telegram.py imports this module for the Sink types
        from .telegram import telegram_sink_from_env

        log = LogSink()
        primary: list[Sink] = [log]
        secondary: list[Sink] = [log]
        if settings.notify_webhook_url:
            webhook = WebhookSink(url=settings.notify_webhook_url)
            primary.append(webhook)
            secondary.append(webhook)
        telegram = telegram_sink_from_env(settings)
        if telegram is not None:
            primary.append(telegram)
            secondary.append(telegram)
        if extra_primary is not None:
            primary.append(extra_primary)
        return cls(
            primary=tuple(primary),
            secondary=tuple(secondary),
            night_start_hour=settings.night_start_hour,
            night_end_hour=settings.night_end_hour,
            motion_burst_count=settings.motion_burst_count,
            motion_burst_window_min=settings.motion_burst_window_min,
        )

    # -- rule engine --------------------------------------------------------

    def route(self, ctx: RoutingContext) -> RoutingDecision:
        event, result, applied = ctx.event, ctx.intent, ctx.apply_result
        primary_names = tuple(s.name for s in self.primary)
        secondary_names = tuple(s.name for s in self.secondary)
        every_name = tuple(dict.fromkeys(primary_names + secondary_names))
        night = is_night(event.occurred_at, self.night_start_hour, self.night_end_hour)

        def notify(title, body, sinks, severity=Severity.INFO):
            return RoutingDecision(
                Action.NOTIFY,
                sinks,
                Notification(title, body, severity, event.event_id, result.intent.value, event.occurred_at),
                f"intent={result.intent.value}",
            )

        if applied.transition is Transition.DUPLICATE:
            return RoutingDecision(Action.SUPPRESS, (), None, applied.note)

        if result.intent is Intent.PACKAGE_DEPOSITED:
            if applied.transition in (Transition.TRACK_OPENED, Transition.TRACK_REFRESHED):
                return notify("Package deposited", f"{event.device_name}: a package was left at the door.", primary_names)
            return RoutingDecision(Action.SUPPRESS, (), None, applied.note)

        if result.intent is Intent.PACKAGE_PICKED_UP:
            if applied.transition is Transition.TRACK_CLOSED:
                return notify("Package picked up", f"{event.device_name}: the package was taken from the door.", primary_names)
            return notify(
                "Possible package pickup",
                f"{event.device_name}: a pickup with no matching deposit on record "
                "(ignore if you or family took it in).",
                primary_names,
            )

        if result.intent is Intent.PERSON_AT_DOOR:
            if night:
                return RoutingDecision(
                    Action.ESCALATE,
                    every_name,
                    Notification(
                        "Person at the door at night",
                        f"{event.device_name}: person detected at {event.occurred_at.strftime('%H:%M')} (night window).",
                        Severity.CRITICAL,
                        event.event_id,
                        result.intent.value,
                        event.occurred_at,
                    ),
                    f"intent={result.intent.value} night=True -> escalate all sinks",
                )
            return notify("Person at the door", f"{event.device_name}: someone is at the door.", primary_names)

        if result.intent is Intent.VEHICLE_AT_DOOR:
            return notify("Vehicle at the door", f"{event.device_name}: a vehicle is at the door.", secondary_names)

        if result.intent is Intent.MOTION_NOISE:
            if ctx.burst_count >= self.motion_burst_count:
                return notify(
                    "Repeated motion",
                    f"{event.device_name}: {ctx.burst_count} unclassified motions in the last "
                    f"{self.motion_burst_window_min} min — worth a look.",
                    primary_names,
                )
            return RoutingDecision(
                Action.SUPPRESS,
                (),
                None,
                f"lone unclassified motion ({ctx.burst_count}/{self.motion_burst_count} in window)",
            )

        return RoutingDecision(Action.SUPPRESS, (), None, f"no rule for intent={result.intent.value}")

    # -- delivery -----------------------------------------------------------

    def deliver(self, decision: RoutingDecision) -> int:
        """Send the notification to the sinks named in the decision."""
        if decision.notification is None or decision.action is Action.SUPPRESS:
            return 0
        by_name = {s.name: s for s in (*self.primary, *self.secondary)}
        delivered = 0
        for name in decision.sinks:
            by_name[name].deliver(decision.notification)
            delivered += 1
        return delivered

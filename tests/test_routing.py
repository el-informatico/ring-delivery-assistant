"""Routing: every rule, night escalation, sinks, burst suppression."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ring_assistant.routing import (
    Action,
    LogSink,
    Notification,
    Router,
    RoutingContext,
    Severity,
    WebhookSink,
    is_night,
)
from ring_assistant.schema import Intent, IntentResult, parse_event_payload
from ring_assistant.settings import Settings
from ring_assistant.state import ApplyResult, Transition

UTC = timezone.utc


def ctx(
    *,
    intent: Intent,
    transition: Transition = Transition.NO_TRACK_CHANGE,
    occurred: str = "2026-09-14T10:00:00Z",
    burst_count: int = 0,
    note: str = "",
) -> RoutingContext:
    event = parse_event_payload(
        {
            "id": "evt-r",
            "type": "motion_detected",
            "sub_type": None,
            "device_id": "front-door",
            "device_name": "Front Door",
            "occurred_at": occurred,
        }
    )
    return RoutingContext(
        event=event,
        intent=IntentResult(intent, 0.9, "test rationale", "rules"),
        apply_result=ApplyResult(transition, note, 0),
        burst_count=burst_count,
    )


@pytest.fixture
def router():
    log = LogSink()
    other = LogSink(name="secondary-log")
    return Router(primary=(log,), secondary=(log, other))


# -- is_night ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(6, False), (5, True), (21, True), (20, False), (23, True), (12, False)],
)
def test_default_night_window(hour, expected):
    assert is_night(datetime(2026, 9, 14, hour, 0, tzinfo=UTC), 21, 6) is expected


def test_non_wrapping_window():
    assert is_night(datetime(2026, 9, 14, 3, 0, tzinfo=UTC), 1, 5) is True
    assert is_night(datetime(2026, 9, 14, 5, 0, tzinfo=UTC), 1, 5) is False


# -- package rules -----------------------------------------------------------


def test_deposit_opened_notifies_primary(router):
    decision = router.route(ctx(intent=Intent.PACKAGE_DEPOSITED, transition=Transition.TRACK_OPENED))
    assert decision.action is Action.NOTIFY
    assert decision.sinks == ("log",)
    assert decision.notification.severity is Severity.INFO
    assert "deposited" in decision.notification.title.lower()


def test_deposit_refreshed_notifies_primary(router):
    decision = router.route(ctx(intent=Intent.PACKAGE_DEPOSITED, transition=Transition.TRACK_REFRESHED))
    assert decision.action is Action.NOTIFY


def test_deposit_superseded_suppressed(router):
    decision = router.route(ctx(intent=Intent.PACKAGE_DEPOSITED, transition=Transition.DEPOSIT_SUPERSEDED, note="late"))
    assert decision.action is Action.SUPPRESS
    assert decision.notification is None
    assert decision.reason == "late"


def test_pickup_closed_notifies(router):
    decision = router.route(ctx(intent=Intent.PACKAGE_PICKED_UP, transition=Transition.TRACK_CLOSED))
    assert decision.action is Action.NOTIFY
    assert "picked up" in decision.notification.title.lower()


def test_pickup_orphan_notifies_with_caveat(router):
    decision = router.route(ctx(intent=Intent.PACKAGE_PICKED_UP, transition=Transition.PICKUP_ORPHAN))
    assert decision.action is Action.NOTIFY
    assert "no matching deposit" in decision.notification.body


# -- person / vehicle rules ---------------------------------------------------


def test_person_day_notifies(router):
    decision = router.route(ctx(intent=Intent.PERSON_AT_DOOR, occurred="2026-09-14T16:50:00Z"))
    assert decision.action is Action.NOTIFY
    assert decision.notification.severity is Severity.INFO


def test_person_night_escalates_all_sinks(router):
    decision = router.route(ctx(intent=Intent.PERSON_AT_DOOR, occurred="2026-09-14T21:37:00Z"))
    assert decision.action is Action.ESCALATE
    assert decision.notification.severity is Severity.CRITICAL
    assert decision.sinks == ("log", "secondary-log")
    assert "night" in decision.reason


def test_person_before_dawn_escalates(router):
    decision = router.route(ctx(intent=Intent.PERSON_AT_DOOR, occurred="2026-09-14T05:30:00Z"))
    assert decision.action is Action.ESCALATE


def test_vehicle_notifies_secondary(router):
    decision = router.route(ctx(intent=Intent.VEHICLE_AT_DOOR))
    assert decision.action is Action.NOTIFY
    assert decision.sinks == ("log", "secondary-log")


# -- noise burst rule ----------------------------------------------------------


def test_lone_noise_suppressed_with_count_reason(router):
    decision = router.route(ctx(intent=Intent.MOTION_NOISE, burst_count=1))
    assert decision.action is Action.SUPPRESS
    assert "1/3" in decision.reason


def test_burst_reaching_threshold_sends_digest(router):
    decision = router.route(ctx(intent=Intent.MOTION_NOISE, burst_count=3))
    assert decision.action is Action.NOTIFY
    assert "3 unclassified motions" in decision.notification.body
    assert "10 min" in decision.notification.body


def test_duplicate_suppressed(router):
    decision = router.route(
        ctx(intent=Intent.PACKAGE_DEPOSITED, transition=Transition.DUPLICATE, note="already applied")
    )
    assert decision.action is Action.SUPPRESS
    assert "already applied" in decision.reason


# -- delivery ---------------------------------------------------------------


def test_deliver_reaches_named_sinks_only(router):
    decision = router.route(ctx(intent=Intent.VEHICLE_AT_DOOR))
    assert router.deliver(decision) == 2
    log: LogSink = router.primary[0]  # type: ignore[assignment]
    assert len(log.received) == 1
    assert log.received[0].event_id == "evt-r"


def test_deliver_suppressed_is_noop(router):
    decision = router.route(ctx(intent=Intent.MOTION_NOISE, burst_count=1))
    assert router.deliver(decision) == 0


# -- webhook sink -------------------------------------------------------------


def test_webhook_sink_posts_json():
    calls = []

    def transport(url, body, headers, timeout):
        calls.append({"url": url, "body": json.loads(body), "headers": headers})
        return b"ok"

    sink = WebhookSink(url="https://notify.example/hook", transport=transport)
    notification = Notification(
        "Package deposited",
        "Front Door: a package was left at the door.",
        Severity.INFO,
        "evt-4",
        "package_deposited",
        datetime(2026, 9, 14, 9, 14, tzinfo=UTC),
    )
    sink.deliver(notification)
    assert calls[0]["url"] == "https://notify.example/hook"
    assert calls[0]["headers"]["Content-Type"] == "application/json"
    assert calls[0]["body"] == {
        "title": "Package deposited",
        "body": "Front Door: a package was left at the door.",
        "severity": "info",
        "event_id": "evt-4",
        "intent": "package_deposited",
        "at": "2026-09-14T09:14:00+00:00",
    }
    assert sink.sent == [notification]


# -- wiring from settings ------------------------------------------------------


def test_from_settings_omits_webhook_when_unconfigured():
    router = Router.from_settings(Settings())
    assert [s.name for s in router.primary] == ["log"]
    assert [s.name for s in router.secondary] == ["log"]


def test_from_settings_adds_webhook_when_configured():
    settings = Settings(notify_webhook_url="https://notify.example/hook", night_start_hour=22)
    router = Router.from_settings(settings)
    assert [s.name for s in router.primary] == ["log", "webhook"]
    assert router.night_start_hour == 22
    # 21:37 with a 22:00 start is day again:
    decision = router.route(ctx(intent=Intent.PERSON_AT_DOOR, occurred="2026-09-14T21:37:00Z"))
    assert decision.action is Action.NOTIFY

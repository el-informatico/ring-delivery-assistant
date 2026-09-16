"""Telegram sink: payload shape, severity conditioning, mock mode, wiring.

The sink never touches the network in tests — every call goes through
the recording transport, which is the same object the offline CLIs
substitute when the environment has no ``TELEGRAM_BOT_TOKEN``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ring_assistant.routing import Notification, Router, Severity
from ring_assistant.settings import Settings
from ring_assistant.telegram import (
    ConfigurationError,
    RecordingTelegramTransport,
    TelegramError,
    TelegramSink,
    format_message,
    mock_telegram_sink,
    telegram_sink_from_env,
)

UTC = timezone.utc


def note(severity: Severity = Severity.INFO, intent: str = "package_deposited") -> Notification:
    return Notification(
        title="Package deposited",
        body="Front Door: a package was left at the door.",
        severity=severity,
        event_id="evt-1",
        intent=intent,
        at=datetime(2026, 9, 16, 8, 3, tzinfo=UTC),
    )


@pytest.fixture
def sink():
    return mock_telegram_sink()


def _transport(sink) -> RecordingTelegramTransport:
    return sink.transport


# -- formatting ---------------------------------------------------------------


def test_info_message_carries_the_bell_marker_and_intent():
    text = format_message(note())
    assert text.startswith("\U0001f514 Package deposited\n")
    assert "Front Door: a package was left at the door." in text
    assert text.endswith("intent: package_deposited · severity: info")


def test_critical_message_carries_the_rotating_light_marker():
    text = format_message(note(Severity.CRITICAL, intent="person_at_door"))
    assert text.startswith("\U0001f6a8 ")
    assert text.endswith("severity: critical")


# -- payload shape ------------------------------------------------------------


def test_deliver_posts_sendmessage_json(sink):
    sink.deliver(note())
    call = _transport(sink).calls[0]
    assert call.url == sink.api_url
    assert "/bot" in call.url and "sendMessage" in call.url
    assert call.headers["Content-Type"] == "application/json"
    assert call.payload["chat_id"] == sink.chat_id
    assert call.payload["text"] == format_message(note())
    assert sink.sent == [note()]


def test_info_notifications_are_silent_critical_ones_ring(sink):
    sink.deliver(note(Severity.INFO))
    sink.deliver(note(Severity.CRITICAL))
    silent, audible = _transport(sink).calls
    assert silent.payload["disable_notification"] is True
    assert audible.payload["disable_notification"] is False


def test_mock_transport_answers_with_the_bot_api_success_shape(sink):
    raw = _transport(sink)(
        sink.api_url,
        json.dumps({"chat_id": "1", "text": "x"}).encode(),
        {"Content-Type": "application/json"},
        5.0,
    )
    answer = json.loads(raw)
    assert answer["ok"] is True
    assert answer["result"]["message_id"] == 1


# -- failure modes ------------------------------------------------------------


def test_refused_message_raises():
    def refusing(url, body, headers, timeout):
        return b'{"ok": false, "description": "chat not found"}'

    sink = TelegramSink(token="t", chat_id="c", transport=refusing)
    with pytest.raises(TelegramError, match="chat not found"):
        sink.deliver(note())


def test_unparseable_answer_raises():
    def garbage(url, body, headers, timeout):
        return b"<html>502</html>"

    sink = TelegramSink(token="t", chat_id="c", transport=garbage)
    with pytest.raises(TelegramError, match="unparseable"):
        sink.deliver(note())


def test_missing_credentials_refuse_to_construct():
    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
        TelegramSink(token="", chat_id="123")
    with pytest.raises(ConfigurationError, match="TELEGRAM_CHAT_ID"):
        TelegramSink(token="abc", chat_id="")


# -- environment gating -------------------------------------------------------


def _settings(**env):
    return Settings.from_env(env)


def test_from_env_wires_the_sink_when_configured():
    settings = _settings(TELEGRAM_BOT_TOKEN="abc", TELEGRAM_CHAT_ID="12345")
    sink = telegram_sink_from_env(settings)
    assert isinstance(sink, TelegramSink)
    assert sink.token == "abc" and sink.chat_id == "12345"


def test_from_env_returns_none_when_credentials_absent():
    assert telegram_sink_from_env(_settings()) is None
    assert telegram_sink_from_env(_settings(TELEGRAM_BOT_TOKEN="abc")) is None
    assert telegram_sink_from_env(_settings(TELEGRAM_CHAT_ID="123")) is None


def test_partial_credentials_do_not_leak_a_half_wired_router():
    # token without chat (or vice versa) must NOT produce a sink that
    # raises at delivery time inside Router.from_settings
    router = Router.from_settings(_settings(TELEGRAM_BOT_TOKEN="abc"))
    assert [s.name for s in router.primary] == ["log"]


def test_router_wires_telegram_into_both_groups_when_configured():
    settings = _settings(
        TELEGRAM_BOT_TOKEN="abc", TELEGRAM_CHAT_ID="12345"
    )
    router = Router.from_settings(settings)
    assert "telegram" in [s.name for s in router.primary]
    assert "telegram" in [s.name for s in router.secondary]
    telegram = next(s for s in router.primary if s.name == "telegram")
    assert telegram.sent == []  # wired, not yet delivered to


def test_escalations_reach_the_mock_telegram_sink():
    # night person -> ESCALATE every sink, critical: the phone rings
    from ring_assistant.routing import Action, RoutingContext
    from ring_assistant.schema import Intent, IntentResult, parse_event_payload
    from ring_assistant.state import ApplyResult, Transition

    event = parse_event_payload(
        {
            "id": "evt-night",
            "type": "motion_detected",
            "sub_type": "human",
            "device_id": "front-door",
            "device_name": "Front Door",
            "occurred_at": "2026-09-16T22:41:00Z",
        }
    )
    log_router = Router.from_settings(_settings())
    telegram = mock_telegram_sink()
    router = Router(
        primary=(*log_router.primary, telegram),
        secondary=(*log_router.secondary, telegram),
    )
    decision = router.route(
        RoutingContext(
            event=event,
            intent=IntentResult(Intent.PERSON_AT_DOOR, 0.9, "test", "rules"),
            apply_result=ApplyResult(Transition.NO_TRACK_CHANGE, "", 0),
            burst_count=0,
        )
    )
    assert decision.action is Action.ESCALATE
    assert "telegram" in decision.sinks
    router.deliver(decision)
    call = _transport(telegram).calls[0]
    assert call.payload["disable_notification"] is False  # critical rings

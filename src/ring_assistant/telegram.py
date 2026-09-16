"""Telegram notification sink: the one routed sink a phone actually sees.

Implements the same ``Sink`` protocol as ``LogSink`` / ``WebhookSink``,
so the router delivers to it like any other. Messages go through the
Bot API ``sendMessage`` call; severity maps onto Telegram's own silent
vs. audible distinction:

  info     -> ``disable_notification=true``  (silent push)
  critical -> ``disable_notification=false`` (sound / vibration)

Credentials live in ``.env`` (gitignored) and nowhere else:
``TELEGRAM_BOT_TOKEN`` from @BotFather, ``TELEGRAM_CHAT_ID`` the chat
the bot delivers to. When they are absent the sink is simply not wired
(``telegram_sink_from_env`` returns ``None``) and offline runs use the
recording transport instead — same formatting, same payload assembly,
same response parsing, only the HTTP POST is fake:

    from ring_assistant.telegram import mock_telegram_sink
    sink = mock_telegram_sink()          # records; never touches network

Delivery failures are loud: a non-``ok`` Telegram answer raises
``TelegramError`` (the webhook sink propagates transport errors the
same way — a notification nobody saw must not look like success).
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .routing import Notification, Severity

Transport = Callable[[str, bytes, Mapping[str, str], float], bytes]
"""``transport(url, body, headers, timeout) -> response_bytes``."""

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """Telegram answered but refused the message (``ok`` not true)."""


class ConfigurationError(RuntimeError):
    """The Telegram environment contract is incomplete (token / chat id)."""


def _urllib_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def format_message(notification: Notification) -> str:
    """The exact text delivered for a notification (deterministic)."""
    marker = "\U0001f6a8" if notification.severity is Severity.CRITICAL else "\U0001f514"
    return (
        f"{marker} {notification.title}\n"
        f"{notification.body}\n"
        f"intent: {notification.intent} · severity: {notification.severity.value}"
    )


@dataclass
class TelegramSink:
    """Deliver notifications to one Telegram chat via ``sendMessage``."""

    token: str
    chat_id: str
    timeout_s: float = 10.0
    transport: Transport = _urllib_transport
    name: str = "telegram"
    sent: list[Notification] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.token or not self.chat_id:
            missing = ", ".join(
                label
                for label, value in (
                    ("TELEGRAM_BOT_TOKEN", self.token),
                    ("TELEGRAM_CHAT_ID", self.chat_id),
                )
                if not value
            )
            raise ConfigurationError(
                f"Telegram sink not configured; set {missing} in .env "
                "(see .env.example — never chat, never commit)"
            )

    @property
    def api_url(self) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.token}/sendMessage"

    def _payload(self, notification: Notification) -> dict:
        return {
            "chat_id": self.chat_id,
            "text": format_message(notification),
            # severity-conditioned delivery: critical messages ring,
            # informational ones arrive silently
            "disable_notification": notification.severity is not Severity.CRITICAL,
        }

    def deliver(self, notification: Notification) -> None:
        body = json.dumps(self._payload(notification)).encode("utf-8")
        raw = self.transport(
            self.api_url, body, {"Content-Type": "application/json"}, self.timeout_s
        )
        try:
            answer = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TelegramError(f"unparseable Telegram answer: {raw[:120]!r}") from exc
        if not answer.get("ok"):
            raise TelegramError(f"Telegram refused the message: {answer}")
        self.sent.append(notification)


def telegram_sink_from_env(
    settings, transport: Transport | None = None
) -> TelegramSink | None:
    """Live sink when the environment configures one; ``None`` otherwise.

    ``None`` is the offline mode: the router is built without Telegram
    and nothing changes for the rest of the pipeline. Demos and the
    star-metric run substitute :func:`mock_telegram_sink` so the full
    delivery path still executes offline.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return None
    return TelegramSink(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        timeout_s=settings.telegram_timeout_s,
        transport=transport or _urllib_transport,
    )


@dataclass(frozen=True)
class TelegramCall:
    """One recorded ``sendMessage`` attempt (mock mode)."""

    url: str
    payload: dict
    headers: dict
    timeout_s: float


@dataclass
class RecordingTelegramTransport:
    """Mock transport: records the exact request, answers like the Bot API.

    The URL, headers, and JSON body are what the live sink would have
    POSTed; the answer is the Bot API's success shape with a
    deterministic message id. Optional ``latency_s`` simulates network
    RTT for latency demos (default 0 — the offline measurement honestly
    excludes the network).
    """

    calls: list[TelegramCall] = field(default_factory=list)
    latency_s: float = 0.0

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
        if self.latency_s:
            time.sleep(self.latency_s)
        self.calls.append(
            TelegramCall(
                url=url,
                payload=json.loads(body.decode("utf-8")),
                headers=dict(headers),
                timeout_s=timeout,
            )
        )
        return json.dumps(
            {"ok": True, "result": {"message_id": len(self.calls), "chat": {"id": 0}}}
        ).encode("utf-8")


def mock_telegram_sink(latency_s: float = 0.0) -> TelegramSink:
    """A Telegram sink whose network leg is a recorder, clearly labeled.

    Everything except the socket is the real sink: message formatting,
    payload assembly, response parsing. The token/chat placeholders
    never leave the process — they only mark the recorded URLs as mock.
    """
    return TelegramSink(
        token="mock-token",
        chat_id="mock-chat",
        transport=RecordingTelegramTransport(latency_s=latency_s),
    )

"""Shared test fixtures — synthetic payloads and signature helpers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from ring_assistant.verify import sign_payload

SECRET = "synthetic-test-secret"

# Env-only keys that flip a sink/leg to LIVE. The suite's contract is
# offline/zero-sockets; a developer's populated .env must not leak in
# (the CLI tests call main(), whose load_env_file() would otherwise
# leave real Telegram credentials in os.environ for every later test).
LIVE_LEG_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "RING_LLM_ENDPOINT",
    "RING_LLM_MODEL",
    "RING_LLM_API_KEY",
    "RING_NOTIFY_WEBHOOK_URL",
)


@pytest.fixture(autouse=True)
def hermetic_live_legs(monkeypatch):
    """Pin live-credential env vars to empty so no test can open a socket.

    Empty (not deleted): ``load_env_file`` skips keys already present in
    ``os.environ``, so the CLI tests' in-process ``main()`` call cannot
    re-import real credentials from the developer's ``.env`` either.
    """
    for key in LIVE_LEG_KEYS:
        monkeypatch.setenv(key, "")


@pytest.fixture
def secret() -> str:
    return SECRET


@pytest.fixture
def make_payload():
    """Factory for valid synthetic webhook payloads."""

    def _make(**overrides) -> dict:
        payload = {
            "id": "evt-0001",
            "type": "motion_detected",
            "sub_type": "human",
            "device_id": "front-door",
            "device_name": "Front Door",
            "occurred_at": "2026-09-14T09:14:02+00:00",
            "received_at": "2026-09-14T09:14:06+00:00",
            "snapshot_path": "snapshots/person-door.png",
        }
        payload.update(overrides)
        return payload

    return _make


@pytest.fixture
def body_of():
    """Serialize a payload to webhook body bytes."""

    def _body(payload: dict) -> bytes:
        return json.dumps(payload).encode("utf-8")

    return _body


@pytest.fixture
def signed_headers():
    """Headers carrying a valid signature for the given body/secret."""

    def _headers(body: bytes, secret: str = SECRET) -> dict[str, str]:
        return {"X-Ring-Signature": sign_payload(body, secret)}

    return _headers


@pytest.fixture
def froze_received_at():
    """Deterministic 'now' for stamping tests."""
    return datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)

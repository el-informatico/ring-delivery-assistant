"""FastAPI adapter: status-code mapping and end-to-end accept path.

Runs in-process via TestClient (httpx) — no sockets, offline.
"""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from ring_assistant.classify import RuleBasedClassifier  # noqa: E402
from ring_assistant.replay import Pipeline  # noqa: E402
from ring_assistant.routing import Router  # noqa: E402
from ring_assistant.server import build_pipeline, create_app  # noqa: E402
from ring_assistant.settings import Settings  # noqa: E402
from ring_assistant.state import StateStore  # noqa: E402
from ring_assistant.synth import generate_to  # noqa: E402
from ring_assistant.verify import sign_payload  # noqa: E402

SECRET = "server-test-secret"


@pytest.fixture
def client(tmp_path):
    settings = Settings(webhook_secret=SECRET, db_path=str(tmp_path / "state.db"))
    pipeline = build_pipeline(settings, db_path=str(tmp_path / "state.db"))
    return TestClient(create_app(pipeline))


def make_body(tmp_path, index=0):
    _, payloads = generate_to(tmp_path / "synth")
    return json.dumps(payloads[index]).encode("utf-8")


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_valid_webhook_accepted(client, tmp_path):
    body = make_body(tmp_path, 0)  # first storyboard event
    response = client.post(
        "/webhooks/ring", content=body, headers={"X-Ring-Signature": sign_payload(body, SECRET)}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["event_id"] == "evt-001"
    assert payload["intent"] == "motion_noise"
    assert payload["action"] in ("notify", "escalate", "suppress")


def test_bad_signature_401(client, tmp_path):
    body = make_body(tmp_path)
    response = client.post(
        "/webhooks/ring", content=body, headers={"X-Ring-Signature": sign_payload(body, "nope")}
    )
    assert response.status_code == 401
    assert "signature" in response.json()["detail"]


def test_missing_signature_401(client, tmp_path):
    response = client.post("/webhooks/ring", content=make_body(tmp_path))
    assert response.status_code == 401


def test_signed_garbage_400(client, tmp_path):
    body = b"not-json-at-all"
    response = client.post(
        "/webhooks/ring", content=body, headers={"X-Ring-Signature": sign_payload(body, SECRET)}
    )
    assert response.status_code == 400


def test_signed_schema_violation_400(client, tmp_path):
    _, payloads = generate_to(tmp_path / "synth")
    payloads[0]["type"] = "doorbell_exploded"
    body = json.dumps(payloads[0]).encode("utf-8")
    response = client.post(
        "/webhooks/ring", content=body, headers={"X-Ring-Signature": sign_payload(body, SECRET)}
    )
    assert response.status_code == 400


def test_duplicate_webhook_is_idempotent_200(client, tmp_path):
    body = make_body(tmp_path, 0)
    headers = {"X-Ring-Signature": sign_payload(body, SECRET)}
    first = client.post("/webhooks/ring", content=body, headers=headers)
    second = client.post("/webhooks/ring", content=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["transition"] == "no_track_change"
    assert second.json()["transition"] == "duplicate"
    assert second.json()["action"] == "suppress"


def test_default_app_refuses_to_start_without_secret(monkeypatch):
    from ring_assistant import server

    monkeypatch.delenv("RING_WEBHOOK_SECRET", raising=False)
    monkeypatch.chdir("/")  # no .env lying around
    with pytest.raises(RuntimeError, match="RING_WEBHOOK_SECRET"):
        server.build_default_app()


def test_pipeline_uses_rules_stub_when_llm_unconfigured(tmp_path):
    settings = Settings(webhook_secret=SECRET, db_path=str(tmp_path / "s.db"))
    pipeline = build_pipeline(settings, db_path=str(tmp_path / "s.db"))
    assert isinstance(pipeline.classifier, RuleBasedClassifier)
    assert isinstance(pipeline.store, StateStore)
    assert isinstance(pipeline.router, Router)

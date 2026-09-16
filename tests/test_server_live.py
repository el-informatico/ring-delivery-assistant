"""FastAPI adapter in live-wire mode: Ring's v1.1 contract over HTTP.

Same in-process TestClient approach as ``test_server.py`` (no sockets);
the only difference is ``create_app(..., live_wire=True)`` — the mode
the served app (``uvicorn ring_assistant.server:app``) actually runs.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from ring_assistant.classify import RuleBasedClassifier  # noqa: E402
from ring_assistant.replay import Pipeline  # noqa: E402
from ring_assistant.routing import Router  # noqa: E402
from ring_assistant.server import create_app  # noqa: E402
from ring_assistant.settings import Settings  # noqa: E402
from ring_assistant.state import StateStore  # noqa: E402
from ring_assistant.verify import sign_payload  # noqa: E402
from ring_assistant.wire import signed_v1_1  # noqa: E402
from test_wire import SECRET, load_fixture  # noqa: E402


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "state.db"))
    pipeline = Pipeline(
        secret=SECRET,
        classifier=RuleBasedClassifier(),
        store=StateStore(settings.db_path),
        router=Router.from_settings(settings),
        signature_header="x-signature",
    )
    return TestClient(create_app(pipeline, live_wire=True))


def test_live_webhook_accepted_200(client):
    body, headers = signed_v1_1(load_fixture("motion_sub_type_human"), SECRET)
    response = client.post("/webhooks/ring", content=body, headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["intent"] == "person_at_door"
    assert payload["action"] == "notify"


def test_live_ignored_event_type_acks_200(client):
    body, headers = signed_v1_1(load_fixture("subscription_activated"), SECRET)
    response = client.post("/webhooks/ring", content=body, headers=headers)
    assert response.status_code == 200  # NOT 4xx: Ring would treat that as permanent failure
    assert response.json() == {"status": "ignored", "event_type": "subscription_activated"}


def test_live_bad_signature_401(client):
    body, _ = signed_v1_1(load_fixture("button_press"), SECRET)
    response = client.post(
        "/webhooks/ring", content=body, headers={"X-Signature": sign_payload(body, "nope")}
    )
    assert response.status_code == 401


def test_live_signed_garbage_400(client):
    body = b"not-json-at-all"
    response = client.post(
        "/webhooks/ring", content=body, headers={"X-Signature": sign_payload(body, SECRET)}
    )
    assert response.status_code == 400


def test_default_served_app_speaks_live_wire(tmp_path, monkeypatch):
    from ring_assistant import server

    monkeypatch.setenv("RING_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("RING_SIGNATURE_HEADER", "x-signature")
    monkeypatch.setenv("RING_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.chdir(tmp_path)  # no stray .env interferes

    client = TestClient(server.build_default_app())
    body, headers = signed_v1_1(load_fixture("button_press"), SECRET)
    response = client.post("/webhooks/ring", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["intent"] == "person_at_door"

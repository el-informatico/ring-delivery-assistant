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


def test_portal_endpoint_stubs(client):
    """The three non-webhook portal URLs answer honestly (staging tab)."""
    # Homepage: any HTTPS page is valid per the guide's table.
    r = client.get("/")
    assert r.status_code == 200
    assert "ring-delivery-assistant" in r.text
    assert "Staging endpoints" in r.text

    # Account link: honest gate page, links the constraint doc.
    r = client.get("/account-link")
    assert r.status_code == 200
    assert "Account linking" in r.text
    assert "US-located devices" in r.text

    # Token exchange without credentials: honest 501, code not swallowed.
    r = client.get("/oauth/callback")
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"
    r = client.get("/oauth/callback?code=SplwbOjb64&state=xyz")
    assert r.status_code == 501
    assert r.json()["error"] == "not_configured"
    assert "RING_CLIENT_ID" in r.json()["detail"]
    r = client.post("/oauth/callback", data={"code": "SplwbOjb64"})
    assert r.status_code == 501


def _oauth_client(tmp_path, mock_endpoint, *, with_store=True):
    from ring_assistant.oauth import TokenExchanger, TokenStore
    from ring_assistant.server import create_app

    settings = Settings(webhook_secret=SECRET, db_path=str(tmp_path / "s.db"))
    pipeline = build_pipeline(settings, db_path=str(tmp_path / "s.db"))
    exchanger = TokenExchanger(
        client_id="cid",
        client_secret="client-secret-test-value",
        transport=mock_endpoint,
    )
    store = TokenStore(tmp_path / "tokens.json") if with_store else None
    return TestClient(create_app(pipeline, token_exchanger=exchanger, token_store=store))


def test_token_exchange_route_mints_and_persists(tmp_path):
    import json as _json

    from test_oauth import MockTokenEndpoint

    mock = MockTokenEndpoint()
    client = _oauth_client(tmp_path, mock)
    r = client.post("/oauth/callback", json={"code": "one-time-code"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "token_exchanged"
    assert body["expires_in"] == 14400
    assert body["persisted"] is True
    # tokens are NEVER echoed in the response
    assert "at-1" not in _json.dumps(body) and "rt-1" not in _json.dumps(body)
    # the documented outbound exchange actually happened
    form = mock.requests[0][2].decode("utf-8")
    assert "grant_type=authorization_code" in form and "one-time-code" in form
    # the minted bundle landed in the store (the RING_API_TOKEN source)
    assert (tmp_path / "tokens.json").read_text(encoding="utf-8").count("at-1") == 1


def test_token_exchange_route_form_body_and_errors(tmp_path):
    from test_oauth import MockTokenEndpoint
    from ring_assistant.oauth import OAuthError

    mock = MockTokenEndpoint()
    client = _oauth_client(tmp_path, mock)
    # form-urlencoded inbound body is accepted too (docs don't pin the shape)
    r = client.post(
        "/oauth/callback",
        content=b"code=form-code",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    assert "form-code" in mock.requests[0][2].decode("utf-8")
    # unparseable body -> 400
    r = client.post(
        "/oauth/callback",
        content=b"{nope",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    # endpoint failure -> 502 with the JSON:API detail, never the client secret
    mock.status = 400
    mock.payload = {"errors": [{"code": "INVALID_GRANT", "detail": "code expired"}]}
    r = client.post("/oauth/callback", json={"code": "late"})
    assert r.status_code == 502
    assert "code expired" in r.json()["detail"]
    assert "client-secret-test-value" not in r.json()["detail"]

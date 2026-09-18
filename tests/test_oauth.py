"""Token exchange against a mock oauth.ring.com endpoint (in-process).

No sockets: the mock is an injected transport answering with the
documented token-endpoint shapes (developer.amazon.com/docs/ring/
api-documentation.html §5), so these tests pin the request
construction (form body, credentials IN the body, no PKCE) and the
bundle parsing / persistence without a network — same pattern as
``test_events_api``.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from ring_assistant.events_api import HttpResponse
from ring_assistant.oauth import (
    DOCUMENTED_EXPIRES_IN,
    DOCUMENTED_TOKEN_URL,
    OAuthConfigError,
    OAuthError,
    TokenBundle,
    TokenExchanger,
    TokenStore,
)

CLIENT_ID = "partner-client-id"
CLIENT_SECRET = "partner-client-secret"  # labeled test value, not a real one


class MockTokenEndpoint:
    """Answers POSTs like the documented oauth.ring.com/oauth/token."""

    def __init__(self, *, status: int = 200, payload: dict | None = None):
        self.status = status
        self.payload = payload if payload is not None else {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "scope": "devices.read",
            "expires_in": 14400,
            "token_type": "Bearer",
        }
        self.requests: list[tuple[str, str, bytes, dict, float]] = []

    def __call__(self, method, url, body, headers, timeout) -> HttpResponse:
        self.requests.append((method, url, body, dict(headers), timeout))
        if isinstance(self.status, Exception):
            raise self.status
        return HttpResponse(
            self.status,
            {"content-type": "application/json"},
            json.dumps(self.payload).encode("utf-8"),
        )


def make_exchanger(mock: MockTokenEndpoint) -> TokenExchanger:
    return TokenExchanger(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=mock,
    )


@pytest.fixture
def mock() -> MockTokenEndpoint:
    return MockTokenEndpoint()


# -- construction (fail-closed) ------------------------------------------------


def test_empty_credentials_are_a_construction_error():
    with pytest.raises(OAuthConfigError, match="RING_CLIENT_ID"):
        TokenExchanger(client_id="", client_secret="s")
    with pytest.raises(OAuthConfigError, match="RING_CLIENT_ID"):
        TokenExchanger(client_id="id", client_secret="")


def test_empty_code_is_refused_before_any_request(mock):
    exchanger = make_exchanger(mock)
    with pytest.raises(OAuthConfigError, match="code is empty"):
        exchanger.exchange_code("")
    assert mock.requests == []


# -- authorization-code grant (request shape pinned to the docs) ---------------


def test_exchange_code_posts_documented_form(mock):
    make_exchanger(mock).exchange_code("auth-code-1", now=1000.0)
    method, url, body, headers, _ = mock.requests[0]
    assert method == "POST"
    assert url == DOCUMENTED_TOKEN_URL
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
    # documented confidential-client grant: credentials in the FORM BODY,
    # no PKCE verifier, no Basic auth
    assert form == {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": "auth-code-1",
        "client_secret": CLIENT_SECRET,
    }


def test_exchange_code_parses_bundle(mock):
    bundle = make_exchanger(mock).exchange_code("auth-code-1", now=1000.0)
    assert bundle.access_token == "at-1"
    assert bundle.refresh_token == "rt-1"
    assert bundle.scope == "devices.read"
    assert bundle.expires_in == 14400
    assert bundle.token_type == "Bearer"
    assert bundle.obtained_at == 1000.0
    assert bundle.expires_at == 1000.0 + 14400
    assert not bundle.expired(1000.0 + 100)
    assert bundle.expired(1000.0 + 14400)


# -- refresh grant --------------------------------------------------------------


def test_refresh_posts_documented_form(mock):
    make_exchanger(mock).refresh("rt-1", now=2000.0)
    _, _, body, _, _ = mock.requests[0]
    form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
    assert form == {
        "grant_type": "refresh_token",
        "refresh_token": "rt-1",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }


def test_refresh_without_token_is_refused_before_any_request(mock):
    with pytest.raises(OAuthConfigError, match="refresh token is empty"):
        make_exchanger(mock).refresh("")
    assert mock.requests == []


# -- error paths ----------------------------------------------------------------


def test_http_error_surfaces_jsonapi_detail_not_the_request(mock):
    mock.status = 400
    mock.payload = {"errors": [{"code": "INVALID_GRANT", "detail": "code already used"}]}
    with pytest.raises(OAuthError) as excinfo:
        make_exchanger(mock).exchange_code("stale")
    assert "HTTP 400" in str(excinfo.value)
    assert "INVALID_GRANT" in str(excinfo.value)
    assert "code already used" in str(excinfo.value)
    assert CLIENT_SECRET not in str(excinfo.value)


def test_transport_failure_becomes_oauth_error():
    def broken(method, url, body, headers, timeout):
        raise urllib.error.URLError("no route to oauth host")

    exchanger = TokenExchanger(CLIENT_ID, CLIENT_SECRET, transport=broken)
    with pytest.raises(OAuthError, match="transport failure"):
        exchanger.exchange_code("c")


def test_missing_access_token_is_an_error(mock):
    mock.payload = {"refresh_token": "rt-1"}
    with pytest.raises(OAuthError, match="access_token"):
        make_exchanger(mock).exchange_code("c")


def test_non_json_body_is_an_error(mock):
    with pytest.raises(OAuthError, match="non-JSON"):
        TokenExchanger(
            CLIENT_ID,
            CLIENT_SECRET,
            transport=lambda *a: HttpResponse(200, {}, b"<html>oops</html>"),
        ).exchange_code("c")


def test_garbage_expires_in_falls_back_to_documented_default(mock):
    mock.payload = {"access_token": "at-2", "expires_in": "soon"}
    bundle = make_exchanger(mock).exchange_code("c", now=0.0)
    assert bundle.expires_in == DOCUMENTED_EXPIRES_IN


# -- token store (the mint target) ---------------------------------------------


def test_store_roundtrip(tmp_path: Path):
    store = TokenStore(tmp_path / "nested" / "tokens.json")
    bundle = TokenBundle("at-1", "rt-1", "s", 14400, "Bearer", 1000.0)
    store.save(bundle)
    assert (tmp_path / "nested" / "tokens.json").exists()
    assert store.load() == bundle


def test_store_writes_owner_only_permissions(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(TokenBundle("at", "rt", "", 1, "Bearer", 0.0))
    assert ((tmp_path / "tokens.json").stat().st_mode & 0o777) == 0o600


def test_store_load_never_raises(tmp_path: Path):
    missing = TokenStore(tmp_path / "absent.json")
    assert missing.load() is None
    corrupt = TokenStore(tmp_path / "corrupt.json")
    corrupt.path.write_text("{not json", encoding="utf-8")
    assert corrupt.load() is None
    no_token = TokenStore(tmp_path / "empty.json")
    no_token.path.write_text(json.dumps({"scope": "s"}), encoding="utf-8")
    assert no_token.load() is None

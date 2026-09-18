"""Turso/libsql backends: the connection-injection seam + token store.

Zero sockets: injected connections are in-memory sqlite3 — libsql speaks
SQLite's dialect, so running the stores' exact SQL against sqlite3 pins
the query surface the deploy will use. The real client import and the
Turso wire are monkeypatched/deferred to the first deploy (the same
documented-contract honesty as test_events_api).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from ring_assistant.oauth import TokenBundle, TokenStore
from ring_assistant.schema import Intent
from ring_assistant.state import StateStore, Transition
from ring_assistant.turso import (
    TursoError,
    TursoTokenStore,
    _connect_libsql,
    connect_turso,
)

from test_state import at, event

fastapi = pytest.importorskip("fastapi")

URL = "libsql://ring-staging-example.turso.io"
TOKEN = "turso-db-token-test-value"  # labeled test value, never a real secret


def memory_connection() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", check_same_thread=False)


@pytest.fixture
def turso_conn():
    conn = connect_turso(URL, TOKEN, connector=lambda url, token: memory_connection())
    yield conn
    conn.close()


# -- connect_turso: fail-closed config, injectable client ---------------------


def test_connect_turso_passes_credentials_to_connector():
    seen = []

    def connector(url, token):
        seen.append((url, token))
        return memory_connection()

    conn = connect_turso(URL, TOKEN, connector=connector)
    conn.close()
    assert seen == [(URL, TOKEN)]


def test_connect_turso_requires_both_coordinates():
    with pytest.raises(TursoError, match="together"):
        connect_turso("", TOKEN, connector=lambda url, token: None)
    with pytest.raises(TursoError, match="together"):
        connect_turso(URL, "", connector=lambda url, token: None)


def test_missing_client_is_an_install_error(monkeypatch):
    # None in sys.modules makes `import ...` raise ImportError for each name
    monkeypatch.setitem(sys.modules, "libsql", None)
    monkeypatch.setitem(sys.modules, "libsql_experimental", None)
    with pytest.raises(TursoError, match="requirements-render.txt"):
        _connect_libsql(URL, TOKEN)


# -- StateStore over an injected connection -----------------------------------


@pytest.fixture
def conn_store(turso_conn):
    with StateStore(connection=turso_conn) as s:
        yield s


def test_state_store_on_injected_connection_tracks_lifecycle(conn_store):
    d1 = event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 0), received=at(9, 0, 5))
    assert conn_store.apply(d1, Intent.PACKAGE_DEPOSITED).transition is Transition.TRACK_OPENED
    d2 = event("d2", intent=Intent.PACKAGE_DEPOSITED, occurred=at(10, 0), received=at(10, 0, 5))
    assert conn_store.apply(d2, Intent.PACKAGE_DEPOSITED).transition is Transition.TRACK_REFRESHED
    p1 = event("p1", intent=Intent.PACKAGE_PICKED_UP, occurred=at(11, 0), received=at(11, 0, 5))
    assert conn_store.apply(p1, Intent.PACKAGE_PICKED_UP).transition is Transition.TRACK_CLOSED
    tracks = conn_store.tracks("front-door")
    assert len(tracks) == 1 and not tracks[0].is_open and tracks[0].deposits == 2
    # duplicate stays idempotent on the injected backend
    again = conn_store.apply(d1, Intent.PACKAGE_DEPOSITED)
    assert again.transition is Transition.DUPLICATE


def test_injected_and_file_stores_behave_identically(tmp_path, turso_conn):
    script = [
        ("d1", Intent.PACKAGE_DEPOSITED, at(9, 0), at(9, 0, 5)),
        ("p1", Intent.PACKAGE_PICKED_UP, at(10, 0), at(10, 0, 5)),
        ("m1", Intent.MOTION_NOISE, at(10, 30), at(10, 30, 5)),
        ("d2", Intent.PACKAGE_DEPOSITED, at(12, 0), at(12, 0, 5)),
    ]
    results = {}
    with StateStore(tmp_path / "file.db") as file_store:
        for eid, intent, occurred, received in script:
            file_store.apply(event(eid, intent=intent, occurred=occurred, received=received), intent)
        results["file"] = file_store.tracks("front-door")
    with StateStore(connection=turso_conn) as injected_store:
        for eid, intent, occurred, received in script:
            injected_store.apply(
                event(eid, intent=intent, occurred=occurred, received=received), intent
            )
        results["conn"] = injected_store.tracks("front-door")

    def rows(tracks):
        return [
            (t.deposited_at, t.picked_up_at, t.deposits, t.pickup_event_id, t.out_of_order)
            for t in tracks
        ]

    assert rows(results["file"]) == rows(results["conn"])


def test_recent_intents_on_injected_connection(conn_store):
    for i in range(3):
        conn_store.apply(
            event(f"m{i}", intent=Intent.MOTION_NOISE, occurred=at(9, i), received=at(9, i, 5)),
            Intent.MOTION_NOISE,
        )
    assert (
        conn_store.recent_intents(
            "front-door", Intent.MOTION_NOISE, window=timedelta(minutes=10), before=at(9, 3)
        )
        == 3
    )


def test_state_store_requires_exactly_one_backend(tmp_path, turso_conn):
    with pytest.raises(ValueError, match="exactly one"):
        StateStore()
    with pytest.raises(ValueError, match="exactly one"):
        StateStore(tmp_path / "s.db", connection=turso_conn)


# -- TursoTokenStore -----------------------------------------------------------


@pytest.fixture
def token_store(turso_conn):
    return TursoTokenStore(turso_conn)


def test_token_roundtrip_single_row(token_store, turso_conn):
    first = TokenBundle("at-1", "rt-1", "read", 14400, "Bearer", 1000.0)
    token_store.save(first)
    assert token_store.load() == first
    second = TokenBundle("at-2", "rt-2", "read", 14400, "Bearer", 2000.0)
    token_store.save(second)
    assert token_store.load() == second
    rows = turso_conn.execute("SELECT COUNT(*) FROM tokens", []).fetchone()
    assert rows[0] == 1  # upsert: one row, always the newest bundle


def test_token_load_never_raises(token_store, turso_conn):
    assert token_store.load() is None  # empty table
    for corrupt in ('"just a string"', "{nope", "42"):
        turso_conn.execute("DELETE FROM tokens", [])
        turso_conn.execute("INSERT INTO tokens (id, bundle_json) VALUES (1, ?)", [corrupt])
        turso_conn.commit()
        assert token_store.load() is None
    turso_conn.execute("DELETE FROM tokens", [])
    turso_conn.execute(
        "INSERT INTO tokens (id, bundle_json) VALUES (1, ?)", [json.dumps({"scope": "no token"})]
    )
    turso_conn.commit()
    assert token_store.load() is None  # document without access_token


def test_document_is_shared_with_the_file_store(token_store, turso_conn, tmp_path):
    bundle = TokenBundle("at-1", "rt-1", "read", 14400, "Bearer", 1000.0)
    token_store.save(bundle)
    row = turso_conn.execute("SELECT bundle_json FROM tokens WHERE id = 1", []).fetchone()
    # the exact document a Turso save lands as loads from the file store
    path = tmp_path / "tokens.json"
    path.write_text(row[0], encoding="utf-8")
    assert TokenStore(path).load() == bundle


# -- server wiring: fail-closed half config + full Turso serving ----------------


def test_build_default_app_half_turso_config_fails_closed(monkeypatch, tmp_path):
    from ring_assistant import server

    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("RING_WEBHOOK_SECRET", "server-test-secret")
    monkeypatch.setenv("RING_TURSO_URL", URL)
    monkeypatch.delenv("RING_TURSO_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="together"):
        server.build_default_app()


def test_build_default_app_serves_webhooks_through_turso(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from ring_assistant import server
    from ring_assistant.verify import sign_payload

    secret = "server-test-secret"
    connections = []

    def fake_connect(url, token):
        assert (url, token) == (URL, TOKEN)  # coordinates flow to the client
        conn = connect_turso(url, token, connector=lambda u, t: memory_connection())
        connections.append(conn)
        return conn

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RING_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("RING_TURSO_URL", URL)
    monkeypatch.setenv("RING_TURSO_TOKEN", TOKEN)
    # earlier CLI tests may have loaded the real .env into os.environ
    # (load_env_file is process-global); scrub every knob that could
    # point this app at sockets or a different signature header
    for leaked in (
        "RING_SIGNATURE_HEADER",
        "RING_LLM_ENDPOINT",
        "RING_LLM_MODEL",
        "RING_LLM_API_KEY",
        "RING_NOTIFY_WEBHOOK_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "RING_API_TOKEN",
        "RING_RECORD_DIR",
    ):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setattr(server, "connect_turso", fake_connect)

    app = server.build_default_app()
    assert connections, "build_default_app must open the Turso connection itself"
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        body = (Path(__file__).parent / "fixtures/wire/motion_sub_type_motion.json").read_bytes()
        response = client.post(
            "/webhooks/ring", content=body, headers={"x-ring-signature": sign_payload(body, secret)}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"


# -- mint-token CLI store selection ---------------------------------------------


def test_mint_token_half_turso_config_exits(monkeypatch, tmp_path, capsys):
    from ring_assistant.oauth_cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RING_CLIENT_ID", "cid")
    monkeypatch.setenv("RING_CLIENT_SECRET", "client-secret-test-value")
    monkeypatch.setenv("RING_TURSO_URL", URL)
    monkeypatch.delenv("RING_TURSO_TOKEN", raising=False)
    assert main(["some-code"]) == 1
    assert "together" in capsys.readouterr().err


def test_mint_token_turso_store_selected(monkeypatch, tmp_path):
    import ring_assistant.turso as turso_module
    from ring_assistant.oauth_cli import main

    opened = []

    def fake_connect(url, token):
        opened.append((url, token))
        return connect_turso(url, token, connector=lambda u, t: memory_connection())

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RING_TURSO_URL", URL)
    monkeypatch.setenv("RING_TURSO_TOKEN", TOKEN)
    monkeypatch.setattr(turso_module, "connect_turso", fake_connect)
    # no client credentials: the store opens first, then the exchanger
    # fails closed — proving selection order without any network call
    monkeypatch.delenv("RING_CLIENT_ID", raising=False)
    monkeypatch.delenv("RING_CLIENT_SECRET", raising=False)
    assert main(["some-code"]) == 1
    assert opened == [(URL, TOKEN)]

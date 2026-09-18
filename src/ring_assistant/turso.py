"""Turso (libsql) backends — durable storage for the deployed app.

Render's free instance has an EPHEMERAL disk: whatever the process writes
to a file is gone on the next deploy or restart. The two stores that must
survive move to Turso's free tier (libsql — SQLite's dialect over HTTPS):

* the state DB (``events`` / ``tracks``) — same schema, same SQL, through
  :class:`ring_assistant.state.StateStore`'s ``connection`` seam;
* the token store (the minted OAuth bundle) — one JSON row in a
  ``tokens`` table, same contract as :class:`ring_assistant.oauth.TokenStore`.

Selection is configuration, not code: BOTH ``RING_TURSO_URL`` and
``RING_TURSO_TOKEN`` set -> Turso; both unset -> the file/sqlite stores,
byte-for-byte today's behavior (local dev). Half-configured is a startup
error — fail-closed like every other credential seam in this repo.

The real client (``libsql-experimental``) is imported lazily so the core
stays dependency-free and offline; tests inject sqlite3 connections
(same DBAPI subset, zero sockets) — the same documented-contract honesty
as ``test_events_api``: Turso's wire behavior is exercised on the first
deploy, not in the suite.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from .oauth import TokenBundle, bundle_from_document, bundle_to_document

_TOKENS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS tokens ("
    " id INTEGER PRIMARY KEY CHECK (id = 1),"
    " bundle_json TEXT NOT NULL)"
)


class TursoError(RuntimeError):
    """Turso configuration or client failure (never carries secrets)."""


def _connect_libsql(url: str, token: str):
    """Open the real client — the only place the package is imported."""
    try:
        import libsql_experimental as libsql
    except ImportError as exc:  # pragma: no cover — exercised via monkeypatch
        raise TursoError(
            "the libsql client is not installed; install libsql-experimental "
            "(see requirements-render.txt / docs/DEPLOY-RENDER.md) or unset "
            "RING_TURSO_URL and RING_TURSO_TOKEN to use the file stores"
        ) from exc
    return libsql.connect(url, auth_token=token)


def connect_turso(url: str, token: str, *, connector=_connect_libsql):
    """Return a locked DBAPI connection to the Turso database.

    ``connector`` (url, token) -> raw connection is injectable for tests;
    production uses the lazy ``libsql-experimental`` import. The URL and
    token are credentials-shaped: they are passed to the connector and
    never appear in errors or logs.
    """
    if not url or not token:
        raise TursoError(
            "RING_TURSO_URL and RING_TURSO_TOKEN must be set together; "
            "unset both to use the local file stores"
        )
    return _LockedConnection(connector(url, token))


class _LockedConnection:
    """DBAPI subset (``execute``/``commit``/``close``) over any
    sqlite3-shaped backend, serialized by one lock.

    The ASGI adapter runs handlers on worker threads while the stores are
    built on the main thread; libsql's client is not documented thread-
    safe the way sqlite3 is. Single-writer skeleton, same as the file
    stores. ``execute`` always passes parameters explicitly — some
    backends reject the bare ``execute(sql)`` form.
    """

    def __init__(self, raw):
        self._raw = raw
        self._lock = threading.RLock()

    def execute(self, sql: str, params=()):
        with self._lock:
            return self._raw.execute(sql, params)

    def commit(self) -> None:
        with self._lock:
            self._raw.commit()

    def close(self) -> None:
        with self._lock:
            self._raw.close()


@dataclass
class TursoTokenStore:
    """Same save/load contract as ``oauth.TokenStore``, single JSON row.

    When Turso is configured this store REPLACES the file one (the
    deployed disk is ephemeral, so the mint must land where it survives);
    the document persisted is identical — ``oauth.bundle_to_document`` —
    so a bundle saved by either backend loads from the other. Tokens are
    never logged, echoed, or included in exceptions.
    """

    connection: object  # DBAPI subset from connect_turso

    def __post_init__(self) -> None:
        self.connection.execute(_TOKENS_SCHEMA, [])
        self.connection.commit()

    def save(self, bundle: TokenBundle) -> None:
        self.connection.execute(
            "INSERT INTO tokens (id, bundle_json) VALUES (1, ?)"
            " ON CONFLICT(id) DO UPDATE SET bundle_json = excluded.bundle_json",
            [json.dumps(bundle_to_document(bundle))],
        )
        self.connection.commit()

    def load(self) -> TokenBundle | None:
        """Newest bundle, or None when absent/corrupt (reads never raise)."""
        row = self.connection.execute(
            "SELECT bundle_json FROM tokens WHERE id = 1", []
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):  # json.JSONDecodeError subclasses ValueError
            return None
        return bundle_from_document(payload)

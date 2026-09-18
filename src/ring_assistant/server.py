"""Optional FastAPI adapter exposing the pipeline at POST /webhooks/ring.

The library core is framework-agnostic; this module only maps HTTP to
``Pipeline.handle`` / ``Pipeline.handle_v1_1`` and back:

  SignatureError -> 401 (verify failed: wrong secret or tampered body)
  WebhookError / SchemaError -> 400 (well-signed but unusable payload)
  UnsupportedEvent -> 200 {"status": "ignored", ...} (live wire only)
  success -> 200 {"status": "accepted", ...}

Requires the ``server`` extra (fastapi, uvicorn). The classifier is
chosen at startup: LLM when the RING_LLM_* environment is fully
configured, rule stub otherwise — no secrets are ever hardcoded.

Run (after ``cp .env.example .env`` and setting RING_WEBHOOK_SECRET):

  uv run --extra server uvicorn ring_assistant.server:app --port 8000

Honest caveat: the handler runs classification + state + routing
inline; with the LLM classifier the ack may exceed Ring's <5s webhook
timeout. A queue in front of the pipeline is the production answer
(out of scope for this skeleton).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .capture import CAPTURE_FILE_NAME, WebhookRecorder
from .ingest import WebhookError
from .llm import build_classifier
from .oauth import OAuthError, TokenExchanger, TokenStore
from .replay import Pipeline
from .routing import Router
from .schema import SchemaError
from .settings import Settings
from .state import StateStore
from .turso import TursoTokenStore, connect_turso
from .verify import SignatureError
from .wire import UnsupportedEvent


def build_pipeline(
    settings: Settings,
    db_path: str | None = None,
    *,
    state_store: StateStore | None = None,
) -> Pipeline:
    """LLM classifier when fully configured, rule stub otherwise.

    Same composition point as every CLI (:func:`llm.build_classifier`):
    a configured endpoint wins, and endpoint trouble degrades to rules
    per event instead of failing the delivery. ``state_store`` injects a
    pre-built store (the Turso/libsql deploy path); default is the
    sqlite3 file store at ``db_path``.
    """
    classifier = build_classifier(settings)
    store = state_store if state_store is not None else StateStore(db_path or settings.db_path)
    router = Router.from_settings(settings)
    return Pipeline(
        secret=settings.webhook_secret,
        classifier=classifier,
        store=store,
        router=router,
        signature_header=settings.signature_header,
    )


def create_app(
    pipeline: Pipeline,
    *,
    live_wire: bool = False,
    recorder: WebhookRecorder | None = None,
    token_exchanger: TokenExchanger | None = None,
    token_store: TokenStore | TursoTokenStore | None = None,
) -> FastAPI:
    """``live_wire=True`` speaks Ring's documented webhook v1.1 contract
    (JSON:API envelope + ``X-Signature``) — the mode to point Ring's
    staging endpoint at; documented-but-ignored event types ack 200.
    The default accepts the flat internal contract (local replay and
    tests). Both share the pipeline downstream of the edge.

    ``recorder`` (built from ``RING_RECORD_DIR`` by ``build_default_app``)
    appends every delivery — accepted, ignored, or rejected — to a JSONL
    capture that ``replay-webhooks`` can re-run offline. Recording
    failures warn on stderr and never fail the delivery.

    ``token_exchanger`` (built from ``RING_CLIENT_ID``/``RING_CLIENT_SECRET``)
    turns the Token Exchange URL from a 501 stub into the real S2
    machine: the posted authorization code goes to ``oauth.ring.com``
    and the minted bundle persists via ``token_store`` when configured
    (``RING_TOKEN_STORE``). Tokens are never echoed in responses.
    """
    app = FastAPI(title="ring-delivery-assistant", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    # ---- portal endpoint stubs (staging tab; full machine is S2) ---------
    # The four URLs pasted into the Ring Developer Portal must answer over
    # public HTTPS. Only /webhooks/ring does real work today; these three are
    # honest stubs: a landing page, the account-link gate, and a 501 for the
    # OAuth redirect until the S2 token exchange lands.

    @app.get("/", response_class=HTMLResponse)
    def homepage() -> str:
        return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>ring-delivery-assistant</title></head><body style="font-family:system-ui">
<h1>ring-delivery-assistant</h1>
<p>Staging endpoints: <code>POST /webhooks/ring</code> (live wire, HMAC-signed),
<code>/oauth/callback</code> (token exchange), <code>/account-link</code> (pending).</p>
<p>Health: <code>/healthz</code></p></body></html>"""

    @app.get("/account-link", response_class=HTMLResponse)
    def account_link() -> str:
        # Honest gate: linking needs a Ring login, and Ring staging test users
        # require US-located devices under an active Protection plan — out of
        # scope for this build (docs/REGISTRATION-GUIDE.md §5).
        return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Account link — pending</title></head><body style="font-family:system-ui">
<h1>Account linking not enabled yet</h1>
<p>Ring account linking requires a Ring login, and staging logins need a
Ring account with US-located devices under an active Protection plan
(docs/REGISTRATION-GUIDE.md &sect;5). See docs/SUBMISSION.md for the demo
path.</p></body></html>"""

    @app.api_route("/oauth/callback", methods=["GET", "POST"])
    async def oauth_callback(request: Request) -> JSONResponse:
        # Token Exchange URL (portal staging tab). Ring's backend POSTs a
        # one-time authorization code (60 s lifetime) here backend-to-
        # backend; the documented inbound body shape is not pinned in the
        # API reference, so JSON {"code": ...} and form code=... are both
        # accepted, plus a GET ?code= for portal smoke checks. Tokens are
        # never echoed — only non-secret mint metadata.
        code = request.query_params.get("code")
        if request.method == "POST":
            body = await request.body()
            if body:
                content_type = request.headers.get("content-type", "")
                try:
                    if content_type.startswith("application/json"):
                        payload = json.loads(body.decode("utf-8"))
                        code = payload.get("code") if isinstance(payload, dict) else None
                    else:
                        values = parse_qs(body.decode("utf-8")).get("code", [])
                        code = values[0] if values else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return JSONResponse(
                        status_code=400,
                        content={"error": "bad_request", "detail": "unparseable body"},
                    )
        if not code:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "bad_request",
                    "detail": "no authorization code: POST JSON {code} or form "
                    "code=..., or GET ?code= (see docs/REGISTRATION-GUIDE.md)",
                },
            )
        if token_exchanger is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_configured",
                    "detail": "set RING_CLIENT_ID and RING_CLIENT_SECRET in .env "
                    "to enable the token exchange",
                },
            )
        try:
            # inline on purpose: the code dies 60 s after Ring mints it
            bundle = token_exchanger.exchange_code(code)
        except OAuthError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": "token_exchange_failed", "detail": str(exc)},
            )
        persisted = False
        if token_store is not None:
            token_store.save(bundle)
            persisted = True
        return JSONResponse(
            {
                "status": "token_exchanged",
                "token_type": bundle.token_type,
                "expires_in": bundle.expires_in,
                "expires_at": bundle.expires_at,
                "persisted": persisted,
            }
        )

    def _record(body: bytes, request: Request, outcome: str, status: int, note: str = "") -> None:
        if recorder is None:
            return
        recorder.record(
            body=body,
            headers=dict(request.headers),
            outcome=outcome,
            status=status,
            note=note,
        )

    @app.post("/webhooks/ring")
    async def receive(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            if live_wire:
                entry = pipeline.handle_v1_1(body, request.headers)
            else:
                entry = pipeline.handle(body, request.headers)
        except SignatureError as exc:
            _record(body, request, "rejected", 401, str(exc))
            raise HTTPException(status_code=401, detail=f"signature rejected: {exc}") from exc
        except UnsupportedEvent as exc:
            # Valid delivery of a type we ignore on purpose: 200, not a
            # 4xx — Ring treats 4xx as PERMANENT failure with no retry
            _record(body, request, "ignored", 200, exc.event_type)
            return JSONResponse({"status": "ignored", "event_type": exc.event_type})
        except (WebhookError, SchemaError) as exc:
            _record(body, request, "rejected", 400, str(exc))
            raise HTTPException(status_code=400, detail=f"bad payload: {exc}") from exc
        _record(body, request, "accepted", 200)
        return JSONResponse(
            {
                "status": "accepted",
                "event_id": entry.event.event_id,
                "intent": entry.intent.intent.value,
                "transition": entry.apply_result.transition.value,
                "action": entry.decision.action.value,
                "delivered": entry.delivered,
            }
        )

    return app


def _require_secret(settings: Settings) -> str:
    if not settings.webhook_secret:
        raise RuntimeError(
            "RING_WEBHOOK_SECRET is not set; refusing to start a verifier with no secret"
        )
    return settings.webhook_secret


def build_default_app() -> FastAPI:
    from .settings import load_env_file

    load_env_file()  # best-effort .env; real env vars always win
    settings = Settings.from_env()
    _require_secret(settings)
    # Registration-day switch: point RING_RECORD_DIR at a directory and
    # every live delivery lands in webhooks.jsonl for offline replay
    recorder = (
        WebhookRecorder(Path(settings.record_dir) / CAPTURE_FILE_NAME)
        if settings.record_dir
        else None
    )
    # The served app speaks the live v1.1 wire contract; set
    # RING_SIGNATURE_HEADER=x-signature for Ring traffic (see .env.example)
    # Token exchange goes live the moment both portal credentials exist;
    # until then the Token Exchange URL answers an honest 501.
    exchanger = (
        TokenExchanger(
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            timeout_s=settings.api_timeout_s,
        )
        if settings.client_id and settings.client_secret
        else None
    )
    # Deployed storage: Turso (libsql) when BOTH coordinates exist — Render's
    # disk is ephemeral, so the state DB and the minted bundle must live off
    # the instance. Half-configured fails closed; unset keeps local files.
    turso = None
    if settings.turso_url or settings.turso_token:
        if not (settings.turso_url and settings.turso_token):
            raise RuntimeError(
                "RING_TURSO_URL and RING_TURSO_TOKEN must be set together; "
                "unset both to use the local file stores"
            )
        turso = connect_turso(settings.turso_url, settings.turso_token)
    state_store = StateStore(connection=turso) if turso is not None else None
    store = (
        TursoTokenStore(turso)
        if turso is not None
        else TokenStore(Path(settings.token_store_path))
        if settings.token_store_path
        else None
    )
    return create_app(
        build_pipeline(settings, state_store=state_store),
        live_wire=True,
        recorder=recorder,
        token_exchanger=exchanger,
        token_store=store,
    )


def __getattr__(name: str):
    """Lazy ``app`` for ``uvicorn ring_assistant.server:app``.

    Built on first access so importing this module (docs, tests) never
    requires RING_WEBHOOK_SECRET — but serving does, loudly.
    """
    if name == "app":
        return build_default_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

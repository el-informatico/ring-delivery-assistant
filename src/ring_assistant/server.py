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

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .capture import CAPTURE_FILE_NAME, WebhookRecorder
from .ingest import WebhookError
from .llm import build_classifier
from .replay import Pipeline
from .routing import Router
from .schema import SchemaError
from .settings import Settings
from .state import StateStore
from .verify import SignatureError
from .wire import UnsupportedEvent


def build_pipeline(settings: Settings, db_path: str | None = None) -> Pipeline:
    """LLM classifier when fully configured, rule stub otherwise.

    Same composition point as every CLI (:func:`llm.build_classifier`):
    a configured endpoint wins, and endpoint trouble degrades to rules
    per event instead of failing the delivery.
    """
    classifier = build_classifier(settings)
    store = StateStore(db_path or settings.db_path)
    router = Router.from_settings(settings)
    return Pipeline(
        secret=settings.webhook_secret,
        classifier=classifier,
        store=store,
        router=router,
        signature_header=settings.signature_header,
    )


def create_app(
    pipeline: Pipeline, *, live_wire: bool = False, recorder: WebhookRecorder | None = None
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
    """
    app = FastAPI(title="ring-delivery-assistant", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

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
    return create_app(build_pipeline(settings), live_wire=True, recorder=recorder)


def __getattr__(name: str):
    """Lazy ``app`` for ``uvicorn ring_assistant.server:app``.

    Built on first access so importing this module (docs, tests) never
    requires RING_WEBHOOK_SECRET — but serving does, loudly.
    """
    if name == "app":
        return build_default_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

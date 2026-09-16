"""Optional FastAPI adapter exposing the pipeline at POST /webhooks/ring.

The library core is framework-agnostic; this module only maps HTTP to
``Pipeline.handle`` and back:

  SignatureError -> 401 (verify failed: wrong secret or tampered body)
  WebhookError / SchemaError -> 400 (well-signed but unusable payload)
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .classify import RuleBasedClassifier
from .ingest import WebhookError
from .llm import ConfigurationError, LLMClassifier
from .replay import Pipeline
from .routing import Router
from .schema import SchemaError
from .settings import Settings
from .state import StateStore
from .verify import SignatureError


def build_pipeline(settings: Settings, db_path: str | None = None) -> Pipeline:
    """LLM classifier when fully configured, rule stub otherwise."""
    try:
        classifier = LLMClassifier.from_env()
    except ConfigurationError:
        classifier = RuleBasedClassifier()
    store = StateStore(db_path or settings.db_path)
    router = Router.from_settings(settings)
    return Pipeline(
        secret=settings.webhook_secret,
        classifier=classifier,
        store=store,
        router=router,
        signature_header=settings.signature_header,
    )


def create_app(pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="ring-delivery-assistant", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/webhooks/ring")
    async def receive(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            entry = pipeline.handle(body, request.headers)
        except SignatureError as exc:
            raise HTTPException(status_code=401, detail=f"signature rejected: {exc}") from exc
        except (WebhookError, SchemaError) as exc:
            raise HTTPException(status_code=400, detail=f"bad payload: {exc}") from exc
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
    return create_app(build_pipeline(settings))


def __getattr__(name: str):
    """Lazy ``app`` for ``uvicorn ring_assistant.server:app``.

    Built on first access so importing this module (docs, tests) never
    requires RING_WEBHOOK_SECRET — but serving does, loudly.
    """
    if name == "app":
        return build_default_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

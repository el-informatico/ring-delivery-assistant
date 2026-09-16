# Usage — exact commands

Everything below runs offline. A recent `uv` (tested with 0.12) and
Python 3.12+ are the only prerequisites.

## Setup

```bash
git clone <repo> ring-delivery-assistant
cd ring-delivery-assistant
uv sync                          # core env — zero runtime dependencies
uv sync --extra server --dev     # + FastAPI/uvicorn extras and test deps
```

`uv sync` creates `.venv` and installs the project (editable) with no
runtime dependencies at all — the core is pure stdlib.

## Tests (offline, no network)

```bash
uv run pytest
# 184 passed, 2 warnings in ~1s   (warnings come from fastapi's own
#                                  testclient shim, not this codebase)
```

Per-area runs:

```bash
uv run pytest tests/test_verify.py     # HMAC gate
uv run pytest tests/test_schema.py     # payload normalization
uv run pytest tests/test_ingest.py     # webhook entry + error layering
uv run pytest tests/test_synth.py      # generator determinism
uv run pytest tests/test_classify.py   # rule-stub table
uv run pytest tests/test_llm.py        # LLM adapter (fake transport)
uv run pytest tests/test_state.py      # state machine transitions
uv run pytest tests/test_routing.py    # routing rules + sinks
uv run pytest tests/test_replay.py     # end-to-end pipeline
uv run pytest tests/test_wire.py       # live v1.1 wire contract (docs fixtures)
uv run pytest tests/test_server.py     # FastAPI adapter (TestClient)
uv run pytest tests/test_server_live.py # FastAPI adapter in live-wire mode
```

## Generate a synthetic event stream

```bash
uv run generate                       # default seed 20260914 -> .synth/
uv run generate --seed 7 --out /tmp/synth
```

Output: `events.jsonl` (12 Ring-shaped payloads in arrival order,
including an out-of-order pair and a duplicate redelivery) and
`snapshots/*.png` (six 96x64 fixture scenes). Same seed → byte-identical
output.

## Demo: end-to-end offline replay

```bash
uv run demo
uv run demo --seed 7        # different jitter, same shape
```

Signs every synthetic payload with the HMAC scheme, pushes it through
verify → normalize → classify (rule stub) → state → routing, and prints
a deterministic timeline plus a summary. Artifacts land in `.demo/`
(`events.jsonl`, `snapshots/`, `state.db`).

Secrets: the demo signs with `RING_WEBHOOK_SECRET` when set, otherwise
a clearly-labeled synthetic `demo-webhook-secret`.

## Optional webhook server

The served app speaks the **live Ring wire contract** (webhook v1.1
envelope, `X-Signature` header) — this is what you point Ring's staging
endpoint at. For flat internal-payload replay, build the app yourself:
`create_app(pipeline)` (no `live_wire`).

```bash
cp .env.example .env       # then set at least RING_WEBHOOK_SECRET
uv run --extra server uvicorn ring_assistant.server:app --port 8000
```

Endpoints:

- `POST /webhooks/ring` — raw body + `X-Signature` (or the header your
  `.env` names). `200` accepted, `200` ignored (documented event type
  this pipeline doesn't consume), `401` bad signature, `400`
  well-signed but unusable payload.
- `GET /healthz` — liveness.

A quick signed request against it, using a documented example payload:

```bash
uv run python - <<'PY'
import json, urllib.request
from pathlib import Path
from ring_assistant.verify import sign_payload
from ring_assistant.wire import encode_v1_1

payload = json.loads(Path("tests/fixtures/wire/button_press.json").read_text())
body = encode_v1_1(payload)
req = urllib.request.Request(
    "http://127.0.0.1:8000/webhooks/ring",
    data=body,
    headers={"Content-Type": "application/json",
             "X-Signature": sign_payload(body, "your-secret")},
)
print(urllib.request.urlopen(req).read().decode())
PY
```

The server refuses to start without `RING_WEBHOOK_SECRET` (fail-closed).
Classifier selection is automatic: LLM when the full `RING_LLM_*`
contract is in the environment, rule stub otherwise.

## Library use

```python
from ring_assistant.classify import RuleBasedClassifier
from ring_assistant.replay import Pipeline, format_timeline
from ring_assistant.routing import Router
from ring_assistant.settings import Settings
from ring_assistant.state import StateStore
from ring_assistant.synth import signed_webhooks, build_sequence

settings = Settings.from_env()
secret = settings.webhook_secret or "demo-webhook-secret"
pipeline = Pipeline(
    secret=secret,
    classifier=RuleBasedClassifier(),          # or LLMClassifier.from_env()
    store=StateStore("state.db"),
    router=Router.from_settings(settings),
)
for body, headers in signed_webhooks(build_sequence(), secret):
    entry = pipeline.handle(body, headers)
    print(entry.event.event_id, entry.decision.action.value)
```

For live Ring v1.1 envelopes, call `pipeline.handle_v1_1(body, headers)`
instead — same downstream path, different edge (`wire.py` adapts the
JSON:API envelope; documented-but-ignored event types raise
`UnsupportedEvent` for you to ack and drop).

## Environment contract

See `.env.example` — every knob (signature header name, LLM endpoint /
model / key / timeout, notification webhook URL, night hours, burst
threshold and window, DB path) is documented there and read only from
the environment. No secret is ever hardcoded or committed.

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
# 281 passed, 2 warnings in ~2.5s  (warnings come from fastapi's own
#                                  testclient shim, not this codebase)
```

Per-area runs:

```bash
uv run pytest tests/test_verify.py     # HMAC gate
uv run pytest tests/test_schema.py     # payload normalization
uv run pytest tests/test_ingest.py     # webhook entry + error layering
uv run pytest tests/test_synth.py      # generator determinism
uv run pytest tests/test_classify.py   # rule-stub table
uv run pytest tests/test_classify_snapshot.py # pixel rules over decoded snapshots
uv run pytest tests/test_snapshots.py  # PNG decoder + snapshot sources
uv run pytest tests/test_events_api.py # Events API client (mock transport)
uv run pytest tests/test_llm.py        # LLM adapter (fake transport)
uv run pytest tests/test_state.py      # state machine transitions
uv run pytest tests/test_routing.py    # routing rules + sinks
uv run pytest tests/test_replay.py     # end-to-end pipeline
uv run pytest tests/test_wire.py       # live v1.1 wire contract (docs fixtures)
uv run pytest tests/test_server.py     # FastAPI adapter (TestClient)
uv run pytest tests/test_server_live.py # FastAPI adapter in live-wire mode
uv run pytest tests/test_capture.py    # record/replay harness (JSONL captures)
uv run pytest tests/test_timeline.py   # the scripted-day demo + artifact
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

## Replay: documented or captured webhooks

```bash
uv run replay-webhooks                                  # the docs' own
                                                        # example envelopes
                                                        # (tests/fixtures/wire/)
uv run replay-webhooks .timeline/webhooks.jsonl         # a capture file
uv run replay-webhooks .timeline/webhooks.jsonl --snapshots .timeline
uv run replay-webhooks capture/webhooks.jsonl --include-rejected --db replay.db
```

One code path for both sources: `Pipeline.handle_v1_1`, the live-wire
edge. Fixtures are the documented example payloads; captures are what
the server records when `RING_RECORD_DIR` is set (see below) — same
signed-body shape, so registration day swaps the file, not the
command. Captures hold no secrets: replay re-signs each body with the
local secret (`RING_WEBHOOK_SECRET` or the demo key).

Classification is platform-fields-only by default; `--snapshots DIR`
points the classifier at the offline snapshot source in `DIR`
(`manifest.json` + images, the shape `uv run timeline` writes). With
it, replaying a timeline capture reproduces the original day row for
row. `--include-rejected` re-runs refused deliveries too (diagnosis
mode); `--db` keeps the derived state instead of throwing it away.

## Timeline: one scripted day, as an artifact

```bash
uv run timeline                 # writes .timeline/
uv run timeline --out /tmp/day
```

Plays a scripted Friday — deposit, van, ding, its own redelivery, a
second box refreshing the open track, pickup, the late straggler
deposit the pickup supersedes, a three-motion burst, a 22:41 ring —
through the live-wire edge with snapshot classification, and writes:

- `.timeline/timeline.md` — the artifact: timeline table (platform
  said vs snapshot said), the notifications that would have gone out,
  caveats;
- `.timeline/webhooks.jsonl` — the day as a capture (replay it, see
  above);
- `.timeline/manifest.json` + `snapshots/` — the offline snapshot
  source, keyed `<device>@<epoch_ms>` exactly like the Image
  Snapshots API;
- `.timeline/state.db` — the tracks the day derived (gitignored).

Regeneration is byte-identical: the artifact is diffable in review.

## Star metric: ding → notification, measured

```bash
uv run star-metric            # writes .star/star-metric.md
```

Plays the same scripted day with `perf_counter` timings per stage —
edge, classify, state, route, deliver — and prints the summary: N,
min/median/mean/p90 of ding → routed notification, plus the
classification stage alone. Classification runs the full multimodal
LLM call path (deterministic mock model until `RING_LLM_*` is
configured); delivery reaches a Telegram sink (recording transport
until `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` are configured —
README §Value layer has the 2-minute BotFather walkthrough). `.star/`
is gitignored: latencies belong to the machine that measured them.

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

Set `RING_RECORD_DIR` to capture live traffic as it arrives:

```bash
RING_RECORD_DIR=capture uv run --extra server uvicorn ring_assistant.server:app --port 8000
# every delivery (accepted, ignored, rejected) lands in
# capture/webhooks.jsonl — then, offline:
uv run replay-webhooks capture/webhooks.jsonl
```

Recording never fails a delivery: if the capture file cannot be
written the server warns on stderr and serves on.

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

Snapshot classification is the same kind of swap — one protocol
(`SnapshotSource`), two implementations:

```python
from pathlib import Path

from ring_assistant.classify import SnapshotRuleClassifier
from ring_assistant.events_api import RingApiClient
from ring_assistant.snapshots import ApiSnapshotSource, ManifestSnapshotSource

# offline today: manifest keyed <device>@<epoch_ms>, like the API
source = ManifestSnapshotSource(
    root=Path(".timeline"), manifest_path=Path(".timeline/manifest.json")
)
# registration day, when RING_API_TOKEN is set:
# source = ApiSnapshotSource(
#     client=RingApiClient(access_token=settings.api_token,
#                          base_url=settings.api_base_url)
# )
classifier = SnapshotRuleClassifier(source=source)
```

A missing or undecodable snapshot degrades to the platform-fields
classifier — an event is never failed for want of an image. JPEG bytes
(the real snapshot format) are refused by the stdlib rules decoder on
purpose: they belong to the LLM adapter (`llm.py` attaches them as
data URIs via the same `SnapshotSource`).

## Environment contract

See `.env.example` — every knob (signature header name, LLM endpoint /
model / key / timeout, notification webhook URL, night hours, burst
threshold and window, DB path, capture directory, Events API base
URL / token / timeout, Telegram bot token / chat id / timeout) is
documented there and read only from the environment. No secret is
ever hardcoded or committed.

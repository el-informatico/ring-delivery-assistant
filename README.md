# Ring Delivery & Safety Assistant

An **intent + routing layer** for Ring-style doorbell event streams, built
as an offline-first OSS library skeleton. Apache-2.0.

> **Positioning (read this first).** Ring already ships native Package
> Alerts and Person Alerts — this project is *not* another package
> detector. The gap it fills is everything *after* detection: what the
> event **means** across events (a deposit was later picked up), and
> **who gets told what** (package deposited → notify; stranger at the
> door at night → escalate; motion-only noise → suppressed unless it
> repeats). Detection is an input (`sub_type`), not the product.

## What is real vs synthetic

This repository is the **offline skeleton**: no Ring hardware, no real
credentials, no network calls in tests or the demo. Events come from a
deterministic synthetic generator (seeded); snapshots are generated PNG
fixtures; the HMAC secret is a test/demo key. The LLM adapter is real
code with a configurable endpoint, exercised in tests through an
injected fake transport — never a live model. The live **wire
contract**, however, is pinned, not invented: Ring's published webhook
v1.1 example payloads are fixtures (`tests/fixtures/wire/`, provenance
included) replayed through the same verify→…→routing path a real
delivery takes.

Real-ingestion readiness is built the same way — against documented
contracts, with the live swap isolated to configuration. The server can
**record** every delivery it receives (`RING_RECORD_DIR`) and
`replay-webhooks` re-runs those captures through the identical
live-wire edge, so registration day replaces the fixtures file, not the
code. The documented **Events API** (event history, image snapshots)
has a fail-closed client (`events_api.py`, Bearer auth) tested against
an offline mock transport; live use is one env var (`RING_API_TOKEN`).
Snapshot **classification** runs pixel rules over decoded PNGs keyed
`<device>@<epoch_ms>` — the API's own keying — with the offline
manifest source standing in for the live one; the first scripted-day
artifact lives in `.timeline/timeline.md`.

See `USAGE.md` for exact commands and `VALIDATION.md` for test/demo
output and honest caveats.

## Architecture

One webhook travels left to right through five small modules; the core
is framework-agnostic and dependency-free (stdlib only):

```
live Ring webhooks              synthetic / internal payloads
(v1.1 JSON:API + X-Signature)   (flat contract, X-Ring-Signature)
        │                                │
        ▼                                │
   wire.py ── the edge adapter           │
   JSON:API envelope -> flat payload,    │
   epoch-ms timestamp -> UTC datetime    │
        │                                │
        ▼                                ▼
   ingest.py ── verify.py    HMAC sha256 gate, then JSON + schema gates
        │                     (401 vs 400 error layering)
        ▼
   schema.py                 Ring payload -> frozen RingEvent (UTC-normalized)
        │
        ▼
   classify.py / llm.py      event -> Intent (protocol; rules stub,
        │                     pixel rules over decoded snapshots, or
        │                     OpenAI-compatible multimodal endpoint)
        ▼                     snapshots arrive via snapshots.py sources
   state.py                  append-only event log -> derived package
        │                     tracks (SQLite), survives out-of-order arrival
        ▼
   routing.py                intent + transition + burst count -> decision
        │                     (notify / escalate / suppress) -> sinks
        ▼
   LogSink | WebhookSink     delivery
```

- `wire.py` adapts Ring's **documented webhook v1.1 contract** at the
  edge (`X-Signature: sha256=<hex HMAC of the raw body>`, JSON:API
  envelope, epoch-ms timestamps): only the two intent-bearing event
  types (`motion_detected`, `button_press`) map inward; the other
  eight documented types raise `UnsupportedEvent` so the host acks
  HTTP 200 and ignores them (a 4xx would make Ring treat the delivery
  as a permanent failure). The original envelope rides along in
  `RingEvent.raw["wire"]`, uninterpreted.
- `synth.py` + `imaging.py` generate the deterministic storyboard
  (ding/motion, all `sub_type`s, out-of-order pair, duplicate
  redelivery) and its PNG snapshot fixtures.
- `replay.py` is the single pipeline used by the demo, the tests, and
  the optional FastAPI adapter (`server.py`, `[server]` extra) —
  `handle()` for the internal contract, `handle_v1_1()` for the live
  wire; everything downstream of the edge is shared.
- `capture.py` is the record/replay harness: the server appends every
  delivery (body, headers, outcome, status) to a JSONL capture;
  `replay-webhooks` feeds fixtures or captures through the same edge.
  Captures carry no secrets — replay re-signs locally — and recording
  can never fail a delivery.
- `events_api.py` + `snapshots.py` implement the documented Events API
  (Bearer auth, JSON:API event history, two-step pre-signed snapshot
  download) behind an injected transport, plus the offline sources that
  mirror its `(device, epoch-ms)` keying. Missing media degrades to
  event-only classification; misconfiguration raises.
- `timeline.py` plays a scripted day through the live-wire edge and
  writes the `.timeline/` artifact (see Quick start).
- `settings.py` defines the whole environment contract (see
  `.env.example`); nothing is hardcoded.

## Routing rules

The rules table is the product's judgment, stated plainly:

| Intent | State transition | Decision |
| --- | --- | --- |
| any | duplicate redelivery | **suppress** (idempotent) |
| package_deposited | track opened / refreshed | **notify** primary — "package at the door" |
| package_deposited | deposit superseded (late webhook) | **suppress** — track already closed |
| package_picked_up | track closed | **notify** primary — "package taken" |
| package_picked_up | orphan (no deposit on record) | **notify** with caveat |
| person_at_door | during night window (default 21:00–06:00) | **escalate** every sink, `critical` |
| person_at_door | daytime | **notify** primary |
| vehicle_at_door | — | **notify** secondary |
| motion_noise | < 3 in 10 min (default) | **suppress** with count |
| motion_noise | ≥ 3 in 10 min | **notify** once — burst digest |

Night hours, burst threshold/window, and the optional notification
webhook are environment-configurable (`RING_NIGHT_START`,
`RING_MOTION_BURST_COUNT`, `RING_NOTIFY_WEBHOOK_URL`, …).

## State machine

`state.py` keeps an append-only event log (arrival order) and derives
per-device **tracks** by replaying in `occurred_at` order on every
apply. Consequences:

- out-of-order webhooks cannot corrupt state: a deposit arriving after
  its pickup lands inside the already-closed track
  (`deposit_superseded` → suppressed), and a pickup arriving first is
  an `pickup_orphan` (notified with a caveat) that re-pairs once the
  deposit shows up;
- deposits within the open track refresh it; pickups pair only inside
  a 24 h window (`PAIRING_WINDOW`);
- event ids are idempotent — redelivery is a no-op.

## Quick start

```bash
uv sync                   # core: zero runtime dependencies
uv run pytest             # 281 tests, offline
uv run demo               # end-to-end timeline (deterministic)
uv run timeline           # scripted day -> .timeline/timeline.md
uv run replay-webhooks    # re-run documented fixtures or a capture
```

Details: `USAGE.md`. Validation evidence: `VALIDATION.md`.

## License

Apache-2.0 (see `LICENSE`).

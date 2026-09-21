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

## Live demo

**<https://ring-delivery-assistant.onrender.com>** — this server,
deployed on Render's free tier (it spins down after ~15 min idle, so
the first request may take a few seconds to wake). The webhook portal
(Ring's v1.1 wire contract, HMAC-verified), the OAuth token-exchange
endpoint, and the account-link gate are documented in
`docs/PORTAL-ENDPOINTS.md`; the $0 deploy runbook (Render + Turso) is
`docs/DEPLOY-RENDER.md`.

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

The **value layer** follows the same discipline: the multimodal LLM
classifier runs its full call path against a deterministic mock model
(a real endpoint is configuration, not code, with rules-based fallback
wrapping it), the Telegram sink speaks the real Bot API payload shape
through a recording transport until `TELEGRAM_BOT_TOKEN` +
`TELEGRAM_CHAT_ID` appear in `.env`, and the star metric — ding →
classification → routed notification — is measured, not estimated
(below).

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
        │                     OpenAI-compatible multimodal endpoint
        ▼                     with rules fallback; snapshots via snapshots.py)
   state.py                  append-only event log -> derived package
        │                     tracks (SQLite), survives out-of-order arrival
        ▼
   routing.py                intent + transition + burst count -> decision
        │                     (notify / escalate / suppress) -> sinks
        ▼
   LogSink | WebhookSink | TelegramSink   delivery
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
- `timeline.py` plays a scripted day (11 beats: deposit, redelivery,
  track refresh, pickup, a late straggler the pickup supersedes, motion
  burst, night ring) through the live-wire edge and writes the
  `.timeline/` artifact (see Quick start).
- `telegram.py` is the one routed sink a phone actually sees: Bot API
  `sendMessage`, severity-conditioned (info → silent push, critical →
  rings). Credentials live in `.env` only; absent credentials wire
  nothing, and offline runs substitute a sink whose network leg is a
  recorder — same formatting, payload, and response parsing.
- `star.py` measures the star metric (below) over the scripted day.
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

The scripted day in `.timeline/timeline.md` exercises every row of
that machine on one real sequence, including the awkward ones: a
second box refreshing the open track, and the 10:31 deposit whose
webhook delivery failed upstream and finally landed at 13:20 — after
the pickup closed the track — so the machine answers
`deposit_superseded` and routing stays quiet instead of re-alarming
for a parcel that already left.

## Quick start

```bash
uv sync                   # core: zero runtime dependencies
uv run pytest             # 354 tests, offline
uv run demo               # end-to-end timeline (deterministic)
uv run timeline           # scripted day -> .timeline/timeline.md
uv run star-metric        # measured ding -> notification latencies
uv run replay-webhooks    # re-run documented fixtures or a capture
```

Details: `USAGE.md`. Validation evidence: `VALIDATION.md`.

## Value layer (S3)

Three pieces, each live-configurable and offline-runnable:

**Multimodal LLM classifier, rules fallback included.**
`llm.py` speaks any OpenAI-compatible `/chat/completions` endpoint and
attaches the event's snapshot as a base64 data URI. Offline, the full
call path (request assembly, image encoding, response parsing) runs
against `mock_vision_transport` — a deterministic stand-in that reads
the attached PNG with the same decoder and pixel ladder as the rules
path. `FallbackClassifier` wraps the LLM so a transport failure,
timeout, or unusable reply degrades the ONE event to the rules verdict
(`source: "fallback:rules"` — visible, never silent). The swap is
configuration:

```bash
# .env — then every entry (server, CLIs) uses the LLM
RING_LLM_ENDPOINT=https://api.example.com/v1
RING_LLM_MODEL=vision-1
RING_LLM_API_KEY=...
```

**Telegram notification sink.** Routed notifications reach one sink a
phone actually sees. To go live (2 minutes, no code):

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
   → follow the prompts → copy the bot token.
2. Send any message to your new bot (it cannot initiate chats).
3. Look up your numeric chat id (any "get my id" bot, or the Bot API's
   `getUpdates`).
4. Put both in `.env` (gitignored — never chat, never commit):

   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```

`uv run star-metric` then delivers to the real Bot API. Without the
credentials the sink's network leg is a recorder: message formatting,
payload assembly (`disable_notification` = true unless severity is
critical — info arrives silently, night escalations ring), and Bot API
response parsing all still execute, offline and deterministically.

**Star metric — measured, not estimated.** `uv run star-metric` plays
the scripted day (N = 11 events, all through the live v1.1 edge) with
`perf_counter` timings per stage and writes `.star/star-metric.md`
(gitignored — latencies belong to the machine that measured them).
One measured offline run (WSL2, Python 3.13, no sockets):

| id | intent | action | edge ms | classify ms | state ms | route ms | deliver ms | total ms |
|---|---|---|---|---|---|---|---|---|
| s3-001 | package_deposited ★ | notify | 0.087 | 2.691 | 3.590 | 0.081 | 0.046 | 6.495 |
| s3-002 | vehicle_at_door ★ | notify | 0.044 | 2.747 | 3.743 | 0.058 | 0.047 | 6.638 |
| s3-003 | person_at_door ★ | notify | 0.033 | 2.627 | 3.447 | 0.037 | 0.026 | 6.169 |
| s3-004 | person_at_door | suppress | 0.021 | 2.735 | 0.043 | 0.023 | 0.000 | 2.823 |
| s3-005 | package_deposited ★ | notify | 0.023 | 1.964 | 3.668 | 0.049 | 0.039 | 5.742 |
| s3-006 | package_picked_up ★ | notify | 0.035 | 2.295 | 3.965 | 0.045 | 0.033 | 6.373 |
| s3-007 | package_deposited | suppress | 0.025 | 2.805 | 3.637 | 0.047 | 0.000 | 6.515 |
| s3-008 | motion_noise | suppress | 0.034 | 2.035 | 3.400 | 0.065 | 0.000 | 5.535 |
| s3-009 | motion_noise | suppress | 0.031 | 2.744 | 4.556 | 0.083 | 0.000 | 7.413 |
| s3-010 | motion_noise ★ | notify | 0.039 | 2.058 | 3.620 | 0.053 | 0.038 | 5.808 |
| s3-011 | person_at_door ★ | escalate | 0.029 | 2.167 | 4.388 | 0.069 | 0.034 | 6.687 |

**N = 11 events, 7 routed notifications, 7 Telegram `sendMessage`
calls; ding → routed notification: min 2.823 ms · median 6.373 ms ·
mean 6.018 ms · p90 6.687 ms. Classification stage alone: median
2.627 ms.** ★ marks the events a routed notification left the sinks
for. What is live vs mock in that run: classification ran the full
multimodal call path against the deterministic mock model (no socket;
a real endpoint adds its RTT), and delivery ran the real Telegram sink
against the recording transport (no POST; a live chat adds the Bot API
RTT). The two suppressed deposits show the value layer earning its
keep: the redelivery (s3-004) dedupes in 2.8 ms total, and the
superseded straggler (s3-007) costs no notification at all.

**The same day, live delivery (2026-09-18).** With Telegram credentials
in `.env`, `uv run star-metric` POSTs to the real Bot API — 7 messages
reached a phone. One measured live run (WSL2, Python 3.13;
classification still the mock vision model, no LLM endpoint configured):

| id | intent | action | edge ms | classify ms | state ms | route ms | deliver ms | total ms |
|---|---|---|---|---|---|---|---|---|
| s3-001 | package_deposited ★ | notify | 0.090 | 2.534 | 3.417 | 0.098 | 704.192 | 710.330 |
| s3-002 | vehicle_at_door ★ | notify | 0.094 | 2.404 | 3.789 | 0.163 | 867.455 | 873.905 |
| s3-003 | person_at_door ★ | notify | 0.061 | 2.234 | 4.721 | 0.191 | 694.273 | 701.481 |
| s3-004 | person_at_door | suppress | 0.041 | 3.748 | 0.089 | 0.039 | 0.000 | 3.917 |
| s3-005 | package_deposited ★ | notify | 0.032 | 2.315 | 4.092 | 0.080 | 690.600 | 697.118 |
| s3-006 | package_picked_up ★ | notify | 0.060 | 2.553 | 5.339 | 0.177 | 699.047 | 707.176 |
| s3-007 | package_deposited | suppress | 0.050 | 2.284 | 4.148 | 0.085 | 0.001 | 6.568 |
| s3-008 | motion_noise | suppress | 0.051 | 2.275 | 20.022 | 0.103 | 0.001 | 22.451 |
| s3-009 | motion_noise | suppress | 0.055 | 2.274 | 4.297 | 0.098 | 0.001 | 6.725 |
| s3-010 | motion_noise ★ | notify | 0.055 | 2.466 | 4.237 | 0.096 | 703.736 | 710.590 |
| s3-011 | person_at_door ★ | escalate | 0.040 | 2.421 | 4.673 | 0.420 | 717.107 | 724.660 |

**N = 11 events, 7 routed notifications, 7 live Telegram `sendMessage`
deliveries; ding → routed notification: min 3.917 ms · median
701.481 ms · mean 469.538 ms · p90 724.660 ms. Classification stage
alone: median 2.404 ms.** The offline table above stays deliberately:
the two runs differ almost entirely in the deliver leg, where the Bot
API round trip (~0.7 s median) now dominates — the pipeline's own
stages (edge → route) sit in the same millisecond band either way, and
the suppressed rows (s3-004, s3-007–s3-009) still cost no delivery at
all.

## License

Apache-2.0 (see `LICENSE`).

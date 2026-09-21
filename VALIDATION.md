# Validation — real outputs, honest caveats

Everything here was produced on 2026-09-16 by the commands in
`USAGE.md`, in this repository, offline (no network). The test-run
capture below was re-run at the 2026-09-21 pre-publication sync —
the suite had grown and the docs lagged (323 → 354, synced the same
day).

## Test run

```
$ uv run pytest
354 passed, 2 warnings in 3.95s
```

The two warnings come from `fastapi`'s own testclient shim
(`StarletteDeprecationWarning` about `httpx`, inside `.venv`), not from
this codebase.

Breakdown by area (collected test counts):

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_verify.py` | 10 | HMAC sign/verify: scheme, constant-time compare, refusal on empty secret |
| `tests/test_schema.py` | 29 | payload normalization, lenient/strict fields, contract violations |
| `tests/test_ingest.py` | 9 | webhook entry, header lookup, 401-vs-400 error layering |
| `tests/test_synth.py` | 14 | determinism, storyboard shape, out-of-order pair, duplicate, PNG fixtures |
| `tests/test_classify.py` | 21 | total rule table incl. `ding`+`vehicle`, context transparency, shared pixel ladder + platform verdict helpers |
| `tests/test_llm.py` | 26 | adapter request shape, data-URI snapshot, prose/JSON parsing, failure modes, deterministic mock vision transport, `FallbackClassifier` degrade, `build_classifier` matrix — all via fake/mock transports, zero sockets |
| `tests/test_state.py` | 14 | every transition, out-of-order both directions, window expiry, persistence, scrambled arrival |
| `tests/test_routing.py` | 24 | every rule, night escalation, burst digest, sinks, settings wiring |
| `tests/test_replay.py` | 8 | end-to-end storyboard, bad-signature isolation, timeline determinism, demo parity |
| `tests/test_wire.py` | 34 | live v1.1 contract: docs-fixture adaptation, epoch-ms→UTC, UnsupportedEvent vs SchemaError, raw-bytes signature binding, duplicate redelivery, full offline path to routing |
| `tests/test_server.py` | 9 | FastAPI adapter: 200/401/400 mapping, idempotent duplicate, fail-closed startup |
| `tests/test_server_live.py` | 5 | FastAPI adapter in live-wire mode: v1.1 accept, ignored-type acks 200, 401/400, default app serves v1.1 |
| `tests/test_events_api.py` | 25 | documented Events API client: Bearer auth fail-closed, event-history pagination + `event_types` filter, two-step snapshot download (303 → pre-signed GET), documented error codes — all via injected mock transport |
| `tests/test_snapshots.py` | 23 | stdlib PNG decoder (filters 0–4, verbatim pixels, CRC, truncation, profile limits) + snapshot sources: manifest keying, API degrade-vs-raise split, composition |
| `tests/test_classify_snapshot.py` | 20 | snapshot classifier: six-scene calibration pin, night-wheel collision gate, threshold margins, degradation paths, LLM source priority, manifest/API end-to-end |
| `tests/test_capture.py` | 17 | record/replay harness: JSONL round-trip, never-crash replay loop, original `received_at`, local re-signing of foreign captures, server-side recording of every outcome, recorded-traffic-replays-offline |
| `tests/test_timeline.py` | 14 | the scripted day: per-beat outcomes, redelivery dedupe, track refresh, superseded straggler, burst digest on the third, night escalation, artifact contents, capture replay parity, byte-identical regeneration |
| `tests/test_telegram.py` | 13 | Telegram sink: formatting markers, `sendMessage` payload, severity-conditioned `disable_notification`, Bot API ok/refused/unparseable answers, missing-creds gating, router wiring both groups, escalation reaches the sink |
| `tests/test_star.py` | 8 | star metric: N ≥ 10, every stage measured, parity with the rules timeline, telegram severity conditioning end to end, injected live transport, summary statistics, artifact contents, CLI |

## Demo run

```
$ uv run demo
```

Captured output (byte-identical across repeated runs — verified with
`diff` on two consecutive invocations):

```
ring-delivery-assistant offline demo
seed: 20260914   events: 12   source: .demo/events.jsonl
signature secret: synthetic (demo-webhook-secret)
classifier: rules (stub)   sinks: log

occurred              event     signal                              intent                action    transition          reason
----------------------------------------------------------------------------------------------------------------------------------------
2026-09-14 07:52:13  evt-001   motion_detected                     motion_noise          SUPPRESS  no_track_change     lone unclassified motion (1/3 in window)
2026-09-14 07:57:19  evt-002   motion_detected                     motion_noise          SUPPRESS  no_track_change     lone unclassified motion (2/3 in window)
2026-09-14 08:01:14  evt-003   motion_detected                     motion_noise          NOTIFY    no_track_change     intent=motion_noise
2026-09-14 09:14:11  evt-004   ding/package_delivery               package_deposited     NOTIFY    track_opened        intent=package_deposited
2026-09-14 09:15:38  evt-005   motion_detected/vehicle             vehicle_at_door       NOTIFY    no_track_change     intent=vehicle_at_door
2026-09-14 12:41:00  evt-006   ding/package_pickup                 package_picked_up     NOTIFY    track_closed        intent=package_picked_up
2026-09-14 16:50:11  evt-007   motion_detected/human               person_at_door        NOTIFY    no_track_change     intent=person_at_door
2026-09-14 21:37:09  evt-008   ding/human                          person_at_door        ESCALATE  no_track_change     intent=person_at_door night=True -> escalate all sinks
2026-09-15 11:52:26  evt-009   ding/package_pickup                 package_picked_up     NOTIFY    pickup_orphan       intent=package_picked_up
2026-09-15 11:47:06  evt-010   motion_detected/package_delivery    package_deposited     SUPPRESS  deposit_superseded  deposit evt-010 landed in track already closed by pickup evt-009 (arrived out of order)
2026-09-15 15:20:17  evt-011   motion_detected                     motion_noise          SUPPRESS  no_track_change     lone unclassified motion (1/3 in window)
2026-09-14 09:14:11  evt-004   ding/package_delivery               package_deposited     SUPPRESS  duplicate           event evt-004 already applied

summary
  events processed: 12
  notify: 6   escalate: 1   suppress: 5
  escalations (person at night): 1
  notifications -> log: 7

artifacts: events.jsonl, snapshots/, state.db (in the --out directory)
```

Rows worth reading closely:

- `evt-009` vs `evt-010`: the pickup webhook *arrives first* (received
  11:52:30 vs 11:53:47) even though the deposit *happened first*
  (11:47:06 vs 11:52:26). The state machine pairs them correctly: the
  orphan pickup notifies with a caveat, then the late deposit is
  suppressed as superseded instead of announcing "package deposited"
  for a package already taken.
- the final `evt-004` row (timestamped day 1 because it is the same
  event) is the duplicate redelivery: suppressed, pipeline state
  unchanged.

## Timeline run (S2/S3: scripted day, real-ingestion path)

```
$ uv run timeline
```

Captured output (the same `format_timeline` the demo prints; trimmed
to the notifications and summary — the full table is below under
Replay parity):

```
  [info    ] 08:03:21 Package deposited: ava1.ring.device.door001: a package was left at the door.
  [info    ] 08:04:02 Vehicle at the door: ava1.ring.device.door001: a vehicle is at the door.
  [info    ] 09:12:47 Person at the door: ava1.ring.device.door001: someone is at the door.
  [info    ] 10:26:53 Package deposited: ava1.ring.device.door001: a package was left at the door.
  [info    ] 12:47:05 Package picked up: ava1.ring.device.door001: the package was taken from the door.
  [info    ] 13:38:41 Repeated motion: ava1.ring.device.door001: 3 unclassified motions in the last 10 min — worth a look.
  [critical] 22:41:09 Person at the door at night: ava1.ring.device.door001: person detected at 22:41 (night window).

11 deliveries, 7 notifications
artifact: .timeline/timeline.md
capture:  .timeline/webhooks.jsonl
replay:   uv run replay-webhooks .timeline/webhooks.jsonl --snapshots .timeline
```

The artifact is committed at `.timeline/timeline.md` (full table with
per-row "platform said vs snapshot said", notifications, caveats).
Rows worth reading closely: 08:03:21 — on the wire it is bare
`motion_detected` (no sub_type), and only the snapshot (a package on
the mat) makes it a deposit that opens a track; 10:26:53 — a second
box refreshes that open track; and 10:31:18 — the straggler deposit
whose webhook delivery failed upstream and finally landed at 13:20,
after the 12:47 pickup closed the track, so the state machine answers
`deposit_superseded` and no notification fires. Regeneration is
byte-identical (`sha256sum` across two runs, checked), so the artifact
is diffable in review.

### Replay parity

```
$ uv run replay-webhooks .timeline/webhooks.jsonl --snapshots .timeline
occurred              event     signal                              intent                action    transition          reason
----------------------------------------------------------------------------------------------------------------------------------------
2026-05-15 08:03:21  ava1.ring.device.door001_motion_detected_1778832201210  motion_detected                     package_deposited     NOTIFY    track_opened        intent=package_deposited
2026-05-15 08:04:02  ava1.ring.device.door001_motion_detected_1778832242805  motion_detected/vehicle             vehicle_at_door       NOTIFY    no_track_change     intent=vehicle_at_door
2026-05-15 09:12:47  ava1.ring.device.door001_button_press_1778836367338  ding                                person_at_door        NOTIFY    no_track_change     intent=person_at_door
2026-05-15 09:12:47  ava1.ring.device.door001_button_press_1778836367338  ding                                person_at_door        SUPPRESS  duplicate           event ava1.ring.device.door001_button_press_1778836367338 already applied
2026-05-15 10:26:53  ava1.ring.device.door001_motion_detected_1778840813640  motion_detected                     package_deposited     NOTIFY    track_refreshed     intent=package_deposited
2026-05-15 12:47:05  ava1.ring.device.door001_motion_detected_1778849225062  motion_detected/human               package_picked_up     NOTIFY    track_closed        intent=package_picked_up
2026-05-15 10:31:18  ava1.ring.device.door001_motion_detected_1778841078412  motion_detected                     package_deposited     SUPPRESS  deposit_superseded  deposit ava1.ring.device.door001_motion_detected_1778841078412 landed in track already closed by pickup ava1.ring.device.door001_motion_detected_1778849225062 (arrived out of order)
2026-05-15 13:30:00  ava1.ring.device.door001_motion_detected_1778851800500  motion_detected   motion_noise          SUPPRESS  no_track_change     lone unclassified motion (1/3 in window)
2026-05-15 13:34:18  ava1.ring.device.door001_motion_detected_1778852058120  motion_detected   motion_noise          SUPPRESS  no_track_change     lone unclassified motion (2/3 in window)
2026-05-15 13:38:41  ava1.ring.device.door001_motion_detected_1778852321977  motion_detected   motion_noise          NOTIFY    no_track_change     intent=motion_noise
2026-05-15 22:41:09  ava1.ring.device.door001_button_press_1778884869104  ding                                person_at_door        ESCALATE  no_track_change     intent=person_at_door night=True -> escalate all sinks

replayed 11 delivery(ies): 11 accepted, 0 ignored, 0 rejected
```

The capture (received order) reproduces the day row for row against
the manifest snapshot source, including the straggler's
`deposit_superseded` row — pinned by
`tests/test_timeline.py::test_capture_replays_to_the_same_day`, so the
replay engine and the scripted day cannot drift apart: they share one
edge and one state machine. Without `--snapshots`, the same capture
classifies from platform fields only — 08:03 reads as `motion_noise`,
honestly demonstrating what the snapshot contributes.

## Star metric run (S3: ding → classification → routed notification)

```
$ uv run star-metric
classifier: mock vision transport (no socket) · sink: mock Telegram transport (no socket)

N = 11 events · ding -> routed notification: min 3.556 ms · median 6.172 ms · mean 6.008 ms · p90 6.961 ms
classification stage: median 2.655 ms
artifact: .star/star-metric.md
```

Same 11 signed wire deliveries as the timeline run, but through the
LLM call path (deterministic mock model offline — verdicts match the
pixel rules, pinned by `test_star_run_agrees_with_the_rules_timeline`)
and a Telegram sink (recording transport offline), with `perf_counter`
timings per stage. Timings vary run to run and belong to this machine;
README §Value layer publishes one representative run's full
per-event table. `.star/` is gitignored for exactly that reason.

## Server (HTTP contract)

Three levels of evidence:

1. **HTTP contract (in-process, full ASGI stack):** the nine tests in
   `tests/test_server.py` drive the app through `TestClient` — real
   routing, real status-code mapping (`200` accepted / `401` bad
   signature / `400` unusable payload), idempotent duplicate handling,
   and the fail-closed no-secret startup. All pass offline.
2. **Documented launch command:** `uv run --extra server uvicorn
   ring_assistant.server:app` starts cleanly with
   `RING_WEBHOOK_SECRET` set — captured log:

   ```
   INFO:     Started server process [472975]
   INFO:     Application startup complete.
   INFO:     Uvicorn running on http://127.0.0.1:8137 (Press CTRL+C to quit)
   ```

   A socket-level probe (curl from the same shell) was **not** possible
   in this development sandbox: it blocks loopback connections from
   sandboxed processes. That is an environment limitation, not a
   server one — the in-process tests above are the HTTP-contract
   evidence.

3. **Live wire contract (in-process, full ASGI stack):** the five tests
   in `tests/test_server_live.py` drive the served app
   (`build_default_app()` → `live_wire=True`) with Ring's **published
   v1.1 example payloads** (`tests/fixtures/wire/`, provenance in
   `PROVENANCE.md` there): signed accept, ignored-type acks
   `200 {"status": "ignored"}`, bad signature 401, garbage 400.
   Together with `tests/test_wire.py` (34 tests) this pins the whole
   documented edge — signature scheme, envelope shape, epoch-ms time,
   retry-idempotency — against the docs' own examples, offline.

## `ring-intent` adoption verdict

**Not adopted — the package does not exist (yet).**

Checked 2026-09-16: `GET https://pypi.org/pypi/ring-intent/json` returns
HTTP 404 (network access confirmed working — the probe itself succeeded).
`ring-intent` is the library this project's evaluation describes as a
planned deliverable, not an installable dependency.

Consequence, per the fallback plan: the intent layer here is
self-contained and dependency-free (`classify.py` protocol + rules stub,
`llm.py` adapter). This repository is effectively the seed of that
library. If `ring-intent` is ever published, the integration point is a
single `IntentClassifier` implementation class — nothing else in the
pipeline would need to change.

## Honest caveats

What this skeleton does NOT establish:

1. **Internal events are synthetic; the wire contract is documented,
   not observed.** The storyboard comes from `synth.py`; the v1.1
   fixtures are Ring's *published example payloads* (docs pages, cited
   per fixture in `tests/fixtures/wire/PROVENANCE.md`). No live Ring
   delivery has ever been received — registration is still pending —
   so real-world variance beyond the docs (extra fields, timing skew,
   undocumented event types) is unmeasured. The schema tolerates
   unknown fields (preserved in `raw`, never interpreted) and the edge
   degrades unknown event types to ack-and-ignore by design.
2. **Signature scheme and header pinned to the docs, unverified
   against a live sender.** `X-Signature: sha256=<hex HMAC-SHA256 of
   the raw body>` is exactly as documented
   (developer.amazon.com/docs/ring/api-documentation.html, "Webhook
   Authentication & Verification"); the header name stays configurable
   (`RING_SIGNATURE_HEADER`) so a divergence costs a config change,
   not code. One pinned docs quirk: the motion example's
   `timestamp_readable` is a local-time rendering (5 h off UTC) — the
   adapter parses only the epoch-ms `timestamp`, which the docs
   designate for all time calculations.
3. **The LLM adapter has never talked to a live model.** Tests exercise
   it fully (request shape, multimodal data-URI attachment, response
   parsing, failure modes) through an injected fake transport. Prompt
   quality against a real vision model is unmeasured.
4. **Sync handler vs Ring's <5 s ack budget.** `POST /webhooks/ring`
   runs verify→classify→state→routing inline. With the rule stub this
   is sub-millisecond; with a remote LLM it can exceed the budget. The
   production answer is accepting-then-queueing, deliberately out of
   scope for this skeleton.
5. **SQLite assumptions.** Single writer, no concurrent access, and
   `check_same_thread=False` only because the ASGI event loop differs
   from the builder thread. Not a multi-process deployment store.
6. **No track auto-expiry.** An open track with no pickup stays open
   forever (`PAIRING_WINDOW` bounds pairing, not cleanup). A retention
   job is future work.
7. **Idempotency is event-id based only.** No content-level dedupe of
   distinct events describing the same physical moment beyond the
   24 h pairing window.
8. **HMAC comparison is constant-time; replay protection is not
   implemented.** A captured valid body+signature pair replays within
   the duplicate-id guard only if the same `id`; genuine replay
   protection needs timestamp-nonce state.
9. **Snapshot pixel rules are fixture-calibrated, not field-calibrated.**
   The thresholds (box ≥ 2.5% of frame, person ≥ 0.5%, vehicle body
   ≥ 5% AND wheels ≥ 1%) were measured against the six deterministic
   synthetic scenes. Real Ring snapshots are watermarked JPEG — the
   stdlib decoder refuses them by design, routing to the LLM adapter
   — so the rules path's accuracy on real imagery is unmeasured until
   live snapshots exist. The product insight stands regardless: the
   documented motion sub_type vocabulary has no `package_delivery` on
   the live wire, so SOME image consumer is required for package
   intents from live traffic.
10. **The Events API contract is documented, not observed.** The client
   (`events_api.py`) is tested against an injected mock transport
   honoring the documented shapes (Bearer auth, JSON:API history with
   `page[key]` cursor, 303-redirect pre-signed snapshot download,
   MEDIA_NOT_FOUND/RECORDING_NOT_READY/CORRUPT_RECORDING degrade
   codes). No request has ever been sent to `api.amazonvision.com`;
   `RING_API_TOKEN` is an input, and the token exchange that mints it
   (from `RING_CLIENT_ID`/`RING_CLIENT_SECRET`) is not built yet.
11. **Captures hold traffic data, not credentials.** Recorded
   signatures are informational (replay re-signs locally), but capture
   files do contain device ids, timestamps, and payload bodies. They
   are fine to keep on the recording host; do not publish them.
12. **Star-metric latencies exclude both network legs.** Offline, the
   LLM call path answers from the deterministic mock vision transport
   and Telegram delivery from a recording transport — no socket
   either way. The measured stages are this pipeline's own work
   (edge verify/normalize, request assembly + response parse, state,
   routing, sink formatting); a live endpoint adds its RTT on top,
   which no offline number here can honestly include. Timings are
   also machine-specific (WSL2, Python 3.13) and vary run to run —
   which is why `.star/` is gitignored and README §Value layer
   publishes one representative run, clearly labeled.

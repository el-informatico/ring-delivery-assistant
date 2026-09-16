# Validation — real outputs, honest caveats

Everything here was produced on 2026-09-16 by the commands in
`USAGE.md`, in this repository, offline (no network).

## Test run

```
$ uv run pytest
145 passed, 2 warnings in 1.22s
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
| `tests/test_classify.py` | 13 | total rule table incl. `ding`+`vehicle`, context transparency |
| `tests/test_llm.py` | 15 | adapter request shape, data-URI snapshot, prose/JSON parsing, failure modes — all via fake transport |
| `tests/test_state.py` | 14 | every transition, out-of-order both directions, window expiry, persistence, scrambled arrival |
| `tests/test_routing.py` | 24 | every rule, night escalation, burst digest, sinks, settings wiring |
| `tests/test_replay.py` | 8 | end-to-end storyboard, bad-signature isolation, timeline determinism, demo parity |
| `tests/test_server.py` | 9 | FastAPI adapter: 200/401/400 mapping, idempotent duplicate, fail-closed startup |

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

## Server adapter verification

Two levels of evidence:

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

1. **Synthetic data only.** All events are generated (`synth.py`); no
   Ring hardware or Ring API was ever contacted. Payload shapes follow
   the documented webhook style (`ding` / `motion_detected` with
   `sub_type`), but real payloads may have fields this schema rejects
   or ignores (unknown fields are preserved in `raw`, not interpreted).
2. **Signature header name is configurable, not pinned.** The HMAC
   scheme (`sha256=<hexdigest>`) matches the documented style; the
   exact header name a live Ring integration sends was not verified
   against hardware, hence `RING_SIGNATURE_HEADER` exists.
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

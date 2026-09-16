# STATUS — ring-delivery-assistant

Build log. Last entry: 2026-09-16 (S2 — real-ingestion readiness;
281 tests).

## S2 — real ingestion (readiness built, live swap pending the gate)

Goal: replace the synthetic-only event flow with REAL-ingestion
readiness — webhook replay, Events API adapter, real snapshot path —
so registration day is a plug-in, not a rewrite. The owner has NOT
completed portal registration (S1 Lane A still open), so everything
below is built against the documented contracts and exercised offline.

### What works offline, today

- [x] **Replay engine** (`capture.py` + `replay-webhooks` CLI): the
      served app records every delivery (body, headers, outcome,
      status) to JSONL under `RING_RECORD_DIR`; recording can never
      fail a delivery (OSError → stderr warning). Fixtures and
      captures are the SAME shape — signed v1.1 bodies through ONE
      edge (`Pipeline.handle_v1_1`) — so `replay_fixture_dir` today
      and `replay_capture_file` on live traffic are the same code
      path. Replay re-signs with the LOCAL secret (captures hold no
      secrets; a capture recorded under another host's secret still
      replays — tested) and replays preserve the ORIGINAL
      `received_at`, so dedupe and burst windows behave as they did
      live.
- [x] **Events API adapter** (`events_api.py`): fail-closed Bearer
      client for the documented surface — `GET /v1/users/me`, event
      history with `page[key]` cursor and the literal-comma
      `event_types` filter, and the two-step Image Snapshot download
      (POST at_timestamp → 303 + pre-signed Location → GET without
      Bearer → bytes + provenance headers). Documented no-media errors
      (MEDIA_NOT_FOUND 416 / RECORDING_NOT_READY 425 /
      CORRUPT_RECORDING 422) degrade; auth/transport/malformed raise.
      All tested against an offline mock transport (`tests/
      ring_api_mock.py`) — zero sockets anywhere in the suite.
- [x] **Snapshot path through the classifier** (`snapshots.py` +
      `SnapshotRuleClassifier`): stdlib PNG decode (filters 0–4,
      CRC-checked), `SceneEvidence` fractions, decision ladder
      (person+box → picked_up / box → deposited / person → person /
      body AND wheels → vehicle / empty → platform fallback). Sources
      keyed `(device_id, epoch_ms)` — the API's own keying, computed
      exactly (whole seconds + microseconds, never float truncation):
      `ManifestSnapshotSource` offline, `ApiSnapshotSource` live,
      `CompositeSnapshotSource` to mix. Missing/undecodable snapshots
      DEGRADE to the platform classifier — an event is never failed
      for want of an image. JPEG (the real format) is refused by the
      rules decoder on purpose and travels to `llm.py` as a data URI.
- [x] **First live timeline demo** (`timeline.py` → `uv run
      timeline`): a scripted Friday — deposit at 08:03 (wire says bare
      motion; the snapshot says package), van, bell, its own
      redelivery (DUPLICATE, suppressed), pickup closing the track,
      a 3-motion burst (2 suppress + digest), a 22:41 ring
      (ESCALATE, critical) — written to `.timeline/timeline.md` with
      the day also emitted as a replayable capture. Regeneration is
      byte-identical; the artifact is committed and diffable.
      `replay-webhooks --snapshots` reproduces the day row for row.
- [x] New knobs (`settings.py`, `.env.example`): `RING_RECORD_DIR`,
      `RING_API_BASE_URL`, `RING_API_TOKEN`, `RING_API_TIMEOUT` —
      all optional, empty = offline mode, secrets .env-only.
- **Suite: 281 tests, 0 failures (~2.5 s)** — 184 prior + 97 new
  (`test_events_api.py` 25, `test_snapshots.py` 23,
  `test_classify_snapshot.py` 20, `test_capture.py` 17,
  `test_timeline.py` 12), all offline.

### What plugs in on registration day (no rewrite)

1. `RING_WEBHOOK_SECRET` + `RING_SIGNATURE_HEADER=x-signature` in
   `.env` → live deliveries verify (scheme already byte-identical).
2. `RING_RECORD_DIR=capture` on the server → real traffic lands in
   the same JSONL; `uv run replay-webhooks capture/webhooks.jsonl`
   re-runs it — same command as the fixtures.
3. `RING_API_TOKEN` in `.env` → `ApiSnapshotSource(client=
   RingApiClient(...))` replaces the manifest source (one composition
   change, tested contract) and snapshots come from the documented
   download. Real snapshots are JPEG → the LLM path takes over from
   the pixel rules automatically (decoder refuses JPEG by design).
4. Still to build (S2 residue → next): the OAuth token exchange that
   MINTS `RING_API_TOKEN` from `RING_CLIENT_ID`/`RING_CLIENT_SECRET`,
   and the account-link stub going real.

Honest caveats live in `VALIDATION.md` (items 9–11): pixel-rule
thresholds are calibrated to the synthetic scenes, the Events API
contract is documented-not-observed (mock-tested, zero live calls),
and captures hold traffic data though never credentials.

## S1 — registration gate + documented-wire foundations

Due ≤ 19-Sep. Two lanes, worked in parallel so the human-dependent gate
never blocks the buildable part.

### Lane A — registration gate (HUMAN-DEPENDENT, critical path)

- [ ] Register at the Ring Developer Portal using
      `docs/REGISTRATION-GUIDE.md` — click-by-click, every claim
      URL-cited, budget ~30–45 min: Amazon developer account →
      identity verification (passport accepted; IDs from 249
      countries; issuing country ≠ residence is fine) → private app
      (no certification needed) → three credentials shown ONCE.
- What the repo needs from the human, and nothing else: paste the
      HMAC Signature Key into `.env` as `RING_WEBHOOK_SECRET` (never
      chat, never commit) and set `RING_SIGNATURE_HEADER=x-signature`.
      `RING_CLIENT_ID` / `RING_CLIENT_SECRET` are parked for S2.
- Kill condition (same-day fallback track): account rejected,
      identity verification exhausted (3 attempts), or the portal
      refuses the region. Research found no developer-region
      restriction and no registration fee — expectation: gate passes.
- Blocked on the human ONLY for the portal visit itself. Everything
      below already works and keeps working without it.

### Lane B — documented-wire foundations (AUTONOMOUS, done)

- [x] Simulator-target decision, with evidence: Ring publishes **no
      sandbox base URL and no console webhook simulator** — the API
      docs' Environment Configuration table (§10) is Production-only
      (`https://api.amazonvision.com`; staging means YOUR registered
      endpoints in the portal's staging tab), and the testing docs
      point at curl/Postman against your own server ("There is no
      official Ring Partner SDK"). The glossary names a "Sandbox
      Environment" but no URL or activation path appears anywhere in
      the docs. Staging *users* need a Ring account on an active Ring
      Protection plan with US-located devices (develop.html#test) —
      this owner has no Ring hardware. Integration target: the
      **documented webhook v1.1 wire contract**, exercised by
      replaying the docs' own example payloads — sanctioned for the
      demo by the hackathon rules ("through a simulator or an actual
      Ring device you have for testing"). If registration reveals a
      console-side simulator after all, fixtures can switch to it
      without code changes: the adapter already speaks the documented
      contract.
- [x] Edge adapter `src/ring_assistant/wire.py`: v1.1 JSON:API
      envelope → internal flat contract. `X-Signature:
      sha256=<hex HMAC-SHA256 of the raw body>` — byte-identical
      scheme to the existing verifier, only the header name differs
      (configurable, as before). Epoch-ms `timestamp` → UTC datetime.
      Original envelope preserved uninterpreted in
      `RingEvent.raw["wire"]`. The two intent-bearing types
      (`motion_detected`, `button_press`) map inward; the other eight
      documented types raise `UnsupportedEvent` so the host acks HTTP
      200 and ignores them (a 4xx would read as PERMANENT failure to
      Ring); structural violations stay `SchemaError` → 400.
- [x] `Pipeline.handle_v1_1()` shares every step downstream of the
      edge with `handle()` — internal replay and live traffic cannot
      drift apart.
- [x] Server live-wire mode: `create_app(..., live_wire=True)`; the
      served app (`uvicorn ring_assistant.server:app`) speaks v1.1 and
      acks ignored types `200 {"status": "ignored"}`.
- [x] Fixtures are the docs' published example payloads (placeholders
      substituted, per-file provenance):
      `tests/fixtures/wire/motion_sub_type_motion.json`,
      `motion_sub_type_human.json`, `button_press.json`,
      `subscription_activated.json` + `PROVENANCE.md`.
- [x] Full offline path tested end to end: fixture → sign → verify →
      normalize → classify → state → routing — day person → notify,
      night ding → escalate, lone unclassified motion → suppress,
      3-in-10-min burst → one digest, duplicate redelivery →
      idempotent suppress.
- [x] Pinned docs quirk: the motion example's `timestamp_readable` is
      a LOCAL-time rendering (exactly 5 h off UTC); the adapter parses
      only the epoch-ms `timestamp`, which the docs designate for all
      time calculations.
- **Suite: 184 tests, 0 failures (~1.3 s)** — 145 prior + 39 new
      (`tests/test_wire.py` 34, `tests/test_server_live.py` 5), all
      offline.

## Honest caveats (S1)

- No live Ring delivery has ever been received. The wire contract is
  pinned to the *published documentation* (its own example payloads),
  not observed traffic; real-world variance is unmeasured until the
  registration gate passes and a live window exists.
- The four portal endpoints (webhook/token-exchange/account-link/
  homepage) need a publicly reachable HTTPS host; the guide covers
  tunnels. The three non-webhook endpoints are honest stubs until S2.
- `RING_CLIENT_ID` / `RING_CLIENT_SECRET` are declared in
  `.env.example` but consumed by nothing yet — S2 (OAuth token
  exchange, account linking) inputs.
- Sync handler vs Ring's <5 s ack budget is unchanged (VALIDATION.md
  caveat 4): fine with the rules stub, risky with a remote LLM.

## Next

- [ ] S1 gate: human runs `docs/REGISTRATION-GUIDE.md` (≤ 19-Sep)
- [ ] S2 residue: OAuth token exchange + account linking (needs S1
      client credentials; endpoint stubs become real; mints
      RING_API_TOKEN for the live snapshot source)
- [ ] S3: snapshot classification over the LLM adapter with real
      (watermarked JPEG) snapshots — the pixel rules stay as the
      offline/calibration path

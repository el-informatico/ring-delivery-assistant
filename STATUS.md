# STATUS — ring-delivery-assistant

Build log. Last entry: 2026-09-16 (S4 — submission readiness; suite
count synced 2026-09-21: 354 tests).

## S4 — submission readiness (everything but the human's two gates)

Goal: submission day (deadline 23-Oct-2026 12:00 PDT = 14:00 Lima) is
pure execution. Everything the repo can produce for Devpost now exists;
what remains is the human's portal registration (S1 Lane A, still
open), the video recording, and the publication-approval gate. Suite
green: **354 tests, 0 failures (~4 s)** (count re-verified 2026-09-21),
`uv lock
--check` clean, `uv sync --frozen` installs from the lock.

### Deliverables landed

- [x] **`docs/SUBMISSION.md`** — Devpost text, all sections (problem →
      built-with) in English, paste-ready, plus binding honesty rules:
      no live-traffic claims without credentials on screen, latency
      quotes keep their offline/mock qualifiers, the demo is named a
      simulator (rules-sanctioned, URL cited), no invented
      people/metrics. Numbers used: 354 tests; N = 11, median 6.373 ms
      ding → routed notification, classify median 2.627 ms (README's
      published run).
- [x] **`docs/VIDEO-SCRIPT.md`** — ~2:50 script (under the 3-min cap),
      390 words at 150 wpm, storyboard with timecodes; latency-counter
      moments mapped (M1 summary line, M2/M3 the artifact's total +
      classify columns, M4 optional live-server timer IF registered);
      side-by-side segments for native-alert-vs-routed-intent (the
      08:03 timeline row) AND rules-vs-LLM classifiers (replay table vs
      star-metric table, identical verdicts); clip list C1–C8 with
      exact commands and expected on-screen output; honest-language
      guardrails for editing.
- [x] **`docs/ARCHITECTURE.md`** — canonical diagram (mermaid + compact
      ASCII): live lane and replay/simulator lane converging on ONE
      v1.1 edge (verify raw bytes → adapt envelope → normalize) →
      classify → state → routing → Telegram, with the five
      registration-day plug-in points marked as dashed PLUG nodes (webhook secret,
      API token, LLM endpoint, Telegram creds, record dir), stage table
      with failure behavior, and the star-metric stage boundaries.
- [x] **`docs/SUBMISSION-CHECKLIST.md`** — every Devpost-day artifact
      with an owner per item ([repo] done / [human] action /
      [if-reg]); deadline converted to Lima time; pre-flight commands
      including the audit re-run; mini-track decisions (Open Source =
      enter once public; AWS Builder = only with honest scope, nothing
      AWS is built); 45-min submission-day runbook; contingencies
      (registration never completes → still submit, simulator is
      sanctioned).
- [x] **Deps pinned, verified**: core has zero runtime dependencies;
      `uv.lock` is tracked and exact (fastapi 0.141.1, uvicorn 0.53.0,
      httpx 0.28.1, pytest 9.1.1); `uv lock --check` passes; frozen
      sync from the lock works; exact versions recorded in the
      checklist §G.
- [x] **Pre-publication audit, clean** (2026-09-16, re-run commands in
      checklist §G): no AI attribution in history or tracked content
      (only hits are the guard scripts' own detection patterns — the
      policy, not attribution); no absolute/home/mount paths; no
      secret-shaped strings (only labeled mocks like `mock-token`);
      `.env` untracked; no personal emails; git author is the owner's
      noreply GitHub identity.

### What submission day still needs (all [human])

1. Record the video per `docs/VIDEO-SCRIPT.md` (C7 and §H of the
   checklist only if registration completed).
2. Fill the Devpost form from `docs/SUBMISSION.md` +
   `SUBMISSION-CHECKLIST.md` §A–F; submit the day before the deadline.
3. Publication gate: passed 2026-09-21 — owner approved publication;
   `main` pushed (verified three ways) and the repo made public.

## S3 — value layer live (mock legs until credentials, swap is config)

Goal: ding → classification → routed notification, working end to end
and MEASURED (N ≥ 10 scripted events, latencies published in
README §Value layer). The owner has not completed portal registration
(S1 Lane A) and no `.env` exists, so the two network legs — LLM
endpoint, Telegram Bot API — run through their deterministic offline
stand-ins; each swap is documented and is configuration, not code.

### What works offline, today

- [x] **Telegram sink** (`telegram.py`): Bot API `sendMessage` with
      severity-conditioned delivery — info → `disable_notification:
      true` (silent push), critical → false (rings). Credentials
      (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are .env-only;
      `telegram_sink_from_env` returns `None` when either is missing
      (offline = simply not wired), and `mock_telegram_sink` swaps in
      a recording transport so formatting, payload assembly, and the
      `{"ok": true}` response parse all execute with no socket. A
      non-ok Bot API answer raises `TelegramError` — a notification
      nobody saw must not look like success. `Router.from_settings`
      wires the live sink into both groups when configured.
- [x] **Multimodal LLM classifier + rules fallback**
      (`llm.py`): `LLMClassifier` speaks OpenAI-compatible
      `/chat/completions`, attaches the snapshot as a base64 data URI
      (content type from the source — real JPEG travels as JPEG).
      Offline, `mock_vision_transport` answers as a vision model
      would: it decodes the attached PNG with the same stdlib decoder
      and the same pixel ladder (`verdict_from_evidence`) the rules
      classifier uses, so mock verdicts == rules verdicts and the run
      is deterministic; undecodable input (real Ring JPEG) degrades
      to the platform fields — the honest answer for a vision-less
      mock. `FallbackClassifier` makes "rules fallback if API
      friction" structural: endpoint trouble degrades the ONE event
      to the rules verdict, `source: "fallback:rules"` (visible).
      `build_classifier` is the single composition point (server +
      every CLI): LLM when fully configured, pixel rules when a
      snapshot source is wired, plain stub otherwise; partial config
      stays offline.
- [x] **Cross-event state machine on a real sequence**
      (`timeline.py`, 11 beats now): the scripted day gained a second
      box at 10:26 (`track_refreshed`) and the 10:31 deposit whose
      delivery failed upstream, landing 13:20 — inside the track the
      12:47 pickup already closed (`deposit_superseded`, suppressed).
      Day vocabulary now covers duplicate / opened / refreshed /
      closed / superseded / orphan / no-change. Artifact regenerated,
      still byte-identical across re-runs; `replay-webhooks
      --snapshots` still reproduces it row for row.
- [x] **Star metric, measured** (`star.py` → `uv run star-metric`):
      plays the same 11 signed wire deliveries with `perf_counter`
      timings per stage (`StageTimings` on every `TimelineEntry`;
      timeline rendering ignores it, so `.timeline/` stayed
      byte-identical). Publishes `.star/star-metric.md` (gitignored —
      latencies belong to the machine that measured them): per-event
      table + N/min/median/mean/p90 summary + live-vs-mock modes.
      Measured offline run (README carries the table): N = 11, 7
      routed notifications, 7 Telegram calls, median total 6.4 ms,
      median classify 2.6 ms.
- [x] New knobs (`settings.py`, `.env.example`):
      `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_TIMEOUT` —
      all optional, empty = offline mode, secrets .env-only.
- **Suite: 354 tests, 0 failures** — 281 prior + 42 new at S3 (323
  then), +31 since; count synced 2026-09-21
  (`test_telegram.py` +13, `test_star.py` +8, `test_classify.py` +8,
  `test_llm.py` +11, `test_timeline.py` +2), all offline, zero
  sockets.

### Going live (each is .env-only, no code change)

1. `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env` (README §Value
   layer has the 4-step BotFather walkthrough) → star-metric and the
   server deliver to the real Bot API; severity conditioning already
   ships.
2. `RING_LLM_ENDPOINT` + `RING_LLM_MODEL` + `RING_LLM_API_KEY` in
   `.env` → every entry classifies via the real endpoint with the
   rules fallback behind it.
3. S1 gate (portal registration) → real webhooks/snapshots replace
   the scripted day, per the S2 plug-in list.

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
4. ~~OAuth token exchange~~ **BUILT (S2, 2026-09-17)**: `oauth.py` +
   the served `/oauth/callback` (the portal's Token Exchange URL) +
   `uv run mint-token <code> | --refresh`. Ring POSTs a one-time
   authorization code (60 s lifetime) backend-to-backend; the server
   exchanges it at `https://oauth.ring.com/oauth/token`
   (form-urlencoded confidential-client grant, NO PKCE — the docs use
   the client secret, server-to-server only) and persists the bundle
   (access ~4 h, refresh ~30 d) to `RING_TOKEN_STORE` (owner-only
   JSON; gitignored). Tokens are never echoed or logged. 17 new
   mock-transport tests (docs-pinned request shape, error paths,
   store roundtrip, route behavior) — suite 340, zero sockets.
   **Human gate (honest)**: minting a REAL token needs a Ring user
   with US-located devices under an active Protection plan (guide §5)
   to click Authorize — no such account exists here. A live probe
   with a bogus code reached Ring's endpoint (HTTP 403 — edge/egress
   rejection or no pending authorization; see docs/PORTAL-ENDPOINTS.md).
   Remaining S2 residue: the account-link sign-in + HMAC nonce match
   (same human gate — needs the /v1/users/me account id first).

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
  homepage) are served over a public HTTPS tunnel (localhost.run;
  docs/PORTAL-ENDPOINTS.md): webhook live, token exchange real (S2
  machine, gated on a Ring-minted code), homepage live, account-link
  an honest gate page (guide §5 US-device constraint).
- `RING_CLIENT_ID` / `RING_CLIENT_SECRET` feed the S2 token exchange
  (`oauth.py`, `/oauth/callback`, `uv run mint-token`); a real mint
  still needs the guide-§5 human gate (Ring login with US devices).
- Sync handler vs Ring's <5 s ack budget is unchanged (VALIDATION.md
  caveat 4): fine with the rules stub, risky with a remote LLM.

## Next

- [ ] S4 human items (submission day is pure execution — see
      `docs/SUBMISSION-CHECKLIST.md`): record the video per
      `docs/VIDEO-SCRIPT.md`; fill the Devpost form from
      `docs/SUBMISSION.md`; submit before 23-Oct 14:00 Lima
- [x] S4 publication gate: PASSED 2026-09-21 — owner approved;
      `main` pushed and the repo made public
- [x] S1 gate: PASSED 2026-09-17 — owner completed the portal
      registration; credentials live in `.env` (verified end-to-end:
      signed webhook accepted through the public tunnel URL,
      docs/PORTAL-ENDPOINTS.md)
- [ ] S2 residue: account-link sign-in + HMAC nonce match on top of the
      now-built token exchange (both behind the guide-§5 human gate:
      a Ring user with US devices authorizes → code arrives → bundle
      mints RING_API_TOKEN for the live snapshot source)
- [ ] S3 live legs: `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` and/or
      `RING_LLM_*` in `.env` (human, 2 minutes each — see README §Value
      layer); real watermarked-JPEG snapshots through the LLM adapter
      once the S1 gate passes (pixel rules stay the offline/calibration
      path)

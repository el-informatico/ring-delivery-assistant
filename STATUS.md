# STATUS — ring-delivery-assistant

Build log. Last entry: 2026-09-16 (S1 — registration runbook +
documented-wire adapter; 184 tests).

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
- [ ] S2: OAuth token exchange + account linking (needs S1 client
      credentials; endpoint stubs become real)
- [ ] S3: snapshot classification over the LLM adapter (media scopes
      from the Cameras and Doorbell group)

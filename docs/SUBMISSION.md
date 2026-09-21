# Devpost submission text — Ring Delivery & Safety Assistant

Drafted 2026-09-16, S4. Paste-ready for the Devpost form
(amazonappdev2026.devpost.com); every claim below is verifiable in the
repo (tests, artifacts, README §Value layer) or cites its source. The
**honesty rules** at the bottom are part of the deliverable — submission
day edits must keep them true.

## Short-form fields

- **Project title:** Ring Delivery & Safety Assistant
- **Tagline / elevator pitch:** The layer after the ding — Ring events
  become intents, intents become cross-event state, state decides who
  gets told what.
- **Team:** [owner fills on Devpost — solo or team name]
- **Track:** Ring (built on Ring's documented Webhook v1.1 + Events API
  contracts). Mini tracks: Open Source (Apache-2.0 repo) — enter the
  AWS Builder mini only after deciding whether its criteria are met by
  what exists (see SUBMISSION-CHECKLIST §F; nothing AWS-deployed is
  built — never claim it).
- **Priority category (Ring track):** business systems (primary) ·
  IoT home automation (secondary) — fit argued in §Priority-category
  fit below.

## Priority-category fit (Ring track)

The Ring track names five priority categories — "access control,
business systems, IoT home automation, accessibility, and caretaking"
(rules, verbatim) — and its creative-use-case list explicitly includes
"package/delivery management" and "business system integration". We
claim **business systems**: this is the receiving-operations layer for
deliveries — deposit → pickup tracking per device, out-of-order
straggler suppression, courier-vs-visitor discrimination,
severity-conditioned routing — the workflow a front desk, a small
business, or a household runs after the carrier scans the parcel. The
**IoT home automation** angle is the device class itself: doorbell
events are home-IoT telemetry, and the pipeline converts them into
automated action (digests, night escalation, quiet suppression) with
no human in the loop. The demo is, literally, the delivery-management
use case the rules' creative list names.

## Open Source mini — opt-in fields

Official field list (rules, verbatim): "Provide contribution URL,
project repository URL, GitHub username, and a description of what you
did, how it works, and why it matters."

- **Contribution URL:** the repository itself (solo project — the
  commit history is the contribution record).
- **Project repository URL:** `[publication-gated — paste after
  SUBMISSION-CHECKLIST §E passes]`
- **GitHub username:** `[owner fills — expected el-informatico]`
- **License:** Apache-2.0 — full standard text at the repository root
  ([`LICENSE`](../LICENSE), 201 lines, unmodified), matching the "Apache-2.0
  repo" expectation stated for this mini track.
- **Description (did / how it works / why it matters):** A
  stdlib-only intent and routing layer for Ring doorbell webhooks:
  signed events become intents (package deposited, package picked up,
  person, vehicle, motion noise) via deterministic pixel rules or a
  multimodal adapter, a cross-event state machine tracks deposit →
  pickup per device (late stragglers suppressed, redeliveries no-ops),
  and a rules engine decides who gets told what — Telegram,
  severity-conditioned. 354 offline tests, zero sockets, Apache-2.0;
  every network leg is a documented contract behind an env-only swap.

**In-window note (pre-existing-work disclosure is trivially
satisfied):** repository history starts 2026-09-16 — every commit
falls inside the 31-Aug → 23-Oct submission window, and no pre-window
history exists. Verified with `git log --format='%ai'` (first commit
2026-09-16 12:03:07 -0500); re-run the same command on submission
morning before pasting this claim.

---

## Inspiration

Every doorbell owner knows the problem: the app pings constantly, and
none of it is knowledge. "Motion detected" — was that my package, the
courier's van, or a leaf? A delivery arrives, but nobody tracks whether
it was *picked up*. A stranger at 22:41 gets the same chime as a friend
at noon.

Ring already detects — native Package Alerts and Person Alerts exist.
The gap is everything **after** detection: what the event *means* across
events, and *who gets told what*. This project is that layer.

## What it does

An intent + routing pipeline for Ring-style doorbell webhook streams.
Each signed webhook becomes an **intent** (package deposited, package
picked up, person at door, vehicle at door, motion noise) — using the
event's camera snapshot, because the live wire's own motion vocabulary
never says "package". A **cross-event state machine** tracks deposit →
pickup per device, surviving out-of-order and duplicate deliveries: a
late deposit webhook landing after the pickup is *suppressed* instead of
re-alarming for a parcel that already left. **Routing** then decides per
event: notify, escalate (person at night → every sink, critical), or
suppress (lone motion stays quiet; only the 3rd motion in 10 minutes
triggers one digest). Routed notifications reach Telegram,
severity-conditioned — informational alerts arrive silently, night
escalations ring.

The whole trip is measured: **ding → routed notification, median
6.4 ms** over an 11-event day (N = 11, 7 routed notifications,
classification stage alone 2.6 ms) — offline timings of the pipeline's
own stages, with both network legs mocked (details under "How we built
it"). 354 offline tests, zero sockets, keep all of it pinned.

## How we built it

Python 3.12+, **stdlib-only core** (zero runtime dependencies: `hmac`
signature verification, a from-scratch PNG decoder for snapshot pixel
rules, `sqlite3` state). The edge adapter speaks Ring's **documented
webhook v1.1 contract** — JSON:API envelope, `X-Signature:
sha256=<hex HMAC-SHA256 of the raw body>`, epoch-millisecond timestamps
— and pins it with Ring's own published example payloads as fixtures.
The two network legs are built against real contracts but run offline
through deterministic transport-boundary mocks:

- **Classification** — a multimodal, OpenAI-compatible adapter that
  attaches the event's snapshot as a base64 data URI; offline it runs
  the full call path against a mock vision model that decodes the same
  PNG with the same pixel ladder as the rules path, so verdicts are
  identical and deterministic. A fallback wrapper degrades any endpoint
  failure to the rules verdict, visibly (`source: "fallback:rules"`).
- **Delivery** — a Telegram Bot API sink whose formatting, severity
  conditioning, and response parsing all execute offline against a
  recording transport; adding credentials makes it POST for real.

Going live is configuration, not code: five env-only plug-in points
(webhook secret, Events API token, LLM endpoint, Telegram credentials,
traffic recording) are mapped in `docs/ARCHITECTURE.md`.

**Honest scope:** Ring publishes no sandbox environment, and we own no
Ring hardware — the demo runs the documented wire contract through the
repo's replay/simulator lane, which the hackathon rules sanction ("The
demo video must show the project working through a simulator or an
actual Ring device you have for testing", amazonappdev2026.devpost.com/
rules). No live Ring delivery has been received yet; the wire contract
is pinned to Ring's published documentation, and the measured latencies
are this pipeline's own work — network RTT is excluded and labeled as
such wherever a number appears.

## Challenges we ran into

- **No sandbox exists.** Ring's docs define no sandbox base URL or
  console simulator; the Environment Configuration table is
  production-only. We pivoted to contract-first: fixtures are Ring's
  published example payloads, replayed through the *same* edge a live
  delivery takes, so the simulator demo and production cannot drift.
- **The wire never says "package".** The documented motion `sub_type`
  vocabulary has no delivery type — so some image consumer is required.
  We split it in two: deterministic pixel rules over decoded PNGs
  (offline, calibration path) and a multimodal LLM path for real
  watermarked JPEGs (the stdlib decoder refuses JPEG by design, routing
  it to the LLM adapter instead of pretending).
- **Webhook semantics are hostile to honest errors.** Ring reads a 4xx
  as permanent failure, so the eight documented event types we don't
  consume must ack 200 and be ignored — only real signature/schema
  failures answer 4xx. A pinned docs quirk: the motion example's
  `timestamp_readable` renders local time (5 h off UTC); we parse only
  the epoch-ms field the docs designate for time math.
- **Out-of-order and duplicate deliveries.** Webhooks arrive late and
  twice; state must be derived in occurred order, not arrival order,
  and redelivery must be a no-op. The scripted demo day includes a
  straggler deposit whose delivery failed upstream and lands 2 h 49 m
  late — after its pickup closed the track — and the machine answers
  `deposit_superseded`: no re-alarm.
- **Measuring honestly.** Latencies that silently include mocked legs
  would be a lie; latencies on someone else's machine are noise. Every
  timing artifact is gitignored, runs are labeled live-vs-mock, and the
  README publishes one representative run with both exclusions stated.

## Accomplishments we're proud of

- **354 tests, 0 failures, ~4 s, zero sockets** — the entire system,
  including the HTTP contract and both network legs' call paths, is
  tested offline through injected transports.
- **A measured star metric**, not an estimate: ding → routed
  notification, N = 11, median 6.373 ms / p90 6.687 ms, classification
  median 2.627 ms (offline pipeline stages; README §Value layer carries
  the full per-event table).
- **Byte-identical artifacts**: the scripted-day timeline and its
  replayable capture regenerate deterministically — the demo is
  diffable in review.
- **The 08:03 story**: on the wire, bare `motion_detected`; the
  snapshot says a package is on the mat; a track opens and a routed
  notification fires. One row shows the whole thesis.
- **Degradation that never hides**: missing snapshots, undecodable
  images, and LLM failures each fall back with a *visible* source
  label — an event is never failed for want of an image, and a
  notification nobody saw can never look like success.

## What we learned

- Contract-first beats hardware-first: pinning the published wire
  contract with the docs' own examples bought everything the demo needs
  before registration ever completed.
- Mock at the transport boundary, not the function boundary: running
  the real request assembly and response parsing against a recorder
  makes the offline demo exercise the live code path, and makes the
  swap a one-line configuration change.
- Arrival order is not truth. Deriving state in occurred order on every
  apply is what turns "a pile of notifications" into "the package was
  taken".
- Honesty is a discipline you can engineer: gitignored timing
  artifacts, live-vs-mock labels, and visible fallback sources make
  every claim checkable — which is also what makes the demo
  defensible.

## What's next

Complete the developer-portal registration gate (in progress) and
exercise a live window: real signed webhooks through the recording
server, OAuth token exchange and account linking (the built-but-pending
Events API client goes live with one token), real watermarked JPEG
snapshots through the multimodal leg, and a live re-measure of the star
metric with network legs included. Further out: track retention/expiry,
and packaging the intent layer as the standalone OSS library it was
extracted from.

## Built with

Python 3.12+ (stdlib-only core — `hmac`, `json`, `sqlite3`, `zlib`,
`struct`), FastAPI + uvicorn (optional server extra), pytest, uv,
Telegram Bot API, any OpenAI-compatible multimodal chat-completions
endpoint (adapter), Ring Webhook v1.1 and Ring Events API (documented
contracts), WSL2.

---

## Honesty rules (binding for submission-day edits)

1. Never claim live Ring traffic, a live LLM call, or a live Telegram
   delivery unless `.env` credentials exist and the run's mode line
   says so on screen.
2. Every latency quoted keeps its qualifiers: *offline, pipeline stages
   only, network legs mocked* (README §Value layer's published run:
   N = 11, median 6.373 ms).
3. The demo is the replay/simulator lane — say "simulator" in the video
   and text, and cite the rules sanction when relevant.
4. No invented team members, awards, users, or metrics; "354 tests"
   moves only when `uv run pytest` says so.
5. The AWS Builder mini is entered only with an honest scope statement,
   or not at all.

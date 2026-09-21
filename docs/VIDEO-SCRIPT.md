# Video script + storyboard — demo recording (S4)

**Target: < 3 min hard; this script times to ~2:50** with clip beats.
English narration, ~390 words at a comfortable 150 wpm. Record the clips
first (list below), then narrate over them — every clip is a terminal or
a rendered file, no camera needed.

Honesty is load-bearing here: the demo runs the **replay/simulator
lane** (rules-sanctioned — "The demo video must show the project working
through a simulator or an actual Ring device you have for testing",
amazonappdev2026.devpost.com/rules). The narration below already says
"simulator", "scripted", "offline", and "mock" where they are true —
**do not smooth them out while editing**.

## Timing budget

| # | Time | Segment | Words |
|---|---|---|---|
| 1 | 0:00–0:13 | Hook — the problem | 30 |
| 2 | 0:13–0:30 | Positioning — the layer after the ding | 45 |
| 3 | 0:30–1:07 | The scripted day (native alert vs routed intent) | 93 |
| 4 | 1:07–1:35 | Star metric — latency on screen | 65 |
| 5 | 1:35–2:05 | Side-by-side: rules vs LLM classifier | 68 |
| 6 | 2:05–2:25 | Engineering quality | 46 |
| 7 | 2:25–2:50 | Close — honest scope + sign-off | 43 |

## Narration + storyboard

### 1. Hook (0:00–0:13)

> Your doorbell app pings constantly — and none of it is knowledge.
> "Motion detected." Was that my package, the courier, or a leaf?
> This project is the layer after the ding.

**On screen:** terminal, idle prompt → type `uv run pytest` (clip C1),
let the tail land: `354 passed in 3.95s`. Cut on the number.

### 2. Positioning (0:13–0:30)

> Ring already detects packages and people — natively. So this is not
> another package detector. It's an intent, state, and routing layer:
> each signed webhook becomes an intent; a state machine tracks deposit
> to pickup; routing decides who gets told what — notify, escalate, or
> stay quiet.

**On screen:** `docs/ARCHITECTURE.md` rendered (GitHub or editor
preview, clip C8). Slow scroll: two lanes → one edge → five stages →
Telegram. End on the plug-in points.

### 3. The scripted day (0:30–1:07)

> Here's one scripted day through the live wire contract: eleven signed
> deliveries. The first row is the thesis. At eight-oh-three, the
> platform said "motion detected" — that's the native alert. The
> snapshot said: package on the mat. Intent: deposited. A track opens,
> one routed notification fires. Ten twenty-six, a second box refreshes
> it. Twelve forty-seven, the pickup closes it. And when the
> ten-thirty deposit's webhook finally lands three hours late — after
> that pickup — the machine answers "superseded": no re-alarm for a
> parcel that already left. At twenty-two forty-one, a person at night
> escalates everywhere.

**On screen:**
- 0:30 `uv run timeline` (clip C2) — let the seven notification lines
  and `11 deliveries, 7 notifications` land.
- 0:40 open `.timeline/timeline.md` (clip C3). Zoom the 08:03:21 row —
  **the side-by-side moment**: `platform said: motion_detected` vs
  `snapshot said: package present, no person` → `package_deposited` →
  `notify`. This row IS "native alert vs routed intent".
- 0:52 zoom the s3-007 row (10:31:18, `deposit_superseded`) while
  narration covers the straggler.
- 1:00 zoom the s3-011 row (22:41, `escalate`) → cut to the
  notifications list, `[critical] 22:41:09 Person at the door at night`.

### 4. Star metric — latency on screen (1:07–1:35)

> Now the number this was built around: ding to routed notification,
> measured. Eleven events, seven routed. On this machine, offline, the
> median total is about six milliseconds; classification alone, about
> two point six. Those are the pipeline's own stages — verify, classify,
> state, route, deliver — with both network legs mocked. A live endpoint
> adds its round trip on top; we say so, on the artifact itself.

**On screen:**
- 1:07 `uv run star-metric` (clip C4). Let the mode line settle
  ("mock vision transport · mock Telegram transport (no socket)") —
  **latency counter moment M1**: zoom the summary line
  `N = 11 events · ding -> routed notification: min … · median … ·
  mean … · p90 …`.
- 1:20 open `.star/star-metric.md` (clip C5) — **M2**: the per-event
  table, `total ms` column; **M3**: the `classify ms` column and the
  summary line `classification stage: median … ms`.
- 1:30 quick flash of the artifact's "Live vs mock" table while
  narration says the last sentence.

Speak the numbers **from the run you recorded** — narration hedges to
"about six" / "about two point six" so any honest run fits. The
README-published run (median 6.373 ms, classify 2.627 ms) is the
fallback if you quote exact figures on screen instead.

### 5. Side-by-side: rules vs LLM classifier (1:35–2:05)

> Two classifiers, same eleven events. Left: deterministic pixel rules
> over the decoded snapshot. Right: the multimodal LLM call path —
> request assembly, base64 image, response parse — running offline
> against a mock vision model that reads the same PNG with the same
> ladder. Identical verdicts, row for row. If the endpoint ever fails
> on a live setup, the fallback degrades that one event to the rules
> verdict — visibly, never silently.

**On screen:** split screen (clip C6):
- **Left:** `uv run replay-webhooks .timeline/webhooks.jsonl
  --snapshots .timeline` — the rules lane; verdicts from
  `rules+snapshot`.
- **Right:** `.star/star-metric.md` per-event table (same day) —
  verdicts from `llm:mock-vision-1`.
- Drag a highlight down the `intent`/`action` columns of both tables in
  lockstep — identical rows. Optional: end on the Telegram severity
  note (info silent / critical rings) from the README table.

### 6. Engineering quality (2:05–2:25)

> The whole system is built like that: three hundred twenty-three
> offline tests, zero sockets, about four seconds. The core is Python
> standard library only. And every live swap — the real secret, the
> Events API, the real model, real Telegram — is an environment
> variable, mapped in the architecture diagram.

**On screen:** C1 tail again (`354 passed`), then the ARCHITECTURE.md
plug-in list (the five `PLUG` nodes) — zoom each env var name as it is
spoken.

### 7. Close (2:25–2:50)

> Ring publishes no sandbox and we own no hardware — so this demo runs
> the documented wire contract through a simulator, exactly what the
> hackathon rules allow. The layer after the ding: what the event means,
> and who gets told what. Thanks for watching.

**On screen:** `tests/fixtures/wire/PROVENANCE.md` (the docs' own
example payloads — one beat), then title card with repo name +
Apache-2.0 + the repo URL (only if the publication gate has passed by
recording day; otherwise name only).

## Clips to record

Record each clip separately (retakes stay cheap). Terminal: large font
(18 pt+), consistent dark theme, 1920×1080, `clear` before every
command, cursor noise off. On WSL2, record the Windows side (OBS Studio
or Win+G) — the terminal is Windows Terminal hosting the WSL shell.

| Clip | What | Command / file | Expected on screen |
|---|---|---|---|
| C1 | test suite | `uv run pytest` | `354 passed, 2 warnings in ~4s` (tail only needs recording) |
| C2 | the day, live-typed | `uv run timeline` | 7 notification lines, `11 deliveries, 7 notifications`, artifact paths |
| C3 | the artifact | open `.timeline/timeline.md` | full table; rows 08:03:21, s3-007, s3-011; notifications + caveats |
| C4 | star metric | `uv run star-metric` | mode line + `N = 11 … median … ms` summary |
| C5 | star artifact | open `.star/star-metric.md` | per-event table (total + classify columns), summary, live-vs-mock |
| C6a | rules lane | `uv run replay-webhooks .timeline/webhooks.jsonl --snapshots .timeline` | 11-row table, `replayed 11 delivery(ies): 11 accepted` |
| C6b | LLM lane | `.star/star-metric.md` table (from C5) | same 11 rows, `llm:` sources |
| C8 | architecture | `docs/ARCHITECTURE.md` rendered | mermaid flow, two lanes, PLUG nodes |

**Before recording, regenerate everything fresh** (deterministic — same
bytes, honest "this run" numbers):

```bash
git status                          # clean tree, nothing distracting
uv run pytest                       # confirm 354 first
uv run timeline && uv run star-metric
```

Disable OS notifications; close personal apps/tabs (nothing personal
should appear in the recording).

## Latency-counter moments (the S4 requirement)

| Moment | Where | What the viewer sees |
|---|---|---|
| M1 | C4, 1:07–1:20 | `N = 11 events · ding -> routed notification: min/median/mean/p90` zoomed as it prints |
| M2 | C5, 1:20–1:27 | per-event `total ms` column (e.g. `6.495` for s3-001) |
| M3 | C5, 1:27–1:35 | `classify ms` column + `classification stage: median … ms` |
| M4 (optional, IF registered) | C7 below | wall-clock timer on a signed POST → 200 round trip |

### C7 — optional live-server clip (record ONLY if registration completed)

```bash
# shell 1 (.env must carry the real RING_WEBHOOK_SECRET)
uv run --extra server uvicorn ring_assistant.server:app --port 8000
# shell 2 — the signed-request snippet from USAGE.md, wrapped in time:
time uv run python - <<'PY'
# (USAGE.md "Optional webhook server" snippet verbatim — posts the
#  documented button_press fixture, signed, to 127.0.0.1:8000)
PY
```

Show the `200` response body plus the wall-clock time, and say so:
"with the real secret in place, the same documented payload verifies
through the live edge." This clip replaces or extends segment 6 — keep
total runtime under 3:00 either way. If Telegram credentials exist by
then, re-run `uv run star-metric` first so C4/C5 show `live Telegram
API` on the mode line, and speak "median" without "offline" only if
BOTH legs are live.

## Editing notes

- Zooms: 150–200 % on table rows; keep each zoom ≥ 2 s.
- No background music with vocals; low ambient only, narration front.
- Burned-in captions welcome (English) — keep the honesty words intact.
- Export: 1080p, H.264, ≤ 3:00; upload unlisted to YouTube first, check
  the end-to-end time, then paste the URL into Devpost.
- Filename: `ring-delivery-assistant-demo-<date>.mp4`.

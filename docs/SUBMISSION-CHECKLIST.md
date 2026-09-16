# Submission checklist — Devpost day (S4)

**Deadline: 23-Oct-2026 12:00 PDT = 14:00 Lima (PET, UTC-5).**
Submit the day before; Devpost locks hard.

State as of 2026-09-16: everything the REPO can produce is done — text,
script, diagram, pins, audit. What remains is HUMAN: the video
recording, the Devpost form, and the two standing gates (portal
registration, publication approval).

Owner legend: **[repo]** = exists in this repo, ready to paste/upload ·
**[human]** = owner action outside the repo · **[if-reg]** = only if the
S1 registration gate has completed by then.

## A. Devpost account + project [human]

- [ ] Devpost account working; joined the hackathon
      (amazonappdev2026.devpost.com) — the project gallery/form was
      unpublished as of 16-Sep; **re-check the form's actual field list
      the day it opens** and reconcile against this checklist.
- [ ] Create project → paste title + tagline from `docs/SUBMISSION.md`
      §Short-form fields.
- [ ] Team: add members (solo is fine — never invent teammates; rule 4
      of SUBMISSION.md §Honesty rules).

## B. Text [repo → paste]

- [ ] Description = `docs/SUBMISSION.md` body (Inspiration → Built
      with), pasted section by section into the Devpost editor.
- [ ] "Built with" tags: Python, Telegram — plus FastAPI, pytest if the
      form's tag list offers them.
- [ ] Custom questions (which Ring/Amazon APIs used, etc.): answer from
      SUBMISSION.md §How we built it — Ring Webhook v1.1 + Events API
      (documented contracts), Telegram Bot API,
      OpenAI-compatible multimodal endpoint adapter.

## C. Video [human records; script is repo-ready]

- [ ] Record per `docs/VIDEO-SCRIPT.md` (clips C1–C8; C7 only if-reg).
- [ ] Runtime < 3:00 confirmed after export.
- [ ] Upload unlisted to YouTube; watch it once end-to-end; paste URL.
- [ ] Captions (optional but recommended) keep the honesty words:
      "simulator", "scripted", "offline", "mock".

## D. Images [repo provides; human uploads]

Devpost wants images; judges scroll before reading. Upload in this
order:

- [ ] Architecture diagram: export `docs/ARCHITECTURE.md`'s mermaid via
      mermaid.live (paste the block, PNG export) — or screenshot the
      GitHub rendering.
- [ ] Timeline table screenshot (`.timeline/timeline.md`, 08:03 row
      visible) — the thesis in one image.
- [ ] Star-metric screenshot (`.star/star-metric.md` summary +
      per-event table) — regenerate fresh: `uv run star-metric`.
- [ ] Optional 4th: split rules-vs-LLM tables (VIDEO-SCRIPT C6).

## E. Links + publication gate [human — explicit approval required]

- [ ] **Publication gate (sprint rule): the repo goes public only after
      the owner explicitly approves.** Until then Devpost gets the video
      and text; the repo link is added only if the gate passes.
- [ ] If approved: make the GitHub repo public, push `main` (no push
      has happened from this workspace — push is part of this item),
      paste the URL into Devpost.
- [ ] Repo link targets: README (star metric table + honest framing) as
      landing, LICENSE (Apache-2.0) visible.

## F. Mini tracks [human decides; honesty rule 5]

- [ ] **Open Source mini** — eligible as-is once E passes: Apache-2.0,
      README, CONTRIBUTING-by-issue, deterministic artifacts. Enter.
- [ ] **AWS Builder mini** — nothing AWS-deployed is built. Enter ONLY
      with an honest scope statement (e.g., "runs locally; Lambda
      deployment is future work") or skip. Never claim a deployment.

## G. Pre-flight (run on submission morning) [repo]

```bash
uv run pytest                     # expect: 323 passed
uv lock --check                   # expect: no output (lock in sync)
git status                        # expect: clean
```

Pre-publication audit (verified clean 2026-09-16 — re-run to be sure):

```bash
# 1. no AI attribution in tracked content or history
#    (expected: hits ONLY in scripts/hooks/* — the guard's own
#     detection patterns, i.e. the policy, not attribution)
git grep -inE 'co-authored-by|claude|anthropic|chatgpt|copilot|🤖' -- .
git log --format='%B' | grep -inE 'co-authored-by.*(claude|anthropic|chatgpt|openai|copilot|gemini)|generated with|🤖' || echo clean

# 2. no absolute/home paths (expected: none)
git grep -inE '/home/[a-z]|/Users/|/mnt/' -- . || echo clean

# 3. no secrets (expected: labeled mock values only, .env untracked)
git grep -inE 'token *= *["'"'"'][A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{15,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}' -- .
git ls-files .env                 # expected: no output
```

Dependency pins (verified 2026-09-16): core = **zero runtime
dependencies**; `uv.lock` tracked and exact — fastapi 0.141.1, uvicorn
0.53.0, httpx 0.28.1, pytest 9.1.1; `uv sync --extra server --dev
--frozen` installs from the lock with no resolution.

## H. Registration-dependent [if-reg]

- [ ] `.env` carries the real `RING_WEBHOOK_SECRET` (from
      `docs/REGISTRATION-GUIDE.md`); a signed documented fixture gets
      200 from the local server (VIDEO-SCRIPT C7 becomes recordable).
- [ ] Optional stronger video: record C7; re-run `uv run star-metric`
      with Telegram credentials so the mode line reads
      `live Telegram API` — then update SUBMISSION.md's qualifier
      wording to match what the recording actually shows.
- [ ] Friction-log row 1 (registration friction) — the S1 deliverable;
      the SUBMISSION.md §Challenges text stands in until it exists.
- [ ] If live legs ran: refresh README §Value layer's representative
      run or add the live-run table beside it — never silently replace
      the offline table.

## I. Submission-day sequence [human, ~45 min]

1. Run §G pre-flight (5 min).
2. Reconcile the Devpost form's fields against §A–B (10 min).
3. Paste description + built-with; upload images (10 min).
4. Paste video URL; preview the whole project page (10 min).
5. Minis per §F; links per §E (5 min).
6. **Submit.** Screenshot the confirmation. Do not plan to edit after
   13:00 Lima on 23-Oct.

## J. Contingencies

- **Registration never completes:** submit anyway — the simulator demo
  is rules-sanctioned; SUBMISSION.md already scopes it honestly. C7 and
  §H simply drop.
- **Video overruns 3:00:** cut segment 6's second half (the env-var
  zooms) — segments 1–5 + close are the irreducible core.
- **Devpost outage on the day:** keep the screenshot evidence of
  attempts; Devpost's own status page governs extensions.
- **Numbers drifted** (pytest count, star-metric medians): update the
  page to the current run's values — rule 4 of SUBMISSION.md.

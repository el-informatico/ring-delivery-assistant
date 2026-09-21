# Deploy runbook — Render (free) + Turso (free) + UptimeRobot (free)

Replace the localhost.run tunnel with a permanent `https://<name>.onrender.com`
URL: no process to keep alive on the LAN, no URL that dies on reconnect, $0
total.

Architecture after this runbook:

```
Ring portal ──POST──> https://<name>.onrender.com/webhooks/ring   (Render free)
                          │  uvicorn ring_assistant.server:app
                          ├── state DB + token bundle ──> Turso (libsql over
                          │                                  HTTPS, free tier)
                          └── notify verdicts ──> Telegram
UptimeRobot ──GET / every 5 min──> keeps the free instance from
                                    spinning down (~15 min idle otherwise)
```

Everything below is click-by-click. Total: ~20 minutes of pointing and
pasting. Secrets are pasted into dashboard fields only — never committed,
never chatted.

---

## Step 0 — the GitHub repo

Render deploys from a GitHub repo. This project's canonical home is
`github.com/el-informatico/ring-delivery-assistant`; to reproduce the
deploy, fork it (or clone and push to your own GitHub account):

```bash
git remote add origin git@github.com:<you>/ring-delivery-assistant.git
git push -u origin main
```

## Step 1 — Turso account + database (~5 min)

1. <https://turso.tech> → **Login** → **Sign in with GitHub** (same account).
2. Install the CLI (Linux/WSL). NOTE 2026-09-18: `get.tur.so` installer is
   DEAD (404). Install from the official GitHub releases instead:

   ```bash
   curl -sL -o /tmp/turso.tar.gz \
     https://github.com/tursodatabase/turso-cli/releases/latest/download/turso-cli_Linux_x86_64.tar.gz
   tar xzf /tmp/turso.tar.gz -C /tmp && mv /tmp/turso ~/.local/bin/
   ```

   Login (WSL has no xdg-open; `--headless` exits immediately without
   waiting, and the plain flow dies with "failed to open auth URL" — the
   fix is an xdg-open stub so the interactive flow keeps its localhost
   listener alive, then open the printed api.turso.tech URL by hand and
   use "Sign in with GitHub"):

   ```bash
   printf '#!/bin/sh\nexit 0\n' > /tmp/xdg-open && chmod +x /tmp/xdg-open
   export PATH="/tmp:$HOME/.local/bin:$PATH"
   turso auth login   # prints https://api.turso.tech?port=<port>&... — open it manually
   ```

   NOTE 2026-09-18: the printed auth URL is short-lived — the flow's
   localhost listener behind it is gone after roughly 20 minutes, so a
   browser tab opened late lands on a dead localhost port and auth never
   completes. Click through promptly after `turso auth login` prints the
   URL; if it expired, start the login again.
3. Create the database and mint credentials:
   ```bash
   turso db create ring-staging
   turso db show ring-staging --url        # -> libsql://ring-staging-<org>.turso.io
   turso db tokens create ring-staging     # -> a long eyJ... string (print once)
   ```
4. Park both values somewhere private (password manager / local notes).
   They are Step 2's `RING_TURSO_URL` and `RING_TURSO_TOKEN`.

Free tier is generous for this app: ~500M row reads/month, ~10M writes —
orders of magnitude above webhook traffic.

## Step 2 — Render web service (~8 min)

1. <https://render.com> → **Get Started** → **Sign in with GitHub**.
   Browser note (2026-09-18): with Brave's shields at default
   (third-party cookies blocked), the render↔github OAuth handshake
   loops forever on "connect account" — lower shields for
   `render.com` + `github.com`, or use another Chromium browser, and
   the handshake completes on the first try.
2. Dashboard → **New +** → **Web Service**.
3. Connect the `ring-delivery-assistant` repo — authorize Render's GitHub
   app if asked.
4. Fill the form:
   | Field | Value |
   |---|---|
   | Name | `ring-delivery-assistant` (this becomes `<name>.onrender.com`) |
   | Region | Frankfurt (closest; latency-only choice) |
   | Branch | `main` |
   | Runtime | Python 3 |
   | Build command | `pip install -e ".[server]" -r requirements-render.txt` |
   | Start command | `uvicorn ring_assistant.server:app --host 0.0.0.0 --port $PORT` |
   | Instance type | **Free** |
5. **Environment** → **Add environment variable**, one row per key (values
   from where noted; never commit these anywhere):
   | Key | Value comes from |
   |---|---|
   | `RING_WEBHOOK_SECRET` | the local `.env` (same HMAC key Ring signs with) |
   | `RING_SIGNATURE_HEADER` | `x-signature` (literal — the live Ring header) |
   | `RING_CLIENT_ID` | local `.env` (Ring portal credential) |
   | `RING_CLIENT_SECRET` | local `.env` (Ring portal credential) |
   | `RING_TURSO_URL` | Step 1 `turso db show --url` |
   | `RING_TURSO_TOKEN` | Step 1 `turso db tokens create` |
   | `TELEGRAM_BOT_TOKEN` | local `.env` (optional — enables notify verdicts via the Telegram Bot API) |
   | `TELEGRAM_CHAT_ID` | local `.env` (same) |
   | `PYTHON_VERSION` | `3.13` (literal — matches the version the suite runs on) |
6. **Deploy Web Service**. First build ~3–5 min (pip + Rust-built libsql
   wheel). Watch the log: the service is up when uvicorn prints
   `Application startup complete` and Render shows **Live**.
7. Smoke-check from any browser: `https://<name>.onrender.com/healthz`
   → `{"ok": true}`.

If the blueprint route is preferred over the manual form: **New +** →
**Blueprint** → pick the repo — `render.yaml` carries the build/start
commands and the same env var list (secrets prompt for paste, `sync: false`).

## Step 3 — UptimeRobot keep-alive (~3 min)

1. <https://uptimerobot.com> → **Sign up** (free).
2. **Add New Monitor**:
   | Field | Value |
   |---|---|
   | Type | HTTPS |
   | Friendly name | `ring-delivery-assistant` |
   | URL | `https://<name>.onrender.com/healthz` |
   | Monitoring interval | 5 minutes (free default) |
3. **Create monitor**.

Without this, Render's free tier spins the instance down after ~15 min
idle and the next webhook waits ~30–60 s for a cold boot. With a 5-min
ping it stays warm (750 free instance-hours/month ≫ 744 wall-clock
hours — check the Render dashboard usage around Oct 1).

## Step 4 — re-paste the portal URLs (final, stable)

Ring Developer Portal → app → **Staging** tab — replace the four
`*.lhr.life` tunnel rows with:

| Endpoint field | URL |
|---|---|
| **Webhook URL** | `https://<name>.onrender.com/webhooks/ring` |
| **Token Exchange URL** | `https://<name>.onrender.com/oauth/callback` |
| **Account Link URL** | `https://<name>.onrender.com/account-link` |
| **App Homepage URL** | `https://<name>.onrender.com/` |

Unlike the tunnel, these URLs never change — this is the last re-paste.
(`docs/PORTAL-ENDPOINTS.md` gets updated to this table after the
end-to-end verify below.)

## Step 5 — end-to-end verification

The signed-leg checks run locally after deploy (same battery the tunnel
passed):

```bash
BASE=https://<name>.onrender.com
curl -fsS $BASE/healthz        # {"ok": true}
curl -fsS $BASE/               # 200 landing page
curl -fsS $BASE/account-link   # 200 gate page
# signed webhook: bad signature -> 401, good signature -> 200 accepted
#   (local helper, URL FIRST then fixture name: /usr/bin/python3.12 \
#    /tmp/send-signed-webhook.py $BASE/webhooks/ring \
#    motion_sub_type_motion.json — suppress verdict, no Telegram message)
curl -fsS -X POST $BASE/oauth/callback   # 400 bad_request (no code) — expected
turso db shell ring-staging "select event_id, intent from events limit 5"
#   ^ the webhook's events, proving Turso persistence end to end
```

## Ops notes (honest limits of the $0 stack)

- **Auto-deploy**: every `git push` to `main` redeploys automatically.
  Disable per-service in Render if a push should not go live.
- **Ephemeral disk**: anything the process writes locally is lost on
  restart — that is why the state DB and OAuth bundle live in Turso
  (both `RING_TURSO_*` vars set → Turso wins over file paths; half-set
  is a startup error on purpose).
- **Token refresh loop**: access tokens live ~4 h. The bundle minted by
  `/oauth/callback` persists to Turso; to repoint `RING_API_TOKEN`
  (Events API snapshots) read the current access token back with
  `turso db shell ring-staging "select bundle_json from tokens"` and
  update the env var in Render (edit → save → redeploy). From local,
  `RING_TURSO_URL` + `RING_TURSO_TOKEN` in the local `.env` let
  `uv run mint-token --refresh` rotate the bundle in place.
- **Client package**: the deploy installs `libsql` (Turso's current
  Python SDK, pinned in `requirements-render.txt`). The older
  `libsql-experimental` is deprecated and its drivers stopped working
  against Turso's free tier in June 2026 — do not "helpfully" swap it in.
- **Cost**: Render free + Turso free + UptimeRobot free = **$0**.

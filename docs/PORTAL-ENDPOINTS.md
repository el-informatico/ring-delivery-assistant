# Portal endpoints (staging tab) — paste-ready URLs

Verified live on 2026-09-17 through the tunnel below. Portal reference:
<https://developer.amazon.com/docs/ring/api-documentation.html#13-configure-endpoints>
(Staging tab: <https://developer.amazon.com/docs/ring/configure.html>).

## Paste these into the Ring Developer Portal → Staging tab

| Endpoint field | URL | Paste note |
|---|---|---|
| **Webhook URL** | `https://aaab70143ca90d.lhr.life/webhooks/ring` | Live v1.1 wire contract; HMAC-verified (bad signature → 401) |
| **Token Exchange URL** | `https://aaab70143ca90d.lhr.life/oauth/callback` | Honest stub: 501 JSON until S2 lands |
| **Account Link URL** | `https://aaab70143ca90d.lhr.life/account-link` | Honest stub page (US-device constraint, guide §5) |
| **App Homepage URL** | `https://aaab70143ca90d.lhr.life/` | Landing page listing the endpoints |

## Tunnel facts (localhost.run, no signup)

- The base `aaab70143ca90d.lhr.life` is **ephemeral** — it changes every time
  the tunnel restarts. If the URL stops resolving, re-run (from the repo):

  ```bash
  # server (port 8000 was taken by an unrelated service; ours is 8200)
  uv run --extra server uvicorn ring_assistant.server:app --host 0.0.0.0 --port 8200 &
  # tunnel — WSL: forward to the LAN IP, not 127.0.0.1 (mirrored-network blackhole)
  ssh -R 80:<LAN-IP>:8200 nokey@localhost.run
  ```

  Then update the four URLs in the portal with the new base.
- TLS terminates at localhost.run; the leg to the LAN IP is plain HTTP on the
  home network (fine for staging).

## End-to-end verification performed (2026-09-17)

From outside via the public URL, with a fixture payload signed using
`RING_WEBHOOK_SECRET` from `.env`:

- `POST /webhooks/ring` → **200** `{"status":"accepted", …intent/action…}` for
  `motion_sub_type_motion.json` (suppress) and `motion_sub_type_human.json`
  (person_at_door → notify, delivered=1); both rows persisted in the state DB
  (`RING_DB_PATH`). A wrong-signature POST → 401.
- `GET /` → 200, `GET /account-link` → 200, `GET /oauth/callback` → 501 (by
  design until S2), `GET /healthz` → 200.

Note: with Telegram creds in `.env`, the notify verdict also sends a real
Telegram message — two were sent during this verification.

## Not yet wired (S2, see REGISTRATION-GUIDE §5 and STATUS.md)

Account linking needs a Ring login; staging test users require US-located
devices under an active Ring Protection plan — out of scope for this build.
The demo path remains the documented webhook contract replayed through the
verifier, sanctioned by the hackathon rules.

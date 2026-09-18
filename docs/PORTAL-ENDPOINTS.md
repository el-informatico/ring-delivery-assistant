# Portal endpoints (staging tab) — paste-ready URLs

Verified live on 2026-09-17 through the tunnel below (re-verified twice as
the tunnel reconnected — the URL changes each reconnect; see below). Portal
reference:
<https://developer.amazon.com/docs/ring/api-documentation.html#13-configure-endpoints>.

## Paste these into the Ring Developer Portal → Staging tab

Current base: `99d36bed0c2cc7.lhr.life` (2026-09-17 late evening). Earlier
bases `aaab70143ca90d.lhr.life` and `33f48535360323.lhr.life` are **dead** —
if the portal still shows one of those, re-paste all four rows below.

| Endpoint field | URL | Paste note |
|---|---|---|
| **Webhook URL** | `https://99d36bed0c2cc7.lhr.life/webhooks/ring` | Live v1.1 wire contract; HMAC-verified (bad signature → 401) |
| **Token Exchange URL** | `https://99d36bed0c2cc7.lhr.life/oauth/callback` | Real exchange machine (S2): posted code goes to oauth.ring.com; needs a code only Ring can mint |
| **Account Link URL** | `https://99d36bed0c2cc7.lhr.life/account-link` | Honest gate page (US-device constraint, guide §5) |
| **App Homepage URL** | `https://99d36bed0c2cc7.lhr.life/` | Landing page listing the endpoints |

## Tunnel facts (localhost.run, no signup)

- The base `99d36bed0c2cc7.lhr.life` is **ephemeral** — it changes every time
  the tunnel reconnects (twice during this session). If the URL
  stops resolving, re-run (from the repo):

  ```bash
  # server (port 8000 was taken by an unrelated service; ours is 8200)
  uv run --extra server uvicorn ring_assistant.server:app --host 0.0.0.0 --port 8200 &
  # tunnel — WSL: forward to the LAN IP, not 127.0.0.1 (mirrored-network blackhole)
  ssh -R 80:<LAN-IP>:8200 nokey@localhost.run
  #   (log the URL: append `> /tmp/ring-tunnel.log 2>&1` and read the
  #   "tunneled with tls termination" line, then update this file's table)
  ```

  Then update the four URLs in the portal with the new base.
- TLS terminates at localhost.run; the leg to the LAN IP is plain HTTP on the
  home network (fine for staging).

## End-to-end verification performed (2026-09-17, re-run on the current base)

From outside via the public URL, with a fixture payload signed using
`RING_WEBHOOK_SECRET` from `.env`:

- `GET /healthz` → **200**; `GET /` → 200; `GET /account-link` → 200.
- `POST /webhooks/ring` (motion fixture, suppress verdict — no Telegram sent):
  correct signature → **200** `{"status":"accepted","intent":"motion_noise",
  "action":"suppress","delivered":0}` (this fixture was also delivered on the
  earlier bases, so it replays as `duplicate` — still a 200 accept; rows
  persist in the state DB). A wrong-signature POST → **401**.
- `POST /oauth/callback` (Token Exchange URL): no code → **400** `bad_request`.
  No bogus-code probe was run this round (the 403 datum below stands from the
  first verification; probing oauth.ring.com again adds nothing).
- Earlier rounds on the dead bases additionally proved: human fixture →
  person_at_door → notify with a real Telegram delivery (delivered=1), and a
  bogus code → 502 `token_exchange_failed` (see the honest note below).

Note: with Telegram creds in `.env`, a notify verdict also sends a real
Telegram message — two were sent during the first verification round.

## Honest note on the 403 from oauth.ring.com

The bogus-code probe drew HTTP 403 (not `400 invalid_grant`) from Ring's
token endpoint. Plausible causes, unresolvable without the human gate:
Ring's edge may reject requests from this machine's egress IP / the
anonymous-tunnel origin, or the staging credentials may only accept
exchanges once a real authorization exists. The exchange machine itself is
pinned by unit tests against the documented contract
(`tests/test_oauth.py`, `tests/test_server.py` — mock token endpoint,
zero sockets).

## Not yet reachable (S2 human gate, see REGISTRATION-GUIDE §5)

Exchanging a REAL code needs a Ring user (with US-located devices under an
active Protection plan, per guide §5) to click Authorize on the staging app —
out of scope for this build. When that happens: the code POSTs to the Token
Exchange URL above, the bundle persists to `RING_TOKEN_STORE`
(set it in `.env`, e.g. `ring-tokens.json`), and the access token from that
file becomes `RING_API_TOKEN`. `uv run mint-token <code>` / `--refresh` do
the same by hand. The demo path remains the documented webhook contract
replayed through the verifier, sanctioned by the hackathon rules.

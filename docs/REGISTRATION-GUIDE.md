# Ring developer registration — S1 gate runbook (human steps)

Goal: register on the Ring Developer Portal, create an app, and walk out
with three credentials. This is the S1 gate of the sprint plan: **due
≤ 19-Sep**, budget ~30–45 minutes. Everything else in this repo keeps
working offline while you do it.

**Kill condition:** if registration is impossible from Peru (account
rejected, identity verification exhausted, portal unreachable), stop and
report back the same day — the fallback track activates immediately. See
"Region, fees, waits" below for why we do not expect this to fire.

---

## 0. Have on hand

- Your Amazon account login (or create one — any email works).
- Your **passport** (or national ID card / driver's license).
  Peru-issued is fine: Amazon accepts IDs from 249 countries and
  territories, and "the country that issued the ID doesn't need to be
  the same as your country of residence"
  (https://developer.amazon.com/docs/app-submission/identity-verification.html).
- Phone for 2FA codes.
- 15 minutes of patience for the identity check (it "typically
  completes within minutes", 3 attempts allowed —
  https://developer.amazon.com/docs/ring/get-started.html#account-identity-verification).

## 1. Amazon developer account (skip if you already have one)

1. Go to **https://developer.amazon.com/ring/console** — this is the
   Ring Developer Portal entry point cited by the official getting
   started guide ("You can request registration at
   signup/registration",
   https://developer.amazon.com/docs/ring/get-started.html#developer-registration).
2. Sign in with your Amazon account. You will be walked through the
   developer profile: contact info, business/organization details
   (sole proprietor / individual is fine), and the use cases your app
   will implement. For use cases, a one-liner is enough, e.g.:
   *"Personal doorbell-event assistant: classify motion/ding events and
   route actionable notifications."*
3. **Before submitting ID photos**, open **My Settings → Company
   Profile** and make the **Full legal name** match your passport
   exactly — including middle names. This is the #1 documented cause of
   verification failure
   (https://developer.amazon.com/docs/app-submission/identity-verification.html).

## 2. Identity verification (one-time)

You should see an "Account Identity Verification Required" banner in
the console (if none appears, this step may already be satisfied —
continue).

1. Click the banner's link → **Verify Identity**.
2. Select ID type **Passport** and issuing country **Peru**.
3. Upload or webcam-capture **front and back** photos: all four
   corners visible, no glare/blur, unexpired document.
4. Continue → status shows within minutes; the page auto-refreshes.

Reference: https://developer.amazon.com/docs/app-submission/identity-verification.html
(tips and troubleshooting at
https://developer.amazon.com/docs/app-submission/id-photos.html).

## 3. Create the app and SAVE THE CREDENTIALS

1. In the console go to the **apps** page:
   https://developer.amazon.com/ring/console/apps
2. You will see two tabs: **Public Apps** and **Private Apps**
   (https://developer.amazon.com/docs/ring/configure.html).
   **Choose the Private tab** — a Private app is "exclusively for
   personal use or testing"
   (https://developer.amazon.com/docs/ring/publish.html#private-use-apps):
   no certification, no store listing, name + optional icon only. That
   is exactly our hackathon shape; we are NOT publishing to the Ring
   App Store (the hackathon submission lives on Devpost). If the
   Private tab turns out to gate something we later need, creating a
   Public app afterwards costs the same (free) — nothing is lost.
3. Click **Create New App**:
   - **App name:** `delivery-assistant` (anything; it is your label).
   - **App type:** Private / personal use (as offered).
   - **API scopes:** enable the **Cameras and Doorbell** group — this
     covers **Motion Events**, **Doorbell press events**, Livestream
     and Video Download (the group toggles as a whole). Motion + ding
     are the two webhook types this repo consumes today; the media
     scopes feed the S3 snapshot-classifier work.
4. Confirm dialog: tick the **Ring Appstore Schedule** checkbox and
   confirm (it appears for both tracks; committing to the schedule
   does not publish anything).
5. **THE CRITICAL SCREEN — App Credentials.** You receive exactly
   three values, **shown ONCE**:

   | Credential | What it is | Where it goes |
   |---|---|---|
   | **Client ID** | OAuth app identifier | `.env` → `RING_CLIENT_ID` (S2 OAuth work) |
   | **Client Secret** | OAuth app authenticator | `.env` → `RING_CLIENT_SECRET` (S2) |
   | **HMAC Signature Key** | Webhook signing key + account-linking nonces | `.env` → `RING_WEBHOOK_SECRET` (live, today) |

   Use the **Copy** buttons or **Download CSV**, store the CSV
   somewhere safe (password manager), tick **"I've saved my
   credentials"**, then Continue. The same credentials serve staging
   and production
   (https://developer.amazon.com/docs/ring/api-documentation.html#12-obtain-credentials).
   **If you ever lose them there is no regenerate** — the only path is
   delete-the-app + create-a-new-one (fine before certification;
   https://developer.amazon.com/docs/ring/configure.html#replacing-your-app-credentials).

## 4. Configure endpoints (staging) — can partly be stubs today

The portal asks for four HTTPS endpoints
(https://developer.amazon.com/docs/ring/api-documentation.html#13-configure-endpoints):

| Endpoint | Purpose | What to enter now |
|---|---|---|
| **Webhook URL** | Ring POSTs signed events here | `https://<your-tunnel>/webhooks/ring` — must be publicly reachable HTTPS |
| **Token Exchange URL** | Receives OAuth codes | `https://<your-tunnel>/oauth/callback` — the real exchange machine (S2 built; see §5b) |
| **Account Link URL** | Your login page for linking | `https://<your-tunnel>/account-link` — honest gate page until a Ring login exists |
| **App Homepage URL** | Post-linking config page | `https://<your-tunnel>/` (the served landing page) |

The live URLs used for this app's staging tab (plus the tunnel
re-establish commands) live in `docs/PORTAL-ENDPOINTS.md`.

Practical notes:

- Your laptop is not publicly reachable. Use any HTTPS tunnel you
  already use, or a free one (e.g. ngrok or localhost.run), and put the
  tunnel base URL in front of the paths above. Configure **staging**
  endpoints first — the portal has separate Staging/Production tabs
  (https://developer.amazon.com/docs/ring/configure.html).
- Local server to point the tunnel at:
  `uv run --extra server uvicorn ring_assistant.server:app --port 8000`
  (route: `POST /webhooks/ring`). It 401s anything whose
  `X-Signature` header does not match `RING_WEBHOOK_SECRET`.
- The three non-webhook endpoints can be honest stubs for S1; the full
  account-linking/OAuth machine is S2 scope.

## 5. (Not the gate) Staging test users — blocked for us, and that's OK

The portal's Test section lets you add up to 10 **staging users** via
"Log in with Ring" — but real-device testing requires a Ring account
with an **active Ring Protection plan** and registered devices, and the
docs warn: **"Only US Located Devices: Currently we only support
devices located in the US"**
(https://developer.amazon.com/docs/ring/develop.html#test). We own no
Ring hardware, so live device events are out of scope for this project.
Our demo path is the documented webhook contract replayed through this
repo's verifier — explicitly sanctioned by the hackathon rules ("The
demo video must show the project working through a simulator or an
actual Ring device you have for testing",
https://amazonappdev2026.devpost.com/rules). Registration + credentials
are still worth it: they unlock the portal, the real signing key, and
any future live window.

## 5b. The token exchange is BUILT — what a real mint needs from a human

The S2 machine exists and runs: the served `/oauth/callback` (the portal's
Token Exchange URL) takes the authorization code Ring POSTs there
(backend-to-backend, one-time code, 60 s lifetime), exchanges it at
`https://oauth.ring.com/oauth/token` (documented confidential-client grant:
`grant_type=authorization_code` + `client_id` + `code` + `client_secret` in
a form body — **no PKCE**, the docs authenticate with the client secret,
server-to-server only), and persists the token bundle (access ~4 h, refresh
~30 d) to `RING_TOKEN_STORE` (set it in `.env`, e.g. `RING_TOKEN_STORE=
ring-tokens.json` — gitignored, owner-only permissions). `uv run
mint-token <code>` and `uv run mint-token --refresh` run the same machine
by hand. All of it is pinned by mock-transport tests (`tests/test_oauth.py`,
`tests/test_server.py`); tokens are never echoed, logged, or committed.

What NO code can do for us — the honest gate: a real `access_token` only
exists after a Ring USER authorizes the app, and per §5 a staging login
needs a Ring account with US-located devices under an active Protection
plan. If such an account ever becomes available, minting is exactly:

1. Server + tunnel up (`docs/PORTAL-ENDPOINTS.md`), `.env` carrying
   `RING_CLIENT_ID`/`RING_CLIENT_SECRET`/`RING_TOKEN_STORE`.
2. The Ring user opens the staging app in the Ring AppStore flow and
   clicks **Authorize** (scopes → device selection → Confirm).
3. Ring POSTs the one-time code to the Token Exchange URL; the server
   answers `200 {"status": "token_exchanged", …}` and the bundle lands in
   the store file — nothing to click on our side.
4. Copy the `access_token` from the store file into `.env` as
   `RING_API_TOKEN` (never chat, never commit); snapshots then go live
   (`ApiSnapshotSource`). Refresh before ~4 h with
   `uv run mint-token --refresh` and re-paste.

Until then `RING_API_TOKEN` stays empty and the submission stands on the
rules-sanctioned documented-contract replay (§5). One observed datum from
2026-09-17: a deliberately bogus code POSTed through the public tunnel
reached `oauth.ring.com` and drew HTTP 403 (edge/egress rejection or no
pending authorization) — recorded honestly in `docs/PORTAL-ENDPOINTS.md`.

## 6. Paste into `.env` — never chat, never commit

Copy `.env.example` → `.env` (gitignored) and fill:

```bash
RING_WEBHOOK_SECRET=<HMAC Signature Key from the credentials screen>
RING_SIGNATURE_HEADER=x-signature     # Ring's live header name (docs:
                                      # https://developer.amazon.com/docs/ring/api-documentation.html#webhook-authentication--verification)
RING_CLIENT_ID=<Client ID>            # feeds /oauth/callback + mint-token (§5b)
RING_CLIENT_SECRET=<Client Secret>    # feeds /oauth/callback + mint-token (§5b)
RING_TOKEN_STORE=ring-tokens.json     # where a successful exchange persists
```

The repo never needs the values in chat or in git — `.env` is the only
home. Sanity check afterwards, fully offline:

```bash
uv run pytest          # whole suite green
uv run demo            # end-to-end replay through verifier + routing
```

## 7. Region, fees, waits

| Question | Answer | Source |
|---|---|---|
| Region restriction on the *developer*? | None found. IDs accepted from 249 countries/territories; issuing country may differ from residence. Peru passport listed as accepted type. | https://developer.amazon.com/docs/app-submission/identity-verification.html |
| Fees? | None found for registration. Publishing on the Ring App Store is free (10% revenue share on *paid* subscriptions — we publish nothing). | https://developer.amazon.com/docs/ring/get-started.html#how-much-does-it-cost-to-publish-an-application |
| How long? | Identity verification "typically completes within minutes" (3 attempts). App creation + credentials are immediate. | get-started.html#account-identity-verification, configure.html |
| Certification needed? | No — Private apps skip it entirely; the hackathon does not require App Store publishing. (For reference: 90% of submissions reviewed < 48h.) | publish.html#private-use-apps, get-started.html#how-long-does-certification-take |
| Devices? | Real-device staging testing needs US-located Ring devices on a plan — not our path; simulator/fixture demo is rules-sanctioned. | develop.html#test, https://amazonappdev2026.devpost.com/rules |

## 8. If it fails

Any hard stop (account rejected, identity verification failed 3×,
portal refuses the region): capture a screenshot of the exact blocker.
That is the S1 kill condition — the fallback track activates the same
day and this repo is archived as-is.

---

## Appendix — every claim's source

- Getting started (registration, identity verification, terminology,
  sandbox definition, fees FAQ):
  https://developer.amazon.com/docs/ring/get-started.html
- Configure (click-by-click app creation, credentials screen, endpoint
  tabs, credential replacement):
  https://developer.amazon.com/docs/ring/configure.html
- API reference (credentials once + shared across environments,
  endpoint configuration table, environment URLs, webhook v1.1
  contract, X-Signature, testing tools, staging notes):
  https://developer.amazon.com/docs/ring/api-documentation.html
- Develop/Test (staging users, Ring Protection plan, US-devices
  warning): https://developer.amazon.com/docs/ring/develop.html
- Identity verification detail (ID types, 249 countries, photos):
  https://developer.amazon.com/docs/app-submission/identity-verification.html
- Private Use Apps: https://developer.amazon.com/docs/ring/publish.html#private-use-apps
- Hackathon rules (simulator acceptable, no device required):
  https://amazonappdev2026.devpost.com/rules and
  https://community.amazondeveloper.com/t/build-ship-shape-amazon-developer-hackathon/29097

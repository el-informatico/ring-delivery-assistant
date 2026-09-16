# Wire fixture provenance

Every fixture here is the documented example payload from Ring's
official Partner API webhook reference — captured verbatim except for
placeholder substitution (noted per file). If Ring's docs change, these
files are the diff surface: what the verifier is pinned against.

Source page (all anchors):
https://developer.amazon.com/docs/ring/api-documentation.html

| Fixture | Docs section | Deviations from the published example |
|---|---|---|
| `motion_sub_type_motion.json` | [Motion Detection](https://developer.amazon.com/docs/ring/api-documentation.html#motion-detection) | `<device_id>` placeholders → `ava1.ring.device.door001` (id format from the multi-camera example on the same page). Everything else verbatim — including the example's own `meta.time` (2026-02-13) sitting months before `data.attributes.timestamp` (2026-08-14), kept as-is because quirks in the source are part of the contract. **Pinned finding:** that timestamp (1786715596787) is 2026-08-14 **13:53:16 UTC**, while the example's `timestamp_readable` says "08:53:16" — exactly 5h off, i.e. a local-time rendering. Epoch ms is the only time field the adapter parses. |
| `motion_sub_type_human.json` | [Motion Detection](https://developer.amazon.com/docs/ring/api-documentation.html#motion-detection), [Event Types](https://developer.amazon.com/docs/ring/api-documentation.html#event-types) | Same example with the documented `sub_type` value `human` (per the Event Types table: `motion_detected` sub-types are motion / human / vehicle / other_motion) and `timestamp` shifted +5 min so its event id differs from the motion fixture; fresh `request_id`. No field the docs don't show. |
| `button_press.json` | [Button Press](https://developer.amazon.com/docs/ring/api-documentation.html#button-press) | `<device_id>` → `ava1.ring.device.door001`. Verbatim otherwise: no `sub_type`, no `timestamp_readable` — the docs example carries neither. |
| `subscription_activated.json` | [Subscription Activated](https://developer.amazon.com/docs/ring/api-documentation.html#subscription-activated) | `<subscription_id>`-style placeholders → concrete `ava1.ring.subscription.*` / `evt_subscription_001` ids; the device relationship points at the same `door001` device as the other fixtures. Verbatim shape: `source: "subscriptions"`, `sub_type: "paid"`, `plan_id`, `expires_at`, subscriptions link. Used as the canonical "documented but ignored" event type. |

Why these four: motion (sub-type noise path), human motion (person
path), button press (ding path, no sub-type) cover every intent-bearing
shape the pipeline consumes; subscription_activated exercises the
ack-and-ignore path for the eight documented types it deliberately does
not (device lifecycle, app integration, subscriptions).

Signature scheme for replaying these against the local server is
documented at [Webhook Authentication & Verification](
https://developer.amazon.com/docs/ring/api-documentation.html#webhook-authentication--verification):
`X-Signature: sha256=<hex HMAC-SHA256 of the raw request body>` —
`ring_assistant.wire.signed_v1_1()` produces exactly that pair.

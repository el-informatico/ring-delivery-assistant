# Ring Delivery & Safety Assistant

An **intent + routing layer** for Ring-style doorbell event streams, built
as an offline-first OSS library skeleton. Apache-2.0.

> **Positioning (read this first).** Ring already ships native Package
> Alerts and Person Alerts — this project is *not* another package
> detector. The gap it fills is everything *after* detection: what the
> event **means** across events (a deposit was later picked up), and
> **who gets told what** (package deposited → notify; stranger at the
> door at night → escalate; motion-only noise → suppressed unless it
> repeats). Detection is an input (`sub_type`), not the product.

## What is real vs synthetic

This repository is the **offline skeleton**: no Ring hardware, no real
credentials, no network calls in tests or the demo. Events come from a
deterministic synthetic generator (seeded); snapshots are generated PNG
fixtures; the HMAC secret is a test/demo key. The LLM adapter is real
code with a configurable endpoint, exercised in tests through an
injected fake transport — never a live model.

See `USAGE.md` for exact commands and `VALIDATION.md` for test/demo
output and honest caveats.

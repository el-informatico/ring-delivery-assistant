"""Offline demo: replay a synthetic webhook stream through the pipeline.

Generates a deterministic two-day event sequence (snapshots included),
signs every payload as Ring would, and feeds it through the SAME
``Pipeline.handle`` the FastAPI adapter uses: verification ->
normalization -> rule-stub classification -> SQLite state machine ->
intent-conditioned routing. Prints the timeline and a summary.

Output is byte-reproducible for a given seed: fixed dates, no
wall-clock, fixed ordering. Secrets: uses ``RING_WEBHOOK_SECRET`` from
the environment when set, otherwise a clearly-labeled synthetic demo
secret (never a real credential).
"""

from __future__ import annotations

import argparse
import shutil
from collections import Counter
from pathlib import Path

from .classify import RuleBasedClassifier
from .replay import Pipeline, TimelineEntry, format_timeline
from .routing import Router
from .settings import Settings
from .state import StateStore
from .synth import DEFAULT_SEED, generate_to, signed_webhooks

DEMO_DIR = Path(".demo")
DEMO_SECRET = "demo-webhook-secret"


def run_demo(seed: int, out_dir: Path = DEMO_DIR) -> list[TimelineEntry]:
    """Generate + replay; returns the timeline entries for printing."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    events_path, payloads = generate_to(out_dir, seed)

    settings = Settings.from_env()
    secret = settings.webhook_secret or DEMO_SECRET
    secret_label = (
        "from RING_WEBHOOK_SECRET" if settings.webhook_secret else f"synthetic ({DEMO_SECRET})"
    )

    store = StateStore(out_dir / "state.db")
    router = Router.from_settings(settings)
    pipeline = Pipeline(
        secret=secret,
        classifier=RuleBasedClassifier(),
        store=store,
        router=router,
        signature_header=settings.signature_header,
    )

    print(f"ring-delivery-assistant offline demo")
    print(f"seed: {seed}   events: {len(payloads)}   source: {events_path}")
    print(f"signature secret: {secret_label}")
    sink_names = ", ".join(dict.fromkeys(s.name for s in (*router.primary, *router.secondary)))
    print(f"classifier: rules (stub)   sinks: {sink_names}")
    print()

    entries = [
        pipeline.handle(body, headers) for body, headers in signed_webhooks(payloads, secret)
    ]
    store.close()
    return entries


def print_summary(entries: list[TimelineEntry]) -> None:
    actions = Counter(e.decision.action.value for e in entries)
    escalated = sum(1 for e in entries if e.decision.action.value == "escalate")
    print()
    print("summary")
    print(f"  events processed: {len(entries)}")
    print(f"  notify: {actions.get('notify', 0)}   escalate: {actions.get('escalate', 0)}   suppress: {actions.get('suppress', 0)}")
    if escalated:
        print(f"  escalations (person at night): {escalated}")
    for name, count in sorted(_delivered_by_sink(entries).items()):
        print(f"  notifications -> {name}: {count}")


def _delivered_by_sink(entries: list[TimelineEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.delivered and entry.decision.notification is not None:
            for sink in entry.decision.sinks:
                counts[sink] = counts.get(sink, 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="demo", description="Replay the synthetic event stream end-to-end (offline)."
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="generator seed")
    parser.add_argument(
        "--out", default=str(DEMO_DIR), help="working directory (default: .demo)"
    )
    args = parser.parse_args(argv)

    entries = run_demo(args.seed, Path(args.out))
    print(format_timeline(entries))
    print_summary(entries)
    print()
    print("artifacts: events.jsonl, snapshots/, state.db (in the --out directory)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

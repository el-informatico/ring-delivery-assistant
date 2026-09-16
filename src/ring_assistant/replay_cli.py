"""``replay-webhooks`` CLI: re-run captured or documented deliveries.

Two sources, one code path (``Pipeline.handle_v1_1``, the live wire):

    replay-webhooks                          # documented envelopes in
                                             # tests/fixtures/wire/
    replay-webhooks capture/webhooks.jsonl   # your own recorded traffic
        --include-rejected                   # also re-run refusals
        --db replay.db                       # keep the derived state

Today the fixtures are the verbatim examples pinned from the Partner
API documentation; the day the portal account goes live, the server's
``RING_RECORD_DIR`` capture replaces them — same command, real bytes.
State is discarded unless ``--db`` names a file; notifications go to
the recording log sink and are printed as the timeline.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from .capture import replay_capture_file, replay_fixture_dir
from .classify import RuleBasedClassifier
from .demo import DEMO_SECRET
from .replay import Pipeline
from .routing import Router
from .settings import Settings
from .state import StateStore

FIXTURE_DIR = Path("tests/fixtures/wire")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay-webhooks",
        description="Replay documented webhook fixtures or a recorded capture "
        "through the live-wire pipeline (offline).",
    )
    parser.add_argument(
        "capture",
        nargs="?",
        help="JSONL capture file recorded by the server (default: documented fixtures)",
    )
    parser.add_argument(
        "--fixtures",
        default=str(FIXTURE_DIR),
        help=f"directory of documented v1.1 envelopes (default: {FIXTURE_DIR})",
    )
    parser.add_argument(
        "--include-rejected",
        action="store_true",
        help="re-run deliveries the host rejected too (diagnosis mode)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="persist derived state to this SQLite file (default: throwaway)",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    secret = settings.webhook_secret or DEMO_SECRET

    temp = tempfile.TemporaryDirectory(prefix="ring-replay-")
    store = StateStore(args.db or Path(temp.name) / "state.db")
    router = Router.from_settings(settings)
    pipeline = Pipeline(
        secret=secret,
        classifier=RuleBasedClassifier(),
        store=store,
        router=router,
        signature_header="x-signature",
    )

    try:
        if args.capture:
            report = replay_capture_file(
                args.capture, pipeline, include_rejected=args.include_rejected
            )
            source = args.capture
        else:
            report = replay_fixture_dir(args.fixtures, pipeline)
            source = f"{args.fixtures} (documented envelopes)"
    finally:
        store.close()
        temp.cleanup()

    print(f"ring-delivery-assistant webhook replay")
    print(f"source: {source}")
    print()
    if report.entries:
        print(report.timeline())
        print()
    for outcome in report.outcomes:
        if outcome.result != "accepted":
            print(f"  {outcome.result:8s} {outcome.label}: {outcome.error}")
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

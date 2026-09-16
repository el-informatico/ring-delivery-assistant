"""``timeline`` CLI: one scripted day through the real-ingestion pipeline.

The first "live" timeline demo — every beat of the day is a real v1.1
envelope, signed and delivered exactly as Ring would deliver it
(``Pipeline.handle_v1_1``, the same edge the server uses), and every
classification consults a snapshot fetched by (device, epoch-ms) key —
the same key the Image Snapshots API uses. The snapshots are the
deterministic synthetic scenes served through ``ManifestSnapshotSource``,
so the whole day runs offline today and swaps to ``ApiSnapshotSource``
on registration day without touching this script.

Output directory (default ``.timeline``):

* ``timeline.md``      — the artifact: timeline table, notifications,
                         caveats
* ``webhooks.jsonl``   — the day AS A CAPTURE, byte-identical in shape
                         to what the live server records, so
                         ``uv run replay-webhooks .timeline/webhooks.jsonl``
                         re-runs the day with zero code change
* ``snapshots/`` + ``manifest.json`` — the offline image source
* ``state.db``         — the tracks the day derived
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .capture import CaptureRecord, write_capture
from .classify import SnapshotRuleClassifier
from .demo import DEMO_SECRET
from .imaging import render_scene
from .replay import Pipeline, TimelineEntry, format_timeline
from .routing import LogSink, Router
from .settings import Settings
from .snapshots import ManifestSnapshotSource, epoch_ms
from .state import StateStore
from .wire import build_v1_1, signed_v1_1

UTC = timezone.utc
DEVICE = "ava1.ring.device.door001"
DAY = datetime(2026, 5, 15, tzinfo=UTC)  # a Friday: parcels, then a late ring
SNAPSHOTS_DIRNAME = "snapshots"
CAPTURE_NAME = "webhooks.jsonl"
MANIFEST_NAME = "manifest.json"
ARTIFACT_NAME = "timeline.md"
RECEIPT_LAG = timedelta(seconds=4)  # scripted network delay


def _t(hour: int, minute: int, second: int = 0, ms: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute, seconds=second, milliseconds=ms)


@dataclass(frozen=True)
class Beat:
    """One scripted delivery: what happened, when, and what the camera saw.

    ``request_id`` defaults to ``beat_id``; a Ring redelivery reuses the
    ORIGINAL request id and timestamp, so a retry beat points both at
    the beat it retries and the envelope comes out byte-identical —
    which is exactly what makes the state machine answer DUPLICATE.
    """

    beat_id: str
    occurred: datetime
    event_type: str
    sub_type: str | None
    scene: str
    story: str
    request_id: str | None = None
    received_lag: timedelta = RECEIPT_LAG

    @property
    def effective_request_id(self) -> str:
        return self.request_id or self.beat_id

    @property
    def received_at(self) -> datetime:
        return self.occurred + self.received_lag


THE_DAY: tuple[Beat, ...] = (
    Beat(
        "s2-001",
        _t(8, 3, 21, 210),
        "motion_detected",
        "motion",
        "package-mat",
        "courier leaves a box; the platform only says 'motion'",
    ),
    Beat(
        "s2-002",
        _t(8, 4, 2, 805),
        "motion_detected",
        "vehicle",
        "van-drive",
        "delivery van still at the curb",
    ),
    Beat(
        "s2-003",
        _t(9, 12, 47, 338),
        "button_press",
        None,
        "person-door",
        "someone rings the bell",
    ),
    Beat(
        "s2-004",
        _t(9, 12, 47, 338),  # same event: a redelivery, not a second ring
        "button_press",
        None,
        "person-door",
        "Ring redelivers the same ding ~85 s later (retry)",
        request_id="s2-003",
        received_lag=timedelta(seconds=89, milliseconds=162),
    ),
    Beat(
        "s2-005",
        _t(12, 47, 5, 62),
        "motion_detected",
        "human",
        "person-with-box",
        "someone carries the box away",
    ),
    Beat(
        "s2-006",
        _t(13, 30, 0, 500),
        "motion_detected",
        "motion",
        "motion-empty",
        "tree shadow (1st unclassified motion in the window)",
    ),
    Beat(
        "s2-007",
        _t(13, 34, 18, 120),
        "motion_detected",
        "motion",
        "motion-empty",
        "tree shadow again (2nd)",
    ),
    Beat(
        "s2-008",
        _t(13, 38, 41, 977),
        "motion_detected",
        "motion",
        "motion-empty",
        "tree shadow a third time (3rd -> digest)",
    ),
    Beat(
        "s2-009",
        _t(22, 41, 9, 104),
        "button_press",
        None,
        "person-door-night",
        "late ring inside the night window",
    ),
)


@dataclass
class DayRun:
    entries: list[TimelineEntry] = field(default_factory=list)
    notifications: list = field(default_factory=list)  # LogSink.received
    beats: tuple[Beat, ...] = THE_DAY
    out_dir: Path = Path(".timeline")


def run_day(out_dir: Path, *, secret: str, settings: Settings | None = None) -> DayRun:
    """Play the scripted day; write snapshots, manifest, capture, state."""
    settings = settings or Settings()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    snapshots_dir = out_dir / SNAPSHOTS_DIRNAME
    snapshots_dir.mkdir()

    for scene in sorted({beat.scene for beat in THE_DAY}):
        render_scene(scene, snapshots_dir / f"{scene}.png")
    manifest = {
        f"{DEVICE}@{epoch_ms(beat.occurred)}": f"{SNAPSHOTS_DIRNAME}/{beat.scene}.png"
        for beat in THE_DAY
    }
    manifest_path = out_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    router = Router.from_settings(settings)
    pipeline = Pipeline(
        secret=secret,
        classifier=SnapshotRuleClassifier(
            source=ManifestSnapshotSource(root=out_dir, manifest_path=manifest_path)
        ),
        store=StateStore(out_dir / "state.db"),
        router=router,
        signature_header="x-signature",
    )

    entries: list[TimelineEntry] = []
    records: list[CaptureRecord] = []
    for beat in THE_DAY:
        envelope = build_v1_1(
            event_type=beat.event_type,
            device_id=DEVICE,
            timestamp_ms=epoch_ms(beat.occurred),
            request_id=beat.effective_request_id,
            sub_type=beat.sub_type,
        )
        body, headers = signed_v1_1(envelope, secret)
        entries.append(pipeline.handle_v1_1(body, headers, received_at=beat.received_at))
        records.append(
            CaptureRecord(
                received_at=beat.received_at,
                body=body,
                headers=headers,
                outcome="accepted",
                status=200,
                note=beat.story,
            )
        )
    write_capture(out_dir / CAPTURE_NAME, records)
    pipeline.store.close()

    log = next(sink for sink in router.primary if isinstance(sink, LogSink))
    run = DayRun(entries=entries, notifications=log.received, out_dir=out_dir)
    (out_dir / ARTIFACT_NAME).write_text(render_markdown(run), encoding="utf-8")
    return run


def _snapshot_said(entry: TimelineEntry) -> str:
    """The snapshot's verdict, or why there wasn't one."""
    rationale = entry.intent.rationale
    if rationale.startswith("snapshot:"):
        return rationale[len("snapshot:") :].split(" [")[0].strip()
    return "— (no verdict; platform fallback)"


def _platform_said(entry: TimelineEntry) -> str:
    return entry.event.label  # kind, or kind/sub_type


def render_markdown(run: DayRun) -> str:
    """The timeline artifact: table, notifications, caveats."""
    out = [f"# A day at the door — {DAY:%Y-%m-%d}"]
    out.append("")
    out.append(
        f"One scripted day replayed through the live-wire pipeline "
        f"(`Pipeline.handle_v1_1`: verify → normalize → snapshot-classify → state → route). "
        f"Every beat is a real v1.1 envelope, signed as Ring would sign it; each "
        f"classification consults a snapshot keyed `<device>@<epoch_ms>` — the same "
        f"key the Image Snapshots API uses, served offline from the manifest."
    )
    out.append("")
    redeliveries = sum(1 for beat in run.beats if beat.request_id)
    plural = "y" if redeliveries == 1 else "ies"
    out.append(
        f"Device `{DEVICE}` · {len(run.beats)} deliveries "
        f"(including {redeliveries} redeliver{plural}) · "
        f"{len(run.entries)} timeline entries · {len(run.notifications)} notifications · "
        f"generated by `uv run timeline`"
    )
    out.append("")
    out.append("## Timeline")
    out.append("")
    out.append(
        "| occurred | id | platform said | snapshot said | intent | action | transition | reason |"
    )
    out.append("|---|---|---|---|---|---|---|---|")
    for beat, entry in zip(run.beats, run.entries, strict=True):
        out.append(
            "| {occurred} | {id} | {platform} | {snapshot} | {intent} | {action} | "
            "{transition} | {reason} |".format(
                occurred=entry.event.occurred_at.strftime("%H:%M:%S.%f")[:-3],
                id=beat.beat_id,
                platform=_platform_said(entry),
                snapshot=_snapshot_said(entry),
                intent=entry.intent.intent.value,
                action=entry.decision.action.value,
                transition=entry.apply_result.transition.value,
                reason=entry.decision.reason,
            )
        )
    out.append("")
    out.append(
        "The 08:03 row is the S2 story in one line: the platform's own vocabulary "
        "never says `package_delivery` on the live wire, so the snapshot is what "
        "turns unclassified motion into a deposit."
    )
    out.append("")
    out.append("## Notifications (what would have gone out)")
    out.append("")
    if run.notifications:
        for note in run.notifications:
            out.append(
                f"- **{note.at:%H:%M:%S} · {note.title}** ({note.severity.value}) — {note.body}"
            )
    else:
        out.append("- (none)")
    out.append("")
    out.append("## Replay the day")
    out.append("")
    capture = run.out_dir / CAPTURE_NAME
    out.append("```console")
    out.append(f"$ uv run replay-webhooks {capture} --snapshots {run.out_dir}")
    out.append("```")
    out.append("")
    out.append(
        "The capture is the same JSONL the live server writes under "
        "`RING_RECORD_DIR`, and `--snapshots` points the classifier at the "
        "manifest source — together they reproduce the table above row for "
        "row. Without `--snapshots` the replay classifies from platform "
        "fields alone (a wiring check: 08:03 reads as unclassified motion). "
        "On registration day the recorded traffic replaces the capture and "
        "`RING_API_TOKEN` replaces the manifest; the command is unchanged."
    )
    out.append("")
    out.append("## Caveats")
    out.append("")
    out.append(
        "- Snapshots are the six deterministic synthetic scenes (`imaging.py`); the "
        "classifier thresholds are calibrated against them. Real Ring snapshots are "
        "watermarked JPEG, which the stdlib decoder refuses by design — those route "
        "to the LLM classifier (`llm.py`) instead of the pixel rules."
    )
    out.append(
        "- `received_at` times are scripted (4 s after occurrence; the redelivery "
        "+85 s), not measured network delay. Occurrence times carry milliseconds "
        "because they are manifest keys, and the manifest keying is the API keying."
    )
    out.append(
        "- Signatures use the demo secret (or `RING_WEBHOOK_SECRET` when set). The "
        "capture holds no secrets: replay re-signs with the local secret."
    )
    out.append(
        f"- `{run.out_dir / 'state.db'}` holds the tracks the day derived; "
        "re-running the CLI regenerates the whole directory."
    )
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="timeline",
        description="Play one scripted day through the real-ingestion pipeline "
        "and write the timeline artifact (offline).",
    )
    parser.add_argument(
        "--out",
        default=".timeline",
        help="output directory (default: .timeline)",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    secret = settings.webhook_secret or DEMO_SECRET
    run = run_day(Path(args.out), secret=secret, settings=settings)

    artifact = run.out_dir / ARTIFACT_NAME
    print(format_timeline(run.entries))
    print()
    for note in run.notifications:
        print(f"  [{note.severity.value:8s}] {note.at:%H:%M:%S} {note.title}: {note.body}")
    print()
    print(f"{len(run.entries)} deliveries, {len(run.notifications)} notifications")
    print(f"artifact: {artifact}")
    print(f"capture:  {run.out_dir / CAPTURE_NAME}")
    print(f"replay:   uv run replay-webhooks {run.out_dir / CAPTURE_NAME} --snapshots {run.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The scripted-day demo: beats, artifact, and replay parity.

``run_day`` plays THE_DAY through the live-wire edge
(``Pipeline.handle_v1_1``) with the offline manifest snapshot source —
the same composition registration day swaps for ``ApiSnapshotSource``.
These tests pin the story the artifact tells: the snapshot overrules
the platform on deposits, redeliveries dedupe, bursts digest on the
third, and night escalates.
"""

from __future__ import annotations

import json

import pytest

from ring_assistant.capture import read_capture, replay_capture_file
from ring_assistant.classify import SnapshotRuleClassifier
from ring_assistant.replay import Pipeline
from ring_assistant.routing import Action, Router
from ring_assistant.schema import Intent
from ring_assistant.settings import Settings
from ring_assistant.snapshots import ManifestSnapshotSource, epoch_ms
from ring_assistant.state import StateStore, Transition
from ring_assistant.timeline import (
    ARTIFACT_NAME,
    CAPTURE_NAME,
    DEVICE,
    MANIFEST_NAME,
    THE_DAY,
    main,
    render_markdown,
    run_day,
)

SECRET = "timeline-test-secret"

# (intent, action, transition) per beat, in THE_DAY order
EXPECTED_DAY = [
    (Intent.PACKAGE_DEPOSITED, Action.NOTIFY, Transition.TRACK_OPENED),
    (Intent.VEHICLE_AT_DOOR, Action.NOTIFY, Transition.NO_TRACK_CHANGE),
    (Intent.PERSON_AT_DOOR, Action.NOTIFY, Transition.NO_TRACK_CHANGE),
    (Intent.PERSON_AT_DOOR, Action.SUPPRESS, Transition.DUPLICATE),
    (Intent.PACKAGE_PICKED_UP, Action.NOTIFY, Transition.TRACK_CLOSED),
    (Intent.MOTION_NOISE, Action.SUPPRESS, Transition.NO_TRACK_CHANGE),
    (Intent.MOTION_NOISE, Action.SUPPRESS, Transition.NO_TRACK_CHANGE),
    (Intent.MOTION_NOISE, Action.NOTIFY, Transition.NO_TRACK_CHANGE),
    (Intent.PERSON_AT_DOOR, Action.ESCALATE, Transition.NO_TRACK_CHANGE),
]


@pytest.fixture
def run(tmp_path):
    return run_day(tmp_path / "day", secret=SECRET, settings=Settings())


def _snapshotted_pipeline(run, db_path):
    return Pipeline(
        secret=SECRET,
        classifier=SnapshotRuleClassifier(
            source=ManifestSnapshotSource(
                root=run.out_dir, manifest_path=run.out_dir / MANIFEST_NAME
            )
        ),
        store=StateStore(db_path),
        router=Router.from_settings(Settings()),
        signature_header="x-signature",
    )


def test_the_day_produces_the_scripted_outcomes(run):
    outcomes = [
        (e.intent.intent, e.decision.action, e.apply_result.transition)
        for e in run.entries
    ]
    assert outcomes == EXPECTED_DAY


def test_snapshot_overrules_the_platform_on_the_deposit(run):
    # 08:03 is bare motion on the wire (no sub_type survives adaptation);
    # only the snapshot makes it a deposit
    first = run.entries[0]
    assert first.event.sub_type is None
    assert first.intent.rationale.startswith("snapshot: package present")


def test_the_redelivery_is_the_same_event_seen_twice(run):
    original, retry = run.entries[2], run.entries[3]
    assert retry.event.event_id == original.event.event_id
    assert retry.event.received_at > original.event.received_at  # +85 s later
    assert retry.apply_result.transition is Transition.DUPLICATE


def test_burst_digest_fires_on_the_third_motion_only(run):
    motions = [e for e in run.entries if e.intent.intent is Intent.MOTION_NOISE]
    assert [e.decision.action for e in motions] == [
        Action.SUPPRESS,
        Action.SUPPRESS,
        Action.NOTIFY,
    ]
    digest = motions[-1].decision.notification
    assert digest is not None and digest.title == "Repeated motion"


def test_night_ring_escalates_with_critical_severity(run):
    last = run.entries[-1]
    assert last.decision.action is Action.ESCALATE
    assert last.decision.notification.severity.value == "critical"


def test_notifications_are_the_expected_six(run):
    assert [n.title for n in run.notifications] == [
        "Package deposited",
        "Vehicle at the door",
        "Person at the door",
        "Package picked up",
        "Repeated motion",
        "Person at the door at night",
    ]


def test_manifest_keys_are_device_at_epoch_ms(run):
    manifest = json.loads((run.out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    for beat in THE_DAY:
        assert manifest[f"{DEVICE}@{epoch_ms(beat.occurred)}"].startswith("snapshots/")
    # the milliseconds are load-bearing: they are the API's key too
    assert any("1778832201210" in key for key in manifest)  # 08:03:21.210


def test_the_day_writes_all_its_files(run):
    expected = (
        run.out_dir / ARTIFACT_NAME,
        run.out_dir / CAPTURE_NAME,
        run.out_dir / MANIFEST_NAME,
        run.out_dir / "state.db",
        run.out_dir / "snapshots" / "person-door-night.png",
    )
    assert all(path.is_file() for path in expected)
    assert len(read_capture(run.out_dir / CAPTURE_NAME)) == len(THE_DAY)


def test_capture_replays_to_the_same_day(run, tmp_path):
    # the row-for-row claim in the artifact: capture + manifest source
    pipeline = _snapshotted_pipeline(run, tmp_path / "replay.db")
    report = replay_capture_file(run.out_dir / CAPTURE_NAME, pipeline)
    pipeline.store.close()
    assert report.counts == {"accepted": len(THE_DAY), "ignored": 0, "rejected": 0}
    assert [e.intent.intent for e in report.entries] == [
        e.intent.intent for e in run.entries
    ]


def test_rerunning_regenerates_the_directory_from_scratch(run, tmp_path):
    stale = run.out_dir / "stale.txt"
    stale.write_text("left over", encoding="utf-8")
    again = run_day(run.out_dir, secret=SECRET, settings=Settings())
    assert not stale.exists()
    assert [e.intent.intent for e in again.entries] == [
        e.intent.intent for e in run.entries
    ]


def test_render_markdown_carries_the_story(run):
    markdown = render_markdown(run)
    assert "# A day at the door — 2026-05-15" in markdown
    assert "| occurred | id | platform said | snapshot said |" in markdown
    assert "package present, no person" in markdown
    assert "— (no verdict; platform fallback)" in markdown  # empty scenes
    assert "## Notifications (what would have gone out)" in markdown
    assert "Person at the door at night" in markdown
    assert f"replay-webhooks {run.out_dir / CAPTURE_NAME} --snapshots" in markdown
    assert "## Caveats" in markdown


def test_cli_main_writes_the_artifact(tmp_path, capsys):
    out = tmp_path / "cli"
    assert main(["--out", str(out)]) == 0
    assert (out / ARTIFACT_NAME).is_file()
    printed = capsys.readouterr().out
    assert "artifact:" in printed and str(out / ARTIFACT_NAME) in printed

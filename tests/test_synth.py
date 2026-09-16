"""Synthetic generator: determinism, storyboard shape, snapshot fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ring_assistant import imaging, synth


def parse_ts(payload: dict, key: str) -> datetime:
    return datetime.fromisoformat(payload[key])


def test_same_seed_identical_sequence():
    assert synth.build_sequence(seed=42) == synth.build_sequence(seed=42)


def test_different_seed_changes_jitter_not_shape():
    a = synth.build_sequence(seed=1)
    b = synth.build_sequence(seed=2)
    assert a != b  # jitter differs
    assert [p["id"] for p in a] == [p["id"] for p in b]  # arrival order fixed
    assert [p["type"] for p in a] == [p["type"] for p in b]


def test_storyboard_mix_present():
    payloads = synth.build_sequence()
    types = {p["type"] for p in payloads}
    assert types == {"ding", "motion_detected"}
    sub_types = {p["sub_type"] for p in payloads}
    assert {None, "human", "vehicle", "package_delivery", "package_pickup"} <= sub_types


def test_noise_burst_within_ten_minutes():
    payloads = synth.build_sequence()
    noise = [
        parse_ts(p, "occurred_at")
        for p in payloads
        if p["type"] == "motion_detected"
        and p["sub_type"] is None
        and parse_ts(p, "occurred_at").date().isoformat() == "2026-09-14"
    ]
    assert len(noise) == 3
    assert max(noise) - min(noise) <= timedelta(minutes=10)


def test_out_of_order_pair_inverts_arrival():
    payloads = synth.build_sequence()
    by_id = {p["id"]: p for p in payloads}
    late_deposit = by_id["evt-010"]  # the day-2 deposit that arrives late
    day2_pickup = by_id["evt-009"]  # the day-2 pickup that arrives first
    # deposit happened earlier but arrived later:
    assert parse_ts(late_deposit, "occurred_at") < parse_ts(day2_pickup, "occurred_at")
    assert parse_ts(late_deposit, "received_at") > parse_ts(day2_pickup, "received_at")
    # and in arrival order the pickup is processed first:
    order = [p["id"] for p in payloads]
    assert order.index(day2_pickup["id"]) < order.index(late_deposit["id"])


def test_duplicate_event_id_present():
    payloads = synth.build_sequence()
    ids = [p["id"] for p in payloads]
    assert len(ids) == len(set(ids)) + 1
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert duplicates == {"evt-004"}


def test_file_order_is_arrival_order():
    payloads = synth.build_sequence()
    received = [parse_ts(p, "received_at") for p in payloads]
    assert received == sorted(received)


def test_generate_to_writes_jsonl_and_png_snapshots(tmp_path):
    events_path, payloads = synth.generate_to(tmp_path, seed=7)
    assert events_path.exists()
    assert synth.load_sequence(events_path) == payloads
    snapshots = sorted((tmp_path / "snapshots").glob("*.png"))
    assert len(snapshots) >= 5
    for snap in snapshots:
        assert snap.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert snap.stat().st_size > 100


def test_generate_to_deterministic_bytes(tmp_path_factory):
    dir_a = tmp_path_factory.mktemp("a")
    dir_b = tmp_path_factory.mktemp("b")
    path_a, _ = synth.generate_to(dir_a, seed=99)
    path_b, _ = synth.generate_to(dir_b, seed=99)
    assert path_a.read_bytes() == path_b.read_bytes()
    for png_a in sorted((dir_a / "snapshots").glob("*.png")):
        png_b = dir_b / "snapshots" / png_a.name
        assert png_a.read_bytes() == png_b.read_bytes()


def test_signed_webhooks_roundtrip(secret):
    payloads = synth.build_sequence(seed=3)[:2]
    pairs = synth.signed_webhooks(payloads, secret)
    import json

    from ring_assistant.ingest import receive_webhook
    from ring_assistant.schema import RingEvent

    events = [receive_webhook(body, headers, secret) for body, headers in pairs]
    assert all(isinstance(e, RingEvent) for e in events)
    assert json.loads(pairs[0][0])["id"] == payloads[0]["id"]


def test_cli_main(tmp_path, capsys):
    rc = synth.main(["--seed", "5", "--out", str(tmp_path / "out")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "seed=5" in out and "events=" in out
    assert (tmp_path / "out" / "events.jsonl").exists()


def test_scene_rendering_deterministic(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    for scene in imaging.SCENE_NAMES:
        imaging.render_scene(scene, a)
        imaging.render_scene(scene, b)
        assert a.read_bytes() == b.read_bytes()


def test_unknown_scene_rejected(tmp_path):
    try:
        imaging.render_scene("ufo-landing", tmp_path / "x.png")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown scene must raise KeyError")


def test_night_scene_selected_for_late_event():
    scene = synth._scene_for(
        synth.EventKind.DING, synth.SubType.HUMAN, datetime(2026, 9, 14, 22, 5, tzinfo=timezone.utc)
    )
    assert scene == "person-door-night"

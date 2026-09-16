"""State machine: every transition, out-of-order arrival, persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ring_assistant.schema import Intent, parse_event_payload
from ring_assistant.state import PAIRING_WINDOW, StateStore, Transition

UTC = timezone.utc


def event(
    eid: str,
    *,
    intent: Intent,
    occurred: datetime,
    received: datetime,
    device: str = "front-door",
):
    """A minimal RingEvent positioned for a given intent/time."""
    sub_type = {
        Intent.PACKAGE_DEPOSITED: "package_delivery",
        Intent.PACKAGE_PICKED_UP: "package_pickup",
        Intent.PERSON_AT_DOOR: "human",
        Intent.VEHICLE_AT_DOOR: "vehicle",
        Intent.MOTION_NOISE: None,
    }[intent]
    kind = "ding" if sub_type in ("package_delivery", "package_pickup") else "motion_detected"
    return parse_event_payload(
        {
            "id": eid,
            "type": kind,
            "sub_type": sub_type,
            "device_id": device,
            "occurred_at": occurred.isoformat(),
            "received_at": received.isoformat(),
        }
    )


def at(h: int, m: int, s: int = 0, day: int = 14) -> datetime:
    return datetime(2026, 9, day, h, m, s, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    with StateStore(tmp_path / "state.db") as s:
        yield s


def test_deposit_opens_track(store):
    result = store.apply(
        event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 14), received=at(9, 14, 5)),
        Intent.PACKAGE_DEPOSITED,
    )
    assert result.transition is Transition.TRACK_OPENED
    assert result.open_tracks == 1
    track = store.tracks("front-door")[0]
    assert track.is_open and track.deposits == 1 and track.deposit_event_ids == ("d1",)


def test_second_deposit_refreshes(store):
    store.apply(
        event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 14), received=at(9, 14, 5)),
        Intent.PACKAGE_DEPOSITED,
    )
    result = store.apply(
        event("d2", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 30), received=at(9, 30, 5)),
        Intent.PACKAGE_DEPOSITED,
    )
    assert result.transition is Transition.TRACK_REFRESHED
    track = store.tracks("front-door")[0]
    assert track.deposits == 2 and track.deposit_event_ids == ("d1", "d2")
    assert result.open_tracks == 1  # still one open track


def test_pickup_closes_track(store):
    store.apply(
        event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 14), received=at(9, 14, 5)),
        Intent.PACKAGE_DEPOSITED,
    )
    result = store.apply(
        event("p1", intent=Intent.PACKAGE_PICKED_UP, occurred=at(12, 41), received=at(12, 41, 5)),
        Intent.PACKAGE_PICKED_UP,
    )
    assert result.transition is Transition.TRACK_CLOSED
    assert result.open_tracks == 0
    track = store.tracks("front-door")[0]
    assert not track.is_open
    assert track.pickup_event_id == "p1"
    assert track.out_of_order is False


def test_pickup_without_deposit_is_orphan(store):
    result = store.apply(
        event("p1", intent=Intent.PACKAGE_PICKED_UP, occurred=at(12, 41), received=at(12, 41, 5)),
        Intent.PACKAGE_PICKED_UP,
    )
    assert result.transition is Transition.PICKUP_ORPHAN
    assert store.tracks("front-door") == []


def test_out_of_order_pair_orphan_then_superseded(store):
    """The demo's day-2 shape: pickup webhook arrives before the deposit."""
    deposit_result_first = store.apply(
        event("p9", intent=Intent.PACKAGE_PICKED_UP, occurred=at(11, 52, 10, day=15), received=at(11, 52, 14, day=15)),
        Intent.PACKAGE_PICKED_UP,
    )
    assert deposit_result_first.transition is Transition.PICKUP_ORPHAN
    deposit_result = store.apply(
        event("d10", intent=Intent.PACKAGE_DEPOSITED, occurred=at(11, 47, day=15), received=at(11, 53, 41, day=15)),
        Intent.PACKAGE_DEPOSITED,
    )
    assert deposit_result.transition is Transition.DEPOSIT_SUPERSEDED
    # final derived state: ONE closed, correctly-paired, out-of-order track
    tracks = store.tracks("front-door")
    assert len(tracks) == 1
    track = tracks[0]
    assert track.deposited_at == at(11, 47, day=15)
    assert track.picked_up_at == at(11, 52, 10, day=15)
    assert track.pickup_event_id == "p9"
    assert track.out_of_order is True
    assert deposit_result.open_tracks == 0


def test_pairing_window_expiry_leaves_track_open(store):
    store.apply(
        event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 0), received=at(9, 0, 5)),
        Intent.PACKAGE_DEPOSITED,
    )
    late_pickup = at(14, 0, day=15)  # 29h after the deposit
    assert late_pickup - at(9, 0) > PAIRING_WINDOW
    result = store.apply(
        event("p1", intent=Intent.PACKAGE_PICKED_UP, occurred=late_pickup, received=late_pickup + timedelta(seconds=5)),
        Intent.PACKAGE_PICKED_UP,
    )
    assert result.transition is Transition.PICKUP_ORPHAN
    assert store.open_track_count("front-door") == 1  # never auto-expires


def test_non_package_intents_do_not_touch_tracks(store):
    for i, intent in enumerate(
        [Intent.PERSON_AT_DOOR, Intent.VEHICLE_AT_DOOR, Intent.MOTION_NOISE], start=1
    ):
        result = store.apply(
            event(f"n{i}", intent=intent, occurred=at(10, i), received=at(10, i, 5)),
            intent,
        )
        assert result.transition is Transition.NO_TRACK_CHANGE
    assert store.tracks("front-door") == []


def test_duplicate_event_id_is_idempotent(store):
    ev = event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 14), received=at(9, 14, 5))
    store.apply(ev, Intent.PACKAGE_DEPOSITED)
    again = store.apply(ev, Intent.PACKAGE_DEPOSITED)
    assert again.transition is Transition.DUPLICATE
    assert store.open_track_count("front-door") == 1  # unchanged
    assert len(store.tracks("front-door")[0].deposit_event_ids) == 1


def test_recent_intents_window(store):
    for i, minute in enumerate([0, 4, 9, 40], start=1):
        store.apply(
            event(f"n{i}", intent=Intent.MOTION_NOISE, occurred=at(8, minute), received=at(8, minute, 5)),
            Intent.MOTION_NOISE,
        )
    before = at(8, 9)
    count = store.recent_intents(
        "front-door", Intent.MOTION_NOISE, window=timedelta(minutes=10), before=before
    )
    assert count == 3  # 8:00, 8:04, 8:09 inside; 8:40 is future here


def test_recent_intents_excludes_other_devices_and_intents(store):
    store.apply(
        event("n1", intent=Intent.MOTION_NOISE, occurred=at(8, 0), received=at(8, 0, 5)),
        Intent.MOTION_NOISE,
    )
    store.apply(
        event("n2", intent=Intent.MOTION_NOISE, occurred=at(8, 1), received=at(8, 1, 5), device="back-yard"),
        Intent.MOTION_NOISE,
    )
    store.apply(
        event("h1", intent=Intent.PERSON_AT_DOOR, occurred=at(8, 2), received=at(8, 2, 5)),
        Intent.PERSON_AT_DOOR,
    )
    assert store.recent_intents("front-door", Intent.MOTION_NOISE, window=timedelta(minutes=10), before=at(8, 3)) == 1


def test_devices_are_independent(store):
    store.apply(
        event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 0), received=at(9, 0, 5)),
        Intent.PACKAGE_DEPOSITED,
    )
    result = store.apply(
        event("d2", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 1), received=at(9, 1, 5), device="back-yard"),
        Intent.PACKAGE_DEPOSITED,
    )
    assert result.transition is Transition.TRACK_OPENED
    assert store.open_track_count("front-door") == 1
    assert store.open_track_count("back-yard") == 1


def test_persistence_across_reopen(tmp_path):
    db = tmp_path / "state.db"
    with StateStore(db) as store:
        store.apply(
            event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 0), received=at(9, 0, 5)),
            Intent.PACKAGE_DEPOSITED,
        )
        store.apply(
            event("p1", intent=Intent.PACKAGE_PICKED_UP, occurred=at(10, 0), received=at(10, 0, 5)),
            Intent.PACKAGE_PICKED_UP,
        )
    with StateStore(db) as reopened:
        tracks = reopened.tracks("front-door")
        assert len(tracks) == 1 and not tracks[0].is_open
        duplicate = reopened.apply(
            event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 0), received=at(9, 0, 5)),
            Intent.PACKAGE_DEPOSITED,
        )
        assert duplicate.transition is Transition.DUPLICATE  # log persisted


def test_scrambled_arrival_converges_to_same_tracks(store):
    """Arrival order is irrelevant to the derived state."""
    events = [
        ("p1", Intent.PACKAGE_PICKED_UP, at(12, 0), at(12, 0, 5)),
        ("d2", Intent.PACKAGE_DEPOSITED, at(10, 0), at(12, 1, 0)),
        ("n1", Intent.MOTION_NOISE, at(11, 0), at(11, 0, 5)),
        ("d1", Intent.PACKAGE_DEPOSITED, at(9, 0), at(12, 2, 0)),
    ]
    for eid, intent, occurred, received in events:
        store.apply(event(eid, intent=intent, occurred=occurred, received=received), intent)
    tracks = store.tracks("front-door")
    assert len(tracks) == 1
    assert tracks[0].deposit_event_ids == ("d1", "d2")
    assert tracks[0].pickup_event_id == "p1"
    assert tracks[0].out_of_order is True  # pickup arrived before both deposits


def test_apply_result_open_tracks_reflects_after_state(store):
    r1 = store.apply(
        event("d1", intent=Intent.PACKAGE_DEPOSITED, occurred=at(9, 0), received=at(9, 0, 5)),
        Intent.PACKAGE_DEPOSITED,
    )
    r2 = store.apply(
        event("d2", intent=Intent.PACKAGE_DEPOSITED, occurred=at(20, 0), received=at(20, 0, 5)),
        Intent.PACKAGE_DEPOSITED,
    )
    assert (r1.open_tracks, r2.open_tracks) == (1, 1)  # refresh keeps one open

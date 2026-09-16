"""Pipeline end-to-end over the synthetic storyboard + demo determinism."""

from __future__ import annotations

from ring_assistant.classify import RuleBasedClassifier
from ring_assistant.replay import Pipeline, format_timeline
from ring_assistant.routing import Action, Router
from ring_assistant.settings import Settings
from ring_assistant.state import StateStore, Transition
from ring_assistant.synth import DEFAULT_SEED, generate_to, signed_webhooks

SECRET = "pipeline-test-secret"


def replay(tmp_path, seed=DEFAULT_SEED, secret=SECRET):
    """Replay the storyboard through a fresh Pipeline; returns (store, router, entries)."""
    _, payloads = generate_to(tmp_path / "synth", seed)
    store = StateStore(tmp_path / "state.db")
    router = Router.from_settings(Settings())
    pipeline = Pipeline(
        secret=secret, classifier=RuleBasedClassifier(), store=store, router=router
    )
    entries = [pipeline.handle(b, h) for b, h in signed_webhooks(payloads, secret)]
    return store, router, entries


def by_id(entries):
    """First arrival wins (duplicate redeliveries share the event id)."""
    return {e.event.event_id: e for e in reversed(entries)}


def test_storyboard_transitions(tmp_path):
    store, _, entries = replay(tmp_path)
    transitions = by_id(entries)
    assert transitions["evt-004"].apply_result.transition is Transition.TRACK_OPENED
    assert transitions["evt-005"].apply_result.transition is Transition.NO_TRACK_CHANGE
    assert transitions["evt-006"].apply_result.transition is Transition.TRACK_CLOSED
    assert transitions["evt-009"].apply_result.transition is Transition.PICKUP_ORPHAN
    assert transitions["evt-010"].apply_result.transition is Transition.DEPOSIT_SUPERSEDED
    # final state: two closed tracks (day-1 normal, day-2 out-of-order), none open
    tracks = store.tracks("front-door")
    assert len(tracks) == 2
    assert all(not t.is_open for t in tracks)
    assert tracks[1].out_of_order is True


def test_duplicate_arrival_suppressed(tmp_path):
    _, router, entries = replay(tmp_path)
    dupes = [e for e in entries if e.apply_result.transition is Transition.DUPLICATE]
    assert len(dupes) == 1
    assert dupes[0].event.event_id == "evt-004"
    assert dupes[0].decision.action is Action.SUPPRESS
    assert dupes[0].delivered == 0


def test_night_person_escalates_day_person_informs(tmp_path):
    _, router, entries = replay(tmp_path)
    got = by_id(entries)
    night = got["evt-008"]  # 21:37 ding+human
    assert night.decision.action is Action.ESCALATE
    assert night.delivered >= 1
    day = got["evt-007"]  # 16:50 motion+human
    assert day.decision.action is Action.NOTIFY
    assert day.decision.notification.severity.value == "info"


def test_noise_burst_digest_once(tmp_path):
    _, router, entries = replay(tmp_path)
    noise = [e for e in entries if e.intent.intent.value == "motion_noise"]
    actions = [e.decision.action for e in noise]
    # day-1 burst: 2 suppressed then 1 digest; day-2 lone noise: suppressed
    assert actions.count(Action.SUPPRESS) == 3
    assert actions.count(Action.NOTIFY) == 1
    digest = next(e for e in noise if e.decision.action is Action.NOTIFY)
    assert "3 unclassified motions" in digest.decision.notification.body


def test_out_of_order_pair_notifies_orphan_then_suppresses_deposit(tmp_path):
    _, router, entries = replay(tmp_path)
    got = by_id(entries)
    orphan = got["evt-009"]
    assert orphan.decision.action is Action.NOTIFY
    assert "no matching deposit" in orphan.decision.notification.body
    superseded = got["evt-010"]
    assert superseded.decision.action is Action.SUPPRESS
    assert superseded.delivered == 0


def test_bad_signature_never_reaches_state(tmp_path):
    import json

    from ring_assistant.verify import SignatureError, sign_payload

    _, payloads = generate_to(tmp_path / "synth")
    store = StateStore(tmp_path / "state.db")
    pipeline = Pipeline(
        secret=SECRET,
        classifier=RuleBasedClassifier(),
        store=store,
        router=Router.from_settings(Settings()),
    )
    body = json.dumps(payloads[0]).encode()
    bad = {"X-Ring-Signature": sign_payload(body, "wrong-secret")}
    try:
        pipeline.handle(body, bad)
    except SignatureError:
        pass
    else:
        raise AssertionError("expected SignatureError")
    assert store.tracks("front-door") == []  # nothing logged on auth failure


def test_format_timeline_deterministic_and_readable(tmp_path):
    _, _, entries_a = replay(tmp_path / "a")
    _, _, entries_b = replay(tmp_path / "b")
    text_a, text_b = format_timeline(entries_a), format_timeline(entries_b)
    assert text_a == text_b
    assert "evt-004" in text_a and "ESCALATE" in text_a and "SUPPRESS" in text_a
    lines = text_a.splitlines()
    assert len(lines) == len(entries_a) + 2  # header + rule + rows


def test_demo_run_matches_pipeline(tmp_path):
    from ring_assistant.demo import run_demo

    entries = run_demo(DEFAULT_SEED, tmp_path)
    assert len(entries) == 12
    actions = [e.decision.action for e in entries]
    assert actions.count(Action.ESCALATE) == 1
    assert actions.count(Action.SUPPRESS) == 5  # 2 burst noise + lone day-2 noise + superseded deposit + duplicate
    assert (tmp_path / "state.db").exists()
    assert (tmp_path / "events.jsonl").exists()

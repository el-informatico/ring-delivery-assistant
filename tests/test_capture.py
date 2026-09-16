"""Record/replay harness: JSONL captures, outcomes, the never-crash loop.

The contract under test is the registration-day swap: fixtures and
captures are the SAME shape at the byte level (signed v1.1 bodies), so
``replay_fixture_dir`` today and ``replay_capture_file`` on live
traffic exercise one code path — ``Pipeline.handle_v1_1``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ring_assistant.capture import (
    CAPTURE_FILE_NAME,
    CaptureRecord,
    WebhookRecorder,
    read_capture,
    replay_capture_file,
    replay_fixture_dir,
    replay_records,
    write_capture,
)
from ring_assistant.classify import RuleBasedClassifier
from ring_assistant.replay import Pipeline
from ring_assistant.routing import Router
from ring_assistant.settings import Settings
from ring_assistant.state import StateStore, Transition
from ring_assistant.wire import signed_v1_1

SECRET = "capture-test-secret"
FIXTURES = Path(__file__).parent / "fixtures" / "wire"


@pytest.fixture
def pipeline(tmp_path) -> Pipeline:
    return Pipeline(
        secret=SECRET,
        classifier=RuleBasedClassifier(),
        store=StateStore(tmp_path / "state.db"),
        router=Router.from_settings(Settings()),
        signature_header="x-signature",
    )


def envelope(
    *, event_type="motion_detected", sub_type=None, timestamp_ms=1_789_000_000_000, request_id="r1"
) -> dict:
    from ring_assistant.wire import build_v1_1

    return build_v1_1(
        event_type=event_type,
        device_id="ava1.ring.device.door001",
        timestamp_ms=timestamp_ms,
        request_id=request_id,
        sub_type=sub_type,
    )


def record_for(envelope_dict: dict, *, outcome="accepted", status=200, note="") -> CaptureRecord:
    body, headers = signed_v1_1(envelope_dict, SECRET)
    return CaptureRecord(
        received_at=datetime(2026, 5, 15, 8, 0, 0, tzinfo=timezone.utc),
        body=body,
        headers=headers,
        outcome=outcome,
        status=status,
        note=note,
    )


# -- capture records + files -------------------------------------------------


def test_capture_record_round_trips_through_json():
    record = record_for(envelope(sub_type="human"))
    restored = CaptureRecord.from_json(record.to_json())
    assert restored.body == record.body
    assert restored.headers["X-Signature"] == record.headers["X-Signature"]
    assert restored.received_at == record.received_at
    assert restored.outcome == "accepted"


def test_capture_record_parses_naive_received_at_as_local():
    data = record_for(envelope()).to_json()
    data["received_at"] = "2026-05-15T08:00:00"  # no offset in the wild
    assert CaptureRecord.from_json(data).received_at.tzinfo is not None


def test_capture_record_rejects_unknown_version():
    data = record_for(envelope()).to_json()
    data["version"] = 99
    with pytest.raises(ValueError, match="version"):
        CaptureRecord.from_json(data)


def test_write_and_read_capture_round_trip(tmp_path):
    records = [
        record_for(envelope(sub_type="human"), outcome="accepted", status=200),
        record_for(envelope(event_type="subscription_activated"), outcome="ignored", status=200),
    ]
    path = write_capture(tmp_path / "cap.jsonl", records)
    assert path.read_text().count("\n") == 2  # one JSON object per line
    restored = read_capture(path)
    assert [r.outcome for r in restored] == ["accepted", "ignored"]
    assert restored[0].body == records[0].body


def test_read_capture_skips_blank_lines_and_names_bad_ones(tmp_path):
    path = tmp_path / "cap.jsonl"
    good = record_for(envelope()).to_json()
    path.write_text(json.dumps(good) + "\n\n", encoding="utf-8")
    assert len(read_capture(path)) == 1  # blank lines are skipped
    path.write_text(json.dumps(good) + "\n{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"cap.jsonl:2: bad capture record"):
        read_capture(path)


def test_recorder_failure_never_raises(tmp_path, capsys):
    recorder = WebhookRecorder(tmp_path)  # a DIRECTORY: open() will fail
    recorder.record(
        body=b"{}",
        headers={},
        outcome="rejected",
        status=401,
        received_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
    )
    assert "could not write" in capsys.readouterr().err


def test_recorder_appends_across_calls(tmp_path):
    recorder = WebhookRecorder(tmp_path / "sub" / CAPTURE_FILE_NAME)
    for i in range(3):
        recorder.record(body=f'{{"i": {i}}}'.encode(), headers={}, outcome="accepted", status=200)
    assert len(read_capture(recorder.path)) == 3  # append-only, parents made


# -- replay semantics ----------------------------------------------------------


def test_replay_runs_accepted_and_ignored_and_reports(pipeline):
    records = [
        record_for(envelope(sub_type="human", request_id="a")),
        record_for(envelope(event_type="subscription_activated", request_id="b")),
    ]
    report = replay_records(records, pipeline)
    assert report.counts == {"accepted": 1, "ignored": 1, "rejected": 0}
    assert report.outcomes[1].error  # why it was ignored is in the report
    assert report.entries[0].intent.intent.value == "person_at_door"


def test_replay_rejects_malformed_bodies_without_crashing(pipeline):
    records = [
        CaptureRecord(
            received_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
            body=b"not json at all",
            headers={},
            outcome="accepted",
            status=200,
        ),
        record_for(envelope(sub_type="human", request_id="ok")),
    ]
    report = replay_records(records, pipeline)
    assert report.counts == {"accepted": 1, "ignored": 0, "rejected": 1}
    assert "body" in report.outcomes[0].error


def test_replay_skips_rejected_records_unless_asked(pipeline):
    records = [
        record_for(envelope(), outcome="rejected", status=400, note="bad payload"),
        record_for(envelope(sub_type="human", request_id="ok")),
    ]
    assert replay_records(records, pipeline).counts["accepted"] == 1  # only the good one
    again = replay_records(records, pipeline, include_rejected=True)
    assert len(again.outcomes) == 2


def test_replayed_records_keep_their_original_received_at(tmp_path):
    # same envelope, two captures: replaying both must DEDUPE on event id
    # (retry semantics), and received_at comes from the record, not now
    first = record_for(envelope(sub_type="human", request_id="r"))
    later = CaptureRecord(
        received_at=datetime(2026, 5, 15, 9, 0, 0, tzinfo=timezone.utc),
        body=first.body,
        headers=first.headers,
        outcome="accepted",
        status=200,
    )
    store = StateStore(tmp_path / "state.db")
    pipeline = Pipeline(
        secret=SECRET,
        classifier=RuleBasedClassifier(),
        store=store,
        router=Router.from_settings(Settings()),
        signature_header="x-signature",
    )
    report = replay_records([first, later], pipeline)
    transitions = [e.apply_result.transition for e in report.entries]
    assert transitions == [Transition.NO_TRACK_CHANGE, Transition.DUPLICATE]
    assert report.entries[1].event.received_at == later.received_at


def test_replay_resigns_with_the_local_secret(pipeline):
    # recorded under a DIFFERENT secret (another host): replay re-signs,
    # so a body rejected only for its signature passes here
    body, _ = signed_v1_1(envelope(sub_type="human"), "a-completely-other-secret")
    foreign = CaptureRecord(
        received_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
        body=body,
        headers={"X-Signature": "sha256=stale"},
        outcome="rejected",
        status=401,
        note="signature rejected",
    )
    report = replay_records([foreign], pipeline, include_rejected=True)
    assert report.counts["accepted"] == 1


def test_unexpected_pipeline_errors_are_reported_not_raised(tmp_path):
    class ExplodingClassifier:
        name = "boom"

        def classify(self, event, context):
            raise RuntimeError("classifier exploded")

    store = StateStore(tmp_path / "s.db")
    pipeline = Pipeline(
        secret=SECRET,
        classifier=ExplodingClassifier(),
        store=store,
        router=Router.from_settings(Settings()),
        signature_header="x-signature",
    )
    records = [record_for(envelope(sub_type="human", request_id=f"r{i}")) for i in range(2)]
    report = replay_records(records, pipeline)
    assert report.counts["rejected"] == 2
    assert "classifier exploded" in report.outcomes[0].error


def test_replay_fixture_dir_runs_the_documented_examples(pipeline):
    report = replay_fixture_dir(FIXTURES, pipeline)
    assert report.counts == {"accepted": 3, "ignored": 1, "rejected": 0}
    assert any("subscription" in o.label for o in report.outcomes if o.result == "ignored")
    assert report.timeline().startswith("occurred")


def test_replay_capture_file_end_to_end(tmp_path, pipeline):
    path = write_capture(
        tmp_path / "cap.jsonl",
        [record_for(envelope(sub_type="human", request_id="x"))],
    )
    report = replay_capture_file(path, pipeline)
    assert report.counts["accepted"] == 1


# -- server-side recording ------------------------------------------------------


def live_app(tmp_path, secret=SECRET):
    from ring_assistant.server import create_app

    store = StateStore(tmp_path / "server.db")
    pipeline = Pipeline(
        secret=secret,
        classifier=RuleBasedClassifier(),
        store=store,
        router=Router.from_settings(Settings()),
        signature_header="x-signature",
    )
    recorder = WebhookRecorder(tmp_path / "record" / CAPTURE_FILE_NAME)
    app = create_app(pipeline, live_wire=True, recorder=recorder)
    return app, recorder


def test_server_records_every_outcome(tmp_path):
    app, recorder = live_app(tmp_path)
    with TestClient(app) as client:
        body, headers = signed_v1_1(envelope(sub_type="human"), SECRET)
        assert client.post("/webhooks/ring", content=body, headers=headers).status_code == 200

        bad = client.post(
            "/webhooks/ring", content=body, headers={"X-Signature": "sha256=wrong"}
        )
        assert bad.status_code == 401

        sub_body, sub_headers = signed_v1_1(envelope(event_type="subscription_activated"), SECRET)
        ignored = client.post("/webhooks/ring", content=sub_body, headers=sub_headers)
        assert ignored.status_code == 200

    captured = read_capture(recorder.path)
    assert [r.outcome for r in captured] == ["accepted", "rejected", "ignored"]
    assert [r.status for r in captured] == [200, 401, 200]
    assert "signature" in captured[1].note


def test_recorded_traffic_replays_offline(tmp_path):
    # the full registration-day loop: serve -> record -> replay elsewhere
    app, recorder = live_app(tmp_path)
    with TestClient(app) as client:
        for sub, rid in [("human", "r1"), ("vehicle", "r2"), (None, "r3")]:
            body, headers = signed_v1_1(
                envelope(sub_type=sub, request_id=rid, timestamp_ms=1_789_000_000_000 + int(rid[1:]) * 60_000),
                SECRET,
            )
            client.post("/webhooks/ring", content=body, headers=headers)

    store = StateStore(tmp_path / "replay.db")
    offline = Pipeline(
        secret=SECRET,
        classifier=RuleBasedClassifier(),
        store=store,
        router=Router.from_settings(Settings()),
        signature_header="x-signature",
    )
    report = replay_capture_file(recorder.path, offline)
    assert report.counts["accepted"] == 3
    intents = [e.intent.intent.value for e in report.entries]
    assert intents == ["person_at_door", "vehicle_at_door", "motion_noise"]
    store.close()

"""Event schema: normalization, lenient fields, and contract violations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ring_assistant.schema import (
    EventKind,
    Intent,
    IntentResult,
    RingEvent,
    SubType,
    parse_event_payload,
)


def test_full_payload_parses(make_payload):
    event = parse_event_payload(make_payload())
    assert event.event_id == "evt-0001"
    assert event.kind is EventKind.MOTION
    assert event.sub_type is SubType.HUMAN
    assert event.device_id == "front-door"
    assert event.device_name == "Front Door"
    assert event.occurred_at == datetime(2026, 9, 14, 9, 14, 2, tzinfo=timezone.utc)
    assert event.received_at == datetime(2026, 9, 14, 9, 14, 6, tzinfo=timezone.utc)
    assert event.snapshot_path == "snapshots/person-door.png"
    assert event.raw["type"] == "motion_detected"
    assert event.label == "motion_detected/human"


def test_payload_received_at_wins_over_caller_stamp(make_payload, froze_received_at):
    event = parse_event_payload(make_payload(), received_at=froze_received_at)
    assert event.received_at != froze_received_at  # payload's own value used


def test_received_at_stamped_when_absent(make_payload, froze_received_at):
    payload = make_payload()
    del payload["received_at"]
    event = parse_event_payload(payload, received_at=froze_received_at)
    assert event.received_at == froze_received_at


def test_received_at_defaults_to_now(make_payload):
    payload = make_payload()
    del payload["received_at"]
    before = datetime.now(timezone.utc)
    event = parse_event_payload(payload)
    after = datetime.now(timezone.utc)
    assert before <= event.received_at <= after
    assert event.received_at.tzinfo is not None


def test_ding_without_sub_type(make_payload):
    event = parse_event_payload(make_payload(type="ding", sub_type=None))
    assert event.sub_type is None
    assert event.label == "ding"


def test_empty_sub_type_treated_as_none(make_payload):
    event = parse_event_payload(make_payload(sub_type=""))
    assert event.sub_type is None


def test_missing_sub_type_key_is_none(make_payload):
    payload = make_payload()
    del payload["sub_type"]
    assert parse_event_payload(payload).sub_type is None


def test_empty_snapshot_path_treated_as_none(make_payload):
    event = parse_event_payload(make_payload(snapshot_path=""))
    assert event.snapshot_path is None


def test_device_name_defaults_to_id(make_payload):
    event = parse_event_payload(make_payload(device_name=None))
    assert event.device_name == "front-door"


def test_naive_timestamp_assumed_utc(make_payload):
    event = parse_event_payload(make_payload(occurred_at="2026-09-14T09:14:02"))
    assert event.occurred_at.tzinfo is not None
    assert event.occurred_at.utcoffset() == timedelta(0)


def test_z_suffix_timestamp_accepted(make_payload):
    event = parse_event_payload(make_payload(occurred_at="2026-09-14T09:14:02Z"))
    assert event.occurred_at == datetime(2026, 9, 14, 9, 14, 2, tzinfo=timezone.utc)


def test_offset_timestamp_normalized_to_utc(make_payload):
    event = parse_event_payload(make_payload(occurred_at="2026-09-14T11:14:02+02:00"))
    assert event.occurred_at == datetime(2026, 9, 14, 9, 14, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": None},
        {"id": ""},
        {"id": "   "},
        {"type": "carbon_monoxide"},
        {"type": None},
        {"sub_type": "raccoon"},
        {"device_id": ""},
        {"device_id": 7},
        {"occurred_at": "not-a-date"},
        {"occurred_at": None},
        {"occurred_at": 1694000000},
        {"received_at": "yesterday"},
        {"snapshot_path": 42},
        {"device_name": []},
    ],
)
def test_contract_violations_rejected(make_payload, overrides):
    with pytest.raises(Exception):  # noqa: B017 - exact type asserted below per class
        parse_event_payload(make_payload(**overrides))


def test_non_mapping_payload_rejected():
    with pytest.raises(Exception):
        parse_event_payload(["not", "an", "object"])


def test_unknown_fields_preserved_in_raw(make_payload):
    event = parse_event_payload(make_payload(api_version="2077-01-01"))
    assert event.raw["api_version"] == "2077-01-01"


def test_intent_result_is_plain_data():
    result = IntentResult(
        intent=Intent.PACKAGE_DEPOSITED,
        confidence=0.95,
        rationale="motion with package_delivery sub_type",
        source="rules",
    )
    assert result.detail == ""

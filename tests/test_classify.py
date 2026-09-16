"""Rule-based classifier: full signal table + context transparency."""

from __future__ import annotations

import pytest

from ring_assistant.classify import ClassificationContext, RuleBasedClassifier
from ring_assistant.schema import EventKind, Intent, SubType, parse_event_payload


def classify_payload(**overrides) -> tuple:
    base = {
        "id": "evt-x",
        "type": "motion_detected",
        "sub_type": None,
        "device_id": "front-door",
        "occurred_at": "2026-09-14T09:00:00Z",
    }
    base.update(overrides)
    event = parse_event_payload(base)
    result = RuleBasedClassifier().classify(event, ClassificationContext())
    return event, result


@pytest.mark.parametrize(
    ("kind", "sub_type", "intent", "confidence"),
    [
        ("ding", "package_delivery", Intent.PACKAGE_DEPOSITED, 1.0),
        ("motion_detected", "package_delivery", Intent.PACKAGE_DEPOSITED, 0.95),
        ("ding", "package_pickup", Intent.PACKAGE_PICKED_UP, 1.0),
        ("motion_detected", "package_pickup", Intent.PACKAGE_PICKED_UP, 0.95),
        ("ding", "human", Intent.PERSON_AT_DOOR, 0.95),
        ("ding", "vehicle", Intent.VEHICLE_AT_DOOR, 0.9),
        ("ding", None, Intent.PERSON_AT_DOOR, 0.85),
        ("motion_detected", "human", Intent.PERSON_AT_DOOR, 0.8),
        ("motion_detected", "vehicle", Intent.VEHICLE_AT_DOOR, 0.85),
        ("motion_detected", None, Intent.MOTION_NOISE, 0.6),
    ],
)
def test_signal_table(kind, sub_type, intent, confidence):
    _, result = classify_payload(type=kind, sub_type=sub_type)
    assert result.intent is intent
    assert result.confidence == confidence
    assert result.source == "rules"
    assert result.rationale


def test_every_kind_subtype_combination_covered():
    """The table is total: no (kind, sub_type) pair can fall through."""
    for kind in EventKind:
        for sub_type in list(SubType) + [None]:
            _, result = classify_payload(type=kind.value, sub_type=sub_type.value if sub_type else None)
            assert isinstance(result.intent, Intent)


def test_context_recorded_but_verdict_stable():
    day = ClassificationContext(is_night=False, open_tracks=0)
    night = ClassificationContext(is_night=True, open_tracks=2)
    _, day_result = classify_payload(type="ding", sub_type="human")
    event, _ = classify_payload(type="ding", sub_type="human")
    night_result = RuleBasedClassifier().classify(event, night)
    assert day_result.intent is night_result.intent  # context never flips verdict
    assert "night" in night_result.detail and "2 open track" in night_result.detail
    assert "day" in day_result.detail


def test_structural_subtyping():
    """RuleBasedClassifier satisfies the protocol's shape."""
    assert callable(RuleBasedClassifier().classify)

"""Snapshot rules classifier: pixels in, intents out, table as fallback.

The parametrized scene table is the CALIBRATION PIN: every threshold in
classify.py is chosen against these measured fractions, so a fixture or
palette change that silently shifts the mapping fails here by name.
"""

from __future__ import annotations

import base64
import json

import pytest
from ring_api_mock import MOCK_TOKEN, MockRingApi

from ring_assistant.classify import (
    ClassificationContext,
    SnapshotRuleClassifier,
    analyze_snapshot,
)
from ring_assistant.events_api import RingApiClient
from ring_assistant.imaging import render_scene_bytes
from ring_assistant.schema import Intent, parse_event_payload
from ring_assistant.snapshots import (
    ApiSnapshotSource,
    ManifestSnapshotSource,
    SnapshotImage,
    decode_png,
)

DEV = "ava1.ring.device.door001"
BASE_MS = 1_789_000_000_000


class SceneSource:
    """Answers every fetch with one fixed rendered scene."""

    def __init__(self, scene: str):
        self.scene = scene

    def fetch(self, event):
        return SnapshotImage(
            data=render_scene_bytes(self.scene), content_type="image/png", source="test"
        )


class NoneSource:
    def fetch(self, event):
        return None


class BytesSource:
    def __init__(self, data: bytes, content_type: str):
        self.data, self.content_type = data, content_type

    def fetch(self, event):
        return SnapshotImage(data=self.data, content_type=self.content_type, source="test")


def event(**overrides):
    payload = {
        "id": "evt-cls",
        "type": "motion_detected",
        "device_id": DEV,
        "occurred_at": "2026-05-15T08:00:00Z",
    }
    payload.update(overrides)
    return parse_event_payload(payload)


CONTEXT = ClassificationContext(is_night=False, open_tracks=1)


# -- the calibration pin -----------------------------------------------------


@pytest.mark.parametrize(
    ("scene", "intent"),
    [
        ("package-mat", Intent.PACKAGE_DEPOSITED),
        ("person-with-box", Intent.PACKAGE_PICKED_UP),
        ("person-door", Intent.PERSON_AT_DOOR),
        ("person-door-night", Intent.PERSON_AT_DOOR),
        ("van-drive", Intent.VEHICLE_AT_DOOR),
        ("motion-empty", Intent.MOTION_NOISE),
    ],
)
def test_scene_maps_to_intent(scene, intent):
    classifier = SnapshotRuleClassifier(source=SceneSource(scene))
    assert classifier.classify(event(), CONTEXT).intent is intent


def test_night_ground_wheel_collision_is_gated_by_body_color():
    # night ground (28,30,40) reads as "wheel" at tolerance 15 — 77% of
    # the frame in person-door-night. The vehicle verdict requires the
    # van body color too, which night scenes never carry. This pin is
    # the reason the rule is body AND wheels, not wheels alone.
    evidence = analyze_snapshot(decode_png(render_scene_bytes("person-door-night")))
    assert evidence.wheel > 0.5
    assert evidence.vehicle < 0.05
    classifier = SnapshotRuleClassifier(source=SceneSource("person-door-night"))
    assert classifier.classify(event(), CONTEXT).intent is Intent.PERSON_AT_DOOR


def test_evidence_fractions_hold_their_threshold_margins():
    for scene, attribute, minimum in [
        ("package-mat", "box", 0.025),
        ("person-with-box", "person", 0.005),
        ("van-drive", "vehicle", 0.05),
        ("van-drive", "wheel", 0.01),
    ]:
        evidence = analyze_snapshot(decode_png(render_scene_bytes(scene)))
        assert getattr(evidence, attribute) >= minimum, (scene, attribute)


def test_verdict_cites_the_measured_evidence():
    result = SnapshotRuleClassifier(source=SceneSource("van-drive")).classify(event(), CONTEXT)
    assert result.source == "rules+snapshot"
    assert "vehicle=12.0%" in result.rationale
    assert result.confidence == pytest.approx(0.8)


# -- degradation: every missing-image path lands on the table ----------------


@pytest.mark.parametrize(
    ("note", "source"),
    [
        ("no snapshot source configured", None),
        ("snapshot unavailable", NoneSource()),
    ],
)
def test_missing_snapshots_fall_back_with_a_reason(note, source):
    result = SnapshotRuleClassifier(source=source).classify(
        event(sub_type="human"), CONTEXT
    )
    assert result.intent is Intent.PERSON_AT_DOOR  # the table's verdict
    assert result.source == "rules+snapshot"
    assert result.detail.startswith(note)
    assert "context:" in result.detail


def test_jpeg_bytes_fall_back_with_the_llm_routing_note():
    result = SnapshotRuleClassifier(
        source=BytesSource(b"\xff\xd8\xff\xe0real-jpeg-bytes", "image/jpeg")
    ).classify(event(sub_type="human"), CONTEXT)
    assert result.intent is Intent.PERSON_AT_DOOR
    assert "snapshot undecodable" in result.detail
    assert "LLM classifier" in result.detail


def test_empty_snapshot_keeps_the_platform_verdict():
    # an empty frame does not refute the platform's detection: motion
    # with human sub_type stays person_at_door, bare motion stays noise
    empty = SnapshotRuleClassifier(source=SceneSource("motion-empty"))
    assert empty.classify(event(sub_type="human"), CONTEXT).intent is Intent.PERSON_AT_DOOR
    assert empty.classify(event(), CONTEXT).intent is Intent.MOTION_NOISE
    ding = empty.classify(event(type="ding"), CONTEXT)
    assert ding.intent is Intent.PERSON_AT_DOOR  # "someone rang" survives
    assert "snapshot empty" in ding.detail


def test_ding_with_human_subtype_beats_empty_snapshot_confidence():
    # platform-authored evidence (ding+human, 0.95) outranks what an
    # empty frame can say; the fallback passes the table's confidence
    result = SnapshotRuleClassifier(source=SceneSource("motion-empty")).classify(
        event(type="ding", sub_type="human"), CONTEXT
    )
    assert result.confidence == pytest.approx(0.95)


# -- end to end through both offline/online source kinds ---------------------


def manifest_event(epoch_ms: int, **overrides):
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    payload = {
        "id": "evt-m",
        "type": "motion_detected",
        "device_id": DEV,
        "occurred_at": moment.isoformat().replace("+00:00", "Z"),
    }
    payload.update(overrides)
    return parse_event_payload(payload)


def test_manifest_source_end_to_end(tmp_path):
    from ring_assistant.imaging import render_scene

    (tmp_path / "snapshots").mkdir()
    render_scene("package-mat", tmp_path / "snapshots" / "package-mat.png")
    manifest = tmp_path / "snapshots" / "manifest.json"
    manifest.write_text(json.dumps({f"{DEV}@{BASE_MS}": "snapshots/package-mat.png"}))
    classifier = SnapshotRuleClassifier(
        source=ManifestSnapshotSource(root=tmp_path, manifest_path=manifest)
    )
    result = classifier.classify(manifest_event(BASE_MS), CONTEXT)
    assert result.intent is Intent.PACKAGE_DEPOSITED
    assert "snapshot from manifest" in result.detail


def test_api_source_end_to_end_against_the_mock():
    # same event, same key, real two-step HTTP shape (via the in-process
    # mock): the manifest test above and this one must agree
    mock = MockRingApi()
    mock.add_snapshot(DEV, BASE_MS, "package-mat")
    classifier = SnapshotRuleClassifier(
        source=ApiSnapshotSource(
            client=RingApiClient(access_token=MOCK_TOKEN, transport=mock)
        )
    )
    result = classifier.classify(manifest_event(BASE_MS + 30_000), CONTEXT)
    assert result.intent is Intent.PACKAGE_DEPOSITED
    assert "snapshot from events-api" in result.detail


def test_api_source_no_media_degrades_to_the_table():
    mock = MockRingApi()
    mock.device_list.append({"type": "devices", "id": DEV, "attributes": {"name": "Door"}})
    classifier = SnapshotRuleClassifier(
        source=ApiSnapshotSource(
            client=RingApiClient(access_token=MOCK_TOKEN, transport=mock)
        )
    )
    result = classifier.classify(manifest_event(BASE_MS, sub_type="human"), CONTEXT)
    assert result.intent is Intent.PERSON_AT_DOOR
    assert "snapshot unavailable" in result.detail


# -- LLM adapter consumes the same sources ----------------------------------


def test_llm_prefers_snapshot_source_over_root(tmp_path):
    from ring_assistant.imaging import render_scene
    from ring_assistant.llm import LLMClassifier

    (tmp_path / "snapshots").mkdir()
    render_scene("person-door", tmp_path / "snapshots" / "person-door.png")
    classifier = LLMClassifier(
        endpoint="https://llm.example/v1",
        model="vision-1",
        api_key="synthetic-key",
        snapshot_root=tmp_path,
        snapshot_source=SceneSource("van-drive"),
    )
    uri = classifier._snapshot_data_uri(event(snapshot_path="snapshots/person-door.png"))
    # the source answered, so the root file is never read
    assert uri is not None and "van" not in uri
    assert uri.startswith("data:image/png;base64,")
    assert uri.endswith(base64.b64encode(render_scene_bytes("van-drive")).decode("ascii"))


def test_llm_data_uri_carries_the_real_content_type():
    from ring_assistant.llm import LLMClassifier

    classifier = LLMClassifier(
        endpoint="https://llm.example/v1",
        model="vision-1",
        api_key="synthetic-key",
        snapshot_source=BytesSource(b"\xff\xd8\xff\xe0jpeg", "image/jpeg"),
    )
    assert classifier._snapshot_data_uri(event()).startswith("data:image/jpeg;base64,")


def test_llm_source_replaces_root_entirely(tmp_path):
    # one mechanism: a configured source fully replaces root reading —
    # wanting "API first, fixtures second" is CompositeSnapshotSource's
    # job, not a hidden fallback inside the adapter
    from ring_assistant.imaging import render_scene
    from ring_assistant.llm import LLMClassifier

    (tmp_path / "snapshots").mkdir()
    render_scene("person-door", tmp_path / "snapshots" / "person-door.png")
    classifier = LLMClassifier(
        endpoint="https://llm.example/v1",
        model="vision-1",
        api_key="synthetic-key",
        snapshot_root=tmp_path,
        snapshot_source=NoneSource(),
    )
    assert classifier._snapshot_data_uri(event(snapshot_path="snapshots/person-door.png")) is None

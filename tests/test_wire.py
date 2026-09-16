"""Webhook v1.1 wire adapter: Ring's documented live contract at the edge.

Fixtures under ``fixtures/wire/`` are the documented example payloads
(provenance in ``fixtures/wire/PROVENANCE.md``). Everything here runs
fully offline: fixture -> sign -> verify -> normalize -> classify ->
state -> routing, the exact path a live Ring delivery takes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ring_assistant.classify import RuleBasedClassifier
from ring_assistant.replay import Pipeline
from ring_assistant.routing import Router
from ring_assistant.schema import (
    EventKind,
    Intent,
    SchemaError,
    SubType,
    parse_event_payload,
)
from ring_assistant.settings import Settings
from ring_assistant.state import StateStore, Transition
from ring_assistant.verify import SignatureError
from ring_assistant.wire import (
    LIVE_SIGNATURE_HEADER,
    UnsupportedEvent,
    adapt_v1_1,
    build_v1_1,
    encode_v1_1,
    receive_v1_1,
    signed_v1_1,
)

SECRET = "wire-test-secret"
FIXTURES = Path(__file__).parent / "fixtures" / "wire"
DEVICE = "ava1.ring.device.door001"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def event_from(payload: dict):
    return parse_event_payload(adapt_v1_1(payload))


# --- documented fixtures adapt to the internal contract ----------------------


def test_motion_fixture_maps_type_and_clears_unclassified_sub_type():
    internal = adapt_v1_1(load_fixture("motion_sub_type_motion"))
    assert internal["type"] == EventKind.MOTION.value
    assert internal["sub_type"] is None  # "motion" is not human/vehicle -> noise
    assert internal["device_id"] == DEVICE


def test_motion_fixture_epoch_ms_is_the_authoritative_time():
    event = event_from(load_fixture("motion_sub_type_motion"))
    # Epoch truth for timestamp 1786715596787 is 13:53:16.787 UTC. The
    # docs example's timestamp_readable ("2026-08-14 08:53:16") is a
    # LOCAL-time rendering — exactly 5h off UTC — so the adapter parses
    # only the epoch ms and never touches the readable field.
    assert event.occurred_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-14 13:53:16"
    assert event.occurred_at.microsecond == 787000
    assert event.occurred_at.utcoffset() == timedelta(0)


def test_original_envelope_survives_uninterpreted_in_raw():
    event = event_from(load_fixture("motion_sub_type_motion"))
    wire = event.raw["wire"]
    assert wire["meta"]["request_id"] == "2ad45ade-1818-4154-813b-afdd8bcd8085"
    assert wire["data"]["attributes"]["component_ids"] == ["0"]


def test_human_sub_type_maps_to_internal_sub_type():
    event = event_from(load_fixture("motion_sub_type_human"))
    assert event.kind == EventKind.MOTION
    assert event.sub_type == SubType.HUMAN


def test_button_press_maps_to_ding_without_sub_type():
    event = event_from(load_fixture("button_press"))
    assert event.kind == EventKind.DING
    assert event.sub_type is None
    # v1.1 carries no display name; the schema's id-fallback applies
    assert event.device_name == event.device_id


def test_button_press_epoch_ms_roundtrips_to_the_millisecond():
    event = event_from(load_fixture("button_press"))
    assert int(event.occurred_at.timestamp() * 1000) == 1771027372393
    assert event.occurred_at.microsecond == 393000


# --- documented-but-ignored types: UnsupportedEvent, not SchemaError --------


def test_subscription_activated_is_unsupported():
    with pytest.raises(UnsupportedEvent) as excinfo:
        adapt_v1_1(load_fixture("subscription_activated"))
    assert excinfo.value.event_type == "subscription_activated"


@pytest.mark.parametrize(
    "event_type",
    [
        "device_added",
        "device_removed",
        "device_online",
        "device_offline",
        "app_integration_added",
        "app_integration_removed",
        "subscription_deactivated",
    ],
)
def test_each_documented_lifecycle_type_is_individually_ignored(event_type):
    payload = build_v1_1(
        event_type=event_type, device_id=DEVICE, timestamp_ms=1, request_id="req-lifecycle"
    )
    with pytest.raises(UnsupportedEvent) as excinfo:
        adapt_v1_1(payload)
    assert excinfo.value.event_type == event_type


def test_future_event_type_is_ignored_not_fatal():
    # Not in today's documented set; the edge must degrade to
    # ack-and-ignore, never crash the schema layer
    payload = build_v1_1(
        event_type="package_detected", device_id=DEVICE, timestamp_ms=1, request_id="req-future"
    )
    with pytest.raises(UnsupportedEvent):
        adapt_v1_1(payload)


def test_unknown_future_sub_type_degrades_to_noise_and_is_preserved():
    payload = load_fixture("motion_sub_type_motion")
    payload["data"]["attributes"]["sub_type"] = "animal"
    event = event_from(payload)
    assert event.kind == EventKind.MOTION
    assert event.sub_type is None
    assert event.raw["wire"]["data"]["attributes"]["sub_type"] == "animal"


# --- structural violations: SchemaError (the 400 path) -----------------------


def schema_broken(mutate) -> None:
    payload = load_fixture("motion_sub_type_motion")
    mutate(payload)
    with pytest.raises(SchemaError):
        event_from(payload)


def test_missing_data_envelope_rejected():
    schema_broken(lambda p: p.pop("data"))


def test_missing_attributes_rejected():
    schema_broken(lambda p: p["data"].pop("attributes"))


def test_missing_timestamp_rejected():
    schema_broken(lambda p: p["data"]["attributes"].pop("timestamp"))


def test_non_numeric_timestamp_rejected():
    schema_broken(lambda p: p["data"]["attributes"].update(timestamp="2026-08-14 08:53:16"))


def test_missing_source_rejected():
    schema_broken(lambda p: p["data"]["attributes"].pop("source"))


def test_missing_event_id_rejected():
    schema_broken(lambda p: p["data"].pop("id"))


# --- signature over RAW bytes (the docs' #1 common mistake) ------------------


def test_signed_fixture_verifies_with_documented_header():
    body, headers = signed_v1_1(load_fixture("motion_sub_type_human"), SECRET)
    event = receive_v1_1(body, headers, SECRET)
    assert event.kind == EventKind.MOTION
    assert event.sub_type == SubType.HUMAN


def test_signature_header_lookup_is_case_insensitive():
    body, headers = signed_v1_1(load_fixture("button_press"), SECRET)
    lowered = {"x-signature": headers["X-Signature"]}
    event = receive_v1_1(body, lowered, SECRET)
    assert event.kind == EventKind.DING


def test_signature_binds_raw_bytes_not_reparsed_json():
    # Docs "Common Mistakes": HMAC the raw body BEFORE parsing — a
    # byte-different serialization of the same JSON must fail
    payload = load_fixture("motion_sub_type_motion")
    body, headers = signed_v1_1(payload, SECRET)
    reserialized = json.dumps(payload, indent=2).encode("utf-8")
    with pytest.raises(SignatureError):
        receive_v1_1(reserialized, headers, SECRET)


def test_wrong_secret_rejected():
    body, headers = signed_v1_1(load_fixture("button_press"), SECRET)
    with pytest.raises(SignatureError):
        receive_v1_1(body, headers, "not-the-secret")


def test_missing_signature_rejected_before_any_parsing():
    body = encode_v1_1(load_fixture("subscription_activated"))
    with pytest.raises(SignatureError):
        receive_v1_1(body, {}, SECRET)


def test_verified_unsupported_event_propagates_for_the_host_to_ignore():
    body, headers = signed_v1_1(load_fixture("subscription_activated"), SECRET)
    with pytest.raises(UnsupportedEvent):
        receive_v1_1(body, headers, SECRET)


# --- builder follows the documented envelope shape ---------------------------


def test_build_v1_1_defaults_and_roundtrip():
    ms = int(datetime(2026, 2, 13, 13, 39, 57, tzinfo=timezone.utc).timestamp() * 1000)
    payload = build_v1_1(
        event_type="motion_detected",
        device_id=DEVICE,
        timestamp_ms=ms,
        request_id="2ad45ade-1818-4154-813b-afdd8bcd8085",
        sub_type="human",
        component_ids=["0"],
    )
    assert payload["meta"]["version"] == "1.1"
    assert payload["data"]["id"] == f"{DEVICE}_motion_detected_{ms}"
    body, headers = signed_v1_1(payload, SECRET)
    event = receive_v1_1(body, headers, SECRET)
    assert event.sub_type == SubType.HUMAN
    assert event.raw["wire"]["data"]["attributes"]["component_ids"] == ["0"]


# --- full offline path: fixture -> verify -> normalize -> classify -> state -> routing


def make_pipeline(tmp_path) -> Pipeline:
    settings = Settings(
        webhook_secret=SECRET,
        signature_header=LIVE_SIGNATURE_HEADER,
        db_path=str(tmp_path / "state.db"),
    )
    return Pipeline(
        secret=SECRET,
        classifier=RuleBasedClassifier(),
        store=StateStore(settings.db_path),
        router=Router.from_settings(settings),
        signature_header=LIVE_SIGNATURE_HEADER,
    )


def feed(pipeline: Pipeline, payload: dict):
    body, headers = signed_v1_1(payload, SECRET)
    return pipeline.handle_v1_1(body, headers)


def test_full_offline_path_from_documented_fixtures(tmp_path):
    pipeline = make_pipeline(tmp_path)

    human = feed(pipeline, load_fixture("motion_sub_type_human"))
    assert human.intent.intent is Intent.PERSON_AT_DOOR
    assert human.decision.action.value == "notify"  # 08:58 UTC = day

    press = feed(pipeline, load_fixture("button_press"))
    assert press.intent.intent is Intent.PERSON_AT_DOOR  # ding, no sub_type
    assert press.decision.action.value == "escalate"  # 00:02 UTC = night

    noise = feed(pipeline, load_fixture("motion_sub_type_motion"))
    assert noise.intent.intent is Intent.MOTION_NOISE
    assert noise.decision.action.value == "suppress"  # lone motion

    # no package tracks from intent-bearing but package-free fixtures
    assert pipeline.store.open_track_count(DEVICE) == 0


def test_duplicate_redelivery_is_suppressed_by_state(tmp_path):
    # Ring retries failed deliveries (up to 7 attempts) — the same
    # event id arriving twice must be a no-op the second time
    pipeline = make_pipeline(tmp_path)
    payload = load_fixture("motion_sub_type_human")

    first = feed(pipeline, payload)
    second = feed(pipeline, payload)

    assert first.apply_result.transition is not Transition.DUPLICATE
    assert second.apply_result.transition is Transition.DUPLICATE
    assert second.decision.action.value == "suppress"
    assert second.delivered == 0


def test_unsupported_event_never_reaches_state(tmp_path):
    pipeline = make_pipeline(tmp_path)
    with pytest.raises(UnsupportedEvent):
        feed(pipeline, load_fixture("subscription_activated"))
    assert pipeline.store.open_track_count(DEVICE) == 0


def test_builder_event_escalates_at_night(tmp_path):
    pipeline = make_pipeline(tmp_path)
    ms = int(datetime(2026, 9, 14, 22, 30, tzinfo=timezone.utc).timestamp() * 1000)
    payload = build_v1_1(
        event_type="motion_detected",
        device_id=DEVICE,
        timestamp_ms=ms,
        sub_type="human",
        request_id="req-night-1",
    )
    entry = feed(pipeline, payload)
    assert entry.intent.intent is Intent.PERSON_AT_DOOR
    assert entry.decision.action.value == "escalate"


def test_motion_burst_reaches_digest_threshold_over_live_wire(tmp_path):
    pipeline = make_pipeline(tmp_path)
    entries = []
    for index, minute in enumerate((0, 4, 8)):  # three inside the 10-min window
        ms = int(
            datetime(2026, 9, 14, 12, minute, tzinfo=timezone.utc).timestamp() * 1000
        )
        payload = build_v1_1(
            event_type="motion_detected",
            device_id=DEVICE,
            timestamp_ms=ms,
            sub_type="motion",
            request_id=f"req-burst-{index}",
        )
        entries.append(feed(pipeline, payload))
    assert [e.decision.action.value for e in entries] == ["suppress", "suppress", "notify"]

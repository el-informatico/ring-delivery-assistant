"""LLM adapter: request shape, parsing, degradation, failure modes.

No network: a fake transport captures requests and returns canned
OpenAI-style responses, and the deterministic mock vision transport
exercises the full multimodal call path offline (data URI in, parsed
verdict out).
"""

from __future__ import annotations

import base64
import json

import pytest

from ring_assistant.classify import (
    ClassificationContext,
    RuleBasedClassifier,
    SnapshotRuleClassifier,
)
from ring_assistant.imaging import render_scene, render_scene_bytes
from ring_assistant.llm import (
    ClassificationError,
    ConfigurationError,
    FallbackClassifier,
    LLMClassifier,
    build_classifier,
    mock_vision_transport,
)
from ring_assistant.schema import Intent, parse_event_payload
from ring_assistant.settings import Settings
from ring_assistant.snapshots import SnapshotImage


def make_classifier(transport, **overrides) -> LLMClassifier:
    params = dict(
        endpoint="https://llm.example/v1",
        model="vision-1",
        api_key="synthetic-key",
        timeout_s=1.0,
        transport=transport,
    )
    params.update(overrides)
    return LLMClassifier(**params)


def fake_transport(reply_content: str):
    """Return a transport that records requests and serves one reply."""

    calls: list[dict] = []

    def transport(url, body, headers, timeout):
        calls.append(
            {"url": url, "body": json.loads(body), "headers": headers, "timeout": timeout}
        )
        return json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": reply_content}}]}
        ).encode("utf-8")

    transport.calls = calls
    return transport


def motion_event(**overrides):
    payload = {
        "id": "evt-100",
        "type": "motion_detected",
        "sub_type": "human",
        "device_id": "front-door",
        "occurred_at": "2026-09-14T21:37:00Z",
        "snapshot_path": "snapshots/person-door-night.png",
    }
    payload.update(overrides)
    return parse_event_payload(payload)


def test_missing_config_raises():
    with pytest.raises(ConfigurationError, match="RING_LLM_ENDPOINT"):
        LLMClassifier(endpoint="", model="m", api_key="k")
    with pytest.raises(ConfigurationError, match="RING_LLM_MODEL"):
        LLMClassifier(endpoint="https://x", model="", api_key="k")
    with pytest.raises(ConfigurationError, match="RING_LLM_API_KEY"):
        LLMClassifier(endpoint="https://x", model="m", api_key="")


def test_from_env_reads_settings_contract():
    classifier = LLMClassifier.from_env(
        env={
            "RING_LLM_ENDPOINT": "https://llm.example/v1",
            "RING_LLM_MODEL": "vision-1",
            "RING_LLM_API_KEY": "synthetic-key",
        }
    )
    assert classifier.model == "vision-1"


def test_from_env_defaults_to_configuration_error():
    with pytest.raises(ConfigurationError):
        LLMClassifier.from_env(env={})


def test_happy_path_parses_verdict():
    transport = fake_transport('{"intent": "person_at_door", "confidence": 0.9, "rationale": "figure at door"}')
    classifier = make_classifier(transport)
    result = classifier.classify(motion_event(), ClassificationContext(is_night=True))
    assert result.intent.value == "person_at_door"
    assert result.confidence == 0.9
    assert result.source == "llm:vision-1"
    # request sanity: OpenAI-compatible shape, auth header, no key in body
    call = transport.calls[0]
    assert call["url"] == "https://llm.example/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer synthetic-key"
    assert call["body"]["model"] == "vision-1"
    assert call["body"]["temperature"] == 0.0
    assert "synthetic-key" not in json.dumps(call["body"])
    user_text = json.dumps(call["body"]["messages"][1]["content"])
    assert "night" in user_text and "human" in user_text


def test_snapshot_attached_as_data_uri(tmp_path):
    (tmp_path / "snapshots").mkdir()
    render_scene("person-door-night", tmp_path / "snapshots" / "person-door-night.png")
    transport = fake_transport('{"intent": "person_at_door", "confidence": 1}')
    classifier = make_classifier(transport, snapshot_root=tmp_path)
    classifier.classify(motion_event(), ClassificationContext())
    content = transport.calls[0]["body"]["messages"][1]["content"]
    image_part = next(p for p in content if p["type"] == "image_url")
    prefix, b64 = image_part["image_url"]["url"].split(",", 1)
    assert prefix == "data:image/png;base64"
    assert base64.b64decode(b64)[:8] == b"\x89PNG\r\n\x1a\n"


def test_missing_snapshot_degrades_to_text_only(tmp_path):
    transport = fake_transport('{"intent": "motion_noise", "confidence": 0.5}')
    classifier = make_classifier(transport, snapshot_root=tmp_path)
    classifier.classify(motion_event(), ClassificationContext())
    content = transport.calls[0]["body"]["messages"][1]["content"]
    assert [p["type"] for p in content] == ["text"]


def test_no_snapshot_root_never_reads_disk():
    transport = fake_transport('{"intent": "motion_noise", "confidence": 0.5}')
    classifier = make_classifier(transport)  # snapshot_root is None
    classifier.classify(motion_event(), ClassificationContext())
    content = transport.calls[0]["body"]["messages"][1]["content"]
    assert [p["type"] for p in content] == ["text"]


def test_verdict_wrapped_in_prose_still_parsed():
    transport = fake_transport(
        'Sure! Here is my assessment:\n```json\n{"intent": "vehicle_at_door", "confidence": 0.7, "rationale": "van"}\n```'
    )
    classifier = make_classifier(transport)
    result = classifier.classify(motion_event(), ClassificationContext())
    assert result.intent.value == "vehicle_at_door"


def test_unknown_intent_rejected():
    transport = fake_transport('{"intent": "ufo_landing", "confidence": 1}')
    classifier = make_classifier(transport)
    with pytest.raises(ClassificationError, match="unusable model reply"):
        classifier.classify(motion_event(), ClassificationContext())


def test_no_json_in_reply_rejected():
    transport = fake_transport("I cannot help with that.")
    classifier = make_classifier(transport)
    with pytest.raises(ClassificationError, match="no JSON object"):
        classifier.classify(motion_event(), ClassificationContext())


def test_confidence_clamped():
    transport = fake_transport('{"intent": "person_at_door", "confidence": 7}')
    classifier = make_classifier(transport)
    assert classifier.classify(motion_event(), ClassificationContext()).confidence == 1.0
    transport2 = fake_transport('{"intent": "person_at_door", "confidence": -3}')
    classifier2 = make_classifier(transport2)
    assert classifier2.classify(motion_event(), ClassificationContext()).confidence == 0.0


def test_transport_error_wrapped():
    def broken_transport(url, body, headers, timeout):
        raise OSError("connection refused")

    classifier = make_classifier(broken_transport)
    with pytest.raises(ClassificationError, match="connection refused"):
        classifier.classify(motion_event(), ClassificationContext())


def test_http_error_body_surfaces():
    import urllib.error

    def http_error_transport(url, body, headers, timeout):
        raise urllib.error.HTTPError(url, 503, "unavailable", {}, None)

    classifier = make_classifier(http_error_transport)
    with pytest.raises(ClassificationError, match="LLM request/response failed"):
        classifier.classify(motion_event(), ClassificationContext())


def test_malformed_response_shape_rejected():
    def transport(url, body, headers, timeout):
        return b'{"unexpected": true}'

    classifier = make_classifier(transport)
    with pytest.raises(ClassificationError):
        classifier.classify(motion_event(), ClassificationContext())


def test_satisfies_protocol_via_duck_typing():
    transport = fake_transport('{"intent": "motion_noise", "confidence": 0.5}')
    classifier = make_classifier(transport)
    assert callable(classifier.classify)


# -- deterministic mock vision transport ---------------------------------------


class SceneSource:
    """Answers every fetch with one fixed rendered scene."""

    def __init__(self, scene: str):
        self.scene = scene

    def fetch(self, event):
        return SnapshotImage(
            data=render_scene_bytes(self.scene), content_type="image/png", source="test"
        )


class BytesSource:
    def __init__(self, data: bytes, content_type: str):
        self.data, self.content_type = data, content_type

    def fetch(self, event):
        return SnapshotImage(data=self.data, content_type=self.content_type, source="test")


def mock_classifier(scene: str, **overrides) -> LLMClassifier:
    return make_classifier(
        mock_vision_transport, snapshot_source=SceneSource(scene), **overrides
    )


def test_mock_transport_classifies_the_attached_png():
    classifier = mock_classifier("package-mat")
    result = classifier.classify(motion_event(sub_type=None), ClassificationContext())
    assert result.intent is Intent.PACKAGE_DEPOSITED
    assert result.source == "llm:vision-1"
    assert "package present, no person" in result.rationale
    assert "[mock vision]" in result.rationale


def test_mock_transport_matches_the_pixel_rules_verdict():
    # same pixels -> same intent on both paths (the shared ladder)
    for scene, expected in (
        ("package-mat", Intent.PACKAGE_DEPOSITED),
        ("person-with-box", Intent.PACKAGE_PICKED_UP),
        ("person-door", Intent.PERSON_AT_DOOR),
        ("van-drive", Intent.VEHICLE_AT_DOOR),
    ):
        via_mock = mock_classifier(scene).classify(
            motion_event(sub_type=None), ClassificationContext()
        )
        via_rules = SnapshotRuleClassifier(source=SceneSource(scene)).classify(
            motion_event(sub_type=None), ClassificationContext()
        )
        assert via_mock.intent is expected
        assert via_mock.intent is via_rules.intent


def test_mock_transport_degrades_jpeg_to_platform_fields():
    # real Ring snapshots are JPEG: the stdlib decoder refuses them, and
    # the mock (no vision of its own) answers from kind/sub_type
    jpeg_source = BytesSource(b"\xff\xd8\xff\xe0 fake jpeg bytes", "image/jpeg")
    classifier = make_classifier(
        mock_vision_transport, snapshot_source=jpeg_source
    )
    result = classifier.classify(motion_event(sub_type="human"), ClassificationContext())
    assert result.intent is Intent.PERSON_AT_DOOR
    assert "no image evidence" in result.rationale


def test_mock_transport_degrades_empty_scene_to_platform_fields():
    classifier = mock_classifier("motion-empty")
    result = classifier.classify(motion_event(sub_type=None), ClassificationContext())
    assert result.intent is Intent.MOTION_NOISE  # unclassified motion on the wire


def test_mock_transport_is_deterministic():
    classifier = mock_classifier("package-mat")
    first = classifier.classify(motion_event(sub_type=None), ClassificationContext())
    second = mock_classifier("package-mat").classify(
        motion_event(sub_type=None), ClassificationContext()
    )
    assert (first.intent, first.rationale) == (second.intent, second.rationale)


# -- FallbackClassifier --------------------------------------------------------


def test_fallback_degrades_to_rules_when_the_endpoint_cannot_answer():
    def broken(url, body, headers, timeout):
        raise OSError("connection refused")

    classifier = FallbackClassifier(
        primary=make_classifier(broken), fallback=RuleBasedClassifier()
    )
    result = classifier.classify(motion_event(), ClassificationContext())
    assert result.intent is Intent.PERSON_AT_DOOR  # the rules verdict
    assert result.source == "fallback:rules"
    assert "primary classifier unavailable (LLM request/response failed" in result.detail
    assert "connection refused" in result.detail


def test_fallback_passes_healthy_llm_answers_through():
    transport = fake_transport('{"intent": "person_at_door", "confidence": 0.9, "rationale": "figure at door"}')
    classifier = FallbackClassifier(
        primary=make_classifier(transport), fallback=RuleBasedClassifier()
    )
    result = classifier.classify(motion_event(), ClassificationContext())
    assert result.source == "llm:vision-1"  # untouched, no fallback marker
    assert result.confidence == 0.9


# -- build_classifier composition ----------------------------------------------


def test_build_classifier_defaults_to_the_rules_stub():
    classifier = build_classifier(Settings())
    assert isinstance(classifier, RuleBasedClassifier)


def test_build_classifier_uses_pixel_rules_when_a_source_is_wired():
    classifier = build_classifier(Settings(), snapshot_source=SceneSource("package-mat"))
    assert isinstance(classifier, SnapshotRuleClassifier)


def test_build_classifier_wraps_a_configured_llm_with_the_fallback():
    settings = Settings.from_env(
        {
            "RING_LLM_ENDPOINT": "https://llm.example/v1",
            "RING_LLM_MODEL": "vision-1",
            "RING_LLM_API_KEY": "synthetic-key",
        }
    )
    classifier = build_classifier(settings, transport=mock_vision_transport)
    assert isinstance(classifier, FallbackClassifier)
    assert isinstance(classifier.primary, LLMClassifier)
    assert isinstance(classifier.fallback, RuleBasedClassifier)
    # end to end through the composition: mock endpoint answers
    result = classifier.classify(motion_event(sub_type=None), ClassificationContext())
    assert result.intent is Intent.MOTION_NOISE  # no snapshot source wired


def test_build_classifier_stays_offline_on_partial_llm_config():
    settings = Settings.from_env({"RING_LLM_MODEL": "vision-1"})  # no endpoint
    assert isinstance(build_classifier(settings), RuleBasedClassifier)

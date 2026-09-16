"""Multimodal LLM adapter behind the ``IntentClassifier`` protocol.

Speaks the OpenAI-compatible ``/chat/completions`` dialect (works with
OpenAI, vLLM, llama.cpp server, Ollama's OpenAI shim, ...). All
credentials and endpoints come from the environment (``settings.py``);
nothing is hardcoded, and the tests never touch the network — they
inject a fake transport.

The adapter is optional at runtime: the core pipeline works with the
rule stub, and this module raises ``ConfigurationError`` instead of
guessing defaults when the environment is not configured.

Offline, the full multimodal call path still runs:
:func:`mock_vision_transport` answers requests as a vision model would
(deterministic — same request in, same bytes out), and
:func:`build_classifier` is the single composition point that picks
rules / pixel rules / LLM-with-fallback for every CLI and the server.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .classify import (
    ClassificationContext,
    IntentClassifier,
    RuleBasedClassifier,
    SnapshotRuleClassifier,
    analyze_snapshot,
    platform_verdict,
    verdict_from_evidence,
)
from .schema import Intent, IntentResult, RingEvent
from .snapshots import ImageDecodeError, SnapshotSource, decode_png

Transport = Callable[[str, bytes, Mapping[str, str], float], bytes]
"""``transport(url, body, headers, timeout) -> response_bytes``.

Any transport error (including ``urllib.error.URLError`` and
``HTTPError``) propagates to the caller wrapped in ``ClassificationError``.
"""


class ConfigurationError(RuntimeError):
    """The LLM environment contract is incomplete (endpoint/model/key)."""


class ClassificationError(RuntimeError):
    """The LLM could not be reached or produced an unusable answer."""


_INTENT_NAMES = [i.value for i in Intent]

_SYSTEM_PROMPT = (
    "You classify doorbell camera events for a delivery-safety assistant. "
    "The camera's own detection (kind, sub_type) is given and may be wrong or "
    "missing; a snapshot image is provided when available. Respond with ONLY "
    "a JSON object: {\"intent\": one of "
    f"{json.dumps(_INTENT_NAMES)}, \"confidence\": 0.0-1.0, "
    "\"rationale\": \"one short sentence\"}."
)

_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _urllib_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


@dataclass
class LLMClassifier:
    """Classify events via an OpenAI-compatible multimodal endpoint.

    ``snapshot_source`` (any ``SnapshotSource`` — manifest, API, ...) is
    consulted first and carries its own content type, so real JPEG
    snapshots from the Events API travel as ``image/jpeg`` data URIs.
    ``snapshot_root`` remains the simple fixture path for synthetic
    payloads' relative ``snapshot_path`` values. A missing snapshot
    degrades to a text-only request instead of failing the event.
    """

    endpoint: str  # base URL, e.g. https://api.example.com/v1
    model: str
    api_key: str
    timeout_s: float = 20.0
    snapshot_source: SnapshotSource | None = None
    snapshot_root: Path | None = None
    transport: Transport = _urllib_transport

    name = "llm"

    def __post_init__(self) -> None:
        missing = [
            label
            for label, value in (
                ("RING_LLM_ENDPOINT", self.endpoint),
                ("RING_LLM_MODEL", self.model),
                ("RING_LLM_API_KEY", self.api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "LLM classifier not configured; set " + ", ".join(missing)
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides):
        """Build from the environment contract (see .env.example)."""
        from .settings import Settings

        settings = Settings.from_env(env)
        return cls(
            endpoint=overrides.get("endpoint", settings.llm_endpoint),
            model=overrides.get("model", settings.llm_model),
            api_key=overrides.get("api_key", settings.llm_api_key),
            timeout_s=overrides.get("timeout_s", settings.llm_timeout_s),
            snapshot_source=overrides.get("snapshot_source"),
            snapshot_root=overrides.get("snapshot_root"),
            transport=overrides.get("transport", _urllib_transport),
        )

    # -- request assembly -------------------------------------------------

    def _snapshot_data_uri(self, event: RingEvent) -> str | None:
        if self.snapshot_source is not None:
            image = self.snapshot_source.fetch(event)
            if image is None:
                return None
            return f"data:{image.content_type};base64," + base64.b64encode(image.data).decode(
                "ascii"
            )
        if not event.snapshot_path or self.snapshot_root is None:
            return None
        path = Path(self.snapshot_root) / event.snapshot_path
        if not path.is_file():
            return None
        media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return "data:" + media + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    def _user_content(self, event: RingEvent, context: ClassificationContext) -> list[dict]:
        fields = {
            "kind": event.kind.value,
            "sub_type": event.sub_type.value if event.sub_type else None,
            "device": event.device_id,
            "occurred_at": event.occurred_at.isoformat(),
            "time_of_day": "night" if context.is_night else "day",
            "open_package_tracks": context.open_tracks,
        }
        parts: list[dict] = [{"type": "text", "text": json.dumps(fields, sort_keys=True)}]
        # snapshot travels as an image_url data URI per the OpenAI
        # multimodal content convention
        data_uri = self._snapshot_data_uri(event)
        if data_uri:
            parts.append({"type": "image_url", "image_url": {"url": data_uri}})
        return parts

    def _request_body(self, event: RingEvent, context: ClassificationContext) -> bytes:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": self._user_content(event, context)},
            ],
            "temperature": 0.0,
            "max_tokens": 200,
        }
        return json.dumps(payload).encode("utf-8")

    # -- protocol implementation ------------------------------------------

    def classify(self, event: RingEvent, context: ClassificationContext) -> IntentResult:
        url = self.endpoint.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            raw = self.transport(url, self._request_body(event, context), headers, self.timeout_s)
            answer = json.loads(raw.decode("utf-8"))
            content = answer["choices"][0]["message"]["content"]
        except ClassificationError:
            raise
        except (OSError, urllib.error.URLError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ClassificationError(f"LLM request/response failed: {exc}") from exc

        match = _JSON_OBJECT_RE.search(content)
        if match is None:
            raise ClassificationError(f"no JSON object in model reply: {content!r}")
        try:
            verdict = json.loads(match.group(0))
            intent = Intent(verdict["intent"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ClassificationError(f"unusable model reply: {content!r}") from exc

        try:
            confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise ClassificationError(f"bad confidence in model reply: {content!r}") from exc
        rationale = str(verdict.get("rationale", "")).strip() or "no rationale given"

        return IntentResult(
            intent=intent,
            confidence=min(1.0, max(0.0, confidence)),
            rationale=rationale,
            source=f"llm:{self.model}",
            detail=f"kind={event.kind.value} sub_type={event.sub_type.value if event.sub_type else None}",
        )


# -- deterministic offline stand-in --------------------------------------------
#
# The mock lets the WHOLE multimodal call path run without a socket:
# request assembly (fields + base64 image data URI), the HTTP-shaped
# call, and the response parse all execute for real; only the model is
# fake. It reads the attached PNG with the same stdlib decoder and the
# same pixel ladder as the rules classifier, so offline verdicts match
# the rules path exactly and stay deterministic. Real Ring snapshots
# (watermarked JPEG) and missing images degrade to the platform table —
# the honest answer for a vision-less mock, and exactly what the prompt
# tells a real model to do with an unhelpful picture.


def mock_vision_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
    """Answer an ``LLMClassifier`` request as a vision model would, offline."""
    request = json.loads(body.decode("utf-8"))
    fields: dict = {}
    data_uri: str | None = None
    for part in request["messages"][1]["content"]:
        if part.get("type") == "text":
            fields = json.loads(part["text"])
        elif part.get("type") == "image_url":
            data_uri = part["image_url"]["url"]

    verdict = None
    if data_uri and data_uri.startswith("data:image/"):
        try:
            decoded = decode_png(base64.b64decode(data_uri.split(",", 1)[1]))
        except (ValueError, ImageDecodeError):
            decoded = None  # JPEG (real Ring), corrupt, or truncated bytes
        if decoded is not None:
            verdict = verdict_from_evidence(analyze_snapshot(decoded))
    if verdict is not None:
        intent, confidence, rationale = (
            verdict.intent.value,
            verdict.confidence,
            f"{verdict.because} [mock vision]",
        )
    else:
        rule = platform_verdict(fields.get("kind", "motion"), fields.get("sub_type"))
        intent, confidence, rationale = (
            rule.intent.value,
            rule.confidence,
            f"{rule.rationale} [mock vision, no image evidence]",
        )

    reply = json.dumps(
        {"intent": intent, "confidence": confidence, "rationale": rationale}
    )
    return json.dumps(
        {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": request["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
        }
    ).encode("utf-8")


# -- composition: which classifier answers, and what happens when the LLM can't


@dataclass
class FallbackClassifier:
    """LLM first; the rules path answers when the endpoint cannot.

    This is the "rules-based fallback if API friction" clause made
    structural: a transport failure, timeout, or unusable reply at
    classification time degrades the ONE event to the rules verdict —
    the event is never failed for want of a model. The degradation is
    visible, not silent: ``source`` is prefixed ``fallback:`` so a
    timeline never claims the LLM answered when it didn't.
    """

    primary: IntentClassifier
    fallback: IntentClassifier
    name: str = "llm+fallback"

    def classify(self, event: RingEvent, context: ClassificationContext) -> IntentResult:
        try:
            return self.primary.classify(event, context)
        except ClassificationError as exc:
            result = self.fallback.classify(event, context)
            return IntentResult(
                intent=result.intent,
                confidence=result.confidence,
                rationale=result.rationale,
                source=f"fallback:{result.source}",
                detail=f"primary classifier unavailable ({exc}); {result.detail}",
            )


def build_classifier(
    settings,
    snapshot_source: SnapshotSource | None = None,
    transport: Transport | None = None,
) -> IntentClassifier:
    """One composition point for every entry that classifies.

    * ``RING_LLM_ENDPOINT`` + ``RING_LLM_MODEL`` configured -> the
      multimodal :class:`LLMClassifier` wrapped in
      :class:`FallbackClassifier` (endpoint trouble degrades to rules,
      per event, visibly). ``transport`` lets offline runs substitute
      :func:`mock_vision_transport`; the swap to a real endpoint is
      configuration, not code.
    * no LLM, but a snapshot source wired -> the pixel rules
      (:class:`SnapshotRuleClassifier`).
    * neither -> the plain deterministic stub
      (:class:`RuleBasedClassifier`).

    A partially configured LLM environment (say, a model but no
    endpoint) stays offline — the same "incomplete contract refuses to
    guess" behavior the served app has always had.
    """
    rules: IntentClassifier = (
        SnapshotRuleClassifier(source=snapshot_source)
        if snapshot_source is not None
        else RuleBasedClassifier()
    )
    if settings.llm_endpoint and settings.llm_model:
        try:
            llm = LLMClassifier(
                endpoint=settings.llm_endpoint,
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                timeout_s=settings.llm_timeout_s,
                snapshot_source=snapshot_source,
                transport=transport or _urllib_transport,
            )
        except ConfigurationError:
            return rules
        return FallbackClassifier(primary=llm, fallback=rules)
    return rules

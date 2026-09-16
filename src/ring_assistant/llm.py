"""Multimodal LLM adapter behind the ``IntentClassifier`` protocol.

Speaks the OpenAI-compatible ``/chat/completions`` dialect (works with
OpenAI, vLLM, llama.cpp server, Ollama's OpenAI shim, ...). All
credentials and endpoints come from the environment (``settings.py``);
nothing is hardcoded, and the tests never touch the network — they
inject a fake transport.

The adapter is optional at runtime: the core pipeline works with the
rule stub, and this module raises ``ConfigurationError`` instead of
guessing defaults when the environment is not configured.
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

from .classify import ClassificationContext
from .schema import Intent, IntentResult, RingEvent
from .snapshots import SnapshotSource

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

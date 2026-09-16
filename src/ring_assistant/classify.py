"""Intent layer: turn a normalized event into a delivery-safety intent.

Ring's native alerts already answer "is this a package / a person?" —
detection is an INPUT here. The product question is "what is HAPPENING
at this door": a deposit, a pickup, a person at night, or throwaway
noise. Two interchangeable implementations behind one protocol:

* ``RuleBasedClassifier`` — deterministic (kind, sub_type) mapping.
  Zero dependencies, zero network; used by every test and by the demo
  so offline runs are byte-reproducible.
* ``LLMClassifier`` (see ``llm.py``) — multimodal adapter for an
  OpenAI-compatible chat endpoint, configured entirely via environment
  (never hardcoded keys). Same protocol, swappable at composition time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .schema import EventKind, Intent, IntentResult, RingEvent, SubType


@dataclass(frozen=True)
class ClassificationContext:
    """Cheap cross-event context available BEFORE state application.

    ``is_night`` mirrors the router's night window; ``open_tracks`` is
    the number of currently-open package tracks for the event's device.
    The rule stub records them for transparency; the LLM adapter can
    feed them to the model as context.
    """

    is_night: bool = False
    open_tracks: int = 0


class IntentClassifier(Protocol):
    """Anything that can label an event with an intent."""

    def classify(self, event: RingEvent, context: ClassificationContext) -> IntentResult:
        ...


@dataclass(frozen=True)
class _Rule:
    intent: Intent
    confidence: float
    rationale: str


# (kind, sub_type) -> deterministic verdict. Confidences express how
# directly the signal implies the intent: a ding subtyped by Ring's own
# package detection is authoritative; bare motion is a weak signal.
_RULES: dict[tuple[EventKind, SubType | None], _Rule] = {
    (EventKind.DING, SubType.PACKAGE_DELIVERY): _Rule(
        Intent.PACKAGE_DEPOSITED, 1.0, "ding with package_delivery sub_type"
    ),
    (EventKind.MOTION, SubType.PACKAGE_DELIVERY): _Rule(
        Intent.PACKAGE_DEPOSITED, 0.95, "motion with package_delivery sub_type"
    ),
    (EventKind.DING, SubType.PACKAGE_PICKUP): _Rule(
        Intent.PACKAGE_PICKED_UP, 1.0, "ding with package_pickup sub_type"
    ),
    (EventKind.MOTION, SubType.PACKAGE_PICKUP): _Rule(
        Intent.PACKAGE_PICKED_UP, 0.95, "motion with package_pickup sub_type"
    ),
    (EventKind.DING, SubType.HUMAN): _Rule(
        Intent.PERSON_AT_DOOR, 0.95, "ding with human sub_type"
    ),
    (EventKind.DING, SubType.VEHICLE): _Rule(
        Intent.VEHICLE_AT_DOOR, 0.9, "ding with vehicle sub_type"
    ),
    (EventKind.DING, None): _Rule(
        Intent.PERSON_AT_DOOR, 0.85, "ding with no sub_type: someone rang"
    ),
    (EventKind.MOTION, SubType.HUMAN): _Rule(
        Intent.PERSON_AT_DOOR, 0.8, "motion with human sub_type"
    ),
    (EventKind.MOTION, SubType.VEHICLE): _Rule(
        Intent.VEHICLE_AT_DOOR, 0.85, "motion with vehicle sub_type"
    ),
    (EventKind.MOTION, None): _Rule(
        Intent.MOTION_NOISE, 0.6, "unclassified motion: treat as noise"
    ),
}


class RuleBasedClassifier:
    """Deterministic offline classifier: a pure table lookup.

    Context never changes the verdict (stays reproducible); it only
    enriches ``detail`` so timelines explain WHY, not just WHAT.
    """

    name = "rules"

    def classify(self, event: RingEvent, context: ClassificationContext) -> IntentResult:
        rule = _RULES[(event.kind, event.sub_type)]
        context_bits = [
            "night" if context.is_night else "day",
            f"{context.open_tracks} open track(s)",
        ]
        return IntentResult(
            intent=rule.intent,
            confidence=rule.confidence,
            rationale=rule.rationale,
            source=self.name,
            detail=f"context: {', '.join(context_bits)}",
        )

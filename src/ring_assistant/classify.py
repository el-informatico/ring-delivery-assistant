"""Intent layer: turn a normalized event into a delivery-safety intent.

Ring's native alerts already answer "is this a package / a person?" —
detection is an INPUT here. The product question is "what is HAPPENING
at this door": a deposit, a pickup, a person at night, or throwaway
noise. Three interchangeable implementations behind one protocol:

* ``RuleBasedClassifier`` — deterministic (kind, sub_type) mapping.
  Zero dependencies, zero network; used by every test and by the demo
  so offline runs are byte-reproducible.
* ``SnapshotRuleClassifier`` — fetches the event's snapshot through a
  ``SnapshotSource`` and rules on the PIXELS; falls back to the sub_type
  table whenever no decodable image is available. This is the path that
  makes package intents reachable from LIVE traffic: neither the v1.1
  webhook envelope nor a history response ever carries ``package_*``
  sub_types — the documented sub_type vocabulary for motion is
  motion / human / vehicle / other_motion — so the snapshot is where a
  deposit or pickup becomes visible.
* ``LLMClassifier`` (see ``llm.py``) — multimodal adapter for an
  OpenAI-compatible chat endpoint, configured entirely via environment
  (never hardcoded keys). Same protocol, swappable at composition time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .schema import EventKind, Intent, IntentResult, RingEvent, SubType
from .snapshots import DecodedImage, ImageDecodeError, SnapshotSource, decode_png


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


# -- snapshot pixel rules ----------------------------------------------------
#
# Palette targets mirror imaging.py's fixture scenes. Thresholds are
# CALIBRATED TO THOSE FIXTURES with wide margins (box regions are 4-6%
# of the frame, a head ~0.8%); they are not trained on real camera
# output. Real Ring snapshots are watermarked JPEG of arbitrary color —
# the honest consumer for those is the LLM adapter, which takes the same
# fetched bytes as a data URI. What this path pins is the CONTRACT:
# event -> fetch -> bytes -> pixels -> intent, with graceful fallback.

_BOX_BROWN = (120, 85, 55)
_BOX_TAPE = (150, 130, 90)
_SKIN = (205, 175, 145)
_VAN_BODY = (200, 205, 210)
_WHEEL = (30, 30, 32)
_COLOR_TOLERANCE = 20
_WHEEL_TOLERANCE = 15

_MIN_BOX_FRACTION = 0.025  # box regions measure 4.1% / 6.2% in the fixtures
_MIN_PERSON_FRACTION = 0.005  # a bare head measures ~0.8% of the frame
_MIN_VEHICLE_FRACTION = 0.05  # the van body (minus cab window) measures ~12%
_MIN_WHEEL_FRACTION = 0.01  # two wheels measure ~2.6%; night ground reads
# ~77% "wheel" at tolerance 15, which is why the vehicle rule requires
# the body color AND wheels — night scenes never carry body pixels


@dataclass(frozen=True)
class SceneEvidence:
    """Pixel fractions the snapshot rules reason over (0.0–1.0)."""

    box: float
    person: float
    vehicle: float
    wheel: float

    def describe(self) -> str:
        return (
            f"box={self.box:.1%} person={self.person:.1%} "
            f"vehicle={self.vehicle:.1%} wheel={self.wheel:.1%}"
        )


def analyze_snapshot(image: DecodedImage) -> SceneEvidence:
    """Measure the fixture palette's share of the frame."""
    return SceneEvidence(
        box=min(
            1.0,
            image.fraction_matching(_BOX_BROWN, _COLOR_TOLERANCE)
            + image.fraction_matching(_BOX_TAPE, _COLOR_TOLERANCE),
        ),
        person=image.fraction_matching(_SKIN, _COLOR_TOLERANCE),
        vehicle=image.fraction_matching(_VAN_BODY, _COLOR_TOLERANCE),
        wheel=image.fraction_matching(_WHEEL, _WHEEL_TOLERANCE),
    )


class SnapshotRuleClassifier:
    """Pixel rules over the fetched snapshot; sub_type table as fallback.

    Decision ladder, first match wins:

    1. person AND box present -> ``package_picked_up`` (someone is
       carrying the box away)
    2. box present, no person -> ``package_deposited``
    3. person present          -> ``person_at_door``
    4. vehicle body AND wheels -> ``vehicle_at_door``
    5. nothing recognizable   -> fall back to the (kind, sub_type)
       table: an empty frame does not refute the platform's human /
       vehicle verdict (the subject may have left the frame), and a
       ding with an empty snapshot still means "someone rang".

    A missing or undecodable snapshot (no source configured, media
    unavailable, JPEG bytes) degrades to the same fallback — the event
    is never failed for want of an image.
    """

    name = "rules+snapshot"

    def __init__(
        self,
        source: SnapshotSource | None = None,
        fallback: RuleBasedClassifier | None = None,
    ):
        self.source = source
        self.fallback = fallback or RuleBasedClassifier()

    def classify(self, event: RingEvent, context: ClassificationContext) -> IntentResult:
        context_bits = [
            "night" if context.is_night else "day",
            f"{context.open_tracks} open track(s)",
        ]
        decoded, note = None, ""
        if self.source is not None:
            image = self.source.fetch(event)
            if image is None:
                note = "snapshot unavailable"
            else:
                try:
                    decoded = decode_png(image.data)
                    note = f"snapshot from {image.source} ({image.content_type})"
                except ImageDecodeError as exc:
                    note = f"snapshot undecodable: {exc}"
        else:
            note = "no snapshot source configured"

        if decoded is None:
            result = self.fallback.classify(event, context)
            return IntentResult(
                intent=result.intent,
                confidence=result.confidence,
                rationale=result.rationale,
                source=self.name,
                detail=f"{note}; {result.detail}",
            )

        evidence = analyze_snapshot(decoded)
        has_person = evidence.person >= _MIN_PERSON_FRACTION
        has_box = evidence.box >= _MIN_BOX_FRACTION
        has_vehicle = (
            evidence.vehicle >= _MIN_VEHICLE_FRACTION
            and evidence.wheel >= _MIN_WHEEL_FRACTION
        )
        if has_person and has_box:
            intent, confidence, because = (
                Intent.PACKAGE_PICKED_UP,
                0.75,
                "snapshot: person carrying a package",
            )
        elif has_box:
            intent, confidence, because = (
                Intent.PACKAGE_DEPOSITED,
                0.8,
                "snapshot: package present, no person",
            )
        elif has_person:
            intent, confidence, because = (
                Intent.PERSON_AT_DOOR,
                0.85,
                "snapshot: person at the door",
            )
        elif has_vehicle:
            intent, confidence, because = (
                Intent.VEHICLE_AT_DOOR,
                0.8,
                "snapshot: vehicle body and wheels",
            )
        else:
            result = self.fallback.classify(event, context)
            return IntentResult(
                intent=result.intent,
                confidence=result.confidence,
                rationale=result.rationale,
                source=self.name,
                detail=f"snapshot empty ({evidence.describe()}); {result.detail}",
            )
        return IntentResult(
            intent=intent,
            confidence=confidence,
            rationale=f"{because} [{evidence.describe()}]",
            source=self.name,
            detail=f"{note}; context: {', '.join(context_bits)}",
        )

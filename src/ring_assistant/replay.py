"""End-to-end pipeline: verify -> normalize -> classify -> state -> route.

One ``handle`` call processes one webhook exactly as production would;
the demo and the tests replay the SAME code path (``receive_webhook``
is the production entry, not a test shortcut). ``handle_v1_1`` is the
live Ring wire entry — same downstream path, different edge (see
``wire.py``); everything after the edge is shared, so internal replay
and live traffic can never drift apart. Ordering inside both is
deliberate:

  1. verify + normalize (raises SignatureError / WebhookError)
  2. classify with PRE-apply context (how many tracks are open BEFORE
     this event changes anything)
  3. apply to the state machine (transition + derived tracks)
  4. route with POST-apply context (transition + burst count that
     INCLUDES the event itself)
  5. deliver to sinks

``format_timeline`` renders entries deterministically (event timestamps
only, never wall-clock) so demo output is byte-reproducible. Each entry
also carries :class:`StageTimings` — per-stage wall-clock latency in
milliseconds, measured with ``time.perf_counter`` — which the star-metric
runner reports and the timeline renderer ignores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter
from typing import Mapping

from .classify import ClassificationContext, IntentClassifier
from .ingest import receive_webhook
from .routing import RoutingContext, RoutingDecision, Router, is_night
from .schema import Intent, IntentResult, RingEvent
from .state import ApplyResult, StateStore
from .wire import receive_v1_1


@dataclass(frozen=True)
class StageTimings:
    """Wall-clock latency of each pipeline stage for one event (ms).

    ``total_ms`` is the ding -> routed-notification latency the star
    metric reports (edge through delivery). Timings are measurements of
    THIS process on THIS machine — environment, not contract.
    """

    edge_ms: float  # verify (HMAC) + normalize (the v1.1 edge)
    classify_ms: float  # classification (snapshot fetch + verdict)
    state_ms: float  # state machine apply (append + rebuild + diff)
    route_ms: float  # routing decision
    deliver_ms: float  # sink delivery (log / Telegram / webhook)

    @property
    def total_ms(self) -> float:
        return self.edge_ms + self.classify_ms + self.state_ms + self.route_ms + self.deliver_ms


@dataclass(frozen=True)
class TimelineEntry:
    event: RingEvent
    intent: IntentResult
    apply_result: ApplyResult
    decision: RoutingDecision
    delivered: int
    timings: StageTimings | None = None  # None only for hand-built entries


class Pipeline:
    def __init__(
        self,
        *,
        secret: str,
        classifier: IntentClassifier,
        store: StateStore,
        router: Router,
        signature_header: str = "x-ring-signature",
    ):
        self.secret = secret
        self.classifier = classifier
        self.store = store
        self.router = router
        self.signature_header = signature_header

    def handle(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        received_at: datetime | None = None,
    ) -> TimelineEntry:
        """Internal contract: verify + parse a flat payload, process it.

        ``received_at`` overrides wall-clock arrival time (capture
        replay wants the ORIGINAL arrival, not "now").
        """
        start = perf_counter()
        event = receive_webhook(
            body,
            headers,
            self.secret,
            signature_header=self.signature_header,
            received_at=received_at,
        )
        edge_ms = (perf_counter() - start) * 1000
        return self.process(event, edge_ms=edge_ms)

    def handle_v1_1(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        received_at: datetime | None = None,
    ) -> TimelineEntry:
        """Live Ring contract: verify raw bytes, adapt the v1.1 envelope.

        Raises ``UnsupportedEvent`` for documented event types this
        pipeline deliberately ignores — the host acks 200 and moves on
        (see ``wire.py`` for why not 4xx).
        """
        start = perf_counter()
        event = receive_v1_1(
            body,
            headers,
            self.secret,
            signature_header=self.signature_header,
            received_at=received_at,
        )
        edge_ms = (perf_counter() - start) * 1000
        return self.process(event, edge_ms=edge_ms)

    def process(self, event: RingEvent, *, edge_ms: float | None = None) -> TimelineEntry:
        # classify BEFORE apply: open_tracks is the world as the
        # classifier saw it when the event arrived
        classification_context = ClassificationContext(
            is_night=is_night(
                event.occurred_at,
                self.router.night_start_hour,
                self.router.night_end_hour,
            ),
            open_tracks=self.store.open_track_count(event.device_id),
        )
        mark = perf_counter()
        intent = self.classifier.classify(event, classification_context)
        classify_ms = (perf_counter() - mark) * 1000

        # state next: transition + tracks this event causes
        mark = perf_counter()
        apply_result = self.store.apply(event, intent.intent)
        state_ms = (perf_counter() - mark) * 1000

        # route AFTER apply: burst count includes this event
        mark = perf_counter()
        burst_count = self.store.recent_intents(
            event.device_id,
            Intent.MOTION_NOISE,
            window=timedelta(minutes=self.router.motion_burst_window_min),
            before=event.occurred_at,
        )
        decision = self.router.route(
            RoutingContext(event, intent, apply_result, burst_count)
        )
        route_ms = (perf_counter() - mark) * 1000

        mark = perf_counter()
        delivered = self.router.deliver(decision)
        deliver_ms = (perf_counter() - mark) * 1000

        timings = (
            StageTimings(
                edge_ms=edge_ms or 0.0,
                classify_ms=classify_ms,
                state_ms=state_ms,
                route_ms=route_ms,
                deliver_ms=deliver_ms,
            )
            if edge_ms is not None
            else None  # direct process() callers get no edge measurement
        )
        return TimelineEntry(event, intent, apply_result, decision, delivered, timings)


_COL = "{time}  {event_id:<8}  {label:<34}  {intent:<20}  {action:<8}  {transition:<18}  {reason}"


def format_timeline(entries: list[TimelineEntry]) -> str:
    """Deterministic readable timeline (one line per event)."""
    lines = [
        _COL.format(
            time="occurred            ",
            event_id="event",
            label="signal",
            intent="intent",
            action="action",
            transition="transition",
            reason="reason",
        )
    ]
    lines.append("-" * 136)
    for entry in entries:
        lines.append(
            _COL.format(
                time=entry.event.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
                event_id=entry.event.event_id,
                label=entry.event.label,
                intent=entry.intent.intent.value,
                action=entry.decision.action.value.upper(),
                transition=entry.apply_result.transition.value,
                reason=entry.decision.reason,
            )
        )
    return "\n".join(lines)

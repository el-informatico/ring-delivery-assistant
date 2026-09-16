"""Cross-event state machine: package tracks derived from the event log.

A single ding means little; ``deposit -> picked up`` is the story users
care about. This module owns that story:

* ``events``  — append-only arrival log (INSERT order == arrival order,
  the order webhooks were accepted in).
* ``tracks``  — DERIVED per-device view, rebuilt from the log sorted by
  ``occurred_at`` on every apply. Arrival order never corrupts it: a
  late-arriving deposit simply lands inside the track it belongs to.

Semantics (per device, replayed in occurred_at order):
  PACKAGE_DEPOSITED -> open a track, or refresh the open one
  PACKAGE_PICKED_UP -> close the open track if it deposited within
                       PAIRING_WINDOW before the pickup, else ORPHAN
  anything else     -> no track change (person/vehicle/noise)

Transitions are reported to the caller (routing conditions on them):

  TRACK_OPENED        first deposit of a still-open track
  TRACK_REFRESHED     additional deposit on the open track
  TRACK_CLOSED        pickup paired with an already-arrived deposit
  PICKUP_ORPHAN       pickup with no pairable deposit (yet)
  DEPOSIT_SUPERSEDED  deposit landed in a track already closed — its
                      pickup was processed on arrival before the
                      deposit showed up (out-of-order webhook)
  NO_TRACK_CHANGE     event does not touch package tracking
  DUPLICATE           event_id already applied; nothing changed

SQLite via stdlib sqlite3. Schema is migration-free for this skeleton
(``CREATE TABLE IF NOT EXISTS``); tracks rows are rebuilt wholesale.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from .schema import Intent, RingEvent

PAIRING_WINDOW = timedelta(hours=24)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    sub_type    TEXT,
    intent      TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    device_id        TEXT NOT NULL,
    deposited_at     TEXT NOT NULL,
    picked_up_at     TEXT,
    deposits         INTEGER NOT NULL,
    deposit_event_ids TEXT NOT NULL,
    pickup_event_id  TEXT,
    out_of_order     INTEGER NOT NULL DEFAULT 0
);
"""


class Transition(Enum):
    DUPLICATE = "duplicate"
    TRACK_OPENED = "track_opened"
    TRACK_REFRESHED = "track_refreshed"
    TRACK_CLOSED = "track_closed"
    PICKUP_ORPHAN = "pickup_orphan"
    DEPOSIT_SUPERSEDED = "deposit_superseded"
    NO_TRACK_CHANGE = "no_track_change"


@dataclass(frozen=True)
class Track:
    """One deposit->pickup pairing (or an open track, ``picked_up_at``
    ``None``) for a device."""

    device_id: str
    deposited_at: datetime
    picked_up_at: datetime | None
    deposits: int
    deposit_event_ids: tuple[str, ...]
    pickup_event_id: str | None
    out_of_order: bool

    @property
    def is_open(self) -> bool:
        return self.picked_up_at is None


@dataclass(frozen=True)
class ApplyResult:
    transition: Transition
    note: str
    open_tracks: int


@dataclass(frozen=True)
class _LoggedEvent:
    event_id: str
    device_id: str
    intent: Intent
    occurred_at: datetime
    received_at: datetime


def _rebuild(device_id: str, events: list[_LoggedEvent]) -> list[Track]:
    """Derive tracks by replaying ``events`` in occurred_at order."""
    tracks: list[Track] = []
    open_track: Track | None = None

    def received_of(event_id: str) -> datetime:
        return next(e.received_at for e in events if e.event_id == event_id)

    for ev in sorted(events, key=lambda e: (e.occurred_at, e.received_at)):
        if ev.intent is Intent.PACKAGE_DEPOSITED:
            if open_track is None:
                open_track = Track(
                    device_id=device_id,
                    deposited_at=ev.occurred_at,
                    picked_up_at=None,
                    deposits=1,
                    deposit_event_ids=(ev.event_id,),
                    pickup_event_id=None,
                    out_of_order=False,
                )
            else:
                open_track = replace(
                    open_track,
                    deposits=open_track.deposits + 1,
                    deposit_event_ids=open_track.deposit_event_ids + (ev.event_id,),
                )
        elif ev.intent is Intent.PACKAGE_PICKED_UP:
            pairable = (
                open_track is not None
                and open_track.deposited_at <= ev.occurred_at
                and ev.occurred_at - open_track.deposited_at <= PAIRING_WINDOW
            )
            if pairable:
                first_deposit_received = min(
                    received_of(eid) for eid in open_track.deposit_event_ids
                )
                tracks.append(
                    replace(
                        open_track,
                        picked_up_at=ev.occurred_at,
                        pickup_event_id=ev.event_id,
                        # the pickup webhook arrived before the deposit's
                        out_of_order=ev.received_at < first_deposit_received,
                    )
                )
                open_track = None
            # else: orphan pickup — no track row; routing saw PICKUP_ORPHAN
    if open_track is not None:
        tracks.append(open_track)
    return tracks


class StateStore:
    """SQLite-backed package-track state, one instance per database file."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._db = sqlite3.connect(self.db_path)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- reads --------------------------------------------------------------

    def tracks(self, device_id: str) -> list[Track]:
        rows = self._db.execute(
            "SELECT deposited_at, picked_up_at, deposits, deposit_event_ids,"
            " pickup_event_id, out_of_order FROM tracks WHERE device_id = ?"
            " ORDER BY deposited_at",
            (device_id,),
        ).fetchall()
        return [
            Track(
                device_id=device_id,
                deposited_at=datetime.fromisoformat(r[0]),
                picked_up_at=datetime.fromisoformat(r[1]) if r[1] else None,
                deposits=r[2],
                deposit_event_ids=tuple(json.loads(r[3])),
                pickup_event_id=r[4],
                out_of_order=bool(r[5]),
            )
            for r in rows
        ]

    def open_track_count(self, device_id: str) -> int:
        return sum(1 for t in self.tracks(device_id) if t.is_open)

    def recent_intents(
        self,
        device_id: str,
        intent: Intent,
        *,
        window: timedelta,
        before: datetime,
    ) -> int:
        """Events with ``intent`` in ``(before - window, before]``."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM events WHERE device_id = ? AND intent = ?"
            " AND occurred_at > ? AND occurred_at <= ?",
            (
                device_id,
                intent.value,
                (before - window).isoformat(),
                before.isoformat(),
            ),
        ).fetchone()
        return row[0]

    # -- writes -------------------------------------------------------------

    def apply(self, event: RingEvent, intent: Intent) -> ApplyResult:
        """Log one accepted event (arrival order) and re-derive tracks."""
        existing = self._db.execute(
            "SELECT 1 FROM events WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        if existing:
            return ApplyResult(
                Transition.DUPLICATE,
                f"event {event.event_id} already applied",
                self.open_track_count(event.device_id),
            )

        self._db.execute(
            "INSERT INTO events (event_id, device_id, kind, sub_type, intent,"
            " occurred_at, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.device_id,
                event.kind.value,
                event.sub_type.value if event.sub_type else None,
                intent.value,
                event.occurred_at.isoformat(),
                event.received_at.isoformat(),
            ),
        )
        before = {t.deposited_at: t for t in self.tracks(event.device_id)}
        logged = self._logged_events(event.device_id)
        after = _rebuild(event.device_id, logged)
        self._db.execute("DELETE FROM tracks WHERE device_id = ?", (event.device_id,))
        self._db.executemany(
            "INSERT INTO tracks (device_id, deposited_at, picked_up_at, deposits,"
            " deposit_event_ids, pickup_event_id, out_of_order)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    t.device_id,
                    t.deposited_at.isoformat(),
                    t.picked_up_at.isoformat() if t.picked_up_at else None,
                    t.deposits,
                    json.dumps(list(t.deposit_event_ids)),
                    t.pickup_event_id,
                    int(t.out_of_order),
                )
                for t in after
            ],
        )
        self._db.commit()

        transition, note = self._diff(event, intent, before, after)
        return ApplyResult(transition, note, sum(1 for t in after if t.is_open))

    # -- internals -----------------------------------------------------------

    def _logged_events(self, device_id: str) -> list[_LoggedEvent]:
        rows = self._db.execute(
            "SELECT event_id, device_id, intent, occurred_at, received_at"
            " FROM events WHERE device_id = ?",
            (device_id,),
        ).fetchall()
        return [
            _LoggedEvent(
                event_id=r[0],
                device_id=r[1],
                intent=Intent(r[2]),
                occurred_at=datetime.fromisoformat(r[3]),
                received_at=datetime.fromisoformat(r[4]),
            )
            for r in rows
        ]

    def _diff(
        self,
        event: RingEvent,
        intent: Intent,
        before: dict[datetime, Track],
        after: list[Track],
    ) -> tuple[Transition, str]:
        if intent is Intent.PACKAGE_DEPOSITED:
            mine = next(
                (t for t in after if event.event_id in t.deposit_event_ids), None
            )
            if mine is not None and mine.picked_up_at is not None:
                return (
                    Transition.DEPOSIT_SUPERSEDED,
                    f"deposit {event.event_id} landed in track already closed by"
                    f" pickup {mine.pickup_event_id} (arrived out of order)",
                )
            if mine is not None and before.get(mine.deposited_at) is not None:
                return (
                    Transition.TRACK_REFRESHED,
                    f"deposit {event.event_id} added to open track from"
                    f" {mine.deposited_at.isoformat()} ({mine.deposits} total)",
                )
            if mine is not None:
                return (
                    Transition.TRACK_OPENED,
                    f"deposit {event.event_id} opened track at"
                    f" {mine.deposited_at.isoformat()}",
                )
        elif intent is Intent.PACKAGE_PICKED_UP:
            mine = next(
                (t for t in after if t.pickup_event_id == event.event_id), None
            )
            if mine is not None and before.get(mine.deposited_at) is not None:
                return (
                    Transition.TRACK_CLOSED,
                    f"pickup {event.event_id} closed track deposited at"
                    f" {mine.deposited_at.isoformat()}",
                )
            return (
                Transition.PICKUP_ORPHAN,
                f"pickup {event.event_id} has no pairable deposit within"
                f" {int(PAIRING_WINDOW.total_seconds() // 3600)}h",
            )
        return (
            Transition.NO_TRACK_CHANGE,
            f"{intent.value} does not affect package tracks",
        )

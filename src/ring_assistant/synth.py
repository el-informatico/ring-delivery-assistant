"""Deterministic synthetic event generator for offline testing and demos.

Every payload is Ring-webhook-shaped (see ``schema.py``). The sequence
is a fixed two-day storyboard with a realistic mix:

Day 1 — a delivery day with noise:
  07:52  motion (no sub_type)    -> noise, suppressed (1/3 in window)
  07:57  motion (no sub_type)    -> noise, suppressed (2/3)
  08:01  motion (no sub_type)    -> noise burst reaches threshold -> digest
  09:14  ding + package_delivery -> PACKAGE_DEPOSITED, track opens
  09:15  motion + vehicle        -> courier van still in frame
  12:41  ding + package_pickup   -> PACKAGE_PICKED_UP, track closes
  16:50  motion + human          -> person seen (daytime)
  21:37  ding + human            -> person at the door AT NIGHT -> escalate

Day 2 — out-of-order delivery and idempotency:
  11:52  ding + package_pickup   arrives FIRST (orphan pickup on arrival)
  11:47  motion + package_delivery arrives SECOND, ~6 min late — the state
         machine re-pairs both into one deposited->picked_up track
  15:20  motion (no sub_type)    -> lone noise, suppressed
  18:00  redelivery of the 09:14 event (same id) -> duplicate, suppressed

Determinism: the storyboard SHAPE is fixed; the seed drives only the
sub-minute jitter (0-19 s), never the ordering or the event mix. Same
seed -> byte-identical JSONL. Jitter is capped at 19 s so the 08:0x
noise burst provably stays inside a 10-minute window and the
out-of-order pair provably keeps its arrival inversion.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .imaging import render_scene
from .schema import EventKind, SubType
from .verify import sign_payload

DEFAULT_SEED = 20260914
DEVICE_ID = "front-door"
DEVICE_NAME = "Front Door"
EVENTS_FILENAME = "events.jsonl"
SNAPSHOTS_DIRNAME = "snapshots"
MAX_JITTER_S = 19

DAY1 = date(2026, 9, 14)
DAY2 = date(2026, 9, 15)

# The duplicate redelivery of evt-004 "arrives" here (see _STORY note).
DUPLICATE_RECEIVED_AT = datetime(2026, 9, 15, 18, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Beat:
    """One storyboard beat: what happened, when, and how late it arrives."""

    eid: str
    kind: EventKind
    sub_type: SubType | None
    day: date
    hour: int
    minute: int
    second: int = 0
    receive_lag_s: int = 4  # received = occurred + lag (network latency)


_STORY: tuple[_Beat, ...] = (
    _Beat("evt-001", EventKind.MOTION, None, DAY1, 7, 52),
    _Beat("evt-002", EventKind.MOTION, None, DAY1, 7, 57),
    _Beat("evt-003", EventKind.MOTION, None, DAY1, 8, 1),
    _Beat("evt-004", EventKind.DING, SubType.PACKAGE_DELIVERY, DAY1, 9, 14, 0, 3),
    _Beat("evt-005", EventKind.MOTION, SubType.VEHICLE, DAY1, 9, 15, 20),
    _Beat("evt-006", EventKind.DING, SubType.PACKAGE_PICKUP, DAY1, 12, 41, 0, 5),
    _Beat("evt-007", EventKind.MOTION, SubType.HUMAN, DAY1, 16, 50, 0, 6),
    _Beat("evt-008", EventKind.DING, SubType.HUMAN, DAY1, 21, 37, 0, 3),
    # Out-of-order pair: the pickup occurred 5 min AFTER the deposit but
    # is delivered ~90 s BEFORE it (deposit lag 401 s simulates a retry
    # queue). Robust to jitter: pickup.received <= 11:52:33 < deposit's
    # >= 11:53:41, while deposit.occurred <= 11:47:19 < pickup's 11:52:10.
    _Beat("evt-009", EventKind.DING, SubType.PACKAGE_PICKUP, DAY2, 11, 52, 10, 4),
    _Beat("evt-010", EventKind.MOTION, SubType.PACKAGE_DELIVERY, DAY2, 11, 47, 0, 401),
    _Beat("evt-011", EventKind.MOTION, None, DAY2, 15, 20),
    # Duplicate redelivery of the day-1 deposit: same id and occurred_at,
    # restamped arrival (build_sequence clones the original payload and
    # sets received_at = DUPLICATE_RECEIVED_AT + jitter).
    _Beat("evt-004", EventKind.DING, SubType.PACKAGE_DELIVERY, DAY1, 9, 14, 0, 3),
)


def _scene_for(kind: EventKind, sub_type: SubType | None, occurred: datetime) -> str:
    hour = occurred.hour
    night = hour >= 21 or hour < 6
    if sub_type is SubType.PACKAGE_DELIVERY:
        return "package-mat"
    if sub_type is SubType.PACKAGE_PICKUP:
        return "person-with-box"
    if sub_type is SubType.VEHICLE:
        return "van-drive"
    if sub_type is SubType.HUMAN:
        return "person-door-night" if night else "person-door"
    # bare motion: empty scene; an unclassified ding still shows a door
    return "motion-empty" if kind is EventKind.MOTION else "person-door"


def build_sequence(seed: int = DEFAULT_SEED) -> list[dict]:
    """Build the synthetic payload list in ARRIVAL (received_at) order."""
    rng = random.Random(seed)
    payloads: list[dict] = []
    emitted: dict[str, dict] = {}
    for beat in _STORY:
        if beat.eid in emitted:  # duplicate redelivery of an earlier event
            clone = dict(emitted[beat.eid])
            clone["received_at"] = (
                DUPLICATE_RECEIVED_AT + timedelta(seconds=rng.randint(0, MAX_JITTER_S))
            ).isoformat()
            payloads.append(clone)
            continue
        occurred = datetime(
            beat.day.year,
            beat.day.month,
            beat.day.day,
            beat.hour,
            beat.minute,
            beat.second,
            tzinfo=timezone.utc,
        ) + timedelta(seconds=rng.randint(0, MAX_JITTER_S))
        payload = {
            "id": beat.eid,
            "type": beat.kind.value,
            "sub_type": beat.sub_type.value if beat.sub_type else None,
            "device_id": DEVICE_ID,
            "device_name": DEVICE_NAME,
            "occurred_at": occurred.isoformat(),
            "received_at": (occurred + timedelta(seconds=beat.receive_lag_s)).isoformat(),
            "snapshot_path": f"{SNAPSHOTS_DIRNAME}/{_scene_for(beat.kind, beat.sub_type, occurred)}.png",
        }
        emitted[beat.eid] = payload
        payloads.append(payload)
    payloads.sort(key=lambda p: p["received_at"])
    return payloads


def write_snapshots(out_dir: str | Path, payloads: list[dict]) -> list[Path]:
    """Render every distinct snapshot scene referenced by ``payloads``."""
    scene_names = sorted(
        {
            Path(p["snapshot_path"]).stem
            for p in payloads
            if p.get("snapshot_path")
        }
    )
    target = Path(out_dir) / SNAPSHOTS_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    return [render_scene(name, target / f"{name}.png") for name in scene_names]


def generate_to(
    out_dir: str | Path, seed: int = DEFAULT_SEED
) -> tuple[Path, list[dict]]:
    """Write ``events.jsonl`` + ``snapshots/`` into ``out_dir``.

    Returns the JSONL path and the payload list (arrival order).
    """
    payloads = build_sequence(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    events_path = out / EVENTS_FILENAME
    with events_path.open("w", encoding="utf-8") as fh:
        for payload in payloads:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    write_snapshots(out, payloads)
    return events_path, payloads


def load_sequence(events_path: str | Path) -> list[dict]:
    """Read a JSONL events file (one payload per line, arrival order)."""
    payloads = []
    for line in Path(events_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            payloads.append(json.loads(line))
    return payloads


def signed_webhooks(
    payloads: list[dict], secret: str
) -> list[tuple[bytes, dict[str, str]]]:
    """Turn payloads into (body, headers) pairs signed with ``secret``."""
    pairs = []
    for payload in payloads:
        body = json.dumps(payload).encode("utf-8")
        pairs.append((body, {"X-Ring-Signature": sign_payload(body, secret)}))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate",
        description="Generate a deterministic synthetic Ring-style event sequence.",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="determinism seed")
    parser.add_argument(
        "--out", default=".synth", help="output directory (default: .synth)"
    )
    args = parser.parse_args(argv)
    events_path, payloads = generate_to(args.out, args.seed)
    print(f"seed={args.seed} events={len(payloads)} -> {events_path}")
    snapshot_paths = sorted({p["snapshot_path"] for p in payloads})
    print(f"snapshots ({len(snapshot_paths)} distinct scenes):")
    for path in snapshot_paths:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

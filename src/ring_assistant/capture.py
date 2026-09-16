"""Webhook capture + replay: record live deliveries, replay them offline.

The record/replay harness closes the gap between "built against the
documented contract" and "runs against real traffic":

* TODAY (no portal access): ``replay_fixture_dir`` replays the
  verbatim documented envelopes in ``tests/fixtures/wire/`` through
  the real signed-verification edge — the same ``Pipeline.handle_v1_1``
  live traffic will hit.
* REGISTRATION DAY: point ``RING_RECORD_DIR`` at a directory and the
  server records every delivery it receives (body, headers, outcome,
  status) to an append-only JSONL capture; ``replay_capture_file``
  replays that capture with ZERO code change — only the byte source
  differs.

Captures hold no secrets: a webhook request carries the HMAC of the
body, never the secret itself, and replay re-signs with the local
``RING_WEBHOOK_SECRET`` anyway (the recording device's secret is not
assumed to be available at replay time). Signatures in recorded
headers are therefore informational, not load-bearing.

Failure rules: recording failures warn on stderr and never fail a
delivery; replay failures mark the record ``rejected`` and never
crash the loop — a replay must always produce a full report.
"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .replay import Pipeline, TimelineEntry, format_timeline
from .schema import SchemaError
from .ingest import WebhookError
from .verify import SignatureError, sign_payload
from .wire import LIVE_SIGNATURE_HEADER, UnsupportedEvent

CAPTURE_VERSION = 1
CAPTURE_FILE_NAME = "webhooks.jsonl"
OUTCOMES = ("accepted", "ignored", "rejected")


@dataclass(frozen=True)
class CaptureRecord:
    """One webhook delivery: raw body, headers, and how it fared."""

    received_at: datetime
    body: bytes
    headers: dict[str, str]
    outcome: str  # accepted | ignored | rejected
    status: int | None = None  # HTTP status answered; None off-server
    note: str = ""  # rejection detail, when there is one

    def to_json(self) -> dict:
        return {
            "version": CAPTURE_VERSION,
            "received_at": self.received_at.isoformat(),
            "body_b64": base64.b64encode(self.body).decode("ascii"),
            "headers": dict(self.headers),
            "outcome": self.outcome,
            "status": self.status,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, data: Mapping) -> "CaptureRecord":
        if data.get("version") != CAPTURE_VERSION:
            raise ValueError(f"unsupported capture record version {data.get('version')!r}")
        received_at = datetime.fromisoformat(str(data["received_at"]))
        if received_at.tzinfo is None:
            received_at = received_at.astimezone()  # local wall-clock capture
        return cls(
            received_at=received_at,
            body=base64.b64decode(str(data["body_b64"])),
            headers={str(k): str(v) for k, v in data.get("headers", {}).items()},
            outcome=str(data.get("outcome", "rejected")),
            status=data.get("status"),
            note=str(data.get("note", "")),
        )


def write_capture(path: str | Path, records: list[CaptureRecord]) -> Path:
    """Write records as JSONL (one JSON object per line)."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record.to_json()) for record in records]
    file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return file


def read_capture(path: str | Path) -> list[CaptureRecord]:
    """Read a JSONL capture. A missing file raises — the caller named it."""
    records: list[CaptureRecord] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(CaptureRecord.from_json(json.loads(line)))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: bad capture record: {exc}") from exc
    return records


class WebhookRecorder:
    """Append-only JSONL recorder. Failing to record never fails a delivery."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def record(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        outcome: str,
        status: int | None = None,
        received_at: datetime | None = None,
        note: str = "",
    ) -> None:
        record = CaptureRecord(
            received_at=received_at or datetime.now().astimezone(),
            body=body,
            headers={str(k): str(v) for k, v in headers.items()},
            outcome=outcome,
            status=status,
            note=note,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_json()) + "\n")
        except OSError as exc:
            print(f"webhook recorder: could not write {self.path}: {exc}", file=sys.stderr)


# -- replay -------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayOutcome:
    """What happened when one captured delivery was re-run."""

    label: str  # capture file line, fixture name, or index
    result: str  # accepted | ignored | rejected
    entry: TimelineEntry | None = None
    error: str = ""

    @property
    def accepted(self) -> bool:
        return self.result == "accepted"


@dataclass(frozen=True)
class ReplayReport:
    outcomes: list[ReplayOutcome]

    @property
    def entries(self) -> list[TimelineEntry]:
        return [o.entry for o in self.outcomes if o.entry is not None]

    @property
    def counts(self) -> dict[str, int]:
        return {name: sum(1 for o in self.outcomes if o.result == name) for name in OUTCOMES}

    def timeline(self) -> str:
        return format_timeline(self.entries)

    def summary(self) -> str:
        counts = self.counts
        return (
            f"replayed {len(self.outcomes)} delivery(ies): "
            f"{counts['accepted']} accepted, {counts['ignored']} ignored, "
            f"{counts['rejected']} rejected"
        )


def _replay_one(
    pipeline: Pipeline, body: bytes, received_at: datetime | None, label: str
) -> ReplayOutcome:
    headers = {LIVE_SIGNATURE_HEADER: sign_payload(body, pipeline.secret)}
    try:
        entry = pipeline.handle_v1_1(body, headers, received_at=received_at)
    except UnsupportedEvent as exc:
        return ReplayOutcome(label=label, result="ignored", error=str(exc))
    except (SignatureError, WebhookError, SchemaError) as exc:
        return ReplayOutcome(label=label, result="rejected", error=str(exc))
    except Exception as exc:  # never crash the loop; report instead
        return ReplayOutcome(label=label, result="rejected", error=f"{type(exc).__name__}: {exc}")
    return ReplayOutcome(label=label, result="accepted", entry=entry)


def replay_records(
    records: list[CaptureRecord],
    pipeline: Pipeline,
    *,
    include_rejected: bool = False,
) -> ReplayReport:
    """Re-run captured deliveries through the live-wire edge.

    Bodies are RE-SIGNED with the pipeline's secret (see module
    docstring), and each runs with its recorded ``received_at`` so
    dedup and burst windows see the original timing. By default,
    deliveries the host already rejected are skipped — they never
    reached state; ``include_rejected=True`` re-runs them for
    diagnosis (a payload rejected for its content rejects again; one
    rejected only for a stale signature will now pass).
    """
    outcomes: list[ReplayOutcome] = []
    for number, record in enumerate(records, 1):
        if record.outcome == "rejected" and not include_rejected:
            continue
        outcomes.append(
            _replay_one(pipeline, record.body, record.received_at, f"record {number}")
        )
    return ReplayReport(outcomes)


def replay_capture_file(
    path: str | Path, pipeline: Pipeline, *, include_rejected: bool = False
) -> ReplayReport:
    return replay_records(read_capture(path), pipeline, include_rejected=include_rejected)


def replay_envelopes(
    envelopes: list[dict],
    pipeline: Pipeline,
    *,
    labels: list[str] | None = None,
) -> ReplayReport:
    """Replay v1.1 envelope dicts (documented examples, probe output)."""
    from .wire import encode_v1_1

    outcomes = [
        _replay_one(
            pipeline,
            encode_v1_1(envelope),
            received_at=None,
            label=(labels[i] if labels else f"envelope {i + 1}"),
        )
        for i, envelope in enumerate(envelopes)
    ]
    return ReplayReport(outcomes)


def replay_fixture_dir(
    directory: str | Path, pipeline: Pipeline
) -> ReplayReport:
    """Replay the verbatim documented envelopes shipped in ``directory``.

    Files are processed in sorted order (deterministic). Every ``*.json``
    must hold one v1.1 envelope — the same files S1 pinned from the
    Partner API documentation, and the same shape a future capture
    export will have.
    """
    envelopes: list[dict] = []
    labels: list[str] = []
    for path in sorted(Path(directory).glob("*.json")):
        envelopes.append(json.loads(path.read_text(encoding="utf-8")))
        labels.append(path.name)
    return replay_envelopes(envelopes, pipeline, labels=labels)

"""Snapshot plumbing: stdlib PNG decoding + pluggable snapshot sources.

Both of Ring's ingress paths deliver events WITHOUT pixels — the v1.1
webhook envelope carries no image reference, and an Event History item
has none either. The documented way to get an image is the Image
Snapshots API, keyed by (device, timestamp). This module mirrors that
keying offline:

* ``ApiSnapshotSource`` — fetches through ``RingApiClient.snapshot()``
  (the documented at_timestamp download). Registration day means
  setting ``RING_API_TOKEN``; nothing else in the pipeline changes.
* ``ManifestSnapshotSource`` — offline stand-in: a JSON manifest maps
  ``"<device_id>@<epoch_ms>"`` to a PNG under a root directory. Same
  key, same contract, fixture bytes instead of HTTP.
* ``LocalSnapshotSource`` — resolves the synthetic payloads'
  ``snapshot_path`` field against a root.
* ``CompositeSnapshotSource`` — first source that answers wins.

All sources DEGRADE to None (event-only classification) when the image
is unavailable; a missing snapshot must never fail an event. The PNG
decoder is pure stdlib (zlib + struct): 8-bit RGB/RGBA, filters 0–4,
no interlace — exactly the profile the fixture writer emits and the
common profile of real exports. JPEG bytes raise ``ImageDecodeError``
with an explicit message: real Ring snapshots are watermarked JPEG,
and the honest consumer for arbitrary image bytes is the LLM adapter
(``llm.py`` attaches them as data URIs), not pixel rules.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .events_api import (
    MEDIA_UNAVAILABLE_CODES,
    EventsApiError,
    RingApiClient,
)
from .schema import RingEvent

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImageDecodeError(ValueError):
    """The snapshot bytes cannot be decoded by the stdlib rules path."""


@dataclass(frozen=True)
class DecodedImage:
    """RGB pixel grid; ``pixels[y][x]`` is an ``(r, g, b)`` tuple."""

    width: int
    height: int
    pixels: list[list[tuple[int, int, int]]]

    def fraction_matching(self, target: tuple[int, int, int], tolerance: int) -> float:
        """Share of pixels within ``tolerance`` per channel of ``target``."""
        if not self.pixels or not self.pixels[0]:
            return 0.0
        total = self.width * self.height
        hit = 0
        for row in self.pixels:
            for r, g, b in row:
                if (
                    abs(r - target[0]) <= tolerance
                    and abs(g - target[1]) <= tolerance
                    and abs(b - target[2]) <= tolerance
                ):
                    hit += 1
        return hit / total


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> list[list[int]]:
    """Reverse PNG per-row filters 0–4 into a flat byte grid (per row)."""
    stride = width * bpp
    rows: list[list[int]] = []
    previous: list[int] | None = None
    offset = 0
    for _ in range(height):
        if offset + 1 + stride > len(raw):
            raise ImageDecodeError("PNG pixel data is truncated")
        filter_byte = raw[offset]
        row = list(raw[offset + 1 : offset + 1 + stride])
        offset += 1 + stride
        if filter_byte == 0:
            pass
        elif filter_byte == 1:  # Sub: left neighbor
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif filter_byte == 2:  # Up: row above
            if previous is not None:
                for i in range(stride):
                    row[i] = (row[i] + previous[i]) & 0xFF
        elif filter_byte == 3:  # Average: floor((left + above) / 2)
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                up = previous[i] if previous is not None else 0
                row[i] = (row[i] + ((left + up) // 2)) & 0xFF
        elif filter_byte == 4:  # Paeth
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                up = previous[i] if previous is not None else 0
                up_left = previous[i - bpp] if (previous is not None and i >= bpp) else 0
                row[i] = (row[i] + _paeth(left, up, up_left)) & 0xFF
        else:
            raise ImageDecodeError(f"unsupported PNG row filter {filter_byte}")
        rows.append(row)
        previous = row
    return rows


def decode_png(data: bytes) -> DecodedImage:
    """Decode a PNG to RGB pixels. Stdlib profile only (see module docstring)."""
    if data[:3] == b"\xff\xd8\xff":
        raise ImageDecodeError(
            "JPEG snapshot: the stdlib rules path decodes PNG only; "
            "route JPEGs to the LLM classifier (llm.py accepts arbitrary bytes)"
        )
    if not data.startswith(PNG_SIGNATURE):
        raise ImageDecodeError("not a PNG (bad signature)")
    if len(data) < 8 + 25:
        raise ImageDecodeError("PNG is truncated before IHDR")

    width = height = 0
    bit_depth = color_type = interlace = 0
    idat = bytearray()
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        chunk = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        if zlib.crc32(tag + chunk) & 0xFFFFFFFF != expected_crc:
            raise ImageDecodeError(f"PNG chunk {tag.decode('ascii', 'replace')} failed its CRC")
        if tag == b"IHDR":
            if length != 13:
                raise ImageDecodeError("bad IHDR length")
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
        elif tag == b"IDAT":
            idat.extend(chunk)
        elif tag == b"IEND":
            break  # trailing bytes after IEND are ignored, as readers do
        offset += 12 + length

    if width <= 0 or height <= 0:
        raise ImageDecodeError("bad PNG dimensions")
    if bit_depth != 8:
        raise ImageDecodeError(f"unsupported PNG bit depth {bit_depth} (only 8-bit)")
    if color_type not in (2, 6):
        raise ImageDecodeError(
            f"unsupported PNG color type {color_type} (only RGB/RGBA); "
            "palette/gray fixtures would need a conversion step"
        )
    if interlace != 0:
        raise ImageDecodeError("interlaced PNG is not supported")

    bpp = 3 if color_type == 2 else 4
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ImageDecodeError(f"PNG pixel stream failed to inflate: {exc}") from exc

    rows = _unfilter(raw, width, height, bpp)
    pixels: list[list[tuple[int, int, int]]] = []
    for row in rows:
        pixels.append(
            [tuple(row[x * bpp : x * bpp + 3]) for x in range(width)]  # type: ignore[misc]
        )
    return DecodedImage(width=width, height=height, pixels=pixels)


@dataclass(frozen=True)
class SnapshotImage:
    """Snapshot bytes plus where they came from (provenance for timelines)."""

    data: bytes
    content_type: str
    source: str  # "manifest" | "events-api" | "local" ...
    media_timestamp: datetime | None = None
    origin: str = ""


class SnapshotSource(Protocol):
    """Fetches the image for an event, or None to degrade (never raises)."""

    def fetch(self, event: RingEvent) -> SnapshotImage | None: ...


@dataclass(frozen=True)
class LocalSnapshotSource:
    """Resolves the synthetic payloads' ``snapshot_path`` under ``root``."""

    root: Path

    def fetch(self, event: RingEvent) -> SnapshotImage | None:
        if not event.snapshot_path:
            return None
        path = Path(self.root) / event.snapshot_path
        if not path.is_file():
            return None
        suffix = path.suffix.lower()
        content_type = "image/png" if suffix == ".png" else "image/jpeg"
        return SnapshotImage(
            data=path.read_bytes(), content_type=content_type, source="local"
        )


@dataclass(frozen=True)
class ManifestSnapshotSource:
    """Offline stand-in for the Image Snapshots API, keyed the same way.

    ``manifest`` maps ``"<device_id>@<epoch_ms>"`` (the ms of the event's
    ``occurred_at``) to an image path relative to ``root``. Same key the
    documented API uses (device + timestamp), fixture bytes instead of
    HTTP — swapping to ``ApiSnapshotSource`` is a composition change.
    """

    root: Path
    manifest_path: Path

    def _entries(self) -> dict[str, str]:
        try:
            loaded = json.loads(Path(self.manifest_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def fetch(self, event: RingEvent) -> SnapshotImage | None:
        epoch_ms = int(event.occurred_at.timestamp() * 1000)
        relative = self._entries().get(f"{event.device_id}@{epoch_ms}")
        if not relative:
            return None
        path = Path(self.root) / relative
        if not path.is_file():
            return None
        content_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return SnapshotImage(
            data=path.read_bytes(),
            content_type=content_type,
            source="manifest",
            media_timestamp=event.occurred_at,
        )


@dataclass(frozen=True)
class ApiSnapshotSource:
    """Fetches snapshots through the documented Events API client.

    Degrades to None on the documented no-media answers (MEDIA_NOT_FOUND,
    RECORDING_NOT_READY, CORRUPT_RECORDING) — a missing snapshot must
    not fail the event. Everything else (401, transport, malformed) is
    a misconfiguration and raises, so the host notices.
    """

    client: RingApiClient
    degrade_codes: frozenset[str] = MEDIA_UNAVAILABLE_CODES

    def fetch(self, event: RingEvent) -> SnapshotImage | None:
        epoch_ms = int(event.occurred_at.timestamp() * 1000)
        try:
            snapshot = self.client.snapshot(event.device_id, epoch_ms)
        except EventsApiError as exc:
            if exc.code in self.degrade_codes:
                return None
            raise
        return SnapshotImage(
            data=snapshot.data,
            content_type=snapshot.content_type,
            source="events-api",
            media_timestamp=snapshot.media_timestamp,
            origin=snapshot.origin,
        )


class CompositeSnapshotSource:
    """Tries sources in order; the first one that answers wins."""

    def __init__(self, *sources: SnapshotSource):
        self.sources: tuple[SnapshotSource, ...] = tuple(sources)

    def fetch(self, event: RingEvent) -> SnapshotImage | None:
        for source in self.sources:
            image = source.fetch(event)
            if image is not None:
                return image
        return None

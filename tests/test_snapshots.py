"""Snapshot plumbing: stdlib PNG decoder + the pluggable sources.

The decoder is tested against the fixture writer (round trip) and
against hand-encoded PNGs that exercise row filters 1-4, which the
writer never emits but real encoders do. Sources are tested on their
degradation contract: a missing image is None, never an exception —
except misconfiguration (bad token), which must surface.
"""

from __future__ import annotations

import json
import struct
import zlib

import pytest
from ring_api_mock import MOCK_TOKEN, MockRingApi

from ring_assistant.events_api import EventsApiAuthError, RingApiClient
from ring_assistant.imaging import HEIGHT, WIDTH, render_scene_bytes
from ring_assistant.schema import parse_event_payload
from ring_assistant.snapshots import (
    ApiSnapshotSource,
    CompositeSnapshotSource,
    ImageDecodeError,
    LocalSnapshotSource,
    ManifestSnapshotSource,
    _paeth,
    decode_png,
)

DEV = "ava1.ring.device.door001"
BASE_MS = 1_789_000_000_000


def make_client(mock: MockRingApi) -> RingApiClient:
    return RingApiClient(access_token=MOCK_TOKEN, transport=mock)


def event(**overrides):
    payload = {
        "id": "evt-snap",
        "type": "motion_detected",
        "device_id": DEV,
        "occurred_at": "2026-05-15T08:00:00Z",
    }
    payload.update(overrides)
    return parse_event_payload(payload)


# -- decoder -----------------------------------------------------------------


def test_decode_round_trips_every_fixture_scene():
    for scene in (
        "motion-empty",
        "package-mat",
        "person-door",
        "person-door-night",
        "van-drive",
        "person-with-box",
    ):
        image = decode_png(render_scene_bytes(scene))
        assert (image.width, image.height) == (WIDTH, HEIGHT)
        assert len(image.pixels) == HEIGHT and len(image.pixels[0]) == WIDTH


def test_decode_returns_writer_pixels_verbatim(tmp_path):
    from ring_assistant.imaging import render_scene

    path = render_scene("package-mat", tmp_path / "mat.png")
    image = decode_png(path.read_bytes())
    # spot-check palette anchors: the tape stripe center is tape-colored
    tape_x = (36 + 60) // 2
    assert image.pixels[44][tape_x] == (150, 130, 90)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _hand_png(width: int, height: int, filters: list[int], pixels: list[list[int]]) -> bytes:
    """Encode an 8-bit RGB PNG choosing each row's filter by hand."""

    bpp = 3
    previous: list[int] | None = None
    raw = b""
    for y in range(height):
        row = pixels[y]
        f = filters[y]
        encoded: list[int] = []
        for i in range(width * bpp):
            left = row[i - bpp] if i >= bpp else 0
            up = previous[i] if previous is not None else 0
            up_left = previous[i - bpp] if (previous is not None and i >= bpp) else 0
            if f == 0:
                encoded.append(row[i])
            elif f == 1:
                encoded.append((row[i] - left) & 0xFF)
            elif f == 2:
                encoded.append((row[i] - up) & 0xFF)
            elif f == 3:
                encoded.append((row[i] - (left + up) // 2) & 0xFF)
            else:
                encoded.append((row[i] - _paeth(left, up, up_left)) & 0xFF)
        raw += bytes([f]) + bytes(encoded)
        previous = row
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _channel(x: int, y: int, c: int) -> int:
    return ((x * 8 + c * 37 + y * 11) * (y + 1) + c) % 256


PIXELS = [
    [_channel(x, y, c) for x in range(6) for c in range(3)] for y in range(4)
]  # 4 rows of 6 RGB pixels as flat channel values


@pytest.mark.parametrize("filter_byte", [0, 1, 2, 3, 4])
def test_decode_unfilters_every_row_filter(filter_byte):
    data = _hand_png(6, 4, [filter_byte] * 4, PIXELS)
    image = decode_png(data)
    flat = [value for row in PIXELS for value in row]
    assert [v for row in image.pixels for pixel in row for v in pixel] == flat


def test_decode_mixes_filters_per_row():
    data = _hand_png(6, 4, [0, 1, 2, 4], PIXELS)
    assert decode_png(data).pixels[3] == [tuple(PIXELS[3][x * 3 : x * 3 + 3]) for x in range(6)]


def test_jpeg_magic_routes_to_llm_with_explicit_message():
    with pytest.raises(ImageDecodeError, match="LLM classifier"):
        decode_png(b"\xff\xd8\xff\xe0rest-of-a-jpeg")


def test_bad_signature_is_rejected():
    with pytest.raises(ImageDecodeError, match="not a PNG"):
        decode_png(b"GIF89a-not-a-png-at-all")


def test_corrupted_chunk_crc_is_rejected():
    data = bytearray(render_scene_bytes("package-mat"))
    data[-6] ^= 0xFF  # flip a byte inside IEND's CRC
    with pytest.raises(ImageDecodeError, match="CRC"):
        decode_png(bytes(data))


def test_truncated_pixel_stream_is_rejected():
    # a VALID zlib stream that ends after two of the four scanlines the
    # IHDR promises — inflate succeeds, the unfilterer runs out of rows
    raw = b"".join(b"\x00" + bytes(row) for row in PIXELS[:2])
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 6, 4, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    with pytest.raises(ImageDecodeError, match="truncated"):
        decode_png(data)


def test_unsupported_profiles_are_rejected():
    def png_with(color_type: int, bit_depth: int, interlace: int) -> bytes:
        return (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + struct.pack(">IIBBBBB", 2, 2, bit_depth, color_type, 0, 0, interlace)
            + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 2, 2, bit_depth, color_type, 0, 0, interlace)) & 0xFFFFFFFF)
            + b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    with pytest.raises(ImageDecodeError, match="bit depth"):
        decode_png(png_with(2, 16, 0))
    with pytest.raises(ImageDecodeError, match="color type"):
        decode_png(png_with(3, 8, 0))
    with pytest.raises(ImageDecodeError, match="interlaced"):
        decode_png(png_with(2, 8, 1))


def test_fraction_matching_counts_within_tolerance():
    image = decode_png(render_scene_bytes("package-mat"))
    sky = image.fraction_matching((150, 160, 175), 5)
    assert 0.3 < sky < 0.7  # day sky fills everything above the horizon
    assert image.fraction_matching((0, 0, 0), 0) == 0.0


# -- LocalSnapshotSource -----------------------------------------------------


def test_local_source_resolves_snapshot_path(tmp_path):
    from ring_assistant.imaging import render_scene

    render_scene("person-door", tmp_path / "person-door.png")
    source = LocalSnapshotSource(root=tmp_path)
    image = source.fetch(event(snapshot_path="person-door.png"))
    assert image is not None and image.source == "local"
    assert image.content_type == "image/png"


def test_local_source_degrades_on_missing_file_or_field(tmp_path):
    source = LocalSnapshotSource(root=tmp_path)
    assert source.fetch(event(snapshot_path="never-rendered.png")) is None
    assert source.fetch(event()) is None  # no snapshot_path at all


# -- ManifestSnapshotSource --------------------------------------------------


def manifest_event(epoch_ms: int):
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    payload = {
        "id": "evt-m",
        "type": "ding",
        "device_id": DEV,
        "occurred_at": moment.isoformat().replace("+00:00", "Z"),
    }
    return parse_event_payload(payload)


def test_manifest_source_keys_on_device_at_epoch_ms(tmp_path):
    from ring_assistant.imaging import render_scene

    (tmp_path / "snapshots").mkdir()
    render_scene("package-mat", tmp_path / "snapshots" / "package-mat.png")
    manifest = tmp_path / "snapshots" / "manifest.json"
    manifest.write_text(json.dumps({f"{DEV}@{BASE_MS}": "snapshots/package-mat.png"}))
    source = ManifestSnapshotSource(root=tmp_path, manifest_path=manifest)
    image = source.fetch(manifest_event(BASE_MS))
    assert image is not None and image.source == "manifest"
    assert image.media_timestamp is not None


def test_manifest_source_degrades_on_miss_or_missing_artifacts(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({f"{DEV}@{BASE_MS}": "snapshots/gone.png"}))
    source = ManifestSnapshotSource(root=tmp_path, manifest_path=manifest)
    assert source.fetch(manifest_event(BASE_MS)) is None  # entry points nowhere
    assert source.fetch(manifest_event(BASE_MS + 1)) is None  # no entry for that ms
    absent = ManifestSnapshotSource(root=tmp_path, manifest_path=tmp_path / "nope.json")
    assert absent.fetch(manifest_event(BASE_MS)) is None  # no manifest at all


def test_manifest_source_survives_malformed_manifest(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("not json {")
    source = ManifestSnapshotSource(root=tmp_path, manifest_path=manifest)
    assert source.fetch(manifest_event(BASE_MS)) is None


# -- ApiSnapshotSource -------------------------------------------------------


def test_api_source_fetches_through_the_documented_two_step():
    mock = MockRingApi()
    mock.add_snapshot(DEV, BASE_MS, "van-drive")
    source = ApiSnapshotSource(client=make_client(mock))
    image = source.fetch(manifest_event(BASE_MS + 2_000))  # nearest-match window
    assert image is not None
    assert image.source == "events-api"
    assert image.content_type == "image/png"
    assert image.origin == "recording"
    assert image.media_timestamp is not None


def test_api_source_degrades_on_documented_no_media_codes():
    mock = MockRingApi()
    mock.device_list.append({"type": "devices", "id": DEV, "attributes": {"name": "Front Door"}})
    source = ApiSnapshotSource(client=make_client(mock))
    assert source.fetch(manifest_event(BASE_MS)) is None  # 416 MEDIA_NOT_FOUND
    mock.media_error = (425, "RECORDING_NOT_READY", "not yet")
    assert source.fetch(manifest_event(BASE_MS)) is None  # documented retry-later


def test_api_source_raises_on_misconfiguration():
    client = RingApiClient(access_token="wrong", transport=MockRingApi())
    source = ApiSnapshotSource(client=client)
    with pytest.raises(EventsApiAuthError):
        source.fetch(manifest_event(BASE_MS))


# -- CompositeSnapshotSource -------------------------------------------------


class StubSource:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def fetch(self, e):
        self.calls += 1
        return self.answer


def test_composite_first_answer_wins_and_later_sources_untouched():
    first, second = StubSource(None), StubSource("image")
    composite = CompositeSnapshotSource(first, second)
    assert composite.fetch(event()) == "image"
    assert (first.calls, second.calls) == (1, 1)

    only_miss = CompositeSnapshotSource(StubSource(None), StubSource(None))
    assert only_miss.fetch(event()) is None

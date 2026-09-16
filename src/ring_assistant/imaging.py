"""Minimal pure-stdlib PNG writer and synthetic snapshot scenes.

No PIL / numpy: fixtures must be generatable in a bare Python 3.12
environment (the core is dependency-free by design). Images are tiny
96x64 RGB PNGs standing in for doorbell snapshots. The rules stub
never reads pixels; the LLM adapter sends whatever bytes the snapshot
path holds, so these fixtures exercise the real plumbing offline.

Scenes are deterministic functions of the scene name — no randomness —
so the same scene name always renders byte-identical output (verified
by tests), keeping generated demo artifacts reproducible.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Callable

WIDTH = 96
HEIGHT = 64

RGB = tuple[int, int, int]
Canvas = list[list[RGB]]


def _chunk(tag: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return length + tag + data + crc


def write_png(path: str | Path, width: int, height: int, canvas: Canvas) -> None:
    """Write an 8-bit RGB PNG (no interlace). Rows are per-pixel RGB tuples."""
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("BBB", *pixel) for pixel in row)
        for row in canvas
    )
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    Path(path).write_bytes(payload)


def _canvas(day: bool) -> Canvas:
    sky: RGB = (150, 160, 175) if day else (18, 22, 38)
    ground: RGB = (105, 100, 95) if day else (28, 30, 40)
    horizon = HEIGHT * 2 // 3
    return [
        [sky if y < horizon else ground for _ in range(WIDTH)] for y in range(HEIGHT)
    ]


def _rect(canvas: Canvas, x0: int, y0: int, x1: int, y1: int, color: RGB) -> None:
    for y in range(max(0, y0), min(HEIGHT, y1)):
        for x in range(max(0, x0), min(WIDTH, x1)):
            canvas[y][x] = color


def _circle(canvas: Canvas, cx: int, cy: int, r: int, color: RGB) -> None:
    for y in range(max(0, cy - r), min(HEIGHT, cy + r + 1)):
        for x in range(max(0, cx - r), min(WIDTH, cx + r + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                canvas[y][x] = color


def _door(canvas: Canvas, night: bool) -> None:
    color: RGB = (70, 60, 55) if not night else (10, 12, 20)
    _rect(canvas, 30, 8, 66, 44, color)


def _person(canvas: Canvas, shirt: RGB = (60, 90, 140)) -> None:
    _circle(canvas, 48, 22, 4, (205, 175, 145))  # head
    _rect(canvas, 41, 27, 56, 44, shirt)  # torso


def _package(canvas: Canvas, x0: int, y0: int, x1: int, y1: int) -> None:
    _rect(canvas, x0, y0, x1, y1, (120, 85, 55))  # box
    _rect(canvas, (x0 + x1) // 2 - 2, y0, (x0 + x1) // 2 + 2, y1, (150, 130, 90))  # tape


def _scene_motion_empty() -> Canvas:
    return _canvas(day=True)


def _scene_package_mat() -> Canvas:
    canvas = _canvas(day=True)
    _rect(canvas, 28, 50, 68, 58, (80, 78, 74))  # door mat
    _package(canvas, 36, 36, 60, 52)
    return canvas


def _scene_person_door() -> Canvas:
    canvas = _canvas(day=True)
    _door(canvas, night=False)
    _person(canvas)
    return canvas


def _scene_person_door_night() -> Canvas:
    canvas = _canvas(day=False)
    _door(canvas, night=True)
    _person(canvas, shirt=(45, 60, 95))
    _circle(canvas, 12, 10, 6, (240, 220, 150))  # porch light
    return canvas


def _scene_van_drive() -> Canvas:
    canvas = _canvas(day=True)
    _rect(canvas, 20, 28, 76, 44, (200, 205, 210))  # van body
    _rect(canvas, 58, 31, 72, 40, (160, 180, 200))  # cab window
    _circle(canvas, 32, 46, 5, (30, 30, 32))  # wheels
    _circle(canvas, 64, 46, 5, (30, 30, 32))
    return canvas


def _scene_person_with_box() -> Canvas:
    canvas = _canvas(day=True)
    _door(canvas, night=False)
    _person(canvas, shirt=(90, 70, 120))
    _package(canvas, 52, 36, 70, 50)  # box carried at the side
    return canvas


SCENES: dict[str, Callable[[], Canvas]] = {
    "motion-empty": _scene_motion_empty,
    "package-mat": _scene_package_mat,
    "person-door": _scene_person_door,
    "person-door-night": _scene_person_door_night,
    "van-drive": _scene_van_drive,
    "person-with-box": _scene_person_with_box,
}

SCENE_NAMES = tuple(SCENES)


def render_scene(scene: str, path: str | Path) -> Path:
    """Render one named scene to ``path`` as PNG. Unknown scene -> KeyError."""
    painter = SCENES[scene]
    write_png(path, WIDTH, HEIGHT, painter())
    return Path(path)

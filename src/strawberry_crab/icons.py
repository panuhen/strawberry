"""The tray icon's pixels, with nothing but the standard library (WIRING.md §14).

`strawberry/assets/icons/strawberry-<n>.png` are rendered once from Noto Color Emoji by
`scripts/render_icons.py` and checked in; they ship inside the wheel as package data, and at
runtime we only have to read them, so Pillow stays a dev dependency.

StatusNotifierItem wants `a(iiay)`: width, height, and the pixels as **ARGB32 in network
byte order** (big-endian: A, R, G, B per pixel), which is not what a PNG stores (R, G, B, A).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from . import paths

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ICON_SIZES = (16, 22, 24, 32, 48, 64)


def icons_dir() -> Path:
    """Where the PNGs live: the copy packaged with this module (checkout and wheel alike)."""
    return paths.icons_dir()


def read_png(data: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGBA, non-interlaced PNG to (width, height, rgba bytes).

    That is the only kind render_icons.py writes; anything else raises ValueError rather than
    guessing, because a wrong guess would show as a scrambled tray icon.
    """
    if data[:8] != PNG_MAGIC:
        raise ValueError("not a PNG")
    width = height = 0
    idat = bytearray()
    pos = 8
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length  # length, type, data, CRC
        if kind == b"IHDR":
            width, height, depth, colour, compression, filt, interlace = struct.unpack(">IIBBBBB", chunk)
            if (depth, colour, compression, filt, interlace) != (8, 6, 0, 0, 0):
                raise ValueError(f"unsupported PNG: depth {depth}, colour type {colour}, interlace {interlace}")
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if not width or not height:
        raise ValueError("PNG without an IHDR")
    raw = zlib.decompress(bytes(idat))
    return width, height, _unfilter(raw, width, height)


def _unfilter(raw: bytes, width: int, height: int) -> bytes:
    """Undo the per-scanline filters (PNG spec §9). Four bytes per pixel, so bpp = 4."""
    stride = width * 4
    out = bytearray(stride * height)
    previous = bytearray(stride)
    pos = 0
    for row in range(height):
        method = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if method == 1:      # Sub
            for i in range(4, stride):
                line[i] = (line[i] + line[i - 4]) & 0xFF
        elif method == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif method == 3:    # Average
            for i in range(stride):
                left = line[i - 4] if i >= 4 else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif method == 4:    # Paeth
            for i in range(stride):
                left = line[i - 4] if i >= 4 else 0
                up = previous[i]
                up_left = previous[i - 4] if i >= 4 else 0
                estimate = left + up - up_left
                pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - up_left)
                nearest = left if (pa <= pb and pa <= pc) else (up if pb <= pc else up_left)
                line[i] = (line[i] + nearest) & 0xFF
        elif method != 0:
            raise ValueError(f"unknown PNG filter {method}")
        out[row * stride:(row + 1) * stride] = line
        previous = line
    return bytes(out)


def rgba_to_argb(rgba: bytes) -> bytes:
    """RGBA bytes -> ARGB32 in network byte order, which is what the SNI spec asks for."""
    out = bytearray(len(rgba))
    out[0::4] = rgba[3::4]
    out[1::4] = rgba[0::4]
    out[2::4] = rgba[1::4]
    out[3::4] = rgba[2::4]
    return bytes(out)


def pixmap(path: Path) -> tuple[int, int, bytes]:
    """One (width, height, ARGB32) entry for the IconPixmap property."""
    width, height, rgba = read_png(Path(path).read_bytes())
    return width, height, rgba_to_argb(rgba)


def pixmaps(directory: Path | None = None, sizes=ICON_SIZES) -> list[tuple[int, int, bytes]]:
    """Every icon we have, smallest first: the panel picks the size that fits its height."""
    directory = Path(directory) if directory is not None else icons_dir()
    out = []
    for size in sizes:
        path = directory / f"strawberry-{size}.png"
        if path.is_file():
            out.append(pixmap(path))
    return out

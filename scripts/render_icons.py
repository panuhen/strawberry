#!/usr/bin/env python3
"""Render the 🍓 emoji to the tray icon PNGs (run once; the output is checked in).

Noto Color Emoji (Apache-2.0, /usr/share/fonts/truetype/noto/NotoColorEmoji.ttf on Debian and
Ubuntu) is a CBDT bitmap font: it has one strike, 109 ppem, and Pillow will only load it at
exactly that size. So the glyph is drawn once at the strike size, cropped to its own alpha
box, and scaled down to each size the StatusNotifierItem asks for (WIRING.md §14).

    python3 scripts/render_icons.py [--font PATH] [--out DIR]

Needs Pillow (a dev dependency: `.venv/bin/python scripts/render_icons.py`, or
the system python3 on Ubuntu, which has it). The tray itself reads the PNGs with zlib only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

STRAWBERRY = "\U0001F353"
FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
STRIKE_PPEM = 109          # the font's only bitmap strike; any other size raises "invalid pixel size"
SIZES = (16, 22, 24, 32, 48, 64)
OUT = Path(__file__).resolve().parents[1] / "src" / "strawberry" / "assets" / "icons"
MARGIN = 0.04              # a hair of space so she is not clipped by a panel's own padding


def render(font_path: str, sizes=SIZES, out_dir: Path = OUT) -> list[Path]:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(font_path, STRIKE_PPEM)
    left, top, right, bottom = font.getbbox(STRAWBERRY)
    canvas = Image.new("RGBA", (right - left + 8, bottom - top + 8), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((4 - left, 4 - top), STRAWBERRY, font=font, embedded_color=True)
    glyph = canvas.crop(canvas.getbbox())  # the strike is padded; keep only the berry

    written = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for size in sizes:
        inner = max(1, round(size * (1 - 2 * MARGIN)))
        scale = min(inner / glyph.width, inner / glyph.height)
        resized = glyph.resize((max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))), Image.LANCZOS)
        square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        square.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2))
        path = out_dir / f"strawberry-{size}.png"
        square.save(path, "PNG", optimize=True)
        written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="render the strawberry emoji to tray PNGs")
    parser.add_argument("--font", default=FONT)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    if not Path(args.font).is_file():
        print(f"{args.font} not found (Ubuntu: apt install fonts-noto-color-emoji)", file=sys.stderr)
        return 1
    for path in render(args.font, out_dir=args.out):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

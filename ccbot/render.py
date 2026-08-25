"""Rendering terminal art to PNG.

Telegram's mobile clients wrap long lines inside code blocks instead of
scrolling them, so any drawing wider than roughly 36 characters falls apart on
a phone. An image sidesteps the chat layout entirely: the reader gets pinch-zoom
and panning, and the alignment is exactly what the terminal produced.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Box-drawing glyphs must exist in the face, which rules out most UI fonts.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf",
)

# Telegram's dark bubble, so the image does not glare in a dark chat.
BG = (24, 30, 38)
FG = (206, 221, 234)
PADDING = 18
FONT_SIZE = 22
# Slightly negative: the '│' glyph is rasterised a hair short of its box, so
# rows must overlap by a pixel or vertical rules come out dashed.
LINE_SPACING = -1


class RenderError(RuntimeError):
    pass


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RenderError("no monospace font found")


def available() -> bool:
    try:
        _font(FONT_SIZE)
        return True
    except (RenderError, OSError):
        return False


def text_to_png(text: str, out_path: Path, size: int = FONT_SIZE) -> Path:
    """Draw monospaced *text* onto a PNG sized to fit it exactly."""
    lines = [ln.rstrip("\n") for ln in text.rstrip().splitlines()] or [""]
    font = _font(size)

    # Measuring one glyph is enough — the face is monospaced by construction.
    probe = font.getbbox("M")
    cell_w = probe[2] - probe[0]
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + LINE_SPACING

    width = max(len(ln) for ln in lines) * cell_w + PADDING * 2
    height = len(lines) * line_h + PADDING * 2

    img = Image.new("RGB", (int(max(width, 64)), int(max(height, 48))), BG)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((PADDING, PADDING + i * line_h), line, font=font, fill=FG)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path


def max_line_width(text: str) -> int:
    return max((len(ln) for ln in text.splitlines()), default=0)

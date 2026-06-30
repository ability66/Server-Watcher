from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re

from PIL import Image, ImageDraw, ImageFont


ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
DEFAULT_BG = "#0b1020"
DEFAULT_FG = "#d7e0ea"
ANSI_COLORS = {
    30: "#2e3440",
    31: "#e06c75",
    32: "#98c379",
    33: "#e5c07b",
    34: "#61afef",
    35: "#c678dd",
    36: "#56b6c2",
    37: "#d7dae0",
    90: "#5c6370",
    91: "#ff7b72",
    92: "#7ee787",
    93: "#f2cc60",
    94: "#79c0ff",
    95: "#d2a8ff",
    96: "#a5f3fc",
    97: "#f0f6fc",
}
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansMonoCJK-Regular.ttc",
)


@dataclass(frozen=True)
class TerminalTheme:
    background: str = DEFAULT_BG
    foreground: str = DEFAULT_FG
    padding_x: int = 24
    padding_y: int = 20
    font_size: int = 20
    line_spacing: int = 6


@dataclass(frozen=True)
class StyledSegment:
    text: str
    color: str


def render_terminal_png(text: str, *, theme: TerminalTheme | None = None) -> bytes:
    theme = theme or TerminalTheme()
    font = _load_font(theme.font_size)
    lines = [_parse_ansi_line(line, theme.foreground) for line in text.splitlines() or [""]]

    probe = Image.new("RGB", (16, 16))
    draw = ImageDraw.Draw(probe)
    line_height = _line_height(draw, font, theme)
    content_width = max((_line_width(draw, font, line) for line in lines), default=0)
    width = max(320, int(content_width) + theme.padding_x * 2)
    height = max(120, len(lines) * line_height + theme.padding_y * 2)

    image = Image.new("RGB", (width, height), theme.background)
    draw = ImageDraw.Draw(image)
    y = theme.padding_y
    for line in lines:
        x = theme.padding_x
        if not line:
            y += line_height
            continue
        for segment in line:
            if not segment.text:
                continue
            draw.text((x, y), segment.text, font=font, fill=segment.color)
            x += _text_length(draw, font, segment.text)
        y += line_height

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _parse_ansi_line(line: str, default_color: str) -> list[StyledSegment]:
    segments: list[StyledSegment] = []
    color = default_color
    cursor = 0
    for match in ANSI_RE.finditer(line):
        if match.start() > cursor:
            segments.append(StyledSegment(line[cursor : match.start()], color))
        color = _apply_sgr(match.group(1), color, default_color)
        cursor = match.end()
    if cursor < len(line):
        segments.append(StyledSegment(line[cursor:], color))
    return segments


def _apply_sgr(code_text: str, current_color: str, default_color: str) -> str:
    parts = [part for part in code_text.split(";") if part]
    if not parts:
        return default_color

    color = current_color
    for part in parts:
        code = int(part)
        if code == 0:
            color = default_color
        elif code in ANSI_COLORS:
            color = ANSI_COLORS[code]
    return color


def _load_font(font_size: int):
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, font_size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_length(draw: ImageDraw.ImageDraw, font, text: str) -> float:
    return draw.textlength(text, font=font)


def _line_width(draw: ImageDraw.ImageDraw, font, line: list[StyledSegment]) -> float:
    return sum(_text_length(draw, font, segment.text) for segment in line)


def _line_height(draw: ImageDraw.ImageDraw, font, theme: TerminalTheme) -> int:
    top, bottom = draw.textbbox((0, 0), "Ag", font=font)[1::2]
    return int(bottom - top) + theme.line_spacing

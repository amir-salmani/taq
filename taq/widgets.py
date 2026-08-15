"""Drawing primitives: bordered boxes, braille graphs, gradient meters.

The look is btop's — rounded borders, dense braille plots, meters that shift
from green to red as they fill — implemented on plain curses so it costs a
handful of addstr calls per frame instead of a rendering framework.
"""

from __future__ import annotations

import curses

# --- named colour slots ------------------------------------------------------
C_DIM, C_OK, C_WARN, C_BAD, C_ACCENT, C_HEAD, C_TEXT, C_FOCUS = range(1, 9)

# --- gradient slots ----------------------------------------------------------
# A ramp through the xterm-256 cube, green to red. Meters and graphs colour
# themselves by value, so a glance at hue tells you the state before you read
# the number.
_RAMP = (46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196)
GRAD_BASE = 20
GRAD_N = len(_RAMP)

_has_256 = False


def init_colors() -> None:
    global _has_256
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK

    for slot, colour in (
        (C_DIM, curses.COLOR_WHITE), (C_OK, curses.COLOR_GREEN),
        (C_WARN, curses.COLOR_YELLOW), (C_BAD, curses.COLOR_RED),
        (C_ACCENT, curses.COLOR_CYAN), (C_HEAD, curses.COLOR_MAGENTA),
        (C_TEXT, curses.COLOR_WHITE), (C_FOCUS, curses.COLOR_CYAN),
    ):
        curses.init_pair(slot, colour, bg)

    _has_256 = curses.COLORS >= 256
    if _has_256:
        for i, colour in enumerate(_RAMP):
            try:
                curses.init_pair(GRAD_BASE + i, colour, bg)
            except curses.error:
                _has_256 = False
                break


def attr(slot: int, bold: bool = False, dim: bool = False) -> int:
    a = curses.color_pair(slot)
    if bold:
        a |= curses.A_BOLD
    if dim or slot == C_DIM:
        a |= curses.A_DIM
    return a


def grad(frac: float, bold: bool = False) -> int:
    """Colour for a 0..1 value. Falls back to three coarse steps on a terminal
    without a 256-colour palette."""
    frac = max(0.0, min(1.0, frac))
    if not _has_256:
        slot = C_BAD if frac >= 0.9 else C_WARN if frac >= 0.7 else C_OK
        return attr(slot, bold)
    a = curses.color_pair(GRAD_BASE + min(GRAD_N - 1, int(frac * GRAD_N)))
    return a | curses.A_BOLD if bold else a


# -----------------------------------------------------------------------------
# Box
# -----------------------------------------------------------------------------

TL, TR, BL, BR, H, V = "╭", "╮", "╰", "╯", "─", "│"


class Box:
    """A bordered region. Everything written through it is clipped to the
    inside, so a too-small terminal degrades rather than raising."""

    def __init__(self, win, top: int, left: int, height: int, width: int,
                 title: str = "", focused: bool = False, hint: str = ""):
        self.win, self.top, self.left = win, top, left
        self.height, self.width = height, width
        self.title, self.focused, self.hint = title, focused, hint
        self.y = 0

    # inner geometry
    @property
    def iw(self) -> int:
        return max(0, self.width - 4)

    @property
    def ih(self) -> int:
        return max(0, self.height - 2)

    @property
    def room(self) -> int:
        return self.ih - self.y

    def clear(self) -> None:
        """Blank the interior. Required for any box that floats over already
        drawn content — curses only paints the cells you write, so without this
        the panels underneath show through every gap in the text."""
        blank = " " * max(0, self.width - 2)
        for r in range(1, self.height - 1):
            self._put(r, 1, blank, attr(C_TEXT))

    def frame(self) -> None:
        if self.height < 2 or self.width < 4:
            return
        border = attr(C_FOCUS, True) if self.focused else attr(C_DIM)
        w = self.width
        self._put(0, 0, TL + H * (w - 2) + TR, border)
        for r in range(1, self.height - 1):
            self._put(r, 0, V, border)
            self._put(r, w - 1, V, border)
        self._put(self.height - 1, 0, BL + H * (w - 2) + BR, border)

        if self.title:
            label = f" {self.title} "
            self._put(0, 2, label,
                      attr(C_FOCUS, True) if self.focused else attr(C_HEAD, True))
        if self.hint and w > len(self.hint) + 8:
            self._put(self.height - 1, w - len(self.hint) - 3, f" {self.hint} ",
                      attr(C_FOCUS) if self.focused else attr(C_DIM))

    # -- writing ------------------------------------------------------------
    def _put(self, row: int, col: int, text: str, a: int) -> None:
        """Absolute, border-inclusive placement. Internal."""
        if row < 0 or row >= self.height or col >= self.width or not text:
            return
        text = text[: self.width - col]
        try:
            self.win.addstr(self.top + row, self.left + col, text, a)
        except curses.error:
            pass  # writing the last cell always raises; harmless

    def put(self, row: int, col: int, text: str, a: int = 0) -> None:
        """Inside the border, (0,0) is the first usable cell."""
        if row < 0 or row >= self.ih or col < 0 or col >= self.iw:
            return
        self._put(row + 1, col + 2, text[: self.iw - col], a or attr(C_TEXT))

    def line(self, text: str = "", a: int = 0, indent: int = 0) -> None:
        if self.y < self.ih:
            self.put(self.y, indent, text, a)
        self.y += 1

    def skip(self, n: int = 1) -> None:
        self.y += n


# -----------------------------------------------------------------------------
# meters and graphs
# -----------------------------------------------------------------------------

FULL, HALF, EMPTY = "█", "▌", "░"


def meter(box: Box, row: int, col: int, width: int, frac: float,
          label: str = "") -> None:
    """A gradient bar. Each filled cell is coloured by its own position, so the
    bar itself carries the scale — the tip of a full bar is red."""
    if width <= 0:
        return
    frac = max(0.0, min(1.0, frac))
    filled = frac * width
    whole = int(filled)

    for i in range(width):
        if i < whole:
            ch, a = FULL, grad(i / max(1, width - 1))
        elif i == whole and filled - whole >= 0.4:
            ch, a = HALF, grad(i / max(1, width - 1))
        else:
            ch, a = EMPTY, attr(C_DIM)
        box.put(row, col + i, ch, a)

    if label:
        box.put(row, col + width + 1, label, grad(frac, True))


# Braille packs 2x4 pixels into one cell. Bit per (col, row-within-cell):
_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def braille(values, width: int, height: int, vmax: float | None = None) -> list[str]:
    """Render a series as `height` rows of `width` braille cells.

    Resolution is 2*width horizontal by 4*height vertical pixels, which is why
    btop's graphs read as continuous rather than as a bar chart.
    """
    if width <= 0 or height <= 0:
        return []
    px_w, px_h = width * 2, height * 4
    series = list(values)[-px_w:]
    if not series:
        return [" " * width] * height
    series = [0.0] * (px_w - len(series)) + series

    top = vmax if vmax is not None else max(series)
    if not top or top <= 0:
        top = 1.0

    cells = [[0] * width for _ in range(height)]
    for x, val in enumerate(series):
        h = int(round(max(0.0, min(1.0, val / top)) * px_h))
        cx, sub = divmod(x, 2)
        for y in range(h):
            py = px_h - 1 - y              # fill upward from the baseline
            cy, row = divmod(py, 4)
            if 0 <= cy < height:
                cells[cy][cx] |= _DOTS[sub][row]

    return ["".join(chr(0x2800 + b) for b in row) for row in cells]


def draw_graph(box: Box, row: int, col: int, width: int, height: int,
               values, vmax: float | None = None, frac_for_color=None) -> None:
    """Paint a braille graph, colouring each row by the height it represents."""
    rows = braille(values, width, height, vmax)
    for i, text in enumerate(rows):
        # Row 0 is the top of the plot, so it carries the highest values.
        band = 1.0 - (i / max(1, height)) if frac_for_color is None else frac_for_color
        box.put(row + i, col, text, grad(band))


def spark(values, width: int) -> str:
    """A single-row block sparkline, for places too tight for a real graph."""
    blocks = " ▁▂▃▄▅▆▇█"
    series = list(values)[-width:]
    if not series:
        return " " * width
    top = max(series) or 1.0
    out = "".join(blocks[min(8, int(v / top * 8))] for v in series)
    return out.rjust(width)


# -----------------------------------------------------------------------------
# formatting
# -----------------------------------------------------------------------------

def human_bytes(n: float, per_sec: bool = False) -> str:
    unit = "B"
    for u, div in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= div:
            n, unit = n / div, u
            break
    s = f"{n:.1f}{unit}" if n < 100 and unit != "B" else f"{n:.0f}{unit}"
    return s + "/s" if per_sec else s


def human_count(n: int) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(int(n))


def short_dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h".replace(".0h", "h")
    return f"{seconds / 86400:.1f}d".replace(".0d", "d")


def wrap(text: str, width: int) -> list[str]:
    if width <= 0:
        return []
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            if line:
                out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out

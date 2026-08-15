"""Layout, focus, and the four panels.

Modelled on lazygit/lazydocker: a grid of bordered boxes, exactly one focused,
navigation inside the focused box only. Narrow terminals drop to one full-screen
panel at a time rather than trying to cram four boxes into 70 columns.
"""

from __future__ import annotations

import curses
import time

from . import coherence as coh
from .widgets import (Box, C_ACCENT, C_BAD, C_DIM, C_FOCUS, C_HEAD, C_OK,
                      C_TEXT, C_WARN, attr, draw_graph, grad, human_bytes,
                      human_count, meter, short_dur, spark, wrap)

CLAUDE, DOCKER, SYSTEM, COHERENCE = range(4)
PANEL_NAMES = ("Claude", "Docker", "System", "Coherence")
WIDE_AT = 100          # below this, one panel at a time


class State:
    """Everything the user has navigated to. Survives across frames."""

    def __init__(self) -> None:
        self.focus = CLAUDE
        self.sel: dict[int, int] = {p: 0 for p in range(4)}
        self.overlay: str | None = None        # None | "logs" | "help"
        self.log_lines: list[str] = []
        self.log_scroll = 0
        self.log_title = ""
        self.confirm: tuple[str, str, str] | None = None   # (prompt, cid, verb)
        self.flash = ""
        self.flash_at = 0.0

    def say(self, msg: str) -> None:
        self.flash, self.flash_at = msg, time.time()

    @property
    def flashing(self) -> str:
        return self.flash if time.time() - self.flash_at < 4 else ""

    def move(self, panel: int, delta: int, count: int) -> None:
        if count <= 0:
            self.sel[panel] = 0
            return
        self.sel[panel] = max(0, min(count - 1, self.sel[panel] + delta))


# -----------------------------------------------------------------------------
# panels
# -----------------------------------------------------------------------------

def panel_claude(b: Box, snap: dict, st: State) -> None:
    windows = snap["windows"]
    if not windows:
        b.line("no rate-limit data yet", attr(C_WARN))
        b.skip()
        for chunk in wrap("Rate limits reach a local tool only through the "
                          "statusline hook. Run `taq install`, then open any "
                          "Claude Code session.", b.iw):
            b.line(chunk, attr(C_DIM))
        b.skip()
    else:
        bar_w = max(8, min(26, b.iw - 20))
        for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
            w = windows.get(key)
            if not w or b.room < 2:
                continue
            b.put(b.y, 0, label, attr(C_TEXT, True))
            meter(b, b.y, 3, bar_w, w.used / 100.0, f"{w.used:5.1f}%")
            b.y += 1

            b.put(b.y, 3, f"resets {clock(w.resets_at)} · in {short_dur(w.resets_in)}",
                  attr(C_DIM))
            b.y += 1

            if w.eta is not None and w.will_exhaust:
                b.put(b.y, 3, f"⚠ cap ~{clock(w.eta)} — {short_dur(w.resets_at - w.eta)} early",
                      attr(C_BAD, True))
            elif w.rate is not None and w.rate > 0.05:
                b.put(b.y, 3, f"+{w.rate:.1f}%/h — clears the window", attr(C_OK))
            elif w.rate is not None:
                b.put(b.y, 3, "flat", attr(C_DIM))
            else:
                b.put(b.y, 3, f"learning burn rate ({max(0, 3 - w.samples)} more)",
                      attr(C_DIM))
            b.y += 2

    if split := snap["model_split"]:
        total = sum(v for _, v in split) or 1
        b.line("  ".join(f"{short_model(m)} {v * 100 // total}%"
                         for m, v in split[:3]), attr(C_DIM))
        b.skip()

    sessions = snap["sessions"]
    if b.room > 1:
        b.line(f"SESSIONS ({len(sessions)})", attr(C_HEAD, True))
        focused = st.focus == CLAUDE
        sel = st.sel[CLAUDE]
        for i, s in enumerate(sessions):
            if b.room <= 0:
                break
            on = focused and i == sel
            base = attr(C_TEXT if on else C_DIM, on)
            if on:
                b.put(b.y, 0, "▸", attr(C_FOCUS, True))
            b.put(b.y, 2, "●" if s.busy else "○", attr(C_OK if s.busy else C_DIM))
            b.put(b.y, 4, s.name[:20].ljust(20), base)
            b.put(b.y, 25, s.state, attr(C_OK if s.busy else C_DIM))
            b.y += 1

    projects = snap["projects"]
    if projects and b.room > 2:
        b.skip()
        b.line("OUTPUT TOKENS (7d)", attr(C_HEAD, True))
        top = max(p.output for p in projects) or 1
        bar_w = max(6, min(20, b.iw - 26))
        for pr in projects:
            if b.room <= 0:
                break
            b.put(b.y, 0, pr.name[:17].ljust(17), attr(C_DIM))
            b.put(b.y, 18, human_count(pr.output).rjust(6), attr(C_ACCENT))
            frac = pr.output / top
            b.put(b.y, 25, "█" * max(1, int(frac * bar_w)), grad(frac * 0.7))
            b.y += 1


def panel_coherence(b: Box, snap: dict, st: State) -> None:
    c: coh.Coherence = snap["coh"]
    slot = C_OK if c.verdict == coh.COHERENT else C_BAD

    b.put(b.y, 0, "▲", attr(slot, True))
    b.put(b.y, 2, c.verdict, attr(slot, True))
    b.y += 1
    for chunk in wrap(c.headline, b.iw)[:3]:
        b.line(chunk, attr(slot if c.verdict == coh.SPLIT else C_DIM))
    b.skip()

    for ly in c.layers:
        if b.room <= 0:
            return
        if not ly.known:
            dot, ds = "?", C_WARN
        elif ly.name == "Throne":
            dot, ds = ("●", C_OK) if ly.on else ("○", C_BAD)
        elif ly.on:
            # Pointing at a proxy that is not there is the whole failure mode.
            dot, ds = "●", (C_OK if c.core_up else C_BAD)
        else:
            dot, ds = "○", C_DIM
        b.put(b.y, 0, dot, attr(ds, True))
        b.put(b.y, 2, ly.name[:10].ljust(10), attr(C_DIM))
        b.put(b.y, 13, ly.detail[: max(0, b.iw - 13)], attr(ds))
        b.y += 1

    if c.exit_ip and b.room > 1:
        b.skip()
        b.put(b.y, 0, "exit", attr(C_DIM))
        b.put(b.y, 13, c.exit_ip, attr(C_ACCENT, True))
        b.y += 1

    if c.stale and b.room > 2:
        b.skip()
        b.line(f"STALE ENVIRONMENT ({len(c.stale)})", attr(C_BAD, True))
        for chunk in wrap("started while the proxy was up, still pointing at it. "
                          "A process's environment cannot be changed from "
                          "outside — restart these.", b.iw)[: max(0, b.room - 1)]:
            b.line(chunk, attr(C_DIM))

        groups: dict[str, list[int]] = {}
        for s in c.stale:
            groups.setdefault(s.comm, []).append(s.pid)
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

        for name, pids in ordered:
            if b.room <= 1:
                b.line(f"+{len(ordered) - (b.y - 1)} more — `taq doctor`", attr(C_DIM))
                break
            b.put(b.y, 0, name[:16], attr(C_BAD))
            if len(pids) > 1:
                b.put(b.y, 18, f"×{len(pids)}", attr(C_WARN, True))
            b.put(b.y, 23, " ".join(map(str, pids[:3])) + (" …" if len(pids) > 3 else ""),
                  attr(C_DIM))
            b.y += 1


def panel_docker(b: Box, snap: dict, st: State) -> None:
    d = snap["docker"]
    if not d.available:
        b.line("docker unavailable", attr(C_WARN))
        for chunk in wrap(d.reason, b.iw):
            b.line(chunk, attr(C_DIM))
        return

    if not d.containers:
        b.line("no containers", attr(C_DIM))
        b.line(f"daemon {d.version} responding", attr(C_DIM))
        return

    focused = st.focus == DOCKER
    sel = min(st.sel[DOCKER], len(d.containers) - 1)

    # Keep the selection on screen.
    start = max(0, sel - b.ih + 2) if sel >= b.ih - 1 else 0
    shown = d.containers[start:start + b.ih]

    wide = b.iw >= 62
    last_project = None
    for i, c in enumerate(shown, start=start):
        if b.room <= 0:
            break
        if c.project != last_project and c.project:
            if b.room <= 1:
                break
            b.put(b.y, 0, f"▼ {c.project}", attr(C_HEAD, True))
            b.y += 1
        last_project = c.project

        on = focused and i == sel
        if on:
            b.put(b.y, 0, "▸", attr(C_FOCUS, True))

        dot, ds = {
            "running": ("●", C_OK), "paused": ("◐", C_WARN),
            "restarting": ("◑", C_WARN), "exited": ("○", C_DIM),
        }.get(c.state, ("○", C_DIM))
        if c.health == "unhealthy":
            dot, ds = "✗", C_BAD
        elif c.health == "starting":
            dot, ds = "◌", C_WARN
        b.put(b.y, 2, dot, attr(ds, True))

        name_w = 20 if wide else max(10, b.iw - 14)
        b.put(b.y, 4, c.name[:name_w].ljust(name_w),
              attr(C_TEXT if on else C_DIM, on))

        col = 5 + name_w
        if wide and c.cpu_pct is not None:
            b.put(b.y, col, f"{c.cpu_pct:5.1f}%", grad(min(1.0, c.cpu_pct / 100)))
            col += 7
            if c.mem_bytes:
                b.put(b.y, col, human_bytes(c.mem_bytes).rjust(6),
                      grad(min(1.0, (c.mem_pct or 0) / 100)))
                col += 7
        elif wide:
            col += 14

        if col < b.iw:
            b.put(b.y, col, (c.status or c.state)[: b.iw - col], attr(ds))
        b.y += 1

    if start + len(shown) < len(d.containers):
        b.line(f"  +{len(d.containers) - start - len(shown)} more ↓", attr(C_DIM))


def panel_system(b: Box, snap: dict, st: State) -> None:
    v = snap["vitals"]
    mon = snap["monitor"]

    # CPU — number, meter, then a braille history plot.
    b.put(b.y, 0, "CPU", attr(C_HEAD, True))
    b.put(b.y, 4, f"{v.cpu_pct:5.1f}%", grad(v.cpu_pct / 100, True))
    if v.temp_c is not None:
        b.put(b.y, 12, f"{v.temp_c:.0f}°C", grad(min(1.0, max(0, v.temp_c - 35) / 55)))
    if v.load:
        b.put(b.y, 19, f"load {v.load[0]:.2f}", attr(C_DIM))
    b.y += 1

    gh = 3 if b.room > 12 else 2
    if b.room > gh:
        draw_graph(b, b.y, 0, b.iw, gh, mon.cpu_history, vmax=100.0)
        b.y += gh

    # Per-core, two rows of compact meters when there is room.
    if v.per_core and b.room > 2 and b.iw >= 30:
        # Index and value sit together at the left of each cell, with the meter
        # filling the rest. Putting the value last instead left each core's
        # number touching the next core's index — "2 1 2 2 0 3" reads as noise
        # even though it is correct. The meter now separates the cells.
        cols = 2 if b.iw < 46 else 4
        cell = b.iw // cols
        for row_start in range(0, min(len(v.per_core), cols * 2), cols):
            if b.room <= 1:
                break
            for j in range(cols):
                idx = row_start + j
                if idx >= len(v.per_core):
                    break
                x, pct = j * cell, v.per_core[idx]
                b.put(b.y, x, f"{idx:>2}", attr(C_DIM))
                b.put(b.y, x + 3, f"{pct:>3.0f}", grad(pct / 100))
                meter(b, b.y, x + 7, max(3, cell - 8), pct / 100.0)
            b.y += 1
        b.skip()

    if b.room > 1:
        b.put(b.y, 0, "MEM", attr(C_HEAD, True))
        meter(b, b.y, 4, max(6, b.iw - 22), v.mem_pct / 100.0,
              f"{human_bytes(v.mem_used)}/{human_bytes(v.mem_total)}")
        b.y += 1
    if v.swap_total and b.room > 1:
        b.put(b.y, 0, "SWP", attr(C_DIM))
        meter(b, b.y, 4, max(6, b.iw - 22), v.swap_pct / 100.0,
              human_bytes(v.swap_used))
        b.y += 1

    if b.room > 2:
        b.skip()
        b.put(b.y, 0, "NET", attr(C_HEAD, True))
        b.put(b.y, 4, f"↓{human_bytes(v.net_down, True)}", attr(C_OK))
        b.put(b.y, 18, f"↑{human_bytes(v.net_up, True)}", attr(C_ACCENT))
        b.y += 1
        if b.room > 1:
            half = max(4, (b.iw - 1) // 2)
            b.put(b.y, 0, spark(mon.net_down_history, half), attr(C_OK))
            b.put(b.y, half + 1, spark(mon.net_up_history, half), attr(C_ACCENT))
            b.y += 1

    if mon.disks and b.room > 1:
        b.skip()
        for dk in mon.disks:
            if b.room <= 0:
                break
            # Elide from the left: the tail of a mount path identifies it,
            # the head does not ("/boot/ef" told you nothing).
            name = dk.mount if len(dk.mount) <= 10 else "…" + dk.mount[-9:]
            b.put(b.y, 0, name.ljust(10), attr(C_DIM))
            meter(b, b.y, 11, max(5, b.iw - 28), dk.pct / 100.0,
                  f"{human_bytes(dk.total - dk.used)} free")
            b.y += 1

    if b.room > 1:
        b.skip()
        bits = [f"up {short_dur(v.uptime)}", f"{v.procs} procs"]
        if v.battery_pct is not None:
            bits.append(f"{'⚡' if v.battery_charging else '🔋'}{v.battery_pct:.0f}%")
        b.line("  ".join(bits), attr(C_DIM))


PANELS = {
    CLAUDE: ("Claude", panel_claude, "j/k"),
    DOCKER: ("Docker", panel_docker, "j/k  L logs  S/s/R"),
    SYSTEM: ("System", panel_system, ""),
    COHERENCE: ("Coherence", panel_coherence, "i exit-IP"),
}


# -----------------------------------------------------------------------------
# overlays
# -----------------------------------------------------------------------------

def draw_logs(win, h: int, w: int, st: State) -> None:
    box = Box(win, 1, 2, h - 3, w - 4, f"logs · {st.log_title}", True,
              "↑↓ PgUp/PgDn  q close")
    box.clear()
    box.frame()
    lines = st.log_lines or ["(empty)"]
    st.log_scroll = max(0, min(st.log_scroll, max(0, len(lines) - box.ih)))
    for i in range(box.ih):
        idx = st.log_scroll + i
        if idx >= len(lines):
            break
        text = lines[idx].replace("\t", "    ")
        a = attr(C_BAD) if any(k in text.lower() for k in
                               ("error", "fatal", "panic", "exception")) \
            else attr(C_WARN) if "warn" in text.lower() else attr(C_TEXT)
        box.put(i, 0, text[:box.iw], a)
    pos = f"{st.log_scroll + 1}-{min(len(lines), st.log_scroll + box.ih)}/{len(lines)}"
    box.put(box.ih - 1, max(0, box.iw - len(pos)), pos, attr(C_FOCUS))


HELP = [
    ("Tab / 1-4", "move focus between panels"),
    ("j k ↑ ↓", "move selection inside the focused panel"),
    ("g G", "jump to top / bottom"),
    ("", ""),
    ("L / Enter", "container logs (Docker panel)"),
    ("S", "start container"),
    ("s", "stop container"),
    ("R", "restart container"),
    ("p", "pause / unpause container"),
    ("", ""),
    ("r", "force a full refresh now"),
    ("i", "re-check the exit IP"),
    ("?", "this help"),
    ("q / Esc", "close overlay, or quit"),
]


def draw_help(win, h: int, w: int) -> None:
    bh, bw = min(h - 2, len(HELP) + 4), min(w - 4, 64)
    box = Box(win, (h - bh) // 2, (w - bw) // 2, bh, bw, "keys", True, "any key closes")
    box.clear()
    box.frame()
    for key, desc in HELP:
        if box.room <= 0:
            break
        if key:
            box.put(box.y, 1, key.ljust(12), attr(C_FOCUS, True))
            box.put(box.y, 14, desc, attr(C_TEXT))
        box.y += 1


# -----------------------------------------------------------------------------
# frame
# -----------------------------------------------------------------------------

def draw(win, snap: dict, st: State) -> None:
    win.erase()
    h, w = win.getmaxyx()
    if h < 10 or w < 40:
        try:
            win.addstr(0, 0, "terminal too small"[: w - 1])
        except curses.error:
            pass
        win.noutrefresh()
        return

    _header(win, w, snap, st)

    if st.overlay == "logs":
        draw_logs(win, h, w, st)
    elif st.overlay == "help":
        _grid(win, h, w, snap, st)
        draw_help(win, h, w)
    else:
        _grid(win, h, w, snap, st)

    _footer(win, h, w, snap, st)
    win.noutrefresh()


def _header(win, w: int, snap: dict, st: State) -> None:
    c: coh.Coherence = snap["coh"]
    slot = C_OK if c.verdict == coh.COHERENT else C_BAD
    _safe(win, 0, 1, "taq", attr(C_HEAD, True))

    x = 6
    for i, name in enumerate(PANEL_NAMES):
        on = i == st.focus
        tab = f" {i + 1} {name} "
        _safe(win, 0, x, tab,
              attr(C_FOCUS, True) | curses.A_REVERSE if on else attr(C_DIM))
        x += len(tab) + 1

    d = snap["docker"]
    right = f"◆ {d.running}/{len(d.containers)}   ▲ {c.verdict}   {clock(time.time())} "
    _safe(win, 0, max(x + 2, w - len(right)), right, attr(slot, True))


def _grid(win, h: int, w: int, snap: dict, st: State) -> None:
    top, body = 1, h - 2

    if w < WIDE_AT:
        # One panel at a time — cramming four boxes into 70 columns produces
        # four unreadable boxes.
        title, fn, hint = PANELS[st.focus]
        box = Box(win, top, 0, body, w, title, True, hint or "Tab switch")
        box.frame()
        fn(box, snap, st)
        return

    left_w = w * 58 // 100
    top_h = body * 52 // 100

    for panel, (t, l, hh, ww) in {
        CLAUDE:    (top, 0, top_h, left_w),
        DOCKER:    (top + top_h, 0, body - top_h, left_w),
        SYSTEM:    (top, left_w, top_h, w - left_w),
        COHERENCE: (top + top_h, left_w, body - top_h, w - left_w),
    }.items():
        title, fn, hint = PANELS[panel]
        box = Box(win, t, l, hh, ww, title, st.focus == panel, hint)
        box.frame()
        fn(box, snap, st)


def _footer(win, h: int, w: int, snap: dict, st: State) -> None:
    y = h - 1
    if st.confirm:
        prompt = st.confirm[0]
        _safe(win, y, 1, f" {prompt} [y/N] ", attr(C_BAD, True) | curses.A_REVERSE)
        return

    if msg := st.flashing:
        _safe(win, y, 1, f" {msg} ", attr(C_OK, True))
    else:
        _safe(win, y, 1, "? keys   Tab panel   r refresh   q quit", attr(C_DIM))

    right = f"{snap['rss_mb']:.0f}MB · {snap['tick']}s "
    _safe(win, y, max(0, w - len(right)), right, attr(C_DIM))


def _safe(win, y: int, x: int, text: str, a: int) -> None:
    hh, ww = win.getmaxyx()
    if y >= hh or x >= ww or not text:
        return
    try:
        win.addstr(y, x, text[: ww - x], a)
    except curses.error:
        pass


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def short_model(name: str) -> str:
    return name.replace("claude-", "").replace("-20", " ")[:16]

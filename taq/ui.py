"""Layout, focus, and the four panels.

Modelled on lazygit/lazydocker: a grid of bordered boxes, exactly one focused,
navigation inside the focused box only. Narrow terminals drop to one full-screen
panel at a time rather than trying to cram four boxes into 70 columns.
"""

from __future__ import annotations

import curses
import time

from . import coherence as coh
from . import quota
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
    if plan := snap.get("plan"):
        b.put(b.y, 0, "Plan usage limits", attr(C_HEAD, True))
        b.put(b.y, 18, plan, attr(C_ACCENT, True))
        b.y += 1
        b.skip()
    if not windows:
        # Two very different situations that look identical from here: never
        # set up, versus set up but nothing has fed it. Telling someone to run
        # a command they have already run is the worst possible answer.
        hooked, hooked_at = snap.get("hook", (False, 0.0))
        b.line("no rate-limit data yet", attr(C_WARN))
        b.skip()

        if not hooked:
            msg = ("Rate limits reach a local tool only through the statusline "
                   "hook. Run `taq install` to add it.")
        else:
            stale = sum(1 for s in snap["sessions"] if s.started_at < hooked_at)
            msg = ("The hook is installed. Claude Code reads settings.json at "
                   "session start, so start a NEW session — this fills in "
                   "within seconds of one opening.")
            if stale:
                msg += (f" All {stale} running session"
                        f"{'s' if stale != 1 else ''} predate the hook.")
        for chunk in wrap(msg, b.iw):
            b.line(chunk, attr(C_DIM))
        b.skip()
    else:
        bar_w = max(8, min(30, b.iw - 12))
        for key in ("five_hour", "seven_day"):
            w = windows.get(key)
            if not w or b.room < 3:
                continue
            # Same words the web usage page uses, so the two can be compared
            # without translating "5h" into "current session" in your head.
            b.put(b.y, 0, quota.WINDOW_LABELS[key], attr(C_TEXT, True))
            b.put(b.y, b.iw - 9, f"{w.used:.0f}% used", grad(w.used / 100, True))
            b.y += 1
            meter(b, b.y, 0, bar_w, w.used / 100.0)
            b.y += 1

            b.put(b.y, 0, f"resets {reset_label(w.resets_at)} · in {short_dur(w.resets_in)}",
                  attr(C_DIM))
            b.y += 1

            # A reading is only as current as the last session that reported.
            # Showing a frozen number with no age is how you end up trusting
            # 44% while the web page says 49%.
            feeders = snap.get("feeders", 0)
            if not feeders:
                # Nothing can update this, so say so every time, not just once
                # it has gone visibly old.
                b.put(b.y, 0, f"⚠ frozen · measured {short_dur(w.stale_for)} ago",
                      attr(C_BAD, True))
            elif w.stale_for > 180:
                b.put(b.y, 0, f"⚠ {short_dur(w.stale_for)} old — at least this much",
                      attr(C_WARN))
            elif w.eta is not None and w.will_exhaust:
                b.put(b.y, 0, f"⚠ hits the cap ~{clock(w.eta)}, "
                              f"{short_dur(w.resets_at - w.eta)} early", attr(C_BAD, True))
            elif w.rate is not None and w.rate > 0.05:
                b.put(b.y, 0, f"+{w.rate:.1f}%/h — clears the window", attr(C_OK))
            elif w.rate is not None:
                b.put(b.y, 0, "flat", attr(C_DIM))
            elif w.min_span and w.span < w.min_span:
                # Say what it is waiting for, in time rather than in samples —
                # "3.4h of data" explains the silence better than "1 sample".
                b.put(b.y, 0, f"trend needs {short_dur(w.min_span)} of data "
                              f"({short_dur(w.span)} so far)", attr(C_DIM))
            else:
                b.put(b.y, 0, "measuring burn rate…", attr(C_DIM))
            b.y += 2

        # Why the numbers are not moving, stated once rather than per window,
        # and in terms of things you can see: a clock time and your own open
        # sessions. "The hook" is taq's word for it, not yours.
        if b.room > 0 and not snap.get("feeders", 0):
            n = len(snap["sessions"])
            since = snap.get("hook", (False, 0.0))[1]
            when = f" at {reset_label(since)}" if since else ""
            for chunk in wrap(f"Claude Code only sends these numbers from "
                              f"sessions opened after taq was set up{when}. "
                              f"Your {n} open session{'s' if n != 1 else ''} "
                              f"predate{'' if n != 1 else 's'} that, so nothing "
                              f"is refreshing them. Open any new Claude Code "
                              f"session and this goes live within seconds.",
                              b.iw):
                b.line(chunk, attr(C_WARN))
            b.skip()

        # The web page also shows a per-model weekly bar (Fable). That number is
        # not in the statusline payload, and saying so beats quietly omitting it.
        if b.room > 0:
            b.line("per-model limits are not exposed to local tools", attr(C_DIM))
            b.skip()

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

    # Split the box: list on top, details for the selected row underneath.
    # Reserve room for details only when there is enough height to be useful.
    detail_h = 0
    if b.ih >= 14:
        detail_h = min(14, max(8, b.ih - len(d.containers) - 3))
    list_h = b.ih - detail_h

    start = max(0, sel - list_h + 2) if sel >= list_h else 0
    shown = d.containers[start:start + list_h]

    wide = b.iw >= 62
    last_project = None
    for i, c in enumerate(shown, start=start):
        if b.y >= list_h:
            break
        if c.project != last_project and c.project:
            if b.y >= list_h - 1:
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

    if start + len(shown) < len(d.containers) and b.y < list_h:
        b.line(f"  +{len(d.containers) - start - len(shown)} more ↓", attr(C_DIM))

    if detail_h > 0:
        b.y = list_h
        _docker_detail(b, d.containers[sel], snap.get("detail"), b.ih)


def _docker_detail(b: Box, c, det, bottom: int) -> None:
    """Everything known about the selected container. Nearly all of it comes
    from the list response we already have; only restart count, start time and
    policy need the inspect call, and only for this one row."""
    b.put(b.y, 0, "─" * b.iw, attr(C_DIM))
    b.y += 1

    b.put(b.y, 0, c.name[:28], attr(C_ACCENT, True))
    if c.service:
        b.put(b.y, 30, f"service {c.service}", attr(C_DIM))
    b.put(b.y, b.iw - 13, c.short, attr(C_DIM))
    b.y += 1

    def row(label: str, value: str, a: int = 0) -> None:
        if b.y >= bottom or not value:
            return
        b.put(b.y, 0, label[:10].ljust(10), attr(C_DIM))
        b.put(b.y, 11, value[: max(0, b.iw - 11)], a or attr(C_TEXT))
        b.y += 1

    row("image", c.image)
    row("state", f"{c.state} · {c.status}",
        attr(C_OK if c.up else C_DIM))

    if c.health:
        hs = {"healthy": C_OK, "unhealthy": C_BAD}.get(c.health, C_WARN)
        extra = f" · failing streak {det.health_failing}" if det and det.health_failing else ""
        row("health", c.health + extra, attr(hs, True))

    if det:
        bits = []
        if det.started_at:
            bits.append(f"started {det.started_at}")
        if det.restart_count:
            bits.append(f"{det.restart_count} restarts")
        if det.restart_policy and det.restart_policy != "no":
            bits.append(det.restart_policy)
        row("run", " · ".join(bits))
        if not c.up and det.exit_code is not None:
            row("exit", f"code {det.exit_code}"
                       + (" · OOM KILLED" if det.oom_killed else "")
                       + (f" · {det.finished_at}" if det.finished_at else ""),
                attr(C_BAD if det.exit_code else C_DIM, bool(det.exit_code)))

    if c.up:
        row("cpu / mem", f"{c.cpu_pct:.1f}%   {human_bytes(c.mem_bytes or 0)}"
                        + (f" / {human_bytes(c.mem_limit)}" if c.mem_limit else "")
                        + (f"  ({c.mem_pct:.0f}%)" if c.mem_pct else "")
            if c.cpu_pct is not None else "measuring…")
        row("net", f"↓{human_bytes(c.net_rx)}  ↑{human_bytes(c.net_tx)}")
        row("disk", f"read {human_bytes(c.blk_read)}  write {human_bytes(c.blk_write)}")
        row("pids", str(c.pids) if c.pids else "")

    row("ports", ", ".join(c.port_list) or "none published")
    for i, (net, ip) in enumerate(c.networks.items()):
        row("network" if i == 0 else "", f"{net}  {ip}")
    for i, m in enumerate(c.mounts[:3]):
        row("mounts" if i == 0 else "", m)
    if len(c.mounts) > 3:
        row("", f"+{len(c.mounts) - 3} more")
    row("command", c.command)
    if det and det.health_log:
        row("health log", det.health_log[-1], attr(C_WARN))


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

    # The panel has more to say than it has rows. The graph and the per-core
    # grid are the two things that scale down gracefully, so they give up
    # height first — losing a plot row costs resolution, losing the battery
    # row costs the number you actually opened this for.
    tight = b.ih < 24
    gh = 2 if tight else 3
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
        core_rows = 1 if tight else 2
        for row_start in range(0, min(len(v.per_core), cols * core_rows), cols):
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

    # Power before disks. When the panel runs out of rows something has to go,
    # and on a laptop "4.3h left" earns its place ahead of a disk that has been
    # 70% full for a month.
    _power_block(b, v)

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
        if v.power.cycles:
            bits.append(f"{v.power.cycles} cycles")
        b.line("  ".join(bits), attr(C_DIM))


def _power_block(b: Box, v) -> None:
    p = v.power
    if p.present and p.pct is not None and b.room > 2:
        b.skip()
        icon = "⚡" if p.charging else "🔋"
        b.put(b.y, 0, "PWR", attr(C_HEAD, True))
        b.put(b.y, 4, f"{icon}{p.pct:.0f}%", grad(1.0 - p.pct / 100, True))
        if p.watts:
            b.put(b.y, 13, f"{p.watts:.1f}W", attr(C_ACCENT))
        if p.seconds_left:
            word = "to full" if p.charging else "left"
            b.put(b.y, 21, f"{short_dur(p.seconds_left)} {word}",
                  attr(C_OK if p.charging or p.seconds_left > 3600 else C_WARN, True))
        b.y += 1

        if b.room > 0:
            meter(b, b.y, 0, max(6, b.iw - 18), p.pct / 100.0,
                  f"health {p.health_pct:.0f}%" if p.health_pct else "")
            b.y += 1

        # Brightness, and what it is actually costing — measured at this
        # machine's own idle draw, not guessed from a spec sheet.
        if p.brightness_pct is not None and b.room > 0:
            b.put(b.y, 0, "LCD", attr(C_HEAD, True))
            b.put(b.y, 4, f"{p.brightness_pct:.0f}%", attr(C_TEXT, True))
            bw = max(5, b.iw - 30)
            meter(b, b.y, 9, bw, p.brightness_pct / 100.0)
            b.y += 1

            if b.room > 0:
                delta = p.delta_seconds
                if delta is not None and p.predicted_at_pct is not None:
                    sign = "+" if delta >= 0 else "−"
                    b.put(b.y, 4, f"at {p.predicted_at_pct:.0f}%: "
                                  f"{p.predicted_watts:.1f}W  {sign}{short_dur(abs(delta))}",
                          attr(C_OK if delta >= 0 else C_WARN))
                elif p.charging:
                    b.put(b.y, 4, "on AC — measuring pauses", attr(C_DIM))
                else:
                    b.put(b.y, 4, "learning draw at this level…", attr(C_DIM))
                b.y += 1


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


def reset_label(ts: float) -> str:
    """A reset later today needs only a time; one days away needs the day, the
    way the web page writes "Fri 6:30 PM"."""
    now = time.localtime()
    then = time.localtime(ts)
    if (then.tm_year, then.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime("%H:%M", then)
    if ts - time.time() < 6 * 86400:
        return time.strftime("%a %H:%M", then)
    return time.strftime("%d %b %H:%M", then)


def short_model(name: str) -> str:
    return name.replace("claude-", "").replace("-20", " ")[:16]

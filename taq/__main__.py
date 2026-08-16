"""taq — entry point.

  taq              the TUI
  taq line         one-line verdict, for tmux / a shell prompt / waybar
  taq doctor       print everything taq can see, then exit (no curses)
  taq statusline   read a Claude Code statusline payload on stdin
  taq install      register the statusline hook in ~/.claude/settings.json
"""

from __future__ import annotations

import curses
import json
import os
import sys
import time

from . import coherence as coh
from . import dockerd, paths, quota, system, ui, usage, widgets

# The app is idle almost all the time, so it should cost almost nothing almost
# all the time. These are the ceilings; the loop picks between them.
TICK_ACTIVE = 1.0
TICK_IDLE = 3.0
TICK_QUIET = 15.0
DEEP_EVERY = 10.0     # /proc environ scan
DOCKER_EVERY = 3.0    # container list (stats have their own slower cadence)
# Sources whose data moves slower than the frame rate. Re-reading these every
# tick was most of the app's CPU: the quota projection re-parses its whole
# history file, and the transcript refresh stats every file in the tree.
QUOTA_EVERY = 5.0
SESSIONS_EVERY = 2.0
TRANSCRIPTS_EVERY = 2.0


def rss_mb() -> float:
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, IndexError):
        return 0.0


class App:
    def __init__(self) -> None:
        self.index = usage.TranscriptIndex()
        self.monitor = system.Monitor()
        self.docker = dockerd.Client()
        self.st = ui.State()
        self.coh: coh.Coherence | None = None
        self.dview = dockerd.DockerView(False, "starting")
        self.last_deep = 0.0
        self.last_docker = 0.0
        self.last_change = time.time()
        self.tick = TICK_ACTIVE
        # Cached slow-moving sources, with the time each was last read.
        self._at: dict[str, float] = {}
        self._windows: dict = {}
        self._sessions: list = []
        self._projects: list = []
        self._split: list = []
        self._plan: str = ""
        self._hook: tuple = (False, 0.0)
        self._feeders: int = 0
        self._detail = None

    def _due(self, key: str, every: float, force: bool, now: float) -> bool:
        if force or now - self._at.get(key, 0.0) >= every:
            self._at[key] = now
            return True
        return False

    # -- data ---------------------------------------------------------------
    def poll(self, force: bool = False) -> dict:
        now = time.time()

        deep = force or (now - self.last_deep >= DEEP_EVERY)
        if deep:
            self.last_deep = now
        c = coh.snapshot(deep=deep)
        if not deep and self.coh is not None:
            c.stale = self.coh.stale     # keep the panel from blinking empty
        if self.coh is None or c.verdict != self.coh.verdict:
            self.last_change = now
        self.coh = c

        if force or now - self.last_docker >= DOCKER_EVERY:
            self.last_docker = now
            prev = {x.cid: x.state for x in self.dview.containers}
            self.dview = self.docker.view()
            if {x.cid: x.state for x in self.dview.containers} != prev:
                self.last_change = now

        if self._due("transcripts", TRANSCRIPTS_EVERY, force, now):
            if self.index.refresh():
                self.last_change = now
            self.index.prune()
            self._projects = self.index.projects()
            self._split = self.index.model_split()

        if self._due("quota", QUOTA_EVERY, force, now):
            self._windows = quota.read_windows()
            self._plan = quota.read_plan()
            self._hook = quota.hook_status()

        # Inspect only the container the cursor is on.
        sel = self.selected_container()
        self._detail = self.docker.detail(sel.cid) if sel else None

        if self._due("sessions", SESSIONS_EVERY, force, now):
            self._sessions = usage.live_sessions()
            # Sessions capable of feeding the hook: ones that started after it
            # was installed. Zero of them means the quota numbers cannot move.
            self._feeders = sum(1 for s in self._sessions
                                if s.started_at >= self._hook[1] > 0)

        vitals = self.monitor.sample()
        if any(s.busy for s in self._sessions) or vitals.cpu_pct > 25:
            self.last_change = now

        quiet = now - self.last_change
        self.tick = (TICK_ACTIVE if quiet < 30
                     else TICK_IDLE if quiet < 300 else TICK_QUIET)

        return {
            "coh": c,
            "docker": self.dview,
            "vitals": vitals,
            "monitor": self.monitor,
            "windows": self._windows,
            "plan": self._plan,
            "hook": self._hook,
            "feeders": self._feeders,
            "detail": self._detail,
            "sessions": self._sessions,
            "projects": self._projects,
            "model_split": self._split,
            "rss_mb": rss_mb(),
            "tick": int(self.tick),
        }

    def _empty_snap(self) -> dict:
        """Enough of a snapshot to draw the chrome before any source is read."""
        return {
            "coh": coh.Coherence(coh.UNKNOWN, "reading…", False, 0),
            "docker": self.dview,
            "vitals": system.Vitals(),
            "monitor": self.monitor,
            "windows": {}, "sessions": [], "projects": [], "model_split": [],
            "rss_mb": rss_mb(), "tick": 1,
        }

    def selected_container(self):
        cs = self.dview.containers
        if not cs:
            return None
        return cs[min(self.st.sel[ui.DOCKER], len(cs) - 1)]

    # -- input --------------------------------------------------------------
    def handle(self, ch: int, snap: dict) -> str:
        """Returns "quit", "refresh", or "" (redraw only)."""
        st = self.st

        # A pending confirmation swallows everything else.
        if st.confirm:
            prompt, cid, verb = st.confirm
            if ch in (ord("y"), ord("Y")):
                st.confirm = None
                st.say(f"{verb}… ")
                result = self.docker.action(cid, verb)
                st.say(result)
                return "refresh"
            st.confirm = None
            st.say("cancelled")
            return ""

        if st.overlay == "logs":
            page = 20
            if ch in (curses.KEY_DOWN, ord("j")):
                st.log_scroll += 1
            elif ch in (curses.KEY_UP, ord("k")):
                st.log_scroll = max(0, st.log_scroll - 1)
            elif ch == curses.KEY_NPAGE:
                st.log_scroll += page
            elif ch == curses.KEY_PPAGE:
                st.log_scroll = max(0, st.log_scroll - page)
            elif ch == ord("g"):
                st.log_scroll = 0
            elif ch == ord("G"):
                st.log_scroll = max(0, len(st.log_lines) - 5)
            elif ch in (ord("q"), 27, ord("L")):
                st.overlay = None
            return ""

        if st.overlay == "help":
            st.overlay = None
            return ""

        if ch in (ord("q"), 27):
            return "quit"
        if ch == ord("?"):
            st.overlay = "help"
            return ""
        if ch == ord("r"):
            st.say("refreshing")
            return "refresh"
        if ch == ord("i"):
            coh.exit_ip(self.coh.proxy_port, force=True)
            st.say("re-checked exit IP")
            return "refresh"

        if ch == ord("\t"):
            st.focus = (st.focus + 1) % 4
            return ""
        if ch == curses.KEY_BTAB:
            st.focus = (st.focus - 1) % 4
            return ""
        if ord("1") <= ch <= ord("4"):
            st.focus = ch - ord("1")
            return ""

        count = self._count(st.focus, snap)
        if ch in (ord("j"), curses.KEY_DOWN):
            st.move(st.focus, 1, count)
            return ""
        if ch in (ord("k"), curses.KEY_UP):
            st.move(st.focus, -1, count)
            return ""
        if ch == ord("g"):
            st.sel[st.focus] = 0
            return ""
        if ch == ord("G"):
            st.sel[st.focus] = max(0, count - 1)
            return ""

        # Docker actions, only from the Docker panel.
        if st.focus == ui.DOCKER:
            c = self.selected_container()
            if c is None:
                return ""
            if ch in (ord("L"), ord("\n"), curses.KEY_ENTER):
                st.log_lines = self.docker.logs(c.cid)
                st.log_title = c.name
                st.log_scroll = max(0, len(st.log_lines) - 40)
                st.overlay = "logs"
                return ""
            verb = {ord("S"): "start", ord("s"): "stop",
                    ord("R"): "restart"}.get(ch)
            if ch == ord("p"):
                verb = "unpause" if c.state == "paused" else "pause"
            if verb:
                st.confirm = (f"{verb} {c.name}?", c.cid, verb)
            return ""
        return ""

    def _count(self, panel: int, snap: dict) -> int:
        if panel == ui.CLAUDE:
            return len(snap["sessions"])
        if panel == ui.DOCKER:
            return len(snap["docker"].containers)
        return 0

    # -- loop ---------------------------------------------------------------
    def run(self, win) -> None:
        curses.curs_set(0)
        widgets.init_colors()
        win.nodelay(True)

        # Paint the frame before the first poll. The cold transcript scan takes
        # about a second, and an app that shows nothing for a second reads as
        # broken even when the second is well spent.
        self.dview = dockerd.DockerView(False, "connecting…")
        ui.draw(win, self._empty_snap(), self.st)
        curses.doupdate()

        snap = self.poll(force=True)

        while True:
            ui.draw(win, snap, self.st)
            curses.doupdate()

            deadline = time.time() + self.tick
            force = False
            redraw = False
            while True:
                left = deadline - time.time()
                if left <= 0 or redraw:
                    break
                win.timeout(int(min(left, 0.2) * 1000))
                ch = win.getch()
                if ch == -1:
                    continue
                if ch == curses.KEY_RESIZE:
                    redraw = True
                    break
                action = self.handle(ch, snap)
                if action == "quit":
                    return
                if action == "refresh":
                    force = True
                    break
                redraw = True     # repaint immediately, then resume the tick

            if redraw and not force:
                continue
            snap = self.poll(force=force)


# -----------------------------------------------------------------------------
# subcommands
# -----------------------------------------------------------------------------

def cmd_statusline() -> int:
    """Called by Claude Code with the session payload on stdin. Two jobs:
    persist the rate limits (nothing else can see them), and print a status
    line worth having. Never raises — a traceback here lands in the UI."""
    # Claude Code pipes the payload in. Run by hand on a terminal there is no
    # input, and read() would just sit there looking hung until Ctrl-C.
    if sys.stdin.isatty():
        print("taq statusline expects a Claude Code payload on stdin.")
        print("It is invoked by the statusLine hook, not run directly.")
        print("\nTo see what it would print:")
        print("  echo '{\"model\":{\"display_name\":\"Opus 5\"}}' | taq statusline")
        print("\nTo check whether the hook is working:  taq doctor")
        return 2

    try:
        payload = json.loads(sys.stdin.read())
    except (ValueError, KeyboardInterrupt, EOFError):
        print("taq")
        return 0

    quota.record(payload)

    try:
        model = (payload.get("model") or {}).get("display_name") or "?"
        cwd = os.path.basename((payload.get("workspace") or {}).get("current_dir") or "") or "~"
        ctx = (payload.get("context_window") or {}).get("used_percentage")
        five = ((payload.get("rate_limits") or {}).get("five_hour") or {}).get("used_percentage")

        bits = [f"\x1b[35m{model}\x1b[0m", f"\x1b[36m{cwd}\x1b[0m"]
        if ctx is not None:
            bits.append(f"ctx {float(ctx):.0f}%")
        if five is not None:
            col = "31" if five >= 90 else "33" if five >= 70 else "32"
            bits.append(f"\x1b[{col}m5h {float(five):.0f}%\x1b[0m")

        c = coh.snapshot(deep=False)
        bits.append("\x1b[32m▲\x1b[0m" if c.verdict == coh.COHERENT else "\x1b[31m▲\x1b[0m")
        print("  ".join(bits))
    except Exception:
        print("taq")
    return 0


def cmd_line() -> int:
    c = coh.snapshot(deep=True)
    w = quota.read_windows().get("five_hour")
    out = ["▲" if c.verdict == coh.COHERENT else "▼"]
    if w:
        out.append(f"{w.used:.0f}%")
        if w.will_exhaust:
            out.append(f"!{ui.clock(w.eta)}")
    if c.verdict == coh.SPLIT:
        out.append(f"SPLIT({len(c.stale)})")
    d = dockerd.Client().view(with_stats=False)
    if d.available and d.containers:
        out.append(f"◆{d.running}/{len(d.containers)}")
    print(" ".join(out))
    return 0 if c.verdict == coh.COHERENT else 1


def cmd_doctor() -> int:
    """Everything taq can see, in plain text. The thing to paste into a bug."""
    c = coh.snapshot(deep=True)
    print(f"verdict   {c.verdict}")
    print(f"          {c.headline}")
    print(f"port      {c.proxy_port}  (from {c.port_source})")
    print("\nlayers")
    for ly in c.layers:
        print(f"  {'?' if not ly.known else ('on ' if ly.on else 'off'):4} "
              f"{ly.name:<11} {ly.detail}")
    if c.exit_ip:
        print(f"\nexit ip   {c.exit_ip}")

    print(f"\nstale environment ({len(c.stale)})")
    for s in c.stale:
        print(f"  {s.comm:<20} pid {s.pid:<8} -> {s.target}")
    if not c.stale:
        print("  none")

    windows = quota.read_windows()
    plan = quota.read_plan()
    print(f"\nrate limits ({len(windows)} window(s) reporting)"
          + (f"   plan: {plan}" if plan else ""))
    if not windows:
        # Distinguish "never set up" from "set up, but nothing has fed it yet".
        # They look identical here and have completely different fixes.
        hooked = False
        try:
            sl = json.loads(paths.CLAUDE_SETTINGS.read_text()).get("statusLine") or {}
            hooked = "statusline" in str(sl.get("command", "")).lower()
        except (OSError, ValueError):
            pass
        if hooked:
            print("  none yet — the hook is installed but no session has reported.")
            print("  settings.json is read at session start, so sessions already")
            print("  running will never feed it. Start a new one.")
        else:
            print("  none — statusline hook not installed")
            print("  fix: taq install")
    for name, w in windows.items():
        eta = f", cap ~{ui.clock(w.eta)}" if w.eta else ""
        rate = f", {w.rate:+.2f}%/h" if w.rate is not None else ", rate unknown"
        print(f"  {quota.WINDOW_LABELS.get(name, name):<21} {w.used:5.1f}%  "
              f"resets {ui.reset_label(w.resets_at)}{rate}{eta}  [{w.samples} samples]")
    if windows:
        print("  per-model limits (e.g. Fable) are not in the statusline payload")

    d = dockerd.Client().view()
    print(f"\ndocker    {'ok ' + d.version if d.available else 'unavailable — ' + d.reason}")
    for x in d.containers:
        stats = f"{x.cpu_pct:5.1f}% {widgets.human_bytes(x.mem_bytes or 0):>7}" \
            if x.cpu_pct is not None else " " * 13
        print(f"  {'●' if x.up else '○'} {x.name:<26} {stats}  {x.status}")
    if d.available and not d.containers:
        print("  no containers")

    v = system.Monitor().sample()
    print(f"\nsystem    cpu {v.cpu_pct:.0f}%  mem {v.mem_pct:.0f}% "
          f"({widgets.human_bytes(v.mem_used)}/{widgets.human_bytes(v.mem_total)})"
          f"  up {widgets.short_dur(v.uptime)}")

    sw = v.swap
    if sw.has_zram:
        print(f"zram      {widgets.human_bytes(sw.zram_stored)} stored in "
              f"{widgets.human_bytes(sw.zram_ram)} of RAM"
              + (f" ({sw.ratio:.2f}x {sw.algorithm})" if sw.ratio else "")
              + f" — {widgets.human_bytes(sw.saved)} of RAM you would not have")
    if sw.disk_total:
        print(f"disk swap {widgets.human_bytes(sw.disk_used)} / "
              f"{widgets.human_bytes(sw.disk_total)}   (this is the slow one)")
    if v.mem_pressure is not None:
        verdict = ("not short on memory" if v.mem_pressure < 1
                   else "under memory pressure" if v.mem_pressure < 10
                   else "THRASHING")
        print(f"pressure  {v.mem_pressure:.2f}% of the last 60s stalled on "
              f"memory — {verdict}")

    p = v.power
    if p.present:
        left = (f"  {widgets.short_dur(p.seconds_left)} "
                f"{'to full' if p.charging else 'left'}") if p.seconds_left else ""
        print(f"power     {p.pct:.0f}%  {p.status}"
              + (f"  {p.watts:.2f}W" if p.watts else "") + left)
        print(f"          health {p.health_pct:.1f}% "
              f"({p.full_wh:.1f}/{p.design_wh:.1f}Wh)  {p.cycles or '?'} cycles"
              if p.health_pct else "")
    if p.brightness_pct is not None:
        line = f"backlight {p.brightness_pct:.0f}%"
        if p.predicted_watts and p.delta_seconds is not None:
            line += (f"  — measured: {p.predicted_at_pct:.0f}% draws "
                     f"{p.predicted_watts:.2f}W "
                     f"({widgets.short_dur(abs(p.delta_seconds))} "
                     f"{'more' if p.delta_seconds >= 0 else 'less'} runtime)")
        else:
            line += "  — no comparison level measured yet"
        print(line)

    idx = usage.TranscriptIndex()
    idx.refresh()
    print(f"\ntranscripts   scanned in {idx.cold_seconds:.2f}s")
    for p in idx.projects()[:8]:
        print(f"  {p.name:<20} {widgets.human_count(p.output):>7} out   {p.messages} msgs")

    sess = usage.live_sessions()
    print(f"\nsessions ({len(sess)})")
    for s in sess:
        print(f"  {s.name:<20} pid {s.pid:<8} {s.state:<14} {s.cwd}")

    print(f"\nrss  {rss_mb():.1f} MB")
    return 0


def _hook_command() -> str:
    """The command line Claude Code should run for the status line.

    sys.argv[0] is useless here: bin/taq launches us via `python3 -c`, so argv[0]
    is the literal string "-c". Resolve something that will actually execute.
    """
    import shutil

    exe = shutil.which("taq")
    if exe and not exe.endswith(".py"):
        return f"{exe} statusline"

    # Not on PATH. Run the package directly, pointing the interpreter at
    # whichever directory this copy of taq lives in.
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        import taq  # noqa: F401  — importable without help?
        if pkg_parent in sys.path or "site-packages" in taq.__file__:
            return f"{sys.executable} -m taq statusline"
    except ImportError:
        pass
    return f"env PYTHONPATH={pkg_parent} {sys.executable} -m taq statusline"


def cmd_install() -> int:
    cmd = _hook_command()

    settings = {}
    if paths.CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(paths.CLAUDE_SETTINGS.read_text())
        except ValueError:
            print(f"! {paths.CLAUDE_SETTINGS} is not valid JSON — not touching it")
            return 1
        # Never clobber an existing backup. Running install twice would
        # otherwise overwrite the pristine copy with an already-modified one,
        # so "restore the backup" would restore the problem.
        backup = paths.CLAUDE_SETTINGS.with_suffix(".json.taq-backup")
        if backup.exists():
            print(f"  keeping earlier backup at {backup}")
        else:
            backup.write_text(json.dumps(settings, indent=2))
            print(f"  backed up existing settings to {backup}")

    existing = settings.get("statusLine")
    if existing:
        # Replacing our own entry is a repair, not an overwrite — an earlier
        # version of this command could write a broken path with no "taq" in
        # it at all, so identify ours by the subcommand it invokes.
        prev = str(existing.get("command", "")) if isinstance(existing, dict) else ""
        mine = prev.split()[-1:] == ["statusline"] or "taq" in prev
        if not mine:
            print("! settings.json already defines a statusLine:")
            print(f"    {json.dumps(existing)}")
            print("  taq will not overwrite it. Add this to your own script instead:")
            print(f"    {cmd} >/dev/null")
            return 1
        print(f"  replacing existing taq statusLine: {existing.get('command')}")

    settings["statusLine"] = {"type": "command", "command": cmd, "refreshInterval": 10}
    paths.CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    paths.CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    paths.ensure_state()

    print(f"  wrote statusLine -> {paths.CLAUDE_SETTINGS}")
    print(f"  command: {cmd}")

    # settings.json is read when a session starts, so sessions already running
    # will never invoke this hook. Saying "next render" sent people back to
    # `taq doctor` wondering why it still reported nothing.
    live = len(usage.live_sessions())
    print("\n  Start a NEW Claude Code session to activate it — settings.json is")
    print("  read at session start, so any session already open will not use it.")
    if live:
        print(f"  ({live} session{'s' if live != 1 else ''} currently running, "
              f"none of which will pick this up.)")
    print("\n  Verify with:  taq doctor   (look for 'rate limits')")
    print("  Note a custom status line replaces most built-in footer hints.")
    print("  Undo: delete the statusLine key, or restore the backup.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "tui"

    if cmd == "statusline":
        return cmd_statusline()
    if cmd == "line":
        return cmd_line()
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "install":
        return cmd_install()
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd != "tui":
        print(f"unknown command: {cmd}\n{__doc__}")
        return 2

    paths.ensure_state()
    try:
        return curses.wrapper(App().run) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

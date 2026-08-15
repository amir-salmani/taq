"""Claude rate limits: capture, fold, and project.

WHY THIS IS SHAPED THIS WAY
  The 5-hour and 7-day percentages exist in exactly one place a local tool can
  reach: the JSON payload Claude Code pipes to a `statusLine` command. They are
  NOT in the transcripts (grep them, they aren't there). So taq cannot poll for
  this data — it has to be handed to it. `taq statusline` is that hook.

  Because the hook fires per session, several sessions report independently and
  disagree: an idle one keeps announcing the percentage it saw ten minutes ago.
  Folding by max within a window fixes that — usage only ever goes up until the
  window resets, so the highest report is the truest one.

  Projection regresses on the percentage itself, never on token counts. Mapping
  tokens to rate-limit consumption would mean guessing Anthropic's weighting of
  input vs cache vs output; the percentage is the actual quantity, measured.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

WINDOWS = ("five_hour", "seven_day")

# How long each window actually is. Used to decide when a trend is worth
# extrapolating: twenty minutes says plenty about a five-hour budget and
# nothing at all about a seven-day one.
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
MIN_SPAN_FRACTION = 0.02      # observe at least 2% of the window first

# The web UI calls these "Current session" and "Weekly · All models". Using its
# words means the two screens can be compared without translating.
WINDOW_LABELS = {"five_hour": "Current session", "seven_day": "Weekly · all models"}


def hook_status() -> tuple[bool, float]:
    """(is the statusline hook installed, when settings.json last changed).

    The install time matters: Claude Code reads settings.json at session start,
    so any session older than that timestamp will never invoke the hook. Knowing
    both lets the UI say which of those two situations you are actually in.
    """
    try:
        raw = paths.CLAUDE_SETTINGS.read_text()
        mtime = paths.CLAUDE_SETTINGS.stat().st_mtime
    except OSError:
        return False, 0.0
    try:
        sl = json.loads(raw).get("statusLine") or {}
    except ValueError:
        return False, 0.0
    return "statusline" in str(sl.get("command", "")).lower(), mtime


def read_plan() -> str:
    """Subscription tier, e.g. "Max (5x)".

    Read from ~/.claude.json, which is ordinary config. The same value also sits
    in ~/.claude/.credentials.json alongside OAuth tokens — taq does not open
    that file, because a status display has no business near your credentials.
    """
    try:
        d = json.loads((paths.HOME / ".claude.json").read_text())
    except (OSError, ValueError):
        return ""

    acct = d.get("oauthAccount") or {}
    tier = acct.get("organizationRateLimitTier") or acct.get("userRateLimitTier") or ""
    if not tier:
        return ""

    t = str(tier).replace("default_claude_", "").replace("default_", "")
    if t.startswith("max_"):
        return f"Max ({t[4:]})"
    return t.replace("_", " ").title()


# -----------------------------------------------------------------------------
# Capture (statusline hook side)
# -----------------------------------------------------------------------------

def record(payload: dict) -> None:
    """Persist one statusline payload. Must be fast and must never raise:
    a crash here would put a traceback in the user's status line."""
    try:
        paths.ensure_state()
        sid = str(payload.get("session_id") or "unknown")
        limits = payload.get("rate_limits") or {}

        snap = {
            "ts": time.time(),
            "session_id": sid,
            "session_name": payload.get("session_name"),
            "cwd": (payload.get("workspace") or {}).get("current_dir"),
            "model": (payload.get("model") or {}).get("display_name"),
            "limits": {
                w: {
                    "used_percentage": (limits.get(w) or {}).get("used_percentage"),
                    "resets_at": (limits.get(w) or {}).get("resets_at"),
                }
                for w in WINDOWS
                if limits.get(w)
            },
        }

        # A payload with no rate_limits tells us nothing, and writing it just
        # litters the state directory with files read_windows will skip.
        if not snap["limits"]:
            return

        # Per-session snapshot, written atomically so a concurrent reader never
        # sees a half-written file.
        dest = paths.LIMITS_DIR / f"{sid}.json"
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap))
        os.replace(tmp, dest)

        if snap["limits"]:
            _append_history(snap)
    except Exception:
        pass


def _append_history(snap: dict) -> None:
    """Append a projection sample, but only when it says something new.

    The hook can fire many times a second while you type. Storing every one
    would bloat the file and weight the regression toward whichever minute you
    happened to be most active in.
    """
    row = {"ts": snap["ts"]}
    for w, d in snap["limits"].items():
        if d.get("used_percentage") is None:
            continue
        row[w] = [d["used_percentage"], d.get("resets_at")]
    if len(row) == 1:
        return

    last = _last_history_row()
    if last is not None:
        same = all(last.get(w) == row.get(w) for w in WINDOWS)
        if same and row["ts"] - last.get("ts", 0) < 300:
            return  # nothing moved and we sampled recently

    with open(paths.LIMITS_HISTORY, "a") as fh:
        fh.write(json.dumps(row) + "\n")

    _trim_history()


def _last_history_row() -> dict | None:
    try:
        with open(paths.LIMITS_HISTORY, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            fh.seek(max(0, end - 4096))
            tail = fh.read().splitlines()
        for raw in reversed(tail):
            if raw.strip():
                return json.loads(raw)
    except (OSError, ValueError):
        pass
    return None


def _trim_history() -> None:
    try:
        if paths.LIMITS_HISTORY.stat().st_size < 512 * 1024:
            return
        lines = paths.LIMITS_HISTORY.read_text().splitlines()
        if len(lines) <= paths.HISTORY_MAX_LINES:
            return
        keep = "\n".join(lines[-paths.HISTORY_MAX_LINES:]) + "\n"
        tmp = paths.LIMITS_HISTORY.with_suffix(".tmp")
        tmp.write_text(keep)
        os.replace(tmp, paths.LIMITS_HISTORY)
    except OSError:
        pass


# -----------------------------------------------------------------------------
# Fold (TUI side)
# -----------------------------------------------------------------------------

@dataclass
class Window:
    name: str
    used: float
    resets_at: int
    observed_at: float          # when the freshest report arrived
    eta: float | None = None    # epoch seconds we project hitting 100%
    rate: float | None = None   # percentage points per hour
    samples: int = 0
    span: float = 0.0           # seconds covered by the samples we have
    min_span: float = 0.0       # seconds needed before a trend is credible

    @property
    def stale_for(self) -> float:
        return time.time() - self.observed_at

    @property
    def resets_in(self) -> float:
        return self.resets_at - time.time()

    @property
    def will_exhaust(self) -> bool:
        """True when we project hitting the cap before the window resets."""
        return self.eta is not None and self.eta < self.resets_at


def read_windows() -> dict[str, Window]:
    """Fold every session's latest snapshot into one Window per limit."""
    snaps = []
    try:
        for f in paths.LIMITS_DIR.glob("*.json"):
            try:
                snaps.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
    except OSError:
        return {}

    now = time.time()
    out: dict[str, Window] = {}

    for w in WINDOWS:
        # Only reports for a window that has not already reset are meaningful.
        live = [
            s for s in snaps
            if (s.get("limits") or {}).get(w)
            and (s["limits"][w].get("resets_at") or 0) > now
            and s["limits"][w].get("used_percentage") is not None
        ]
        if not live:
            continue

        # A new window has a later resets_at. Never let an old window's high
        # percentage bleed into a fresh one.
        newest_reset = max(s["limits"][w]["resets_at"] for s in live)
        in_window = [s for s in live if s["limits"][w]["resets_at"] == newest_reset]

        best = max(in_window, key=lambda s: s["limits"][w]["used_percentage"])
        out[w] = Window(
            name=w,
            used=float(best["limits"][w]["used_percentage"]),
            resets_at=int(newest_reset),
            observed_at=max(s.get("ts", 0) for s in in_window),
        )

    _attach_projection(out)
    return out


def _attach_projection(windows: dict[str, Window]) -> None:
    """Least-squares slope of used_percentage over time, within this window."""
    history = _load_history()
    for w, win in windows.items():
        pts = [
            (row["ts"], row[w][0])
            for row in history
            if w in row and row[w][1] == win.resets_at and row[w][0] is not None
        ]
        # Include the current fold so the newest reading always counts.
        pts.append((win.observed_at, win.used))
        pts = sorted(set(pts))
        win.samples = len(pts)

        # Two points a minute apart is noise, not a trend — and the bar for
        # "trend" scales with the window. Extrapolating a seven-day budget from
        # twenty minutes produced confident nonsense ("hits the cap 3.5d early")
        # off a single percentage point of movement.
        min_span = max(300.0, WINDOW_SECONDS.get(w, 5 * 3600) * MIN_SPAN_FRACTION)
        win.span = pts[-1][0] - pts[0][0]
        win.min_span = min_span
        if len(pts) < 3 or win.span < min_span:
            continue

        n = len(pts)
        t0 = pts[0][0]
        xs = [(t - t0) / 3600.0 for t, _ in pts]   # hours, keeps the slope readable
        ys = [p for _, p in pts]
        mx = sum(xs) / n
        my = sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if denom <= 0:
            continue
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
        win.rate = slope
        if slope > 0.05:  # below this the ETA is days away and meaningless
            hours_left = (100.0 - win.used) / slope
            win.eta = time.time() + hours_left * 3600.0


def _load_history() -> list[dict]:
    try:
        rows = []
        with open(paths.LIMITS_HISTORY) as fh:
            for raw in fh:
                if raw.strip():
                    try:
                        rows.append(json.loads(raw))
                    except ValueError:
                        continue
        return rows
    except OSError:
        return []

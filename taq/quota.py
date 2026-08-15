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

        # Two points a minute apart is noise, not a trend.
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 300:
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

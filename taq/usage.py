"""Where the tokens went: an incremental read of the Claude Code transcripts.

WHY A FULL SCAN IS FINE
  The transcript tree is ~270MB here, which sounds like something that needs an
  index and a cache file. It isn't. Only assistant messages carry a usage block,
  so a bytes-level `b'"usage"' in line` prefilter skips ~95% of the input before
  json ever sees it, and the whole tree parses in ~0.4s warm. An on-disk index
  would buy half a second at startup and cost a whole class of staleness bugs.

  So: one full pass at startup, then byte offsets in memory. Rotation and
  truncation are caught by comparing (inode, size) against what we last saw.

WHAT THE NUMBERS MEAN
  We report *output* tokens. Input and cache-read counts are dominated by
  context replay and would rank a long idle session above an afternoon of real
  work. Output is what the model actually produced for you.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

DAY = 86400.0


@dataclass
class ProjectUse:
    name: str
    output: int = 0
    input: int = 0
    cache_read: int = 0
    messages: int = 0
    by_model: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    branches: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_seen: float = 0.0


@dataclass
class LiveSession:
    pid: int
    session_id: str
    name: str
    cwd: str
    started_at: float
    last_activity: float = 0.0
    tmux: str | None = None

    @property
    def alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    @property
    def idle_for(self) -> float:
        return time.time() - self.last_activity if self.last_activity else 0.0

    @property
    def busy(self) -> bool:
        return 0 < self.idle_for < 45

    @property
    def state(self) -> str:
        """A session can be alive with no transcript at all — started, but no
        message sent yet. That is not "idle 0s", it is nothing having happened."""
        if not self.last_activity:
            return "no messages"
        return "busy" if self.busy else f"idle {_short(self.idle_for)}"


def _short(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 129600:
        return f"{seconds / 3600:.1f}h".replace(".0h", "h")
    return f"{seconds / 86400:.1f}d".replace(".0d", "d")


class TranscriptIndex:
    """Rolling aggregate over the transcripts, refreshed by appended bytes only."""

    def __init__(self, window_days: float = 7.0):
        self.window = window_days * DAY
        self._offsets: dict[str, tuple[int, int]] = {}   # path -> (inode, offset)
        # (project, day_bucket) -> ProjectUse, so the window can be re-trimmed
        # without re-reading anything.
        self._daily: dict[tuple[str, int], ProjectUse] = {}
        self.last_refresh: float = 0.0
        self.cold_seconds: float = 0.0

    # -- reading ------------------------------------------------------------
    def refresh(self) -> int:
        """Read whatever is new. Returns the number of usage records absorbed."""
        t0 = time.time()
        absorbed = 0
        try:
            files = sorted(paths.CLAUDE_PROJECTS.glob("*/*.jsonl"))
        except OSError:
            return 0

        for f in files:
            key = str(f)
            try:
                st = f.stat()
            except OSError:
                continue

            inode, offset = self._offsets.get(key, (st.st_ino, 0))
            if inode != st.st_ino or st.st_size < offset:
                offset = 0  # rotated or truncated; start over on this file
            if st.st_size == offset:
                continue

            try:
                with open(f, "rb") as fh:
                    fh.seek(offset)
                    consumed = 0
                    for raw in fh:
                        # A trailing line with no newline is a partial write:
                        # leave it, and pick it up whole on the next refresh.
                        if not raw.endswith(b"\n"):
                            break
                        consumed += len(raw)
                        absorbed += self._absorb_line(raw)
                    self._offsets[key] = (st.st_ino, offset + consumed)
            except OSError:
                continue

        elapsed = time.time() - t0
        if self.last_refresh == 0.0:
            self.cold_seconds = elapsed
        self.last_refresh = time.time()
        return absorbed

    def _absorb_line(self, raw: bytes) -> int:
        """One transcript line. Deliberately takes a line rather than a blob:
        reading a 50MB transcript whole and splitting it costs ~100MB of peak
        RSS for the duration, which is the entire footprint budget."""
        if b'"usage"' not in raw:
            return 0
        try:
            rec = json.loads(raw)
        except ValueError:
            return 0
        if rec.get("type") != "assistant":
            return 0
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return 0
        u = msg.get("usage")
        if not isinstance(u, dict):
            return 0

        ts = _parse_ts(rec.get("timestamp"))
        if ts is None:
            return 0

        proj = _project_name(rec.get("cwd"))
        bucket = int(ts // DAY)
        pu = self._daily.get((proj, bucket))
        if pu is None:
            pu = ProjectUse(name=proj)
            self._daily[(proj, bucket)] = pu

        out = int(u.get("output_tokens") or 0)
        pu.output += out
        pu.input += int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
        pu.cache_read += int(u.get("cache_read_input_tokens") or 0)
        pu.messages += 1
        pu.last_seen = max(pu.last_seen, ts)
        if model := msg.get("model"):
            pu.by_model[model] += out
        if branch := rec.get("gitBranch"):
            pu.branches[branch] += out
        return 1

    # -- querying -----------------------------------------------------------
    def projects(self, days: float | None = None) -> list[ProjectUse]:
        """Merged per-project totals over the trailing window, busiest first."""
        span = self.window if days is None else days * DAY
        floor = int((time.time() - span) // DAY)
        merged: dict[str, ProjectUse] = {}
        for (name, bucket), pu in self._daily.items():
            if bucket < floor:
                continue
            m = merged.get(name)
            if m is None:
                m = merged[name] = ProjectUse(name=name)
            m.output += pu.output
            m.input += pu.input
            m.cache_read += pu.cache_read
            m.messages += pu.messages
            m.last_seen = max(m.last_seen, pu.last_seen)
            for k, v in pu.by_model.items():
                m.by_model[k] += v
            for k, v in pu.branches.items():
                m.branches[k] += v
        return sorted(merged.values(), key=lambda p: p.output, reverse=True)

    def model_split(self, days: float | None = None) -> list[tuple[str, int]]:
        agg: dict[str, int] = defaultdict(int)
        for p in self.projects(days):
            for m, v in p.by_model.items():
                agg[m] += v
        return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)

    def prune(self) -> None:
        """Drop day buckets that fell out of the window."""
        floor = int((time.time() - self.window) // DAY)
        for k in [k for k in self._daily if k[1] < floor]:
            del self._daily[k]


def _parse_ts(value) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _project_name(cwd: str | None) -> str:
    """A short, stable label for a working directory.

    Sessions run in subdirectories all the time, so the raw cwd would split one
    project across a dozen rows. Everything under ~/CodeBase collapses to its
    top-level project; everything else to at most two path components, which is
    enough to stay unambiguous without turning into a 60-character npx path.
    """
    if not cwd:
        return "?"
    p = Path(cwd)
    home = Path.home()
    try:
        rel = p.relative_to(home / "CodeBase")
        return rel.parts[0] if rel.parts else "CodeBase"
    except ValueError:
        pass
    try:
        rel = p.relative_to(home)
        return "~" if not rel.parts else "~/" + "/".join(rel.parts[:2])
    except ValueError:
        return str(p)


# -----------------------------------------------------------------------------
# Live sessions
# -----------------------------------------------------------------------------

def live_sessions() -> list[LiveSession]:
    """Claude Code writes one file per running session. Files outlive crashed
    processes, so every entry is checked against the process table."""
    out: list[LiveSession] = []
    try:
        entries = list(paths.CLAUDE_SESSIONS.glob("*.json"))
    except OSError:
        return out

    for f in entries:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        pid = d.get("pid")
        sid = d.get("sessionId")
        if not pid or not sid:
            continue

        s = LiveSession(
            pid=int(pid),
            session_id=str(sid),
            name=str(d.get("name") or sid[:8]),
            cwd=str(d.get("cwd") or "?"),
            started_at=float(d.get("startedAt") or 0) / 1000.0,
            tmux=d.get("tmux"),
        )
        if not s.alive:
            continue

        # Activity == when its transcript was last appended to.
        try:
            proj_dir = paths.CLAUDE_PROJECTS / _encode_cwd(s.cwd)
            s.last_activity = (proj_dir / f"{sid}.jsonl").stat().st_mtime
        except OSError:
            s.last_activity = 0.0
        out.append(s)

    out.sort(key=lambda s: s.last_activity, reverse=True)
    return out


def _encode_cwd(cwd: str) -> str:
    """Claude Code flattens a cwd into a directory name by replacing every
    non-alphanumeric run with a dash: /home/amir/x -> -home-amir-x"""
    return "".join(c if c.isalnum() else "-" for c in cwd)

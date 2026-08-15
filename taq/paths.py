"""Every path and constant taq touches, in one place.

Nothing here does I/O. If taq ever reads something new, it gets named here
first, so `grep paths.py` answers "what does this thing look at?".
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()

# ---- Claude Code ------------------------------------------------------------
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"      # transcripts: <project>/<uuid>.jsonl
CLAUDE_SESSIONS = CLAUDE_DIR / "sessions"      # live registry: <pid>.json
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"

# ---- taq's own state --------------------------------------------------------
# Rate-limit percentages only ever reach us through the statusline hook, so we
# persist what it hands us. XDG state, not config: it is derived and disposable.
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "taq"
LIMITS_DIR = STATE_DIR / "limits"              # <session_id>.json, one per session
LIMITS_HISTORY = STATE_DIR / "limits.jsonl"    # append-only samples, for projection
HISTORY_MAX_LINES = 4000                       # ~2 days at a sample a minute

# ---- Throne -----------------------------------------------------------------
THRONE_DIR = HOME / ".config/Throne/config"
THRONE_DB = THRONE_DIR / "throne.db"
THRONE_STATS_DB = THRONE_DIR / "throne_stats.db"

# The mixed inbound port. Read from Throne's own settings when we can; this is
# only the fallback for when the DB is unreadable.
PROXY_PORT_FALLBACK = 12334

# ---- Other proxy layers (mirrors ~/.local/bin/prox) -------------------------
APT_GLOBAL_CONF = Path("/etc/apt/apt.conf.d/99-proxy-global.conf")
ZED_SETTINGS = HOME / ".config/zed/settings.json"
SSH_CONFIG = HOME / ".ssh/config"

PROXY_ENV_VARS = (
    "http_proxy", "https_proxy", "ftp_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY",
)


def ensure_state() -> None:
    """Create the state tree. Called by both the TUI and the statusline hook."""
    LIMITS_DIR.mkdir(parents=True, exist_ok=True)

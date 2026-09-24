"""The estate's honesty, read from a cache file.

`jev snapshot` writes it on a real trigger; nothing here ever calls a model. A
statusline refreshes every ten seconds, so a model call on that tick would cost
about $92 a year to display a number that costs $0.0008 to compute — roughly
115,000x the thing it reports. decisions/0022-system-one.md, rule 1.

Stale is fine and unmarked up to a day; past that the reading is not news and
the widget says so rather than implying the estate is clean.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

SNAPSHOT = (Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
            / "jev" / "snapshot.json")
STALE_AFTER = 36 * 3600


@dataclass(frozen=True)
class Estate:
    known: bool = False
    stale: bool = False
    projects: int = 0
    dishonest: int = 0
    undescribed: int = 0
    worst: str = ""
    age: float = 0.0


def read() -> Estate:
    try:
        d = json.loads(SNAPSHOT.read_text())
        at = time.mktime(time.strptime(d["at"], "%Y-%m-%dT%H:%M:%S"))
    except (OSError, ValueError, KeyError):
        return Estate()
    age = time.time() - at
    return Estate(known=True, stale=age > STALE_AFTER, age=age,
                  projects=d.get("projects", 0),
                  dishonest=len(d.get("dishonest") or []),
                  undescribed=len(d.get("undescribed") or []),
                  worst=(d.get("worst") or {}).get("project", ""))


def segment(e: Estate | None = None) -> str:
    """One coloured cell, or empty when there is nothing to say."""
    e = read() if e is None else e
    if not e.known:
        return ""
    if e.stale:
        return f"\x1b[90m◇{e.dishonest}\x1b[0m"
    if not e.dishonest:
        return "\x1b[32m◆\x1b[0m"
    col = "31" if e.dishonest > 6 else "33"
    return f"\x1b[{col}m◆{e.dishonest}\x1b[0m"

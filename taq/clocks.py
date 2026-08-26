"""Two clocks: where you are, and where the rest of your life is.

The interesting part is not the times — it is the gap between them. Finland
observes DST; Iran abolished it in 2022. So Tampere↔Tehran is 30 minutes for
half the year and 90 for the other half, and it changes on a date neither
country announces to you. Getting that wrong costs you an interview.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None                                   # type: ignore

# Ordered as they are drawn. Add more here and the panel follows.
ZONES: tuple[tuple[str, str], ...] = (
    ("Tampere", "Europe/Helsinki"),
    ("Tehran", "Asia/Tehran"),
)


@dataclass
class Clock:
    label: str
    zone: str
    now: datetime
    is_local: bool
    delta_minutes: int          # relative to this machine's own clock

    @property
    def hhmm(self) -> str:
        return f"{self.now:%H:%M}"

    @property
    def date(self) -> str:
        return f"{self.now:%a %d %b}"

    @property
    def delta_label(self) -> str:
        if self.is_local:
            return "local"
        sign = "+" if self.delta_minutes > 0 else "−"
        m = abs(self.delta_minutes)
        h, mm = divmod(m, 60)
        if h and mm:
            return f"{sign}{h}h{mm:02d}"
        return f"{sign}{h}h" if h else f"{sign}{mm}m"


def read() -> list[Clock]:
    if ZoneInfo is None:
        return []
    local = datetime.now().astimezone()
    local_off = local.utcoffset() or timedelta(0)

    out: list[Clock] = []
    for label, zone in ZONES:
        try:
            tz = ZoneInfo(zone)
        except Exception:
            continue
        now = local.astimezone(tz)
        off = now.utcoffset() or timedelta(0)
        delta = int((off - local_off).total_seconds() // 60)
        out.append(Clock(label, zone, now, delta == 0, delta))
    return out


# -----------------------------------------------------------------------------
# When does the gap change?
# -----------------------------------------------------------------------------

_cache: tuple[float, str | None] = (0.0, None)


def next_shift(max_days: int = 400) -> str | None:
    """The next date on which the offset between the two zones changes.

    Walks forward a day at a time — a few hundred cheap comparisons — and is
    cached for six hours, because the answer moves twice a year.
    """
    global _cache
    at, cached = _cache
    if time.time() - at < 6 * 3600:
        return cached

    result = None
    if ZoneInfo is not None and len(ZONES) >= 2:
        try:
            a, b = ZoneInfo(ZONES[0][1]), ZoneInfo(ZONES[1][1])
            base = datetime.now(a)

            def gap(when: datetime) -> int:
                x = when.astimezone(a).utcoffset() or timedelta(0)
                y = when.astimezone(b).utcoffset() or timedelta(0)
                return int((y - x).total_seconds() // 60)

            today = gap(base)
            for d in range(1, max_days):
                probe = base + timedelta(days=d)
                if gap(probe) != today:
                    new = gap(probe)
                    h, mm = divmod(abs(new), 60)
                    span = f"{h}h{mm:02d}" if mm else f"{h}h"
                    result = f"{span} from {probe:%d %b}"
                    break
        except Exception:
            result = None

    _cache = (time.time(), result)
    return result

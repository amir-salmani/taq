"""System vitals from /proc and /sys. No psutil, no subprocesses.

Everything here is a delta between two reads of a counter file, so the object
holds the previous sample. One instance, polled on the app's tick.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field

HISTORY = 240   # samples kept for the graphs


@dataclass
class Disk:
    mount: str
    used: int
    total: int

    @property
    def pct(self) -> float:
        return self.used / self.total * 100.0 if self.total else 0.0


@dataclass
class Vitals:
    cpu_pct: float = 0.0
    per_core: list[float] = field(default_factory=list)
    load: tuple[float, float, float] = (0.0, 0.0, 0.0)
    mem_used: int = 0
    mem_total: int = 0
    swap_used: int = 0
    swap_total: int = 0
    net_up: float = 0.0        # bytes/sec
    net_down: float = 0.0
    temp_c: float | None = None
    battery_pct: float | None = None
    battery_charging: bool = False
    uptime: float = 0.0
    procs: int = 0

    @property
    def mem_pct(self) -> float:
        return self.mem_used / self.mem_total * 100.0 if self.mem_total else 0.0

    @property
    def swap_pct(self) -> float:
        return self.swap_used / self.swap_total * 100.0 if self.swap_total else 0.0


class Monitor:
    def __init__(self) -> None:
        self._cpu_prev: list[tuple[int, int]] = []
        self._net_prev: tuple[float, int, int] | None = None
        self.cpu_history: deque[float] = deque(maxlen=HISTORY)
        self.mem_history: deque[float] = deque(maxlen=HISTORY)
        self.net_up_history: deque[float] = deque(maxlen=HISTORY)
        self.net_down_history: deque[float] = deque(maxlen=HISTORY)
        self.disks: list[Disk] = []
        self._disks_at = 0.0
        self._temp_path: str | None | bool = False   # False = not yet looked

    def sample(self) -> Vitals:
        v = Vitals()
        self._cpu(v)
        self._mem(v)
        self._net(v)
        self._misc(v)
        v.temp_c = self._temp()
        self._battery(v)

        self.cpu_history.append(v.cpu_pct)
        self.mem_history.append(v.mem_pct)
        self.net_up_history.append(v.net_up)
        self.net_down_history.append(v.net_down)

        if time.time() - self._disks_at > 30:
            self._disks_at = time.time()
            self.disks = _disks()
        return v

    # -- pieces -------------------------------------------------------------
    def _cpu(self, v: Vitals) -> None:
        try:
            with open("/proc/stat") as fh:
                lines = [l for l in fh if l.startswith("cpu")]
        except OSError:
            return

        cur: list[tuple[int, int]] = []
        for line in lines:
            f = line.split()
            if len(f) < 5:
                continue
            nums = [int(x) for x in f[1:11] if x.isdigit()]
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
            cur.append((sum(nums), idle))

        if self._cpu_prev and len(self._cpu_prev) == len(cur):
            pcts = []
            for (t0, i0), (t1, i1) in zip(self._cpu_prev, cur):
                dt, di = t1 - t0, i1 - i0
                pcts.append(max(0.0, min(100.0, (dt - di) / dt * 100.0)) if dt > 0 else 0.0)
            if pcts:
                v.cpu_pct = pcts[0]
                v.per_core = pcts[1:]
        self._cpu_prev = cur

    def _mem(self, v: Vitals) -> None:
        try:
            info = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    k, _, rest = line.partition(":")
                    info[k] = int(rest.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return
        v.mem_total = info.get("MemTotal", 0)
        # MemAvailable is the kernel's own answer to "how much can I actually
        # allocate" — far truer than total - free - buffers - cached.
        v.mem_used = v.mem_total - info.get("MemAvailable", info.get("MemFree", 0))
        v.swap_total = info.get("SwapTotal", 0)
        v.swap_used = v.swap_total - info.get("SwapFree", 0)

    def _net(self, v: Vitals) -> None:
        rx = tx = 0
        try:
            with open("/proc/net/dev") as fh:
                for line in fh.readlines()[2:]:
                    name, _, rest = line.partition(":")
                    name = name.strip()
                    # Loopback would double-count every local proxy hop, which
                    # on this machine is most of the traffic.
                    if name == "lo" or name.startswith(("docker", "br-", "veth")):
                        continue
                    f = rest.split()
                    if len(f) >= 9:
                        rx += int(f[0])
                        tx += int(f[8])
        except (OSError, ValueError, IndexError):
            return

        now = time.time()
        if self._net_prev:
            t0, rx0, tx0 = self._net_prev
            dt = now - t0
            if dt > 0:
                v.net_down = max(0.0, (rx - rx0) / dt)
                v.net_up = max(0.0, (tx - tx0) / dt)
        self._net_prev = (now, rx, tx)

    def _misc(self, v: Vitals) -> None:
        try:
            with open("/proc/loadavg") as fh:
                f = fh.read().split()
            v.load = (float(f[0]), float(f[1]), float(f[2]))
            v.procs = int(f[3].split("/")[1])
        except (OSError, ValueError, IndexError):
            pass
        try:
            with open("/proc/uptime") as fh:
                v.uptime = float(fh.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass

    def _temp(self) -> float | None:
        if self._temp_path is False:
            self._temp_path = _find_temp()
        if not self._temp_path:
            return None
        try:
            with open(self._temp_path) as fh:
                return int(fh.read().strip()) / 1000.0
        except (OSError, ValueError):
            self._temp_path = None
            return None

    def _battery(self, v: Vitals) -> None:
        base = "/sys/class/power_supply"
        try:
            names = [n for n in os.listdir(base) if n.startswith("BAT")]
        except OSError:
            return
        if not names:
            return
        try:
            with open(f"{base}/{names[0]}/capacity") as fh:
                v.battery_pct = float(fh.read().strip())
            with open(f"{base}/{names[0]}/status") as fh:
                v.battery_charging = fh.read().strip() in ("Charging", "Full")
        except (OSError, ValueError):
            v.battery_pct = None


def _find_temp() -> str | None:
    """Prefer a CPU package sensor; thermal_zone0 is often the wifi card."""
    import glob
    for pattern, want in (
        ("/sys/class/hwmon/hwmon*/name", ("coretemp", "k10temp", "zenpower")),
        ("/sys/class/thermal/thermal_zone*/type", ("x86_pkg_temp", "cpu-thermal")),
    ):
        for path in sorted(glob.glob(pattern)):
            try:
                with open(path) as fh:
                    if fh.read().strip() not in want:
                        continue
            except OSError:
                continue
            base = os.path.dirname(path)
            for cand in ("temp1_input", "temp"):
                p = os.path.join(base, cand)
                if os.path.exists(p):
                    return p
    for p in ("/sys/class/thermal/thermal_zone0/temp",):
        if os.path.exists(p):
            return p
    return None


def _disks() -> list[Disk]:
    out: list[Disk] = []
    seen: set[str] = set()
    try:
        with open("/proc/mounts") as fh:
            entries = [l.split() for l in fh]
    except OSError:
        return out

    for f in entries:
        if len(f) < 3:
            continue
        dev, mount, fstype = f[0], f[1], f[2]
        if not dev.startswith("/dev/") or fstype in ("squashfs", "iso9660"):
            continue
        if mount in seen or mount.startswith(("/snap", "/var/snap")):
            continue
        seen.add(mount)
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total < 1 << 30:      # ignore boot partitions and other slivers
            continue
        out.append(Disk(mount, total - st.f_bfree * st.f_frsize, total))

    out.sort(key=lambda d: (d.mount != "/", d.mount))
    return out[:4]

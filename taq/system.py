"""System vitals from /proc and /sys. No psutil, no subprocesses.

Everything here is a delta between two reads of a counter file, so the object
holds the previous sample. One instance, polled on the app's tick.
"""

from __future__ import annotations

import json
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
class Power:
    """Battery and backlight, including the one number laptops never show you:
    how long you actually have left at the rate you are drawing right now."""
    present: bool = False
    pct: float | None = None
    status: str = ""              # Discharging | Charging | Full | Not charging
    ac_online: bool | None = None
    watts: float | None = None    # instantaneous draw (or charge rate)
    energy_wh: float | None = None
    full_wh: float | None = None
    design_wh: float | None = None
    cycles: int | None = None
    seconds_left: float | None = None   # to empty when discharging, to full when charging
    brightness_pct: float | None = None
    # Measured, not modelled: watts observed at other brightness levels.
    predicted_watts: float | None = None
    predicted_at_pct: float | None = None

    @property
    def charging(self) -> bool:
        return self.status in ("Charging", "Full")

    @property
    def health_pct(self) -> float | None:
        if not self.full_wh or not self.design_wh:
            return None
        return self.full_wh / self.design_wh * 100.0

    @property
    def delta_seconds(self) -> float | None:
        """How much runtime the predicted brightness level would buy (+) or
        cost (-), at the current charge."""
        if not (self.predicted_watts and self.energy_wh and self.seconds_left):
            return None
        if self.predicted_watts <= 0:
            return None
        return (self.energy_wh / self.predicted_watts) * 3600.0 - self.seconds_left


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
    power: 'Power' = field(default_factory=lambda: Power())
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
        self.power_model = PowerModel()

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
        p = read_power()
        v.power = p
        v.battery_pct = p.pct
        v.battery_charging = p.charging

        # Learn what the machine draws at each brightness level, then use that
        # to predict. Only while discharging (on AC the draw says nothing about
        # battery life) and only when the CPU is quiet, because a busy core
        # swings power by 20W and would drown a 2W backlight difference.
        if p.present and not p.charging and p.watts and p.brightness_pct is not None:
            if v.cpu_pct < 15:
                self.power_model.observe(p.brightness_pct, p.watts)
            pred = self.power_model.predict(p.brightness_pct)
            if pred:
                p.predicted_at_pct, p.predicted_watts = pred


PSU = "/sys/class/power_supply"


def _uv(path: str) -> float | None:
    """sysfs power values are integers in micro-units."""
    try:
        with open(path) as fh:
            return int(fh.read().strip()) / 1e6
    except (OSError, ValueError):
        return None


def _txt(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def read_power() -> Power:
    p = Power()
    try:
        bats = sorted(n for n in os.listdir(PSU) if n.startswith("BAT"))
        supplies = os.listdir(PSU)
    except OSError:
        return p

    for n in supplies:
        if _txt(f"{PSU}/{n}/type") == "Mains":
            online = _txt(f"{PSU}/{n}/online")
            if online in ("0", "1"):
                p.ac_online = online == "1"
            break

    p.brightness_pct = _brightness()

    if not bats:
        return p
    b = f"{PSU}/{bats[0]}"
    p.present = _txt(f"{b}/present") != "0"
    p.status = _txt(f"{b}/status")
    try:
        p.pct = float(_txt(f"{b}/capacity"))
    except ValueError:
        p.pct = None
    try:
        p.cycles = int(_txt(f"{b}/cycle_count")) or None
    except ValueError:
        p.cycles = None

    # Two flavours of battery reporting: energy (Wh, µWh) or charge (Ah, µAh).
    # The charge flavour needs voltage to become watts.
    energy, full, design, watts = (_uv(f"{b}/energy_now"), _uv(f"{b}/energy_full"),
                                   _uv(f"{b}/energy_full_design"), _uv(f"{b}/power_now"))
    if energy is None:
        charge, cfull = _uv(f"{b}/charge_now"), _uv(f"{b}/charge_full")
        cdesign, current = _uv(f"{b}/charge_full_design"), _uv(f"{b}/current_now")
        volts = _uv(f"{b}/voltage_now")
        if charge is not None and volts:
            energy = charge * volts
            full = cfull * volts if cfull else None
            design = cdesign * volts if cdesign else None
            watts = current * volts if current else None

    p.energy_wh, p.full_wh, p.design_wh = energy, full, design
    p.watts = watts if watts and watts > 0 else None

    if p.watts:
        if p.status == "Discharging" and energy:
            p.seconds_left = energy / p.watts * 3600.0
        elif p.status == "Charging" and energy is not None and full:
            p.seconds_left = max(0.0, (full - energy)) / p.watts * 3600.0
    return p


def _brightness() -> float | None:
    base = "/sys/class/backlight"
    try:
        devs = sorted(os.listdir(base))
    except OSError:
        return None
    for d in devs:
        try:
            cur = int(_txt(f"{base}/{d}/brightness"))
            mx = int(_txt(f"{base}/{d}/max_brightness"))
        except ValueError:
            continue
        if mx > 0:
            return cur / mx * 100.0
    return None


class PowerModel:
    """Observed watts per brightness bucket, persisted across runs.

    This measures rather than models. Nobody can tell you what your backlight
    costs from a spec sheet — panels, drivers and ambient sensors differ — but
    the machine will tell you if you watch it at each level and only compare
    like with like.
    """

    BUCKET = 10          # percent
    KEEP = 40            # samples per bucket
    MIN_SAMPLES = 6

    def __init__(self, path=None):
        from . import paths
        self.path = path or (paths.STATE_DIR / "power.json")
        self.buckets: dict[int, list[float]] = {}
        self._dirty = False
        self._saved_at = 0.0
        self._load()

    @staticmethod
    def _key(pct: float) -> int:
        return int(round(pct / PowerModel.BUCKET) * PowerModel.BUCKET)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            self.buckets = {int(k): [float(x) for x in v][-self.KEEP:]
                            for k, v in raw.items()}
        except (OSError, ValueError, TypeError):
            self.buckets = {}

    def save(self) -> None:
        if not self._dirty or time.time() - self._saved_at < 60:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({str(k): v for k, v in self.buckets.items()}))
            os.replace(tmp, self.path)
            self._dirty, self._saved_at = False, time.time()
        except OSError:
            pass

    def observe(self, brightness_pct: float, watts: float) -> None:
        k = self._key(brightness_pct)
        lst = self.buckets.setdefault(k, [])
        lst.append(round(watts, 3))
        if len(lst) > self.KEEP:
            del lst[: len(lst) - self.KEEP]
        self._dirty = True
        self.save()

    def median(self, bucket: int) -> float | None:
        vals = sorted(self.buckets.get(bucket, []))
        if len(vals) < self.MIN_SAMPLES:
            return None
        return vals[len(vals) // 2]

    def predict(self, current_pct: float) -> tuple[float, float] | None:
        """Pick the most informative other level we have data for: the dimmest
        known bucket (biggest saving), or the brightest if we are already at
        the bottom. Returns (bucket_pct, median_watts)."""
        cur = self._key(current_pct)
        known = [b for b in sorted(self.buckets) if self.median(b) is not None]
        others = [b for b in known if b != cur]
        if not others:
            return None
        target = min(others) if min(others) < cur else max(others)
        w = self.median(target)
        return (float(target), w) if w else None


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

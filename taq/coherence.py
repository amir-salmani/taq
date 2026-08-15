"""Is this machine actually doing what you think it is doing?

WHY THIS EXISTS
  Turning the VPN off does not turn the proxy off. The proxy is configured in
  half a dozen places that share no state — GNOME, the shell environment, apt,
  Zed, ssh — and they drift apart silently. You switch Throne off, GNOME goes
  quiet, and every process you started an hour ago is still exporting
  http_proxy=127.0.0.1:12334 pointed at a socket that no longer exists.

  Nothing tells you this. The failure is not an error, it is a hang.

  `prox status` answers the question on demand. This module answers it
  continuously, and adds the part `prox` cannot see: which already-running
  processes are still holding the dead proxy in their environment. You cannot
  fix those from outside — a process's environment is frozen at exec time — but
  naming them turns a mystery hang into a restart.

COST
  Everything on the fast path reads /proc or stats a file. No subprocesses, no
  sockets. The two expensive checks (gsettings, exit IP) are on their own
  cadence and cached.
"""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# Verdicts, worst last — the UI colours on these.
COHERENT = "COHERENT"
SPLIT = "SPLIT"
UNKNOWN = "UNKNOWN"


@dataclass
class Layer:
    name: str
    on: bool           # is this layer pointing at the proxy?
    detail: str
    known: bool = True  # False when we could not determine the state


@dataclass
class StaleProc:
    pid: int
    comm: str
    target: str


@dataclass
class Coherence:
    verdict: str
    headline: str
    core_up: bool
    proxy_port: int
    port_source: str = "built-in default"
    layers: list[Layer] = field(default_factory=list)
    stale: list[StaleProc] = field(default_factory=list)
    exit_ip: str | None = None


# -----------------------------------------------------------------------------
# /proc/net — listening sockets without spawning ss(8)
# -----------------------------------------------------------------------------

def _parse_net_table(path: str, listening_only: bool) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    try:
        with open(path) as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                local, state = parts[1], parts[3]
                if listening_only and state != "0A":  # TCP_LISTEN
                    continue
                addr_hex, _, port_hex = local.partition(":")
                try:
                    port = int(port_hex, 16)
                except ValueError:
                    continue
                out.add((_hex_to_ip(addr_hex), port))
    except (OSError, ValueError):
        pass
    return out


def _hex_to_ip(h: str) -> str:
    """Kernel writes the address little-endian, as hex, without punctuation."""
    try:
        if len(h) == 8:
            return socket.inet_ntoa(struct.pack("<I", int(h, 16)))
        if len(h) == 32:  # IPv6, four little-endian words
            words = [int(h[i:i + 8], 16) for i in range(0, 32, 8)]
            return socket.inet_ntop(socket.AF_INET6, struct.pack("<4I", *words))
    except (OSError, ValueError, struct.error):
        pass
    return h


def listening() -> set[tuple[str, int]]:
    return _parse_net_table("/proc/net/tcp", True) | _parse_net_table("/proc/net/tcp6", True)


def _udp_bound() -> set[tuple[str, int]]:
    return _parse_net_table("/proc/net/udp", False) | _parse_net_table("/proc/net/udp6", False)


# -----------------------------------------------------------------------------
# Throne's own view of itself
# -----------------------------------------------------------------------------

_settings_cache: tuple[float, dict] = (0.0, {})


def throne_settings() -> dict:
    """Throne keeps its config in SQLite. Read-only, short connection: the DB is
    in WAL mode and Throne is writing to it while we look."""
    global _settings_cache
    ts, cached = _settings_cache
    if time.time() - ts < 10:
        return cached

    out: dict = {}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{paths.THRONE_DB}?mode=ro", uri=True, timeout=0.5)
        try:
            for k, v in con.execute("select key, value from settings"):
                out[k] = v
        finally:
            con.close()
    except Exception:
        pass

    _settings_cache = (time.time(), out)
    return out


def proxy_port() -> tuple[int, str]:
    """Returns (port, where-it-came-from). The source matters: Throne's default
    happens to equal our fallback, so a bare port number cannot tell you whether
    the DB was actually read."""
    s = throne_settings()
    for key in ("inbound_socks_port", "mixed_port", "inbound_mixed_port", "socks_port"):
        try:
            p = int(s[key])
            if 0 < p < 65536:
                return p, f"throne.db:{key}"
        except (KeyError, TypeError, ValueError):
            continue
    return paths.PROXY_PORT_FALLBACK, "built-in default"


# -----------------------------------------------------------------------------
# The layers
# -----------------------------------------------------------------------------

_gsettings_cache: tuple[float, str | None] = (0.0, None)


def _gnome_mode() -> str | None:
    """The one check that needs a subprocess. dconf's binary format is not
    something to hand-parse, so we pay ~40ms for it every 15 seconds."""
    global _gsettings_cache
    ts, cached = _gsettings_cache
    if time.time() - ts < 15:
        return cached
    mode = None
    try:
        r = subprocess.run(
            ["gsettings", "get", "org.gnome.system.proxy", "mode"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            mode = r.stdout.strip().strip("'")
    except (OSError, subprocess.SubprocessError):
        pass
    _gsettings_cache = (time.time(), mode)
    return mode


def _env_proxy() -> str | None:
    for v in paths.PROXY_ENV_VARS:
        val = os.environ.get(v)
        if val:
            return val
    return None


def _zed_proxy() -> str | None:
    try:
        raw = paths.ZED_SETTINGS.read_text()
    except OSError:
        return None
    m = re.search(r'"proxy"\s*:\s*"([^"]*)"', raw)
    return m.group(1) if m else None


def _ssh_proxied(port: int) -> bool:
    try:
        return f":{port}" in paths.SSH_CONFIG.read_text()
    except OSError:
        return False


# -----------------------------------------------------------------------------
# The forensics: who is still holding a dead proxy?
# -----------------------------------------------------------------------------

_PROXY_RE = re.compile(
    r"^(?:" + "|".join(paths.PROXY_ENV_VARS) + r")=(.+)$"
)
_HOSTPORT_RE = re.compile(r"://(?:[^@/]*@)?([^/:]+):(\d+)")


def stale_processes(live_ports: set[tuple[str, int]]) -> list[StaleProc]:
    """Processes whose environment names a proxy endpoint that nothing is
    listening on. Own-uid only — /proc/<pid>/environ is not readable otherwise,
    so system services are invisible to us and we do not pretend otherwise."""
    me = os.getuid()
    my_pid = os.getpid()
    ports = {p for _, p in live_ports}
    found: list[StaleProc] = []

    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return found

    for d in pids:
        pid = int(d)
        if pid == my_pid:
            continue
        base = f"/proc/{d}"
        try:
            if os.stat(base).st_uid != me:
                continue
            with open(f"{base}/environ", "rb") as fh:
                blob = fh.read(64 * 1024)
        except OSError:
            continue  # gone, or not ours

        target = None
        for entry in blob.split(b"\0"):
            if not entry:
                continue
            try:
                s = entry.decode("utf-8", "replace")
            except Exception:
                continue
            m = _PROXY_RE.match(s)
            if not m:
                continue
            hp = _HOSTPORT_RE.search(m.group(1))
            if not hp:
                continue
            if int(hp.group(2)) not in ports:
                target = f"{hp.group(1)}:{hp.group(2)}"
                break

        if target:
            found.append(StaleProc(pid=pid, comm=_label(base), target=target))

    found.sort(key=lambda p: (p.comm, p.pid))
    return found


# comm is whatever the program called its main thread, which for anything
# threaded is frequently "MainThread" — useless when the point of the list is
# to tell you what to restart.
_GENERIC_COMM = {
    "MainThread", "python", "python3", "node", "sh", "bash", "zsh", "fish",
    "java", "ruby", "perl", "electron", "sudo", "env",
}


def _label(procdir: str) -> str:
    try:
        comm = Path(f"{procdir}/comm").read_text().strip()
    except OSError:
        comm = ""

    if comm and comm not in _GENERIC_COMM:
        return comm

    try:
        argv = Path(f"{procdir}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return comm or "?"
    argv = [a.decode("utf-8", "replace") for a in argv if a]
    if not argv:
        return comm or "?"

    # Skip past the interpreter to the thing it is actually running.
    for a in argv:
        if a.startswith("-"):
            continue
        name = os.path.basename(a)
        if name and name not in _GENERIC_COMM:
            return name[:24]
    return comm or os.path.basename(argv[0])[:24]


# -----------------------------------------------------------------------------
# Exit IP — the only check that touches the network
# -----------------------------------------------------------------------------

_exit_cache: tuple[float, str | None] = (0.0, None)


def exit_ip(port: int, force: bool = False, ttl: float = 180.0) -> str | None:
    global _exit_cache
    ts, cached = _exit_cache
    if not force and time.time() - ts < ttl:
        return cached
    ip = None
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "6", "-x", f"http://127.0.0.1:{port}", "https://ip.me"],
            capture_output=True, text=True, timeout=8,
        )
        cand = r.stdout.strip()
        if r.returncode == 0 and 6 < len(cand) < 46 and " " not in cand:
            ip = cand
    except (OSError, subprocess.SubprocessError):
        pass
    _exit_cache = (time.time(), ip)
    return ip


# -----------------------------------------------------------------------------
# The verdict
# -----------------------------------------------------------------------------

def snapshot(deep: bool = False) -> Coherence:
    """One full read of every layer.

    `deep` adds the process-environment scan and the DNS check. Those are cheap
    but not free (~300 small reads), so the caller runs them on the slow tick.
    """
    port, port_source = proxy_port()
    live = listening()
    core_up = any(p == port for _, p in live)

    gnome = _gnome_mode()
    env = _env_proxy()
    zed = _zed_proxy()
    apt_global = paths.APT_GLOBAL_CONF.exists()
    ssh_on = _ssh_proxied(port)

    # Throne's resolver. Reading /proc/net/udp is as cheap as the TCP table, so
    # this stays on the fast path — a layer that appears and disappears between
    # frames reads as a glitch, not as data.
    dns_up = any(p == 5533 for _, p in live | _udp_bound())

    layers = [
        Layer("Throne", core_up,
              f"listening :{port}" if core_up else f"NOT listening :{port}"),
        Layer("GNOME", gnome == "manual",
              gnome or "unavailable", known=gnome is not None),
        Layer("shell env", env is not None, env or "unset"),
        Layer("apt", apt_global,
              "all traffic proxied" if apt_global else "direct + per-host"),
        Layer("zed", bool(zed), zed or "direct"),
        Layer("ssh", ssh_on, "CONNECT via proxy" if ssh_on else "direct"),
        Layer("DNS", dns_up,
              "Throne resolver :5533" if dns_up else "system resolver"),
    ]

    # The only genuinely expensive check: ~550 small reads under /proc.
    stale: list[StaleProc] = stale_processes(live) if deep else []

    # Anything that would send traffic at the proxy right now.
    pointing = [ly for ly in layers
                if ly.on and ly.name not in ("Throne", "DNS")]

    if core_up:
        verdict = COHERENT
        headline = (f"tunnelled via :{port}" if pointing
                    else f"Throne up, nothing routed through it")
    elif pointing or stale:
        verdict = SPLIT
        bits = []
        if pointing:
            bits.append(", ".join(ly.name for ly in pointing))
        if stale:
            bits.append(f"{len(stale)} live process{'es' if len(stale) != 1 else ''}")
        headline = f"Throne is down but {' and '.join(bits)} still point at :{port}"
    else:
        verdict = COHERENT
        headline = "direct — no proxy configured anywhere"

    return Coherence(
        verdict=verdict,
        headline=headline,
        core_up=core_up,
        proxy_port=port,
        port_source=port_source,
        layers=layers,
        stale=stale,
        exit_ip=exit_ip(port) if core_up else None,
    )

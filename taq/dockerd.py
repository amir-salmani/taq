"""Docker, over the unix socket, with no dependencies and no subprocesses.

WHY NOT SHELL OUT TO `docker`
  `docker ps` costs ~50ms and ~30MB of RSS per invocation because it starts a Go
  binary that dials the same socket we can dial ourselves. On a one-second tick
  that is the whole footprint budget, spent on process startup. The daemon
  speaks plain HTTP/1.1 over /var/run/docker.sock; http.client speaks that.

  Same reasoning as reading /proc/net/tcp instead of spawning ss(8).

STATS ARE THE EXPENSIVE PART
  There is no bulk stats endpoint — it is one request per container. So stats
  run on their own slow cadence, only for running containers, and capped.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
from pathlib import Path
from dataclasses import dataclass, field

SOCKET_PATHS = (
    os.environ.get("DOCKER_HOST", "").replace("unix://", "") or None,
    "/var/run/docker.sock",
    f"/run/user/{os.getuid()}/docker.sock",          # rootless
    os.path.expanduser("~/.docker/desktop/docker.sock"),
)
API = "v1.44"

STATS_MAX = 24          # do not fan out to a hundred containers
STATS_EVERY = 5.0
_NCPU = os.cpu_count() or 1


@dataclass
class Container:
    cid: str
    name: str
    image: str
    state: str            # running | exited | paused | created | restarting
    status: str           # "Up 3 hours", human text from the daemon
    project: str | None   # compose project, when labelled
    ports: str
    health: str | None = None
    cpu_pct: float | None = None
    mem_bytes: int | None = None
    mem_limit: int | None = None
    # Detail, all of it already present in the list response — no extra calls.
    created: int = 0
    command: str = ""
    service: str | None = None          # compose service name
    port_list: list[str] = field(default_factory=list)
    networks: dict[str, str] = field(default_factory=dict)   # name -> ip
    mounts: list[str] = field(default_factory=list)
    # From the stats sample we already take for CPU and memory.
    net_rx: int = 0
    net_tx: int = 0
    blk_read: int = 0
    blk_write: int = 0
    pids: int = 0

    @property
    def up(self) -> bool:
        return self.state == "running"

    @property
    def short(self) -> str:
        return self.cid[:12]

    @property
    def mem_pct(self) -> float | None:
        if not self.mem_bytes or not self.mem_limit:
            return None
        return self.mem_bytes / self.mem_limit * 100.0


@dataclass
class Detail:
    """The few fields that genuinely need an inspect call. Fetched only for the
    container the cursor is on, so it costs one request regardless of how many
    containers exist."""
    restart_count: int = 0
    restart_policy: str = ""
    started_at: str = ""
    finished_at: str = ""
    exit_code: int | None = None
    oom_killed: bool = False
    pid: int = 0
    env_count: int = 0
    health_log: list[str] = field(default_factory=list)
    health_failing: int = 0

@dataclass
class DockerView:
    available: bool
    reason: str = ""
    version: str = ""
    containers: list[Container] = field(default_factory=list)

    @property
    def running(self) -> int:
        return sum(1 for c in self.containers if c.up)


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 2.0):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


class Client:
    def __init__(self) -> None:
        self.path = self._find_socket()
        self.version = ""
        self.reason = "" if self.path else "no docker socket found"
        self._stats_at = 0.0
        self._stats: dict[str, dict] = {}
        # cid -> (when, cpu_total_ns, system_total_ns, ncpu), our own baseline
        self._counters: dict[str, tuple[float, int, int, int]] = {}
        self._details: dict[str, tuple[float, 'Detail']] = {}

    @staticmethod
    def _find_socket() -> str | None:
        for p in SOCKET_PATHS:
            if p and os.path.exists(p):
                return p
        return None

    # -- transport ----------------------------------------------------------
    def _request(self, method: str, url: str, timeout: float = 2.0) -> bytes | None:
        if not self.path:
            return None
        conn = _UnixHTTP(self.path, timeout=timeout)
        try:
            conn.request(method, f"/{API}{url}", headers={"Host": "localhost"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status >= 400:
                self.reason = f"HTTP {resp.status}: {body[:120].decode('utf-8', 'replace')}"
                return None
            return body
        except PermissionError:
            self.reason = "permission denied on docker.sock (add yourself to the docker group)"
            return None
        except (OSError, http.client.HTTPException) as e:
            self.reason = f"{type(e).__name__}: {e}"
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _json(self, url: str, timeout: float = 2.0):
        raw = self._request("GET", url, timeout)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # -- reads --------------------------------------------------------------
    def view(self, with_stats: bool = True) -> DockerView:
        if not self.path:
            return DockerView(False, self.reason)

        if not self.version:
            v = self._json("/version", timeout=1.0)
            if isinstance(v, dict):
                self.version = v.get("Version", "")

        raw = self._json("/containers/json?all=1")
        if raw is None:
            return DockerView(False, self.reason or "daemon not responding",
                              version=self.version)

        containers = [self._parse(c) for c in raw]
        containers.sort(key=lambda c: (not c.up, c.project or "~", c.name))

        if with_stats:
            self._refresh_stats([c for c in containers if c.up])
        for c in containers:
            if s := self._stats.get(c.cid):
                c.cpu_pct, c.mem_bytes, c.mem_limit = s["cpu"], s["mem"], s["mem_limit"]
                c.net_rx, c.net_tx = s["rx"], s["tx"]
                c.blk_read, c.blk_write, c.pids = s["blk_read"], s["blk_write"], s["pids"]

        return DockerView(True, "", self.version, containers)

    @staticmethod
    def _parse(c: dict) -> Container:
        names = c.get("Names") or []
        labels = c.get("Labels") or {}
        ports = []
        for p in c.get("Ports") or []:
            pub, priv, proto = p.get("PublicPort"), p.get("PrivatePort"), p.get("Type", "tcp")
            ip = p.get("IP") or ""
            if pub:
                host = f"{ip}:" if ip and ip not in ("0.0.0.0", "::") else ""
                ports.append(f"{host}{pub}→{priv}/{proto}")
            else:
                ports.append(f"{priv}/{proto}")
        ports = list(dict.fromkeys(ports))

        status = c.get("Status", "")
        health = None
        for marker, label in (("(healthy)", "healthy"), ("(unhealthy)", "unhealthy"),
                              ("(health: starting)", "starting")):
            if marker in status:
                health = label
                break

        nets = {}
        for nname, nd in ((c.get("NetworkSettings") or {}).get("Networks") or {}).items():
            nets[nname] = (nd or {}).get("IPAddress") or ""

        mounts = []
        for m in c.get("Mounts") or []:
            dest = m.get("Destination", "")
            src = m.get("Name") or m.get("Source") or ""
            kind = m.get("Type", "")
            rw = "rw" if m.get("RW") else "ro"
            if kind == "bind":
                src = src.replace(str(Path.home()), "~")
            mounts.append(f"{src} → {dest} [{kind},{rw}]")

        return Container(
            cid=c.get("Id", ""),
            name=(names[0].lstrip("/") if names else c.get("Id", "")[:12]),
            image=(c.get("Image") or "").split("@")[0],
            state=c.get("State", "?"),
            status=status,
            project=labels.get("com.docker.compose.project"),
            service=labels.get("com.docker.compose.service"),
            ports=", ".join(ports)[:28],
            port_list=ports,
            health=health,
            created=int(c.get("Created") or 0),
            command=(c.get("Command") or "").replace("\n", " ").strip(),
            networks=nets,
            mounts=mounts,
        )

    def detail(self, cid: str) -> Detail | None:
        """Inspect one container. Cached briefly so holding the cursor still
        on a row does not re-request every frame."""
        hit = self._details.get(cid)
        if hit and time.time() - hit[0] < 4.0:
            return hit[1]

        d = self._json(f"/containers/{cid}/json", timeout=3.0)
        if not isinstance(d, dict):
            return None
        st = d.get("State") or {}
        hc = st.get("Health") or {}
        policy = (d.get("HostConfig") or {}).get("RestartPolicy") or {}

        det = Detail(
            restart_count=int(d.get("RestartCount") or 0),
            restart_policy=policy.get("Name") or "",
            started_at=_short_time(st.get("StartedAt")),
            finished_at=_short_time(st.get("FinishedAt")),
            exit_code=st.get("ExitCode"),
            oom_killed=bool(st.get("OOMKilled")),
            pid=int(st.get("Pid") or 0),
            env_count=len((d.get("Config") or {}).get("Env") or []),
            health_failing=int(hc.get("FailingStreak") or 0),
            health_log=[
                (e.get("Output") or "").strip().splitlines()[0][:120]
                for e in (hc.get("Log") or [])[-3:]
                if (e.get("Output") or "").strip()
            ],
        )
        self._details[cid] = (time.time(), det)
        return det

    def _refresh_stats(self, running: list[Container]) -> None:
        """One request per container, so: slow cadence and a hard cap.

        WHY one-shot=true
          `stats?stream=false` alone does NOT return promptly. The daemon
          collects two samples ~1s apart so it can fill in precpu_stats, which
          means N containers cost N seconds and the whole UI loop stalls behind
          them. one-shot=true returns immediately but leaves precpu zeroed.

          So we keep the previous counters ourselves and difference them across
          our own polls — the same thing we already do with /proc/stat and
          /proc/net/dev. The window becomes our poll interval, which is longer
          than Docker's and therefore steadier.
        """
        if time.time() - self._stats_at < STATS_EVERY:
            return
        now = time.time()
        elapsed = now - self._stats_at
        self._stats_at = now

        fresh: dict[str, dict] = {}
        counters: dict[str, tuple[float, int, int, int]] = {}

        for c in running[:STATS_MAX]:
            d = self._json(f"/containers/{c.cid}/stats?stream=false&one-shot=true",
                           timeout=2.0)
            if not isinstance(d, dict):
                continue
            try:
                cpu = d["cpu_stats"]
                total = int(cpu["cpu_usage"]["total_usage"])
                sys_total = int(cpu.get("system_cpu_usage") or 0)
                ncpu = int(cpu.get("online_cpus") or 0) or _NCPU
            except (KeyError, TypeError, ValueError):
                continue

            counters[c.cid] = (now, total, sys_total, ncpu)
            pct = 0.0
            if prev := self._counters.get(c.cid):
                _, ptotal, psys, _ = prev
                d_total = total - ptotal
                d_sys = sys_total - psys
                if d_sys > 0 and d_total >= 0:
                    pct = d_total / d_sys * ncpu * 100.0
                elif d_total >= 0 and elapsed > 0:
                    # Older daemons omit system_cpu_usage in one-shot mode.
                    # total_usage is in nanoseconds, so fall back to wall time.
                    pct = d_total / (elapsed * 1e9) * 100.0
            nets = d.get("networks") or {}
            blk = {"read": 0, "write": 0}
            for e in ((d.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []):
                op = str(e.get("op", "")).lower()
                if op in blk:
                    blk[op] += int(e.get("value") or 0)
            fresh[c.cid] = {
                "cpu": max(0.0, pct),
                "mem": _mem_used(d),
                "mem_limit": int((d.get("memory_stats") or {}).get("limit") or 0),
                "rx": sum(int((n or {}).get("rx_bytes") or 0) for n in nets.values()),
                "tx": sum(int((n or {}).get("tx_bytes") or 0) for n in nets.values()),
                "blk_read": blk["read"],
                "blk_write": blk["write"],
                "pids": int((d.get("pids_stats") or {}).get("current") or 0),
            }

        self._counters = counters
        self._stats = fresh

    def logs(self, cid: str, tail: int = 400) -> list[str]:
        raw = self._request(
            "GET", f"/containers/{cid}/logs?stdout=1&stderr=1&tail={tail}&timestamps=0",
            timeout=4.0,
        )
        if raw is None:
            return [f"could not read logs: {self.reason}"]
        return _demux(raw)

    # -- writes -------------------------------------------------------------
    def action(self, cid: str, verb: str) -> str:
        """start | stop | restart | pause | unpause. Deliberately no remove,
        no kill, no prune: this is a HUD, not a a way to lose a container."""
        if verb not in ("start", "stop", "restart", "pause", "unpause"):
            return f"refused: {verb}"
        raw = self._request("POST", f"/containers/{cid}/{verb}", timeout=15.0)
        if raw is None:
            return self.reason or f"{verb} failed"
        self._stats_at = 0.0     # force a stats refresh on the next tick
        return f"{verb} ok"


# -----------------------------------------------------------------------------
# stats maths, straight from the Docker CLI's own formulas
# -----------------------------------------------------------------------------

def _mem_used(d: dict) -> int:
    """usage minus cache — the number `docker stats` shows."""
    try:
        m = d["memory_stats"]
        usage = int(m.get("usage") or 0)
        stats = m.get("stats") or {}
        cache = int(stats.get("inactive_file") or stats.get("cache") or 0)
        return max(0, usage - cache)
    except (KeyError, TypeError):
        return 0


def _demux(raw: bytes) -> list[str]:
    """Non-TTY container logs arrive framed: an 8-byte header per chunk, with
    the payload size big-endian in bytes 4-8. TTY containers send raw text."""
    out: list[str] = []
    i, n = 0, len(raw)
    framed = n >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\0\0\0"
    if not framed:
        return raw.decode("utf-8", "replace").splitlines()

    while i + 8 <= n:
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        i += 8
        if size <= 0 or i + size > n:
            break
        out.extend(raw[i:i + size].decode("utf-8", "replace").splitlines())
        i += size
    return out


def _short_time(iso: str | None) -> str:
    """Docker hands back RFC3339 with nanoseconds and a zero value for "never"."""
    if not iso or iso.startswith("0001-01-01"):
        return ""
    try:
        from datetime import datetime
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")[:26] + "+00:00"
                                   if len(iso) > 26 else iso.replace("Z", "+00:00"))
        return t.astimezone().strftime("%d %b %H:%M")
    except (ValueError, TypeError):
        return iso[:16].replace("T", " ")

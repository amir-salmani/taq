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
        self._stats: dict[str, tuple[float, int, int]] = {}
        # cid -> (when, cpu_total_ns, system_total_ns, ncpu), our own baseline
        self._counters: dict[str, tuple[float, int, int, int]] = {}

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
                c.cpu_pct, c.mem_bytes, c.mem_limit = s

        return DockerView(True, "", self.version, containers)

    @staticmethod
    def _parse(c: dict) -> Container:
        names = c.get("Names") or []
        labels = c.get("Labels") or {}
        ports = []
        for p in c.get("Ports") or []:
            pub, priv = p.get("PublicPort"), p.get("PrivatePort")
            ports.append(f"{pub}→{priv}" if pub else str(priv))
        health = None
        status = c.get("Status", "")
        if "(healthy)" in status:
            health = "healthy"
        elif "(unhealthy)" in status:
            health = "unhealthy"
        elif "(health: starting)" in status:
            health = "starting"

        return Container(
            cid=c.get("Id", ""),
            name=(names[0].lstrip("/") if names else c.get("Id", "")[:12]),
            image=(c.get("Image") or "").split("@")[0],
            state=c.get("State", "?"),
            status=status,
            project=labels.get("com.docker.compose.project"),
            ports=", ".join(dict.fromkeys(ports))[:28],
            health=health,
        )

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

        fresh: dict[str, tuple[float, int, int]] = {}
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
            fresh[c.cid] = (max(0.0, pct), _mem_used(d),
                            int((d.get("memory_stats") or {}).get("limit") or 0))

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

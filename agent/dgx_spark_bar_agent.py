#!/usr/bin/env python3
"""dgx-spark-bar-agent — system monitor and power switch for an NVIDIA DGX Spark (GB10).

Stdlib only, no pip install, no root, and — deliberately — no background work:
every number is measured when a client asks for it, and between requests the
agent does nothing at all. Serves:

    GET  /ping     discovery beacon: who am I
    GET  /status   one snapshot plus the recent history of the polls before it
    POST /action   one of a fixed whitelist: reboot, poweroff

There is no authentication, deliberately: the agent is meant for a private
network (a tailnet, or a lab LAN), and a shared secret to copy around was more
friction than the threat it removed. On an untrusted network, restrict BIND to
the tailnet address rather than reaching for a token.

DGX Spark specifics that a generic monitor gets wrong:
  * Memory is UNIFIED (Grace + Blackwell share it). `nvidia-smi` reports
    FB Memory Usage = N/A on GB10, so what a dashboard would label "VRAM used"
    comes from /proc/meminfo. Util%, temp, power and SM clock are real and
    read from nvidia-smi.
  * Because there is no separate VRAM, "the model did not fit" shows up as
    memory-pressure stalls (/proc/pressure/memory) and swap, never as a
    full framebuffer.
  * A Spark that looks busy while drawing ~14-25 W is stuck in a low-power
    state — the single most common reason a demo runs at a quarter speed.

The three rules above (power cap, memory pressure, thermal) and their
thresholds are adapted from spark-doctor by Joey (MIT):
https://github.com/joeynyc/spark-doctor — its numbers come from reported
DGX Spark field behaviour, which is worth more than invented ones.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.parse import urlparse

VERSION = "0.2.0"
CONF_PATH = os.environ.get("SPARKBAR_CONF", "/etc/dgx-spark-bar/agent.conf")

HISTORY_LEN = 60      # the last 60 polls — ~5 minutes at the client's 5 s rate
MIN_INTERVAL = 1.0    # two clients polling at once share one measurement
FRESH_WINDOW = 0.2    # length of the self-timed window on the first poll
STALE_AFTER = 30.0    # older than this and the previous poll is not a baseline

# Every default lives here and nowhere else: conf_int/conf_list fall back to
# this table rather than to a literal at the call site.
DEFAULTS = {
    "PORT": "8765",
    "BIND": "0.0.0.0",
    "DISKS": "/",
    # thresholds -> warning, and CRIT_GPU_TEMP -> critical
    "WARN_DISK_PCT": "85",
    "WARN_GPU_TEMP": "85",
    "CRIT_GPU_TEMP": "90",
}

# Rule thresholds adapted from spark-doctor (MIT, github.com/joeynyc/spark-doctor).
POWER_CAP_UTIL_PCT = 80        # "busy" for the purposes of the low-power rule
POWER_CAP_WATTS = 25           # field reports cluster around a 14 W cap
POWER_CAP_CLOCK_MHZ = 800      # a low SM clock alongside it raises confidence
POWER_CAP_MIN_SAMPLES = 3      # one dip is noise; three polls in a row is a state
MEM_AVAIL_WARN_GB = 16
MEM_AVAIL_CRIT_GB = 8
MEM_AVAIL_WARN_RATIO = 0.15
PSI_FULL_WARN = 0.10           # /proc/pressure/memory "full avg10" — real stalls
PSI_FULL_CRIT = 0.25
SWAP_WARN_GB = 8


# --------------------------------------------------------------------------
# config

def load_conf(path: str) -> dict[str, str]:
    conf = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                conf[key.strip()] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return conf


CONF = load_conf(CONF_PATH)


def conf_list(key: str) -> list[str]:
    raw = CONF.get(key) or DEFAULTS.get(key, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def conf_int(key: str) -> int:
    try:
        return int(CONF[key])
    except (KeyError, TypeError, ValueError):
        return int(DEFAULTS[key])


# --------------------------------------------------------------------------
# small helpers

def run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 127, str(exc)


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Stable per-box id, computed once. Hashed — the raw /etc/machine-id is a
# secret-ish value that identifies the host across networks.
MACHINE_ID = hashlib.sha256(
    (read_text("/etc/machine-id").strip() or socket.gethostname()).encode()
).hexdigest()[:16]

# Thermal zones do not come and go at runtime, so the glob runs once.
THERMAL_ZONES = sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))


# --------------------------------------------------------------------------
# metrics

def cpu_total() -> tuple[int, int]:
    """(busy, total) jiffies from the aggregate line of /proc/stat.

    Per-core numbers are deliberately not collected: nothing displays them."""
    for line in read_text("/proc/stat").splitlines():
        if line.startswith("cpu "):
            values = [int(v) for v in line.split()[1:]]
            if len(values) < 4:
                break
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return total - idle, total
    return 0, 0


def meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        val = rest.strip().split(" ")[0]
        if val.isdigit():
            out[key] = int(val)  # kB
    return out


def pressure(kind: str) -> dict[str, dict[str, float]]:
    """/proc/pressure/<kind>. On a unified-memory box this is the only honest
    "not enough memory" signal — it measures time actually stalled, not bytes."""
    out: dict[str, dict[str, float]] = {}
    for line in read_text(f"/proc/pressure/{kind}").splitlines():
        parts = line.split()
        if not parts:
            continue
        scope, values = parts[0], {}
        for item in parts[1:]:
            key, _, val = item.partition("=")
            value = num(val)
            if value is not None:
                values[key] = value
        if values:
            out[scope] = values
    return out


def gpu_snapshot() -> dict:
    """GB10: memory fields are N/A by design (unified memory) — we don't fake them.

    The one fork on the request path, and the reason a poll is worth caching."""
    fields = "name,utilization.gpu,temperature.gpu,power.draw,clocks.current.sm"
    rc, out = run(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        timeout=4.0,
    )
    if rc != 0 or not out.strip():
        return {"present": False}
    parts = [p.strip() for p in out.strip().splitlines()[0].split(",")]
    while len(parts) < 5:
        parts.append("")
    return {
        "present": True,
        "name": parts[0],
        "utilPct": num(parts[1]),
        "tempC": num(parts[2]),
        "powerW": num(parts[3]),
        "smClockMhz": num(parts[4]),
        "unifiedMemory": True,
        "memoryNote": "GB10 shares memory with the CPU — nvidia-smi reports no FB memory",
    }


def default_iface() -> str:
    for line in read_text("/proc/net/route").splitlines()[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == "00000000":
            return parts[0]
    return ""


def net_counters(iface: str) -> tuple[int, int]:
    if not iface:
        return 0, 0
    base = f"/sys/class/net/{iface}/statistics"
    rx = read_text(f"{base}/rx_bytes").strip()
    tx = read_text(f"{base}/tx_bytes").strip()
    return int(rx or 0), int(tx or 0)


def cpu_temp() -> float | None:
    best: float | None = None
    for path in THERMAL_ZONES:
        raw = read_text(path).strip()
        if not raw.lstrip("-").isdigit():
            continue
        value = int(raw) / 1000.0
        if 0 < value < 150 and (best is None or value > best):
            best = value
    return round(best, 1) if best is not None else None


def disks() -> list[dict]:
    out = []
    for mount in conf_list("DISKS"):
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - (st.f_bfree * st.f_frsize)
        out.append(
            {
                "mount": mount,
                "totalGb": round(total / 1e9, 1),
                "usedGb": round(used / 1e9, 1),
                "freeGb": round(free / 1e9, 1),
                "pct": round(100.0 * used / total, 1) if total else 0.0,
            }
        )
    return out


class Reading(NamedTuple):
    """Counters that only mean anything as a delta against an earlier reading."""

    busy: int
    total: int
    rx: int
    tx: int
    at: float
    iface: str


def take_reading() -> Reading:
    iface = default_iface()
    busy, total = cpu_total()
    rx, tx = net_counters(iface)
    return Reading(busy, total, rx, tx, time.time(), iface)


class Monitor:
    """Measures on demand — there is no sampling thread.

    CPU % and network rates are deltas, so they need two readings. Normally the
    previous /status request supplies the first one, which costs nothing: the
    client already polls every few seconds. If nobody has asked for a while,
    though, that window would span minutes and average away everything worth
    seeing, so the first poll of a session times its own short one instead.

    A snapshot is reused for MIN_INTERVAL so that several clients polling at
    once share one nvidia-smi fork rather than queueing up their own.

    The history exists for the sparkline and for the power-cap rule, which
    needs a state rather than a single dip. It fills while a client watches
    and simply stops when one goes away.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prev: Reading | None = None
        self._cached: dict | None = None
        self.history: deque[dict] = deque(maxlen=HISTORY_LEN)

    def read(self) -> tuple[dict, list[dict]]:
        with self._lock:
            if self._cached and time.time() - self._cached["ts"] < MIN_INTERVAL:
                return self._cached, list(self.history)

            snapshot = self._measure()
            self._cached = snapshot
            gpu = snapshot["gpu"]
            self.history.append(
                {
                    "t": round(snapshot["ts"], 1),
                    "cpu": snapshot["cpu"]["pct"],
                    "gpu": gpu.get("utilPct") or 0.0,
                    # kept per-poll so the power-cap rule can ask for a SUSTAINED
                    # low-power state; not sent to the client, which never draws them
                    "w": gpu.get("powerW"),
                    "mhz": gpu.get("smClockMhz"),
                }
            )
            return snapshot, list(self.history)

    def _measure(self) -> dict:
        cur = take_reading()
        prev = self._prev
        if prev is None or cur.at - prev.at > STALE_AFTER or prev.iface != cur.iface:
            prev = cur                     # no usable baseline — time our own window
            time.sleep(FRESH_WINDOW)
            cur = take_reading()
        self._prev = cur

        elapsed = max(cur.at - prev.at, 0.001)
        total_delta = cur.total - prev.total
        cpu_pct = (
            round(100.0 * (cur.busy - prev.busy) / total_delta, 1)
            if total_delta > 0 else 0.0
        )

        mem = meminfo()
        total_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", 0)
        used_kb = max(total_kb - avail_kb, 0)

        return {
            "ts": cur.at,
            "cpu": {"pct": cpu_pct, "tempC": cpu_temp()},
            "memory": {
                "totalKb": total_kb,
                "usedKb": used_kb,
                "availKb": avail_kb,
                "pct": round(100.0 * used_kb / total_kb, 1) if total_kb else 0.0,
                "swapUsedKb": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
                "pressure": pressure("memory"),
            },
            "gpu": gpu_snapshot(),
            "net": {
                "iface": cur.iface,
                "rxMbs": round(max(cur.rx - prev.rx, 0) / elapsed / 1e6, 2),
                "txMbs": round(max(cur.tx - prev.tx, 0) / elapsed / 1e6, 2),
            },
            # rides the same cache: statvfs on a stuck mount is not something to
            # repeat once per request when the numbers move on a scale of minutes
            "disks": disks(),
        }


MONITOR = Monitor()


# --------------------------------------------------------------------------
# status assembly + level

def uptime_seconds() -> float:
    raw = read_text("/proc/uptime").split()
    return float(raw[0]) if raw else 0.0


def finding(fid: str, severity: str, title: str, detail: str, hint: str = "") -> dict:
    return {"id": fid, "severity": severity, "title": title,
            "detail": detail, "hint": hint}


def rule_power_cap(history: list[dict]) -> list[dict]:
    """Busy GPU + very low watts = the Spark is parked in a low-power state.
    Adapted from spark-doctor's power.low_draw_under_load."""
    busy_and_starved = [
        s for s in history
        if (s.get("gpu") or 0) >= POWER_CAP_UTIL_PCT
        and s.get("w") is not None and s["w"] <= POWER_CAP_WATTS
    ]
    if len(busy_and_starved) < POWER_CAP_MIN_SAMPLES:
        return []
    low_clock = any(
        s.get("mhz") is not None and s["mhz"] <= POWER_CAP_CLOCK_MHZ
        for s in busy_and_starved
    )
    worst = min(busy_and_starved, key=lambda s: s["w"])
    return [finding(
        "power.low_draw_under_load",
        "critical" if low_clock else "warning",
        "GPU busy but drawing almost no power",
        f"{len(busy_and_starved)} polls with util ≥ {POWER_CAP_UTIL_PCT}% at "
        f"≤ {POWER_CAP_WATTS} W (low: {worst['w']} W, {worst.get('mhz')} MHz)",
        "Power the Spark down, unplug the brick for 60 s, boot again.",
    )]


def rule_memory(fast: dict) -> list[dict]:
    """On unified memory, exhaustion shows up as stalls and swap, not as a
    full framebuffer. Thresholds from spark-doctor's memory rule."""
    mem = fast.get("memory") or {}
    total_gb = mem.get("totalKb", 0) / 1e6
    avail_gb = mem.get("availKb", 0) / 1e6
    swap_gb = mem.get("swapUsedKb", 0) / 1e6
    psi_full = ((mem.get("pressure") or {}).get("full") or {}).get("avg10")
    out = []

    if total_gb:
        if avail_gb < MEM_AVAIL_CRIT_GB:
            out.append(finding("memory.exhausted", "critical", "Almost no memory left",
                               f"{avail_gb:.1f} GB available of {total_gb:.0f} GB",
                               "Stop whatever is holding it — a model still resident, "
                               "most likely."))
        elif avail_gb < MEM_AVAIL_WARN_GB or avail_gb / total_gb < MEM_AVAIL_WARN_RATIO:
            out.append(finding("memory.low", "warning", "Memory running low",
                               f"{avail_gb:.1f} GB available of {total_gb:.0f} GB"))
    if psi_full is not None and psi_full > PSI_FULL_WARN:
        out.append(finding(
            "memory.pressure",
            "critical" if psi_full > PSI_FULL_CRIT else "warning",
            "Memory pressure — everything is stalling",
            f"/proc/pressure/memory full avg10 = {psi_full:.2f}",
            "Whatever is running does not fit; it is waiting on memory.",
        ))
    if swap_gb > SWAP_WARN_GB:
        out.append(finding("memory.swapping", "warning", "Heavy swap use",
                           f"{swap_gb:.1f} GB of swap in use"))
    return out


def rule_thermal(fast: dict) -> list[dict]:
    temp = (fast.get("gpu") or {}).get("tempC")
    if temp is None:
        return []
    crit = conf_int("CRIT_GPU_TEMP")
    warn = conf_int("WARN_GPU_TEMP")
    if temp >= crit:
        return [finding("thermal.critical", "critical", "GPU is very hot",
                        f"{temp} °C", "Stop the workload and check airflow.")]
    if temp >= warn:
        return [finding("thermal.warm", "warning", "GPU running hot", f"{temp} °C")]
    return []


def rule_disks(fast: dict) -> list[dict]:
    limit = conf_int("WARN_DISK_PCT")
    return [
        finding("disk.filling_up", "warning", f"Disk {d['mount']} is {d['pct']}% full",
                f"{d['freeGb']} GB free of {d['totalGb']} GB")
        for d in fast["disks"] if d["pct"] >= limit
    ]


def evaluate(fast: dict, history: list[dict]) -> tuple[str, list[dict]]:
    findings = (
        rule_power_cap(history)
        + rule_memory(fast)
        + rule_thermal(fast)
        + rule_disks(fast)
    )
    if any(f["severity"] == "critical" for f in findings):
        return "error", findings
    return ("warn" if findings else "ok"), findings


def build_status() -> dict:
    fast, history = MONITOR.read()
    level, findings = evaluate(fast, history)

    return {
        "app": "dgx-spark-bar",
        "version": VERSION,
        "host": socket.gethostname(),
        "machineId": MACHINE_ID,
        "ts": fast["ts"],
        "uptimeSec": round(uptime_seconds()),
        "level": level,
        "findings": findings,
        "cpu": fast["cpu"],
        "memory": fast["memory"],
        "gpu": fast["gpu"],
        "net": fast["net"],
        "disks": fast["disks"],
        # only the two series anyone draws; w/mhz stay on the agent for the rule
        "history": [{"t": s["t"], "cpu": s["cpu"], "gpu": s["gpu"]} for s in history],
        "actions": sorted(ACTIONS),
    }


# --------------------------------------------------------------------------
# actions — a fixed whitelist, never a shell string from the client

ACTIONS = {
    "reboot": ["sudo", "-n", "/usr/sbin/reboot"],
    "poweroff": ["sudo", "-n", "/usr/sbin/poweroff"],
}


def perform(action: str) -> tuple[int, dict]:
    cmd = ACTIONS.get(action)
    if cmd is None:
        return 400, {"ok": False, "error": f"unknown action: {action}"}

    # Both actions take the box away mid-response: answer first, then pull the rug.
    timer = threading.Timer(1.0, run, args=(cmd,), kwargs={"timeout": 30.0})
    timer.daemon = True
    timer.start()
    return 200, {"ok": True, "action": action, "deferred": True}


# --------------------------------------------------------------------------
# HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"dgx-spark-bar/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # journald gets one line per action only
        pass

    # -- helpers -----------------------------------------------------------
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/ping":
            self._send(200, {
                "app": "dgx-spark-bar",
                "version": VERSION,
                "host": socket.gethostname(),
                "machineId": MACHINE_ID,
            })
        elif parsed.path in ("/", "/status"):
            self._send(200, build_status())
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/action":
            self._send(404, {"ok": False, "error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": "bad json"})
            return

        action = str(payload.get("action", ""))
        print(f"[dgx-spark-bar] action requested: {action} from {self.client_address[0]}", flush=True)
        code, result = perform(action)
        self._send(code, result)


def main() -> None:
    port = conf_int("PORT")
    bind = CONF["BIND"]
    print(f"[dgx-spark-bar] {VERSION} listening on {bind}:{port} (no auth — private network only)",
          flush=True)
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()

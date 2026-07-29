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
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.parse import urlparse

VERSION = "0.3.0"
CONF_PATH = os.environ.get("SPARKBAR_CONF", "/etc/dgx-spark-bar/agent.conf")

HISTORY_LEN = 60      # the last 60 polls — ~5 minutes at the client's 5 s rate
MIN_INTERVAL = 1.0    # two clients polling at once share one measurement
FRESH_WINDOW = 0.2    # length of the self-timed window on the first poll
STALE_AFTER = 30.0    # older than this and the previous poll is not a baseline
MAX_BODY_BYTES = 4096  # the only POST body is {"action": "..."} — tens of bytes

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
    # drop-in actions: every executable file here becomes a button (see Plugins)
    "PLUGINS_DIR": "/etc/dgx-spark-bar/plugins",
    "PLUGINS_LOG_DIR": "/var/lib/dgx-spark-bar/plugin-logs",
}

# Rule thresholds adapted from spark-doctor (MIT, github.com/joeynyc/spark-doctor).
POWER_CAP_UTIL_PCT = 80        # "busy" for the purposes of the low-power rule
POWER_CAP_WATTS = 25           # field reports cluster around a 14 W cap
POWER_CAP_CLOCK_MHZ = 800      # a low SM clock alongside it raises confidence
POWER_CAP_MIN_SAMPLES = 3      # one dip is noise; three polls in a row is a state
POWER_CAP_MIN_SECONDS = 10.0   # ...and that run has to span real time. History
                               # grows once per client poll, so without this floor
                               # N Macs watching would shrink "three in a row"
                               # from ~10 s of evidence to ~10/N s.
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
                # Trailing comments are cut, not kept: agent.conf.example is
                # densely commented and teaches the habit, and without this
                # `CRIT_GPU_TEMP=95  # runs hot` parses as a string that conf_int
                # rejects — silently restoring 90 while the file on disk says 95.
                # No value here can legitimately contain '#'.
                val, _, _ = val.partition("#")
                conf[key.strip()] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return conf


CONF = load_conf(CONF_PATH)


def conf_str(key: str) -> str:
    """The one place a configured value is resolved. An explicitly empty value
    (`BIND=` on a line of its own) falls back rather than winning: an empty BIND
    would bind every interface, which is the opposite of what someone editing
    that key wants."""
    return CONF.get(key) or DEFAULTS[key]


def _split_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def conf_list(key: str) -> list[str]:
    # A value that is non-empty but parses to nothing (`DISKS=,`) falls back too,
    # so the root filesystem is always reported and the disk rule always has input.
    return _split_list(conf_str(key)) or _split_list(DEFAULTS[key])


def conf_int(key: str) -> int:
    try:
        return int(conf_str(key))
    except ValueError:
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
    """Counters that only mean anything as a delta against an earlier reading.

    Two clocks on purpose: `at` is monotonic and is the only one the deltas and
    the staleness test are allowed to use, because a Spark with no RTC steps its
    wall clock the moment timesyncd first answers — and a backwards step would
    otherwise read as "no time passed" and turn a 5 s window into a 1 ms one.
    `wall` is what the snapshot reports to a client, which wants a real date."""

    busy: int
    total: int
    rx: int
    tx: int
    at: float
    wall: float
    iface: str


def take_reading() -> Reading:
    iface = default_iface()
    busy, total = cpu_total()
    rx, tx = net_counters(iface)
    return Reading(busy, total, rx, tx, time.monotonic(), time.time(), iface)


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
        # Stamped when _measure RETURNS, not from the snapshot's own ts: ts is
        # taken before the nvidia-smi fork, so reusing it here would spend the
        # whole MIN_INTERVAL window on the very work the window exists to share.
        self._cached_at = 0.0
        self.history: deque[dict] = deque(maxlen=HISTORY_LEN)

    def read(self) -> tuple[dict, list[dict]]:
        with self._lock:
            if self._cached and time.monotonic() - self._cached_at < MIN_INTERVAL:
                return self._cached, list(self.history)

            snapshot = self._measure()
            self._cached = snapshot
            self._cached_at = time.monotonic()
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
            "ts": cur.wall,
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
            # Rides the same cache so concurrent clients share one pass. Note it
            # runs under Monitor._lock: statvfs on a hung NFS mount blocks
            # uninterruptibly and will hold every other /status behind it, so
            # DISKS should list local mounts.
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
    # The longest UNBROKEN run, not a count across the whole history: three dips
    # scattered over five minutes are three dips, and calling that a power cap
    # sends someone off to unplug a working machine.
    longest: list[dict] = []
    run_: list[dict] = []
    for s in history:
        if ((s.get("gpu") or 0) >= POWER_CAP_UTIL_PCT
                and s.get("w") is not None and s["w"] <= POWER_CAP_WATTS):
            run_.append(s)
            if len(run_) > len(longest):
                longest = list(run_)
        else:
            run_ = []
    busy_and_starved = longest
    if len(busy_and_starved) < POWER_CAP_MIN_SAMPLES:
        return []
    if busy_and_starved[-1]["t"] - busy_and_starved[0]["t"] < POWER_CAP_MIN_SECONDS:
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
        f"{len(busy_and_starved)} polls in a row with util ≥ {POWER_CAP_UTIL_PCT}% "
        f"at ≤ {POWER_CAP_WATTS} W (low: {worst['w']} W, "
        f"{worst['mhz'] if worst.get('mhz') is not None else '?'} MHz)",
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
        for d in (fast.get("disks") or []) if d["pct"] >= limit
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
        "plugins": PLUGINS.list(),
    }


# --------------------------------------------------------------------------
# actions — a fixed whitelist, never a shell string from the client

ACTIONS = {
    "reboot": ["sudo", "-n", "/usr/sbin/reboot"],
    "poweroff": ["sudo", "-n", "/usr/sbin/poweroff"],
}


def run_action(action: str, cmd: list[str]) -> None:
    """A reboot that works is never seen again, so only the failure needs saying —
    but it needs saying somewhere, or "the button does nothing" has no trail at
    all. The usual cause is the sudoers drop-in not surviving an upgrade."""
    rc, out = run(cmd, timeout=30.0)
    if rc != 0:
        print(f"[dgx-spark-bar] {action} failed (rc={rc}): {out.strip()}", flush=True)


def perform(action: str) -> tuple[int, dict]:
    if action.startswith("plugin:"):
        return PLUGINS.start(action[len("plugin:"):])

    cmd = ACTIONS.get(action)
    if cmd is None:
        return 400, {"ok": False, "error": f"unknown action: {action}"}

    # Both actions take the box away mid-response: answer first, then pull the rug.
    timer = threading.Timer(1.0, run_action, args=(action, cmd))
    timer.daemon = True
    timer.start()
    return 200, {"ok": True, "action": action, "deferred": True}


# --------------------------------------------------------------------------
# plugins — drop-in actions: executable files in PLUGINS_DIR
#
# The fixed whitelist above is the product's own two switches. Everything
# box-specific (a deploy, a demo reset, a cache warm) belongs to the OPERATOR,
# not to this repo — so it arrives as a plugin: one executable file dropped
# into PLUGINS_DIR becomes one button in every connected client. The agent
# stays generic; the buttons don't.
#
#   * the filename is the action name (`zolli-deploy` -> action "plugin:zolli-deploy")
#   * `# desc: <text>` in the first lines becomes the caption clients show
#   * `# confirm: yes` asks clients to confirm before running
#
# A plugin runs as the agent's user, detached (setsid), with its output in
# PLUGINS_LOG_DIR/<name>.log — `GET /plugin-log?name=X` serves the tail, so a
# 20-minute build is watchable from the panel. One instance per plugin at a
# time: starting a running one answers 409 rather than stacking builds.
#
# There is no auth, so a plugin is remote code execution for anyone who can
# reach the port. Two things keep that honest: the dir itself is expected to
# be writable by root only, and the agent refuses files that are group- or
# world-writable — "can reach the port" must never become "can edit what the
# port runs". No timeout on purpose: a legitimate build outlives any number
# we would pick, and the log shows a hung one.

PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PLUGIN_HEAD_BYTES = 4096          # metadata lives in the first lines only
PLUGIN_LOG_TAIL_MAX = 65536


def _plugin_meta(path: str) -> dict:
    desc, confirm = "", False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh.read(PLUGIN_HEAD_BYTES).splitlines():
                lowered = line.strip().lower()
                if lowered.startswith("# desc:"):
                    desc = line.strip()[7:].strip()
                elif lowered.startswith("# confirm:"):
                    confirm = lowered[10:].strip() in ("yes", "true", "1")
    except OSError:
        pass
    return {"desc": desc, "confirm": confirm}


class Plugins:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, subprocess.Popen] = {}
        self._started: dict[str, float] = {}
        self._last: dict[str, dict] = {}   # name -> {"exit": int, "endedAt": float, "ms": int}

    def _dir(self) -> str:
        return conf_str("PLUGINS_DIR")

    def log_path(self, name: str) -> str:
        return os.path.join(conf_str("PLUGINS_LOG_DIR"), f"{name}.log")

    def _safe_path(self, name: str) -> str | None:
        """The one gate every use of a plugin file goes through."""
        if not PLUGIN_NAME_RE.match(name):
            return None
        path = os.path.join(self._dir(), name)
        try:
            st = os.stat(path)
        except OSError:
            return None
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            return None
        if st.st_mode & 0o022:     # group/world-writable — refuse, see header
            return None
        return path

    def _reap(self) -> None:
        # under self._lock
        for name in list(self._running):
            proc = self._running[name]
            if proc.poll() is None:
                continue
            self._last[name] = {
                "exit": proc.returncode,
                "endedAt": time.time(),
                "ms": int((time.monotonic() - self._started[name]) * 1000),
            }
            del self._running[name]
            del self._started[name]

    def list(self) -> list[dict]:
        try:
            names = sorted(os.listdir(self._dir()))
        except OSError:
            names = []
        with self._lock:
            self._reap()
            out = []
            for name in names:
                path = self._safe_path(name)
                if path is None:
                    continue
                meta = _plugin_meta(path)
                last = self._last.get(name)
                out.append({
                    "name": name,
                    "desc": meta["desc"],
                    "confirm": meta["confirm"],
                    "running": name in self._running,
                    "lastExit": last["exit"] if last else None,
                    "lastEndedAt": last["endedAt"] if last else None,
                    "lastMs": last["ms"] if last else None,
                })
            return out

    def start(self, name: str) -> tuple[int, dict]:
        path = self._safe_path(name)
        if path is None:
            return 400, {"ok": False, "error": f"unknown plugin: {name}"}
        with self._lock:
            self._reap()
            if name in self._running:
                return 409, {"ok": False, "error": f"plugin already running: {name}"}
            log_dir = conf_str("PLUGINS_LOG_DIR")
            try:
                os.makedirs(log_dir, exist_ok=True)
                log_fh = open(self.log_path(name), "ab")
            except OSError as exc:
                return 500, {"ok": False, "error": f"cannot open log: {exc}"}
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log_fh.write(f"\n===== [{stamp}] plugin {name} started =====\n".encode())
            log_fh.flush()
            try:
                proc = subprocess.Popen(
                    [path],
                    stdout=log_fh, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,   # survives an agent restart mid-build
                    cwd="/",
                )
            except OSError as exc:
                log_fh.close()
                return 500, {"ok": False, "error": f"cannot start: {exc}"}
            finally:
                # Popen holds its own reference; ours would leak one fd per run.
                log_fh.close()
            self._running[name] = proc
            self._started[name] = time.monotonic()
        print(f"[dgx-spark-bar] plugin started: {name} (pid {proc.pid})", flush=True)
        return 200, {"ok": True, "action": f"plugin:{name}", "pid": proc.pid}

    def log_tail(self, name: str, limit: int) -> tuple[int, bytes]:
        if not PLUGIN_NAME_RE.match(name):
            return 400, b"bad plugin name"
        limit = max(1, min(limit, PLUGIN_LOG_TAIL_MAX))
        try:
            with open(self.log_path(name), "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(size - limit, 0))
                return 200, fh.read()
        except OSError:
            return 404, b"no log yet"


PLUGINS = Plugins()


# --------------------------------------------------------------------------
# HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"dgx-spark-bar/{VERSION}"
    # There is no auth, so anyone who can reach the port can open a socket and
    # stall. A read timeout keeps a half-sent request from parking a thread.
    timeout = 10.0

    def log_message(self, fmt: str, *args) -> None:  # journald gets one line per action only
        pass

    def log_error(self, fmt: str, *args) -> None:
        """Not covered by the silence above. BaseHTTPRequestHandler routes errors
        through log_message, so overriding that alone also swallowed every 400,
        404 and malformed request line — on a box whose only diagnostic is
        journald."""
        print(f"[dgx-spark-bar] {self.address_string()} {fmt % args}", flush=True)

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
            try:
                self._send(200, build_status())
            except Exception as exc:  # noqa: BLE001 — see below
                # One unreadable metric must not drop the socket. Without this
                # /status dies silently while /ping keeps answering 200, which is
                # exactly the shape the troubleshooting docs read as "discovery,
                # not the agent" — pointing the operator at the wrong half.
                print(f"[dgx-spark-bar] /status failed: {exc!r}", flush=True)
                self._send(500, {"ok": False, "error": "status unavailable"})
        elif parsed.path == "/plugin-log":
            params = dict(
                part.split("=", 1) for part in parsed.query.split("&") if "=" in part
            )
            try:
                limit = int(params.get("bytes", "8192"))
            except ValueError:
                limit = 8192
            code, body = PLUGINS.log_tail(params.get("name", ""), limit)
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/action":
            self._send(404, {"ok": False, "error": "not found"})
            return

        # Content-Length is attacker-supplied on a service with no auth. Unchecked,
        # "abc" raises out of the handler and drops the connection with no status,
        # and "-1" turns rfile.read into read-until-EOF — one parked thread per
        # request. The only body this route accepts is a short JSON object.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY_BYTES:
            self._send(400, {"ok": False, "error": "bad content-length"})
            return
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
    # install.sh asks for this to fill the mDNS TXT record, so the version has
    # exactly one home — the constant above — and no second reader parses for it.
    if "--version" in sys.argv[1:]:
        print(VERSION)
        return

    port = conf_int("PORT")
    bind = conf_str("BIND")
    try:
        server = ThreadingHTTPServer((bind, port), Handler)
    except OSError as exc:
        # The unit is Restart=always/RestartSec=3, so a bad BIND becomes a silent
        # loop. Say why once, in words: the usual cause is BIND set to a tailnet
        # address that tailscaled has not brought up yet, because the unit waits
        # on network-online.target and that does not cover tailscale0.
        print(f"[dgx-spark-bar] cannot listen on {bind}:{port} — {exc}", flush=True)
        raise SystemExit(1)

    print(f"[dgx-spark-bar] {VERSION} listening on {bind}:{port} (no auth — private network only)",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""dgx-spark-bar-agent — monitoring + control agent for an NVIDIA DGX Spark (GB10).

Stdlib only, no pip install, no root. Serves:

    GET  /ping     discovery beacon: who am I
    GET  /status   full snapshot + short history ring buffer
    GET  /         the same snapshot as a phone-friendly HTML page
    POST /action   one of a fixed whitelist of commands

There is no authentication, deliberately: the agent is meant for a private
network (a tailnet, or a lab LAN), and a shared secret to copy around was more
friction than the threat it removed. On an untrusted network, restrict BIND to
the tailnet address rather than reaching for a token.

DGX Spark specifics that a generic monitor gets wrong:
  * Memory is UNIFIED (Grace + Blackwell share it). `nvidia-smi` reports
    FB Memory Usage = N/A on GB10, so "VRAM used" comes from /proc/meminfo
    plus ollama's own per-model `size_vram`. Util%, temp, power, SM clock
    are real and read from nvidia-smi.
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
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

VERSION = "0.1.0"
CONF_PATH = os.environ.get("SPARKBAR_CONF", "/etc/dgx-spark-bar/agent.conf")

SAMPLE_INTERVAL = 3.0      # seconds between ring-buffer samples
HISTORY_LEN = 60           # ~3 minutes of sparkline
SLOW_TTL = 3.0             # cache TTL for docker/ollama/disk/jobs probes

DEFAULTS = {
    "PORT": "8765",
    "BIND": "0.0.0.0",
    "OLLAMA_URL": "http://127.0.0.1:11434",
    "COMPOSE_FILE": "",
    "DISKS": "/",
    "JOB_LOGS": "",
    "JOB_PATTERNS": "",
    "WEBUI_URL": "",
    # thresholds -> warning level
    "WARN_DISK_PCT": "85",
    "WARN_GPU_TEMP": "85",
    "CRIT_GPU_TEMP": "90",
}

# Rule thresholds adapted from spark-doctor (MIT, github.com/joeynyc/spark-doctor).
POWER_CAP_UTIL_PCT = 80        # "busy" for the purposes of the low-power rule
POWER_CAP_WATTS = 25           # field reports cluster around a 14 W cap
POWER_CAP_CLOCK_MHZ = 800      # a low SM clock alongside it raises confidence
POWER_CAP_MIN_SAMPLES = 3      # one dip is noise; three in a row is a state
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
    raw = CONF.get(key, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def conf_int(key: str, fallback: int) -> int:
    try:
        return int(CONF.get(key, ""))
    except (TypeError, ValueError):
        return fallback


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


def machine_id() -> str:
    """Stable per-box id. Hashed — the raw /etc/machine-id is a secret-ish value."""
    raw = read_text("/etc/machine-id").strip() or socket.gethostname()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# fast metrics (sampled on a thread)

def cpu_times() -> dict[str, tuple[int, int]]:
    """{'cpu': (busy, total), 'cpu0': (...), ...} from /proc/stat."""
    out: dict[str, tuple[int, int]] = {}
    for line in read_text("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        parts = line.split()
        name, values = parts[0], [int(v) for v in parts[1:]]
        if len(values) < 4:
            continue
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        out[name] = (total - idle, total)
    return out


def cpu_percents(prev: dict, cur: dict) -> tuple[float, list[float]]:
    def pct(key: str) -> float:
        if key not in prev or key not in cur:
            return 0.0
        busy_d = cur[key][0] - prev[key][0]
        total_d = cur[key][1] - prev[key][1]
        return round(100.0 * busy_d / total_d, 1) if total_d > 0 else 0.0

    cores = [pct(k) for k in sorted(cur) if k != "cpu" and k.startswith("cpu")]
    return pct("cpu"), cores


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
    """GB10: memory fields are N/A by design (unified memory) — we don't fake them."""
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
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        raw = read_text(f"{zone}/temp").strip()
        if not raw.lstrip("-").isdigit():
            continue
        value = int(raw) / 1000.0
        if 0 < value < 150 and (best is None or value > best):
            best = value
    return round(best, 1) if best is not None else None


class Sampler(threading.Thread):
    """Keeps the fast-moving numbers plus a short history, so the client can
    draw a sparkline without any storage on either side."""

    daemon = True

    def __init__(self) -> None:
        super().__init__(name="dgx-spark-bar-sampler")
        self.lock = threading.Lock()
        self.history: deque[dict] = deque(maxlen=HISTORY_LEN)
        self.latest: dict = {}
        self._prev_cpu = cpu_times()
        self._iface = default_iface()
        self._prev_net = net_counters(self._iface)
        self._prev_ts = time.time()

    def run(self) -> None:
        while True:
            time.sleep(SAMPLE_INTERVAL)
            try:
                self._sample()
            except Exception as exc:  # a sampler must never take the agent down
                with self.lock:
                    self.latest = {"error": f"{type(exc).__name__}: {exc}"}

    def _sample(self) -> None:
        now = time.time()
        cur_cpu = cpu_times()
        cpu_pct, cores = cpu_percents(self._prev_cpu, cur_cpu)
        self._prev_cpu = cur_cpu

        iface = default_iface() or self._iface
        cur_net = net_counters(iface)
        elapsed = max(now - self._prev_ts, 0.001)
        if iface != self._iface:
            rx_rate = tx_rate = 0.0
        else:
            rx_rate = max(cur_net[0] - self._prev_net[0], 0) / elapsed / 1e6
            tx_rate = max(cur_net[1] - self._prev_net[1], 0) / elapsed / 1e6
        self._iface, self._prev_net, self._prev_ts = iface, cur_net, now

        mem = meminfo()
        total_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", 0)
        used_kb = max(total_kb - avail_kb, 0)
        mem_pct = round(100.0 * used_kb / total_kb, 1) if total_kb else 0.0

        gpu = gpu_snapshot()
        load = read_text("/proc/loadavg").split()[:3]

        snapshot = {
            "ts": now,
            "cpu": {
                "pct": cpu_pct,
                "cores": cores,
                "loadavg": [num(v) or 0.0 for v in load],
                "tempC": cpu_temp(),
            },
            "memory": {
                "totalKb": total_kb,
                "usedKb": used_kb,
                "availKb": avail_kb,
                "pct": mem_pct,
                "swapTotalKb": mem.get("SwapTotal", 0),
                "swapUsedKb": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
                "pressure": pressure("memory"),
            },
            "gpu": gpu,
            "net": {
                "iface": iface,
                "rxMbs": round(rx_rate, 2),
                "txMbs": round(tx_rate, 2),
            },
        }

        with self.lock:
            self.latest = snapshot
            self.history.append(
                {
                    "t": round(now, 1),
                    "cpu": cpu_pct,
                    "gpu": gpu.get("utilPct") or 0.0,
                    "mem": mem_pct,
                    "rx": round(rx_rate, 2),
                    # kept per-sample so the power-cap rule can ask for a
                    # SUSTAINED low-power state rather than a single dip
                    "w": gpu.get("powerW"),
                    "mhz": gpu.get("smClockMhz"),
                }
            )

    def read(self) -> tuple[dict, list[dict]]:
        with self.lock:
            return dict(self.latest), list(self.history)


# --------------------------------------------------------------------------
# slow metrics (probed on demand, briefly cached)

_slow_cache: dict = {"ts": 0.0, "value": {}}
_slow_lock = threading.Lock()


def disks() -> list[dict]:
    out = []
    for mount in conf_list("DISKS") or ["/"]:
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


def docker_containers() -> list[dict]:
    rc, out = run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=6.0)
    if rc != 0:
        return []
    containers = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(
            {
                "name": row.get("Names", ""),
                "image": row.get("Image", ""),
                "state": row.get("State", ""),
                "status": row.get("Status", ""),
            }
        )
    return containers


def ollama_state() -> dict:
    base = CONF.get("OLLAMA_URL", "").rstrip("/")
    if not base:
        return {"configured": False}

    def fetch(path: str):
        try:
            with urlopen(f"{base}{path}", timeout=3.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            return None

    running = fetch("/api/ps")
    if running is None:
        return {"configured": True, "reachable": False}
    tags = fetch("/api/tags") or {}
    loaded = []
    for model in running.get("models", []) or []:
        loaded.append(
            {
                "name": model.get("name", ""),
                # size_vram on GB10 is unified memory, not dedicated VRAM
                "memGb": round((model.get("size_vram") or 0) / 1e9, 1),
                "expiresAt": model.get("expires_at", ""),
            }
        )
    return {
        "configured": True,
        "reachable": True,
        "loaded": loaded,
        "modelCount": len(tags.get("models", []) or []),
    }


def jobs() -> list[dict]:
    """Long-running work worth watching: configured process patterns + log tails."""
    out = []
    for pattern in conf_list("JOB_PATTERNS"):
        rc, res = run(["pgrep", "-af", pattern], timeout=4.0)
        lines = [ln for ln in res.splitlines() if ln.strip()] if rc == 0 else []
        out.append({"kind": "process", "name": pattern, "running": bool(lines),
                    "count": len(lines), "detail": lines[:3]})
    for path in conf_list("JOB_LOGS"):
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                fh.seek(max(size - 2000, 0))
                tail = fh.read().decode("utf-8", errors="replace")
            tail_lines = [ln for ln in tail.replace("\r", "\n").splitlines() if ln.strip()]
            out.append(
                {
                    "kind": "log",
                    "name": os.path.basename(path),
                    "path": path,
                    "mtime": os.path.getmtime(path),
                    "tail": tail_lines[-4:],
                }
            )
        except OSError:
            continue
    return out


def slow_metrics() -> dict:
    with _slow_lock:
        if time.time() - _slow_cache["ts"] < SLOW_TTL:
            return _slow_cache["value"]
    value = {
        "disks": disks(),
        "docker": docker_containers(),
        "ollama": ollama_state(),
        "jobs": jobs(),
    }
    with _slow_lock:
        _slow_cache["ts"] = time.time()
        _slow_cache["value"] = value
    return value


def tailscale_ip() -> str:
    rc, out = run(["tailscale", "ip", "-4"], timeout=3.0)
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1, no packet is sent
            return sock.getsockname()[0]
    except OSError:
        return ""


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
        f"{len(busy_and_starved)} samples with util ≥ {POWER_CAP_UTIL_PCT}% at "
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
                               "Unload a model (ollama stop) or restart the stack."))
        elif avail_gb < MEM_AVAIL_WARN_GB or avail_gb / total_gb < MEM_AVAIL_WARN_RATIO:
            out.append(finding("memory.low", "warning", "Memory running low",
                               f"{avail_gb:.1f} GB available of {total_gb:.0f} GB"))
    if psi_full is not None and psi_full > PSI_FULL_WARN:
        out.append(finding(
            "memory.pressure",
            "critical" if psi_full > PSI_FULL_CRIT else "warning",
            "Memory pressure — everything is stalling",
            f"/proc/pressure/memory full avg10 = {psi_full:.2f}",
            "The model does not fit; inference is waiting on memory.",
        ))
    if swap_gb > SWAP_WARN_GB:
        out.append(finding("memory.swapping", "warning", "Heavy swap use",
                           f"{swap_gb:.1f} GB of swap in use"))
    return out


def rule_thermal(fast: dict) -> list[dict]:
    temp = (fast.get("gpu") or {}).get("tempC")
    if temp is None:
        return []
    crit = conf_int("CRIT_GPU_TEMP", 90)
    warn = conf_int("WARN_GPU_TEMP", 85)
    if temp >= crit:
        return [finding("thermal.critical", "critical", "GPU is very hot",
                        f"{temp} °C", "Stop the workload and check airflow.")]
    if temp >= warn:
        return [finding("thermal.warm", "warning", "GPU running hot", f"{temp} °C")]
    return []


def rule_services(slow: dict) -> list[dict]:
    out = []
    for container in slow["docker"]:
        if container["state"] != "running":
            out.append(finding("docker.container_down", "warning",
                               f"Container {container['name']} is {container['state']}",
                               container["status"],
                               "Restart the stack from the menu."))
    if slow["ollama"].get("configured") and not slow["ollama"].get("reachable"):
        out.append(finding("ollama.unreachable", "critical", "Ollama is not answering",
                           CONF.get("OLLAMA_URL", ""),
                           "Restart the stack from the menu."))
    return out


def rule_disks(slow: dict) -> list[dict]:
    limit = conf_int("WARN_DISK_PCT", 85)
    return [
        finding("disk.filling_up", "warning", f"Disk {d['mount']} is {d['pct']}% full",
                f"{d['freeGb']} GB free of {d['totalGb']} GB")
        for d in slow["disks"] if d["pct"] >= limit
    ]


def evaluate(fast: dict, slow: dict, history: list[dict]) -> tuple[str, list[dict]]:
    findings = (
        rule_power_cap(history)
        + rule_memory(fast)
        + rule_thermal(fast)
        + rule_services(slow)
        + rule_disks(slow)
    )
    if any(f["severity"] == "critical" for f in findings):
        return "error", findings
    return ("warn" if findings else "ok"), findings


def build_status() -> dict:
    fast, history = SAMPLER.read()
    slow = slow_metrics()
    level, findings = evaluate(fast, slow, history)

    return {
        "app": "dgx-spark-bar",
        "version": VERSION,
        "host": socket.gethostname(),
        "machineId": machine_id(),
        "ts": time.time(),
        "uptimeSec": round(uptime_seconds()),
        "level": level,
        "findings": findings,
        "cpu": fast.get("cpu", {}),
        "memory": fast.get("memory", {}),
        "gpu": fast.get("gpu", {}),
        "net": {
            **fast.get("net", {}),
            "tailscaleIp": tailscale_ip(),
            "lanIp": lan_ip(),
        },
        "disks": slow["disks"],
        "docker": slow["docker"],
        "ollama": slow["ollama"],
        "jobs": slow["jobs"],
        "history": history,
        "actions": sorted(ACTIONS),
        "webUiUrl": CONF.get("WEBUI_URL", ""),
    }


# --------------------------------------------------------------------------
# actions — a fixed whitelist, never a shell string from the client

def _compose_cmd(*args: str) -> list[str]:
    compose = CONF.get("COMPOSE_FILE", "")
    if not compose:
        return []
    return ["docker", "compose", "-f", compose, *args]


ACTIONS = {
    "restart-stack": lambda: _compose_cmd("restart"),
    "reboot": lambda: ["sudo", "-n", "/usr/sbin/reboot"],
    "poweroff": lambda: ["sudo", "-n", "/usr/sbin/poweroff"],
}

DEFERRED = {"reboot", "poweroff"}  # answer first, then pull the rug


def perform(action: str) -> tuple[int, dict]:
    if action not in ACTIONS:
        return 400, {"ok": False, "error": f"unknown action: {action}"}
    cmd = ACTIONS[action]()
    if not cmd:
        return 400, {"ok": False, "error": f"action {action} is not configured"}

    if action in DEFERRED:
        def later() -> None:
            time.sleep(1.0)
            run(cmd, timeout=30.0)

        threading.Thread(target=later, daemon=True).start()
        return 200, {"ok": True, "action": action, "deferred": True}

    rc, out = run(cmd, timeout=120.0)
    return (200 if rc == 0 else 500), {
        "ok": rc == 0,
        "action": action,
        "exitCode": rc,
        "output": out[-4000:],
    }


# --------------------------------------------------------------------------
# HTML view (phone-friendly, no JS framework, auto-refresh)

def human_uptime(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def render_html(status: dict) -> str:
    def esc(value: object) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    gpu = status.get("gpu", {})
    mem = status.get("memory", {})
    cpu = status.get("cpu", {})
    color = {"ok": "#39d353", "warn": "#e3b341", "error": "#f85149"}[status["level"]]

    rows = [
        ("GPU", f"{gpu.get('name', '—')} · {gpu.get('utilPct', 0)}% · "
                f"{gpu.get('tempC', '—')}°C · {gpu.get('powerW', '—')} W"),
        ("CPU", f"{cpu.get('pct', 0)}% · load {', '.join(str(v) for v in cpu.get('loadavg', []))}"
                f" · {cpu.get('tempC', '—')}°C"),
        ("Memory", f"{mem.get('usedKb', 0) / 1e6:.1f} / {mem.get('totalKb', 0) / 1e6:.1f} GB "
                   f"({mem.get('pct', 0)}%) — shared with the GPU"),
        ("Network", f"{status.get('net', {}).get('iface', '—')} · "
                    f"↓{status.get('net', {}).get('rxMbs', 0)} ↑{status.get('net', {}).get('txMbs', 0)} MB/s"),
        ("Uptime", human_uptime(status.get("uptimeSec", 0))),
    ]
    for disk in status.get("disks", []):
        rows.append((f"Disk {disk['mount']}",
                     f"{disk['usedGb']} / {disk['totalGb']} GB ({disk['pct']}%)"))

    body = "".join(
        f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows
    )

    containers = "".join(
        f"<li><b>{esc(c['name'])}</b> — {esc(c['state'])} <span class=dim>{esc(c['status'])}</span></li>"
        for c in status.get("docker", [])
    ) or "<li class=dim>no containers</li>"

    ollama = status.get("ollama", {})
    if not ollama.get("reachable"):
        models = "<li class=dim>ollama is not answering</li>"
    elif not ollama.get("loaded"):
        models = (f"<li class=dim>nothing loaded · {ollama.get('modelCount', 0)} "
                  f"model(s) on disk</li>")
    else:
        models = "".join(
            f"<li><b>{esc(m['name'])}</b> — {m['memGb']} GB resident</li>"
            for m in ollama["loaded"]
        )

    job_items = []
    for job in status.get("jobs", []):
        if job["kind"] == "process":
            state = f"{job['count']} running" if job["running"] else "not running"
            job_items.append(f"<li><b>{esc(job['name'])}</b> — {esc(state)}</li>")
        else:
            tail = "<br>".join(esc(line) for line in job.get("tail", []))
            job_items.append(f"<li><b>{esc(job['name'])}</b><pre>{tail}</pre></li>")
    jobs_html = "".join(job_items) or "<li class=dim>nothing configured</li>"

    findings = "".join(
        f"<li><b>{esc(f['title'])}</b> — {esc(f['detail'])}"
        + (f"<br><span class=dim>{esc(f['hint'])}</span>" if f.get("hint") else "")
        + "</li>"
        for f in status.get("findings", [])
    )
    warn_block = f"<section class=warn><h2>Attention</h2><ul>{findings}</ul></section>" if findings else ""

    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta http-equiv=refresh content="5">
<title>{esc(status['host'])} — dgx-spark-bar</title>
<style>
 :root {{ color-scheme: dark; }}
 body {{ margin:0; padding:16px; background:#0d1117; color:#e6edf3;
        font:15px/1.5 -apple-system,system-ui,sans-serif; }}
 h1 {{ font-size:19px; margin:0 0 4px; display:flex; align-items:center; gap:8px; }}
 .dot {{ width:12px; height:12px; border-radius:50%; background:{color}; flex:none; }}
 .dim {{ color:#8b949e; }}
 table {{ border-collapse:collapse; width:100%; margin:12px 0; }}
 th {{ text-align:left; font-weight:500; color:#8b949e; padding:5px 12px 5px 0;
       white-space:nowrap; vertical-align:top; }}
 td {{ padding:5px 0; }}
 h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.05em;
       color:#8b949e; margin:18px 0 6px; }}
 ul {{ margin:0; padding-left:18px; }}
 li {{ margin:3px 0; }}
 pre {{ margin:4px 0; padding:8px; background:#161b22; border-radius:6px;
        overflow-x:auto; font-size:12px; color:#8b949e; }}
 .warn ul {{ color:#e3b341; }}
</style></head><body>
<h1><span class=dot></span>{esc(status['host'])}</h1>
<div class=dim>dgx-spark-bar {esc(status['version'])} · refreshes every 5 s</div>
{warn_block}
<table>{body}</table>
<h2>Containers</h2><ul>{containers}</ul>
<h2>Models in memory</h2><ul>{models}</ul>
<h2>Jobs</h2><ul>{jobs_html}</ul>
</body></html>"""


# --------------------------------------------------------------------------
# HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"dgx-spark-bar/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # journald gets one line per action only
        pass

    # -- helpers -----------------------------------------------------------
    def _send(self, code: int, payload: dict | str, content_type: str) -> None:
        body = (payload if isinstance(payload, str) else json.dumps(payload)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
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
                "machineId": machine_id(),
            }, "application/json")
        elif parsed.path == "/status":
            self._send(200, build_status(), "application/json")
        elif parsed.path == "/":
            self._send(200, render_html(build_status()), "text/html; charset=utf-8")
        else:
            self._send(404, {"ok": False, "error": "not found"}, "application/json")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/action":
            self._send(404, {"ok": False, "error": "not found"}, "application/json")
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": "bad json"}, "application/json")
            return

        action = str(payload.get("action", ""))
        print(f"[dgx-spark-bar] action requested: {action} from {self.client_address[0]}", flush=True)
        code, result = perform(action)
        self._send(code, result, "application/json")


SAMPLER = Sampler()


def main() -> None:
    SAMPLER.start()
    port = conf_int("PORT", 8765)
    bind = CONF.get("BIND", "0.0.0.0")
    print(f"[dgx-spark-bar] {VERSION} listening on {bind}:{port} (no auth — private network only)",
          flush=True)
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()

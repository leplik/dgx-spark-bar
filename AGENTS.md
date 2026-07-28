# AGENTS.md — install and operate dgx-spark-bar

Written for an AI coding agent with a terminal on the user's **Mac** and ssh
access to their **DGX Spark**. A human can follow the same commands.

Goal: a coloured dot in the Mac's menu bar that says whether the Spark is up.

Two pieces, one on each machine:

| Where | What | How it runs |
|---|---|---|
| DGX Spark (Linux, aarch64) | `dgx-spark-bar-agent` — one Python file, stdlib only | systemd unit, as the user, not root |
| Mac (macOS 14+) | `DGXSparkBar.app` — SwiftUI menu-bar app | menu-bar only, no Dock icon |

## Before you start

You need the Spark's **hostname or IP** and the **ssh user**. Ask for them if
you were not told. Confirm you can reach it:

```bash
ssh <user>@<host> 'hostname; uname -m; python3 --version'
```

Expect `aarch64` and Python 3.9+. If ssh fails, stop and report — everything
below depends on it.

The ssh user must be a **normal user, not `root`**: the agent runs as that user
and the installer takes it from `SUDO_USER`. If you can only log in as root,
prefix Step 1's install command with `SPARKBAR_USER=<user>` and pass it through
with `sudo -E`; otherwise the installer stops and tells you to.

**If `sudo` asks for a password, hand the prompt to the user.** Never try to
guess or supply one.

## Step 1 — the agent, on the Spark

```bash
ssh -t <user>@<host> '
  set -e
  tmp=$(mktemp -d)
  curl -fsSL https://github.com/leplik/dgx-spark-bar/releases/latest/download/dgx-spark-bar-agent.tar.gz | tar xz -C "$tmp"
  sudo "$tmp/dgx-spark-bar-agent/install.sh"
  rm -rf "$tmp"
'
```

`ssh -t` is deliberate: without a tty `sudo` cannot prompt.

If the box has `git` but no route to the releases CDN:

```bash
ssh -t <user>@<host> '
  set -e
  rm -rf /tmp/dgx-spark-bar
  git clone --depth 1 https://github.com/leplik/dgx-spark-bar /tmp/dgx-spark-bar
  sudo /tmp/dgx-spark-bar/agent/install.sh
'
```

The installer is idempotent — re-running it upgrades the agent and the unit but
never overwrites an existing `/etc/dgx-spark-bar/agent.conf`. It prints the
tailnet and LAN URLs when it finishes. It installs:

* `/usr/local/bin/dgx-spark-bar-agent` — the agent;
* `/etc/dgx-spark-bar/agent.conf` — config, generated once;
* `/etc/systemd/system/dgx-spark-bar-agent.service` — enabled and started;
* `/etc/sudoers.d/dgx-spark-bar` — NOPASSWD for exactly `/usr/sbin/poweroff` and
  `/usr/sbin/reboot`, nothing else;
* `/etc/avahi/services/dgx-spark-bar.service` — mDNS beacon, if Avahi is present.

Verify before moving on:

```bash
curl -fsS http://<host>:8765/ping
```

Expect `{"app": "dgx-spark-bar", "version": "...", "host": "...", "machineId": "..."}`.
If it hangs, see [Troubleshooting](#troubleshooting).

## Step 2 — the app, on the Mac

```bash
curl -fsSL -o "${TMPDIR:-/tmp}/DGXSparkBar.zip" \
  https://github.com/leplik/dgx-spark-bar/releases/latest/download/DGXSparkBar.zip
# ditto merges into an existing bundle rather than replacing it, so on an
# upgrade clear the old one out first — stale files break the signature.
osascript -e 'quit app "DGXSparkBar"' 2>/dev/null || true
rm -rf /Applications/DGXSparkBar.app
ditto -x -k "${TMPDIR:-/tmp}/DGXSparkBar.zip" /Applications
xattr -dr com.apple.quarantine /Applications/DGXSparkBar.app 2>/dev/null || true
open /Applications/DGXSparkBar.app
```

Always run the `xattr` line. Releases are Developer ID signed and notarized only
when the repo carries the Apple secrets; without them the workflow publishes an
ad-hoc-signed app on purpose (see the header of `.github/workflows/release.yml`),
and Gatekeeper will refuse a quarantined copy of that. It also covers builds a
user made themselves.

To build from source instead (needs Xcode's Swift toolchain):

```bash
git clone https://github.com/leplik/dgx-spark-bar
cd dgx-spark-bar/macos && ./build-app.sh
cp -R build/DGXSparkBar.app /Applications/
open /Applications/DGXSparkBar.app
```

## Step 3 — verify, then hand back

```bash
pgrep -x DGXSparkBar >/dev/null && echo "app running"
curl -fsS http://<host>:8765/status | python3 -m json.tool | head -30
```

A healthy `/status` has `"level": "ok"` and an empty `findings` array.

**Then ask the user what colour the dot is** — you cannot see the menu bar, and
that dot is the entire point of the project.

### Two things only the user can do

1. **Local Network permission.** On macOS 15+ the first launch shows a system
   dialog. You cannot click it. Bonjour/LAN discovery does nothing until the
   user allows it; tailnet discovery works regardless. If the dot stays grey on
   a LAN-only setup, this is almost always why — also check
   System Settings → Privacy & Security → Local Network.
2. **Autostart.** System Settings → General → Login Items → **+** →
   `/Applications/DGXSparkBar.app`. Offer it; do not assume it.

## Reference

### HTTP API

Plain JSON over HTTP. No auth, no headers required.

| Route | Returns |
|---|---|
| `GET /ping` | `app`, `version`, `host`, `machineId` — the discovery beacon |
| `GET /status` | one snapshot: cpu, memory + pressure, gpu, disks, net, `findings`, and the last 60 polls |
| `GET /` | identical to `/status` |
| `POST /action` | body `{"action": "reboot"}` or `{"action": "poweroff"}` |

`/status` carries `level` (`ok` / `warn` / `error`) and `findings[]`, each with a
stable `id`, a severity and a plain-language `hint` — a client colours the dot
from `level` alone and never re-implements the rules. A full response with all
60 samples is about 3 KB.

Both actions answer **before** they act: the agent replies
`{"ok": true, "action": "...", "deferred": true}` and pulls the rug a second
later, because a box that reboots mid-response never gets to send one. An
unknown action is a 400 and is logged with the caller's address.

```bash
curl -fsS -X POST http://<host>:8765/action \
  -H 'Content-Type: application/json' -d '{"action":"reboot"}'
```

### Config

`/etc/dgx-spark-bar/agent.conf`, written once by the installer. Edit and
`sudo systemctl restart dgx-spark-bar-agent`.

| Key | Default | Meaning |
|---|---|---|
| `BIND` | `0.0.0.0` | listen address — set to the tailnet IP to restrict exposure |
| `PORT` | `8765` | HTTP port — see the warning below before changing it |
| `DISKS` | `/` (plus `/home` if separate) | comma-separated mountpoints to report |
| `WARN_DISK_PCT` | `85` | disk usage that turns the dot yellow |
| `WARN_GPU_TEMP` | `85` | GPU °C that turns the dot yellow |
| `CRIT_GPU_TEMP` | `90` | GPU °C that turns it red |

That is the whole file. There is nothing to point at a container stack, an
inference server or a log directory: this monitors the machine, not what runs on
it.

**`PORT` is not a free knob.** Discovery assumes 8765: the client probes tailnet
peers on that port, and the mDNS record carries whatever `PORT` said when
`install.sh` last ran. Changing it and only restarting the service leaves both
channels pointing at the old port and the dot grey. If you must move it, edit
`agent.conf`, re-run `install.sh` to re-render the mDNS record, and expect to
type `host:8765`-style addresses into the empty-state panel by hand for tailnet
boxes. Restricting exposure is what `BIND` is for.

Inline comments are stripped, so `WARN_GPU_TEMP=90  # rack runs hot` works. A
value the agent cannot parse falls back to the default above rather than failing
the start, so check `journalctl -u dgx-spark-bar-agent` after an edit.

### What turns the dot yellow or red

Three rules, with thresholds adapted from
[spark-doctor](https://github.com/joeynyc/spark-doctor) (MIT):

* **Power cap / throttling** — GPU above 80% utilisation while drawing ≤25 W,
  for three polls in a row. One dip is noise; three is a state. Yellow on its
  own; red when the SM clock is also under 800 MHz in that run. This is the rule
  that catches a demo running at a quarter speed.
* **Memory pressure** — `/proc/pressure/memory` full avg10 above 0.10 (0.25 is
  critical), or available memory under 16 GB / 8 GB. On GB10 this is what "the
  model did not fit" looks like: there is no framebuffer to fill.
* **Thermal** — GPU temperature against `WARN_GPU_TEMP` / `CRIT_GPU_TEMP`.

Plus disk usage against `WARN_DISK_PCT`.

### Discovery

The client is configured with nothing and looks on two independent channels:

| Channel | Works when | How |
|---|---|---|
| **tailnet** | anywhere — office, hotel wifi, phone hotspot | reads the local Tailscale CLI, probes every online peer |
| **Bonjour** | both machines on one network | `_dgx-spark-bar._tcp` over mDNS |

A box found on both appears once; the tailnet address wins, because it is the
one that still works after you leave the building. If neither channel reaches
it, the empty-state panel takes a typed `host:port`.

Install [Tailscale](https://tailscale.com) on both machines and the Spark stays
reachable through any NAT, with no port forwarding and no public exposure.

### Security model

**There is no authentication.** Anyone who can reach the port can read the
status and reboot or power the box off. This is a deliberate trade, not an
oversight: a shared secret to copy between machines was more friction than the
threat it removed on a network that is private by construction.

If you are advising the user:

* keep it on a tailnet or a LAN they trust — not on conference wifi;
* on an untrusted network, set `BIND` to the tailnet address (`100.x.y.z`)
  rather than inventing a password;
* actions are a fixed whitelist inside the agent. The client sends a name, never
  a command.

Do not add an authentication scheme unless the user asks for one.

### Idle cost

The agent has no sampling loop and no background thread. Every number is
measured when a client asks, and between requests the process is asleep on
`accept()` — in particular it does not spawn `nvidia-smi` around the clock.

Rates need two readings, and the previous poll normally supplies the first: a
client polling every 5 s gets a real 5-second CPU average for free. After 30 s
of silence that baseline is stale, so the next poll times its own 200 ms window
instead of reporting an average since whenever someone last looked. Two clients
polling at once share one measurement.

The 60-poll history behind the sparklines fills while a client watches and stops
when it goes away. Nothing is stored on either side.

### Troubleshooting

**`/ping` hangs or refuses.**

```bash
ssh <user>@<host> 'systemctl status dgx-spark-bar-agent --no-pager; journalctl -u dgx-spark-bar-agent -n 40 --no-pager'
```

**The dot is grey / no Spark found.** Run the client from a terminal — it prints
every discovery candidate and every probe result:

```bash
# Quit first — `open` cannot set the environment of a process that is already
# running, and it says so on stderr and exits 0, which is easy to miss.
osascript -e 'quit app "DGXSparkBar"' 2>/dev/null; sleep 1
open --env DGX_SPARK_BAR_DEBUG=1 --stderr /tmp/dgx-spark-bar.log /Applications/DGXSparkBar.app
tail -f /tmp/dgx-spark-bar.log
```

Then, in order: is the agent running (above); does `tailscale status` list the
Spark as active; was Local Network permission granted; does
`curl http://<host>:8765/ping` work from the Mac's shell. A working `curl` with
a grey dot means discovery, not the agent.

**GPU rows missing.** `nvidia-smi` is absent or failing on the Spark — the agent
reports `gpu.present = false` and the client hides the rows rather than showing
zeros.

**Reboot/shutdown does nothing.** Check the sudoers drop-in survived:
`sudo visudo -cf /etc/sudoers.d/dgx-spark-bar`.

### Uninstall

`uninstall.sh` is standalone — it needs nothing else from the repo:

```bash
ssh -t <user>@<host> 'curl -fsSL https://raw.githubusercontent.com/leplik/dgx-spark-bar/main/agent/uninstall.sh | sudo bash -s -- --purge'
rm -rf /Applications/DGXSparkBar.app   # on the Mac
```

Without `--purge` the config at `/etc/dgx-spark-bar/agent.conf` is kept, so a
reinstall does not lose local edits.

On a Spark with no route out, the copy you installed from works the same way —
`sudo <tarball-or-clone>/agent/uninstall.sh --purge`. Do not skip the script and
delete files by hand: the sudoers drop-in granting NOPASSWD `poweroff`/`reboot`
is the one piece that matters.

## Working on this repository

* `agent/dgx_spark_bar_agent.py` — the whole agent. Stdlib only, on purpose: it
  must install on a fresh Spark with no pip and no virtualenv. Keep it that way.
* `macos/Sources/DGXSparkBar/` — `Discovery` (tailnet + Bonjour), `AgentClient`
  (HTTP), `Store` (polling), `MenuView` (UI), `DGXSparkBarApp` (the dot itself),
  `Models` (the agent's JSON), `Log` (the `DGX_SPARK_BAR_DEBUG` output above).
* `macos/build-app.sh` — SwiftPM makes a bare executable; this wraps it in a
  bundle with the `LSUIElement`, Bonjour and ATS keys the app needs. Honours
  `VERSION` and `SIGN_IDENTITY` from the environment.
* Releases are built by `.github/workflows/release.yml` on a `v*` tag.

The comments in those files explain *why*, not *what* — read them before
changing behaviour that looks redundant. Several of them (the ATS exemption, the
non-template menu-bar image, the deferred action reply) encode a bug that was
already paid for once.

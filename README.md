# dgx-spark-bar

A menu-bar monitor and power switch for the **NVIDIA DGX Spark** (GB10).

A green dot in your Mac's menu bar means the Spark is up and nothing is
overheating, throttled or out of memory. Click it for the numbers, or shut the
box down before you pack it into a bag.

```
  ● spark-b0af                                       tailnet · up 4h 12m
  GPU      37%   58°C · 91 W · 1980 MHz
  ▁▂▅▇▇▆▃▂
  CPU      12%   44°C
  ▁▁▂▁▃▂▁▁
  Memory   38 / 128 GB   shared with GPU
  Disk     3% used   3716 GB free
  Network  enP7s7   ↓9.2 ↑0.2 MB/s

  [ Reboot ]  [ Shut down ]
```

Two parts: a tiny Python agent on the Spark and a SwiftUI menu-bar app on the
Mac. No cloud, no account, no database, no daemon on the Mac beyond the app
itself.

## Why not a generic server monitor

A generic monitor reads `nvidia-smi` and shows *"VRAM: N/A"*, because on GB10
there is no separate framebuffer — Grace and Blackwell share one pool, and
`nvidia-smi` reports `FB Memory Usage: N/A` on purpose. dgx-spark-bar knows that:

* **memory** comes from `/proc/meminfo` and is labelled *shared with GPU*
  rather than pretending to be VRAM;
* **"it did not fit"** is detected as memory-pressure stalls
  (`/proc/pressure/memory`) and swap — the only honest signal when there is no
  framebuffer to fill;
* **a throttled Spark** — busy GPU drawing ~14–25 W at a low SM clock — is
  called out explicitly. It is the most common reason a demo runs at a quarter
  speed while every dashboard looks green.

The rules and their thresholds are adapted from
[spark-doctor](https://github.com/joeynyc/spark-doctor) (MIT) — its numbers come
from reported DGX Spark field behaviour, which beats numbers invented at a desk.

## Costs nothing when nobody is looking

The agent has no sampling loop and no background thread. Every number is read
when a client asks, and between requests the process is asleep on `accept()` —
in particular it does not spawn `nvidia-smi` around the clock.

Rates need two readings, and the previous poll normally supplies the first: a
client polling every 5 s gets a real 5-second CPU average for free. When nobody
has asked for more than 30 s that baseline is stale, so the first poll of a
session times its own 200 ms window instead of reporting an average since
whenever someone last looked.

The 60-poll history behind the sparklines fills while a client watches and
stops when it goes away — no storage on either side.

## Install the agent (on the Spark)

```bash
git clone https://github.com/leplik/dgx-spark-bar
sudo dgx-spark-bar/agent/install.sh
```

That is one Python file (stdlib only — no pip, no virtualenv), a systemd unit,
an Avahi service so the box announces itself on the LAN, and a sudoers drop-in
allowing the agent exactly two privileged commands: `poweroff` and `reboot`.

Re-running upgrades the agent and keeps `/etc/dgx-spark-bar/agent.conf`. To remove
everything: `sudo agent/uninstall.sh [--purge]`.

The agent runs as your normal user, not root, and needs no group membership.

## Build the client (on the Mac)

```bash
cd macos && ./build-app.sh
open build/DGXSparkBar.app
```

Requires Xcode's Swift toolchain and macOS 14+. For autostart, add the app in
System Settings → General → Login Items.

## Discovery

The client configures nothing and finds boxes on two independent channels:

| Channel | Works when | How |
|---|---|---|
| **tailnet** | anywhere — office, hotel wifi, phone hotspot | reads the local Tailscale CLI, probes every online peer |
| **Bonjour** | both machines on one network | `_dgx-spark-bar._tcp` via mDNS |

One box found on both is one row; the tailnet address wins, because that is the
one that keeps working when you leave the building. If neither channel reaches
it, you can type an address by hand.

Install [Tailscale](https://tailscale.com) on both machines and the Spark stays
reachable through any NAT, with no port forwarding and no public exposure. That
is also the only sane way to take the box to a customer site.

## Security model

**There is no authentication.** Anyone who can reach the port can read the
status and reboot or power the box off. This is a deliberate trade: a shared
secret to copy between machines was more friction than the threat it removed,
on a network that is private by construction.

What that implies:

* keep it on a tailnet or a LAN you trust — not on conference wifi;
* if you must run on an untrusted network, set `BIND` in
  `/etc/dgx-spark-bar/agent.conf` to the tailnet address (`100.x.y.z`) rather than
  inventing a password;
* actions are a **fixed whitelist** in the agent — `reboot` and `poweroff`,
  nothing else. The client sends a name, never a command, and anything else is
  rejected and logged with the caller's address.

## HTTP API

Plain JSON over HTTP, useful from `curl`.

| Route | Returns |
|---|---|
| `GET /ping` | name, version, machine id — the discovery beacon |
| `GET /status` | snapshot: cpu, memory + pressure, gpu, disks, net, findings, and the last 60 polls |
| `GET /` | the same as `/status` |
| `POST /action` | `{"action": "reboot" \| "poweroff"}` |

`/status` also carries `level` (`ok` / `warn` / `error`) and `findings[]`, each
with a stable `id`, a severity and a plain-language hint — so a client can show
the dot's colour without re-implementing any of the rules. A full response with
all 60 samples is about 3 KB.

Both actions answer **before** they act: the agent replies `{"deferred": true}`
and pulls the rug a second later, because a box that reboots mid-response never
gets to send one.

## Configuration

`/etc/dgx-spark-bar/agent.conf`, written once by the installer:

```ini
BIND=0.0.0.0
PORT=8765
DISKS=/
WARN_DISK_PCT=85
WARN_GPU_TEMP=85
CRIT_GPU_TEMP=90
```

That is the whole file. There is nothing to point at a container stack, an
inference server or a log directory: this monitors the machine, not what runs
on it.

## Troubleshooting

Run the client from a terminal with `DGX_SPARK_BAR_DEBUG=1` and it prints every
discovery candidate and every probe result:

```bash
open --env DGX_SPARK_BAR_DEBUG=1 --stderr /tmp/dgx-spark-bar.log macos/build/DGXSparkBar.app
```

Agent side: `journalctl -u dgx-spark-bar-agent -f`.

## License

MIT.

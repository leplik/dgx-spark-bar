# DGX Spark status LED

The NVIDIA DGX Spark has no status light. No power LED, no activity indicator,
nothing — you cannot tell from across the desk whether the box is on, asleep, or
quietly throttling itself down to a quarter of its speed.

<p>
  <img src="docs/dgx-spark-no-indicators-elon.png" width="420" alt="Elon Musk returning a DGX Spark to Jensen Huang: doesn't work, not a single indicator lights up">
</p>
<p>
  <img src="docs/dgx-spark-no-indicators-altman.png" width="420" alt="Sam Altman returning a DGX Spark to Jensen Huang: I want a refund, not a single indicator had the decency to light up">
</p>

That bothered me enough to build one. It lives in the Mac menu bar.

<img src="docs/screenshot.png" width="420" alt="A green status LED in the macOS menu bar showing an NVIDIA DGX Spark is up, with GPU, CPU, memory, disk and network readings below it">

**🟢 up · 🟡 something is off · 🔴 unreachable**

Green means the Spark is up and nothing is overheating, throttled or out of
memory. Click the dot for the numbers — or shut the box down before you pack it
into a bag.

## Install

Paste this into Claude Code, Cursor, Codex, or whichever agent you keep in a
terminal:

```text
Install dgx-spark-bar: https://github.com/leplik/dgx-spark-bar

My DGX Spark is at <host-or-ip>, ssh user <user>.

Read AGENTS.md in that repo and follow it: install the agent on the Spark
over ssh, then install the menu-bar app on this Mac and launch it. Tell me
what colour the dot is when you're done.
```

Fill in the two placeholders and let it run — about a minute, and the dot
appears. Nothing to configure afterwards: the app finds the box on your tailnet
or over Bonjour by itself.

No agent to hand? [AGENTS.md](AGENTS.md) is the same steps as plain commands,
and [Releases](https://github.com/leplik/dgx-spark-bar/releases/latest) has the
signed app.

## What the dot knows

A generic server monitor reads `nvidia-smi` on a Spark and shows *"VRAM: N/A"*,
because GB10 is not a discrete GPU:

* **There is no separate VRAM.** Grace and Blackwell share one pool, so
  `nvidia-smi` reports `FB Memory Usage: N/A` on purpose. Memory comes from
  `/proc/meminfo`, labelled *shared with GPU* rather than pretending to be a
  framebuffer.
* **"It did not fit"** shows up as memory-pressure stalls
  (`/proc/pressure/memory`) and swap — never as a full framebuffer, because
  there isn't one to fill.
* **A throttled Spark** — busy GPU drawing 14–25 W at a low SM clock — turns the
  dot yellow. It is the most common reason a demo runs at a quarter speed while
  every dashboard still looks green.

Thresholds are adapted from [spark-doctor](https://github.com/joeynyc/spark-doctor)
(MIT), whose numbers come from reported DGX Spark field behaviour rather than
from a desk.

## Two parts, no cloud

A Python agent on the Spark — stdlib only, no pip, no virtualenv, running as
your user rather than root — and a SwiftUI menu-bar app on the Mac. No account,
no database, no telemetry, no daemon on the Mac beyond the app itself.

The agent has no background loop either: every number is read when a client
asks, and between requests the process sleeps on `accept()`. Nothing runs on the
Spark while nobody is looking.

It also has **no authentication**, deliberately — a shared secret to copy
between machines was more friction than the threat it removed on a network that
is private by construction. Keep it on a tailnet or a LAN you trust. Actions are
a fixed whitelist of two: reboot and power off.

The HTTP API, every config key, the discovery channels and the troubleshooting
steps live in [AGENTS.md](AGENTS.md).

## License

MIT.

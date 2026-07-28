#!/usr/bin/env bash
# Remove the dgx-spark-bar agent. Keeps /etc/dgx-spark-bar/agent.conf unless --purge,
# so a reinstall does not lose local edits.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "sudo $0 [--purge]" >&2
  exit 1
fi

systemctl disable --now dgx-spark-bar-agent 2>/dev/null || true
rm -f /etc/systemd/system/dgx-spark-bar-agent.service
systemctl daemon-reload
rm -f /usr/local/bin/dgx-spark-bar-agent
rm -f /etc/sudoers.d/dgx-spark-bar
rm -f /etc/avahi/services/dgx-spark-bar.service
systemctl reload avahi-daemon 2>/dev/null || true

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf /etc/dgx-spark-bar
  echo "removed, including the config"
else
  echo "removed; /etc/dgx-spark-bar/agent.conf kept (use --purge to drop it)"
fi

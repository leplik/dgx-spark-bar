#!/usr/bin/env bash
# Render icon/AppIcon.icns from render-icon.swift.
#
# Run this only when the artwork changes and commit the result: build-app.sh
# just copies the .icns, so neither CI nor a plain build needs a Swift render.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

SET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$SET"
trap 'rm -rf "$(dirname "$SET")"' EXIT

# iconutil takes these names literally — the @2x entries are what a Retina
# Finder reaches for, and a missing one silently degrades to a blurry upscale.
for spec in 16:16x16 32:16x16@2x 32:32x32 64:32x32@2x \
            128:128x128 256:128x128@2x 256:256x256 512:256x256@2x \
            512:512x512 1024:512x512@2x; do
  px="${spec%%:*}"
  name="${spec#*:}"
  swift icon/render-icon.swift "$px" "$SET/icon_$name.png"
done

iconutil -c icns "$SET" -o icon/AppIcon.icns
echo "==> $PWD/icon/AppIcon.icns  ($(du -h icon/AppIcon.icns | cut -f1))"

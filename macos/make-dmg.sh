#!/usr/bin/env bash
# Build DGXSparkBar.dmg — the app and an /Applications symlink side by side, so
# installing is a drag between two icons.
#
# The window's looks (size, icon positions, background picture) live in a
# .DS_Store that Finder writes, and Finder only writes it when a real Finder is
# there to be scripted. A CI runner has none. So the layout is produced once on
# a developer's Mac, committed as dmg/DS_Store, and merely copied in from then
# on — which is why this script has two paths and prefers the boring one.
#
#   ./make-dmg.sh [path/to/DGXSparkBar.app]
#   SIGN_IDENTITY="Developer ID Application: …" ./make-dmg.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

APP="${1:-build/DGXSparkBar.app}"
DMG="${DMG_OUT:-build/DGXSparkBar.dmg}"
VOL="DGX Spark Bar"
SIGN_IDENTITY="${SIGN_IDENTITY:--}"

# Keep in step with render-background.swift: it draws the arrow between icons it
# cannot see, and only these numbers say where they will be.
WIDTH=540 HEIGHT=380 ICON_SIZE=128
APP_X=140 DROP_X=400 ICON_Y=190
TITLEBAR=28   # `bounds` covers the whole window; the background only gets what is left

[[ -d "$APP" ]] || { echo "no app bundle at $APP — run ./build-app.sh first" >&2; exit 1; }

# Signing the image says nothing about what is inside it. An ad-hoc app in a
# Developer ID image looks correct everywhere except on the machine that finally
# runs it, which is the worst place to find out.
if [[ "$SIGN_IDENTITY" != "-" ]] && codesign -dvv "$APP" 2>&1 | grep -q "Signature=adhoc"; then
  echo "!!  $APP is ad-hoc signed, but this image is to be signed for release." >&2
  echo "!!  Rebuild it first:  SIGN_IDENTITY=\"$SIGN_IDENTITY\" ./build-app.sh" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; [[ -n "${DEV:-}" ]] && hdiutil detach "$DEV" -quiet 2>/dev/null || true' EXIT

# --------------------------------------------------------------------------
# background: one TIFF holding both resolutions, which is how a HiDPI image is
# handed to Finder — it has no @2x naming convention to fall back on.

if [[ ! -f dmg/background.tiff ]]; then
  echo "==> background"
  swift dmg/render-background.swift 1 "$TMP/bg.png"
  swift dmg/render-background.swift 2 "$TMP/bg@2x.png"
  tiffutil -cathidpicheck "$TMP/bg.png" "$TMP/bg@2x.png" -out dmg/background.tiff >/dev/null
fi

# --------------------------------------------------------------------------
# staging

echo "==> staging"
STAGE="$TMP/stage"
mkdir -p "$STAGE/.background"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp dmg/background.tiff "$STAGE/.background/"

rm -f "$DMG"
mkdir -p "$(dirname "$DMG")"

if [[ -f dmg/DS_Store ]]; then
  # The fast path, and the only one that works without a Finder.
  echo "==> layout from dmg/DS_Store"
  cp dmg/DS_Store "$STAGE/.DS_Store"
  hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -quiet \
    -format UDZO -imagekey zlib-level=9 -fs HFS+ "$DMG"
else
  echo "==> no dmg/DS_Store yet — scripting Finder to make one"
  hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -quiet \
    -format UDRW -fs HFS+ "$TMP/rw.dmg"

  DEV="$(hdiutil attach -readwrite -noverify -noautoopen "$TMP/rw.dmg" \
         | awk '/^\/dev\// { print $1; exit }')"
  MOUNT="/Volumes/$VOL"

  if ! osascript <<APPLESCRIPT
tell application "Finder"
  tell disk "$VOL"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {300, 140, $((300 + WIDTH)), $((140 + HEIGHT + TITLEBAR))}
    set opts to the icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to $ICON_SIZE
    set background picture of opts to file ".background:background.tiff"
    set position of item "DGXSparkBar.app" of container window to {$APP_X, $ICON_Y}
    set position of item "Applications" of container window to {$DROP_X, $ICON_Y}
    close
    open
    update without registering applications
    delay 2
  end tell
end tell
APPLESCRIPT
  then
    echo "!!  Finder would not be scripted (headless session?)." >&2
    echo "!!  Run this once on a Mac with a desktop, then commit macos/dmg/DS_Store." >&2
    exit 1
  fi

  sync
  cp "$MOUNT/.DS_Store" dmg/DS_Store
  echo "    wrote dmg/DS_Store — commit it so CI never needs Finder again"

  hdiutil detach "$DEV" -quiet
  DEV=""
  hdiutil convert "$TMP/rw.dmg" -format UDZO -imagekey zlib-level=9 -ov -quiet -o "$DMG"
fi

# --------------------------------------------------------------------------
# The app's signature does not cover the image carrying it, so the .dmg is
# signed in its own right — and notarized in its own right too, upstream.

if [[ "$SIGN_IDENTITY" != "-" ]]; then
  echo "==> codesign ($SIGN_IDENTITY)"
  codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
fi

echo "==> $PWD/$DMG"

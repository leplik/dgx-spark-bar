#!/usr/bin/env bash
# Build DGXSparkBar.app. SwiftPM produces a bare executable; a menu-bar app needs a
# bundle with an Info.plist (LSUIElement, and — since macOS 15 — the local-network
# and Bonjour declarations, without which discovery silently finds nothing).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
CONFIG="${1:-release}"
APP="build/DGXSparkBar.app"
VERSION="${VERSION:-0.2.0}"
# '-' is an ad-hoc signature, which is all a local build needs. CI passes a
# Developer ID here so the release can be notarized.
SIGN_IDENTITY="${SIGN_IDENTITY:--}"

echo "==> swift build ($CONFIG)"
swift build -c "$CONFIG"
BIN="$(swift build -c "$CONFIG" --show-bin-path)/DGXSparkBar"

echo "==> bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/DGXSparkBar"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>DGXSparkBar</string>
  <key>CFBundleDisplayName</key><string>DGX Spark Bar</string>
  <key>CFBundleIdentifier</key><string>io.github.leplik.dgx-spark-bar</string>
  <key>CFBundleExecutable</key><string>DGXSparkBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <!-- Menu-bar only: no Dock icon, no window on launch. -->
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>DGXSparkBar looks for your DGX Spark on the local network.</string>
  <key>NSBonjourServices</key>
  <array><string>_dgx-spark-bar._tcp</string></array>
  <!-- The agent speaks plain HTTP on a private network by design. ATS exempts
       RFC1918 addresses on its own, which is why a LAN box works with no keys
       at all — but a tailnet address lives in 100.64/10, which ATS does not
       consider local, so without this the app finds the box at the office and
       nothing anywhere else (NSURLErrorDomain -1022).
       NSAllowsLocalNetworking must NOT be added alongside: when it is present,
       ATS ignores NSAllowsArbitraryLoads entirely and the tailnet stays blocked. -->
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsArbitraryLoads</key><true/>
  </dict>
</dict>
</plist>
PLIST

if [[ "$SIGN_IDENTITY" == "-" ]]; then
  # Ad-hoc signature: gives the bundle a stable identity so macOS can remember the
  # local-network permission instead of re-asking on every rebuild.
  codesign --force --sign - "$APP" >/dev/null 2>&1 \
    || echo "!!  codesign failed — the app still runs, but permissions may reset"
else
  # Notarization rejects anything without the hardened runtime and a secure
  # timestamp, so a signed build must ask for both up front.
  echo "==> codesign ($SIGN_IDENTITY)"
  codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP"
fi

echo "==> $PWD/$APP"
echo "    open it with:  open $APP"
echo "    autostart:     System Settings → General → Login Items → +"

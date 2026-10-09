#!/bin/bash
# Builds Токенометр.app (Apple Silicon + Intel) into build/, plus build/Tokenometr.zip for releases.
#   ./build.sh            build only
#   ./build.sh --install  build, then install into ~/Applications (where Launchpad finds it) and open it
set -euo pipefail
cd "$(dirname "$0")"

NAME="Токенометр"
BUILD="build"
APP="$BUILD/$NAME.app"
MIN_MACOS="13.0"

python3 -m unittest discover -s tests   # never ship a broken counter

rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$BUILD/AppIcon.iconset"

for arch in arm64 x86_64; do
  swiftc -O -target "$arch-apple-macos$MIN_MACOS" -o "$BUILD/Tokenometr-$arch" app/main.swift
done
lipo -create -output "$APP/Contents/MacOS/Tokenometr" "$BUILD/Tokenometr-arm64" "$BUILD/Tokenometr-x86_64"

swiftc -O -o "$BUILD/make-icon" app/make_icon.swift
"$BUILD/make-icon" "$BUILD/icon-1024.png"
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "$BUILD/icon-1024.png" --out "$BUILD/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$BUILD/icon-1024.png" --out "$BUILD/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"

cp app/Info.plist "$APP/Contents/Info.plist"
cp web/index.html usage.py chatsync.py tokenometr_limits.py "$APP/Contents/Resources/"
codesign --force --sign - "$APP"
ditto -c -k --keepParent "$APP" "$BUILD/Tokenometr.zip"
echo "Built: $APP and $BUILD/Tokenometr.zip"

if [ "${1:-}" = "--install" ]; then
  INSTALLED="$HOME/Applications/$NAME.app"
  osascript -e 'tell application id "local.tokenometr" to quit' >/dev/null 2>&1 || true
  for _ in $(seq 1 50); do  # opening the new copy while the old one is still quitting fails
    pgrep -f "Contents/MacOS/Tokenometr" >/dev/null 2>&1 || break
    sleep 0.1
  done
  mkdir -p "$HOME/Applications"
  rm -rf "$INSTALLED"
  ditto "$APP" "$INSTALLED"
  open "$INSTALLED"
  echo "Installed: $INSTALLED"
fi

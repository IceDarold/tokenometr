#!/bin/bash
# Installs Токенометр into ~/Applications from the latest GitHub release:
#   curl -fsSL https://raw.githubusercontent.com/IceDarold/tokenometr/main/install.sh | bash
# Removes it again:
#   curl -fsSL https://raw.githubusercontent.com/IceDarold/tokenometr/main/install.sh | bash -s -- --uninstall
set -euo pipefail

REPO="IceDarold/tokenometr"
APP="$HOME/Applications/Токенометр.app"
ZIP_URL="https://github.com/$REPO/releases/latest/download/Tokenometr.zip"

quit_running_copy() {
  osascript -e 'tell application id "local.tokenometr" to quit' >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--uninstall" ]; then
  quit_running_copy
  rm -rf "$APP" "$HOME/Library/Caches/Tokenometr"
  echo "Токенометр удалён."
  echo "Настройки и сохранённые замеры остались в ~/Library/Application Support/Tokenometr."
  echo "Их можно удалить так: rm -rf ~/Library/Application\\ Support/Tokenometr"
  exit 0
fi

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Токенометр работает только на macOS." >&2
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Скачиваю Токенометр…"
curl -fsSL "$ZIP_URL" -o "$work/Tokenometr.zip"
ditto -x -k "$work/Tokenometr.zip" "$work"

quit_running_copy
mkdir -p "$HOME/Applications"
rm -rf "$APP"
ditto "$work/Токенометр.app" "$APP"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
echo "Готово: $APP. Он также есть в Launchpad."

# The counter runs on the Python that comes with Apple's Command Line Tools.
if ! xcode-select -p >/dev/null 2>&1; then
  echo
  echo "Токенометру нужен Python из Command Line Tools. Сейчас откроется установщик Apple:"
  echo "согласись, дождись конца установки и открой Токенометр снова."
  xcode-select --install >/dev/null 2>&1 || true
  exit 0
fi

open "$APP"

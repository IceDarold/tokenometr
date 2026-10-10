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
  # Opening the new copy while the old one is still quitting fails, so wait for it to exit.
  for _ in $(seq 1 50); do
    pgrep -f "Contents/MacOS/Tokenometr" >/dev/null 2>&1 || return 0
    sleep 0.1
  done
}

if [ "${1:-}" = "--uninstall" ]; then
  quit_running_copy
  # The background chat sync between Claude accounts, and with it the sending of a team's usage, goes too.
  launchctl bootout "gui/$(id -u)/local.tokenometr.chat-sync" >/dev/null 2>&1 || true
  rm -f "$HOME/Library/LaunchAgents/local.tokenometr.chat-sync.plist"
  rm -rf "$APP" "$HOME/Library/Caches/Tokenometr" "$HOME/Library/Logs/Tokenometr"
  # The token of the connection to a team goes from the keychain too.
  security delete-generic-password -s local.tokenometr.team >/dev/null 2>&1 || true
  echo "Токенометр удалён вместе с переносом чатов между аккаунтами и подключением к команде."
  echo "Чаты, которые уже перенесены, остаются в списках аккаунтов."
  echo "Настройки, замеры и резервные копии списков чатов остались в ~/Library/Application Support/Tokenometr."
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

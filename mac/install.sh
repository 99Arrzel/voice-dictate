#!/bin/sh
# Installs the Hammerspoon client: ./mac/install.sh [server-url] [token]
set -eu

here=$(cd "$(dirname "$0")" && pwd)

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi
[ -d /Applications/Hammerspoon.app ] || brew install --cask hammerspoon
command -v rec >/dev/null 2>&1 || brew install sox

mkdir -p "$HOME/.hammerspoon"
ln -sf "$here/voice_dictate.lua" "$HOME/.hammerspoon/voice_dictate.lua"

init="$HOME/.hammerspoon/init.lua"
if grep -q 'require("voice_dictate")' "$init" 2>/dev/null; then
  echo "init.lua already loads voice_dictate; edit $init to change the server or token."
else
  server=${1:-}
  token=${2:-}
  [ -n "$server" ] || { printf 'Server URL (e.g. http://your-pc.local:8765): '; read -r server; }
  [ -n "$token" ] || { printf 'Token ([server].token in the server config): '; read -r token; }
  cat >>"$init" <<EOF

voiceDictate = require("voice_dictate").start({
  server = "$server",
  token = "$token",
})
EOF
  chmod 600 "$init"
fi

if curl -fsS -m 3 -H "Authorization: Bearer $(sed -n 's/^ *token = "\(.*\)",$/\1/p' "$init" | tail -1)" \
  "$(sed -n 's/^ *server = "\(.*\)",$/\1/p' "$init" | tail -1)/health" >/dev/null; then
  echo "Server reachable and token accepted."
else
  echo "Could not reach the server with that token; check the URL, the token and the server's firewall." >&2
fi

open -a Hammerspoon
echo "Reload Hammerspoon's config (menu bar > Reload Config), then press Ctrl+Option+Space to dictate."
echo "macOS asks once for Microphone (Hammerspoon) and Accessibility (to paste) permission."

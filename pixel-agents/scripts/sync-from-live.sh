#!/usr/bin/env bash
# Trae al repo lo que se edito en vivo en la laptop (mapa, asientos, config, units) para
# poder commitearlo. Camino inverso de install.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
LIVE_DIR="$HOME/.pixel-agents"

for f in config.json agent-seats.json layout.json; do
  [[ -e "$LIVE_DIR/$f" ]] && cp "$LIVE_DIR/$f" "$HERE/config/$f" && echo "<- $f"
done
for unit in pixel-agents.service pixel-agents-tunnel.service; do
  [[ -e "$UNIT_DIR/$unit" ]] && cp "$UNIT_DIR/$unit" "$HERE/systemd/$unit" && echo "<- $unit"
done
[[ -e "$LIVE_DIR/chromecast-compat.sh" ]] && cp "$LIVE_DIR/chromecast-compat.sh" "$HERE/scripts/" && echo "<- chromecast-compat.sh"

echo
git -C "$HERE" status --short -- "$HERE" || true

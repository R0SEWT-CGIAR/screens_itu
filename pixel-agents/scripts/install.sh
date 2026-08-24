#!/usr/bin/env bash
# Aplica en la laptop la configuracion versionada de la suite de agentes (pixel-agents).
#
#   install.sh            units + script de compat; config y layout solo si no existen
#   install.sh --force    tambien pisa config.json, agent-seats.json y layout.json
#
# Se copia (no symlink) a proposito: un `git switch` a una rama sin pixel-agents/ borraria
# los units en caliente. Para el camino inverso, usar sync-from-live.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
LIVE_DIR="$HOME/.pixel-agents"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

command -v systemctl >/dev/null || { echo "systemctl no disponible" >&2; exit 1; }

mkdir -p "$UNIT_DIR" "$LIVE_DIR"

for unit in pixel-agents.service pixel-agents-tunnel.service; do
  install -m 644 "$HERE/systemd/$unit" "$UNIT_DIR/$unit"
  echo "unit    $UNIT_DIR/$unit"
done

install -m 755 "$HERE/scripts/chromecast-compat.sh" "$LIVE_DIR/chromecast-compat.sh"
echo "script  $LIVE_DIR/chromecast-compat.sh"

for f in config.json agent-seats.json layout.json; do
  if [[ -e "$LIVE_DIR/$f" && $FORCE -eq 0 ]]; then
    echo "skip    $LIVE_DIR/$f (ya existe; --force para pisarlo)"
  else
    install -m 644 "$HERE/config/$f" "$LIVE_DIR/$f"
    echo "config  $LIVE_DIR/$f"
  fi
done

# La ruta de node del ExecStart esta clavada; avisar antes de que el service falle al arrancar.
NODE_BIN="$(sed -n 's|^ExecStart=\([^ ]*node\) .*|\1|p' "$HERE/systemd/pixel-agents.service")"
[[ -x "$NODE_BIN" ]] || echo "AVISO: $NODE_BIN no existe; actualizar el ExecStart a la version de nvm instalada" >&2

systemctl --user daemon-reload
systemctl --user enable pixel-agents.service pixel-agents-tunnel.service >/dev/null
loginctl enable-linger "$USER" 2>/dev/null || echo "AVISO: enable-linger fallo; los services no arrancaran sin login" >&2

echo
echo "Listo. Arrancar o recargar con:"
echo "  systemctl --user restart pixel-agents pixel-agents-tunnel"
echo "Verificar el bind publico en exodia (debe decir 172.25.21.37:3456, no 127.0.0.1):"
echo "  ssh exodia 'ss -tln | grep 3456'"

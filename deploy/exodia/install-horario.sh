#!/usr/bin/env bash
# Instala (o quita) el horario automatico del quiosco en exodia.
#
#   sudo ./install-horario.sh              -> instala y activa
#   sudo ./install-horario.sh --desinstalar -> quita todo y desarma la alarma
#
# Requiere root: escribe en /opt y /etc/systemd/system.
set -euo pipefail

DESTINO=/opt/quiosco
UNITS=(quiosco-apagado.service quiosco-apagado.timer
        quiosco-aviso.service quiosco-aviso.timer
        quiosco-armar-alarma.service)
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Hay que correrlo con sudo." >&2; exit 1; }

if [ "${1:-}" = "--desinstalar" ]; then
  systemctl disable --now quiosco-apagado.timer quiosco-aviso.timer \
                          quiosco-armar-alarma.service 2>/dev/null || true
  for u in "${UNITS[@]}"; do rm -f "/etc/systemd/system/$u"; done
  systemctl daemon-reload
  echo 0 > /sys/class/rtc/rtc0/wakealarm 2>/dev/null || true
  echo "Horario desinstalado y alarma RTC desarmada."
  echo "OJO: esto NO enciende exodia; si ya se habia apagado, despiertala con WoL."
  exit 0
fi

# Guard de identidad: este script APAGA la maquina donde se instala. Instalarlo
# por error en la laptop del tecnico (paso el 2026-08-24) la deja apagandose a
# las 19:00. La MAC de eno1 es la identidad estable de exodia; el hostname
# (a012413) no dice nada y la IP puede moverse por DHCP.
MAC_EXODIA="6c:0b:84:e2:5e:fc"
es_exodia=0
for dev in /sys/class/net/*/address; do
  [ "$(cat "$dev" 2>/dev/null)" = "$MAC_EXODIA" ] && es_exodia=1
done
if [ "$es_exodia" -ne 1 ]; then
  cat >&2 <<MSG
ABORTA: esta maquina no es exodia (no encuentro el NIC $MAC_EXODIA).

Este script apaga la maquina donde se instala. Se instala EN exodia:

  scp -r "$AQUI" exodia:/tmp/horario
  ssh -t exodia 'sudo /tmp/horario/install-horario.sh'

Si te equivocaste y ya lo instalaste aqui, limpialo con:
  sudo "$0" --desinstalar
MSG
  exit 1
fi

install -d -m 0755 "$DESTINO"
install -m 0755 "$AQUI/quiosco-horario.sh" "$DESTINO/quiosco-horario.sh"
for u in "${UNITS[@]}"; do install -m 0644 "$AQUI/$u" "/etc/systemd/system/$u"; done

systemctl daemon-reload
systemctl enable --now quiosco-armar-alarma.service
systemctl enable --now quiosco-apagado.timer quiosco-aviso.timer

echo
echo "=== instalado ==="
"$DESTINO/quiosco-horario.sh" estado
echo
systemctl list-timers --no-pager 'quiosco-*' || true
echo
echo "AVISO: con esto exodia se apaga hoy a las 17:00 y despierta el proximo dia habil 07:06."
echo "Para revertir: sudo $AQUI/install-horario.sh --desinstalar"

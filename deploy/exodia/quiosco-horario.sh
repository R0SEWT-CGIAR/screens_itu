#!/usr/bin/env bash
# Horario automatico del quiosco en exodia: enciende 07:10 y apaga 17:00, L-V.
#
# El encendido NO usa Wake-on-LAN: usa la alarma del RTC, que exodia se arma a
# si misma justo antes de apagarse. Asi no depende de que ninguna otra maquina
# este encendida para mandar el magic packet. El WoL queda como respaldo manual
# (ver .claude/skills/recover-exodia/SKILL.md).
#
# Por que 07:06 y no 07:10: el POST del P510 es VARIABLE y hay que cubrir el
# peor caso. Medido el 2026-08-24 en dos arranques:
#   - 2m37s de firmware  (primer arranque tras 32 dias de uptime; el P510
#     parece hacer entrenamiento completo de memoria en ese caso)
#   -   34s de firmware  (arranque siguiente, 6 h despues)
# Mas ~1min de userspace. Armar a 07:06 cubre ambos: el quiosco sirve a las
# ~07:08 en el arranque rapido y a las ~07:10 en el lento.
set -euo pipefail

WAKE_HORA="07:06"        # hora local (exodia esta en America/Lima)
FORZAR_DESDE="1900"      # HHMM: pasada esta hora se apaga aunque haya sesion
RTC_ALARM=/sys/class/rtc/rtc0/wakealarm

log() { logger -t quiosco-horario -- "$*"; echo "quiosco-horario: $*"; }

# Proxima fecha habil (Lun-Vie) a WAKE_HORA, en epoch. El viernes salta a lunes.
proxima_alarma_epoch() {
  local d cand dow
  for d in 1 2 3 4 5 6 7; do
    cand=$(date -d "$(date -d "+$d day" +%Y-%m-%d) $WAKE_HORA" +%s)
    dow=$(date -d "@$cand" +%u)   # 1=lunes ... 6=sabado 7=domingo
    if [ "$dow" -le 5 ]; then echo "$cand"; return 0; fi
  done
  return 1
}

# Sesion que merece proteger el apagado: consola fisica o SSH interactivo.
#
# El discriminador es TTY no vacio + Class=user + IdleHint=no. Verificado en
# exodia el 2026-08-24:
#   - ssh sin pty (automatizacion, scripts)  -> TTY vacio      -> se ignora
#   - ssh -t / sesion interactiva humana     -> TTY=pts/N      -> protege
#   - pantalla de login de GDM               -> Class=greeter   -> se ignora
#   - sesion vieja olvidada                  -> IdleHint=yes    -> se ignora
sesion_interactiva_activa() {
  local s tty clase idle
  for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
    tty=$(loginctl show-session "$s" -p TTY --value 2>/dev/null || true)
    clase=$(loginctl show-session "$s" -p Class --value 2>/dev/null || true)
    idle=$(loginctl show-session "$s" -p IdleHint --value 2>/dev/null || true)
    if [ -n "$tty" ] && [ "$clase" = "user" ] && [ "$idle" = "no" ]; then
      echo "sesion $s en $tty"
      return 0
    fi
  done
  return 1
}

armar_alarma() {
  local epoch
  epoch=$(proxima_alarma_epoch)
  # Limpiar primero: el kernel rechaza escribir sobre una alarma ya puesta.
  echo 0 > "$RTC_ALARM"
  echo "$epoch" > "$RTC_ALARM"
  log "alarma RTC armada para $(date -d "@$epoch" '+%F %T %Z') (epoch $epoch)"
}

case "${1:-}" in
  avisar)
    wall "El quiosco (exodia) se apaga a las 17:00 por horario automatico. Guarda tu trabajo." 2>/dev/null || true
    log "aviso enviado por wall"
    ;;

  apagar)
    ahora=$(date +%H%M)
    if quien=$(sesion_interactiva_activa); then
      if [ "$ahora" -lt "$FORZAR_DESDE" ]; then
        log "APAGADO POSTERGADO: $quien esta activa; se reintenta en el proximo tick"
        exit 0
      fi
      log "APAGADO FORZADO: $quien sigue activa pero ya son las $ahora (corte duro)"
      wall "El quiosco (exodia) se apaga AHORA por corte duro del horario automatico." 2>/dev/null || true
    fi
    armar_alarma
    log "apagando"
    systemctl poweroff
    ;;

  armar)
    # Red de seguridad al arrancar: si exodia se apagara sin pasar por 'apagar'
    # (crash, corte de luz), sin esto no quedaria alarma y no despertaria.
    armar_alarma
    ;;

  probar-alarma)
    # Valida que el BIOS honra la alarma RTC desde S5 (apagado total), que es el
    # unico supuesto del diseño que no se puede verificar sin un ciclo real.
    # Reutilizable tras cambios de BIOS o de hardware.
    #
    # No deja el sistema en mal estado: al arrancar, quiosco-armar-alarma.service
    # rearma la alarma para el proximo dia habil.
    mins="${2:-3}"
    epoch=$(( $(date +%s) + mins * 60 ))
    echo 0 > "$RTC_ALARM"
    echo "$epoch" > "$RTC_ALARM"
    log "PRUEBA: alarma en $mins min -> $(date -d "@$epoch" '+%F %T %Z')"
    grep -iE "alrm_time|alrm_date|alarm_IRQ" /proc/driver/rtc | sed 's/^/  /'
    log "PRUEBA: apagando. Si el BIOS honra la alarma, debe encender sola."
    systemctl poweroff
    ;;

  estado)
    echo "ahora:            $(date '+%F %T %Z')"
    echo "proxima alarma:   $(date -d "@$(proxima_alarma_epoch)" '+%F %T %Z')"
    a=$(cat "$RTC_ALARM" 2>/dev/null || echo "")
    if [ -n "$a" ] && [ "$a" != "0" ]; then
      echo "alarma RTC armada: $(date -d "@$a" '+%F %T %Z') (epoch $a)"
    else
      echo "alarma RTC armada: NINGUNA"
    fi
    if quien=$(sesion_interactiva_activa); then
      echo "guard de sesion:   BLOQUEARIA ($quien)"
    else
      echo "guard de sesion:   dejaria apagar"
    fi
    ;;

  *)
    echo "uso: $0 {avisar|apagar|armar|estado|probar-alarma [minutos]}" >&2
    exit 64
    ;;
esac

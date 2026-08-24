---
name: recover-exodia
description: Recuperar el servicio quiosco en la máquina de producción exodia (172.25.21.37) — despertarla por Wake-on-LAN si está apagada y volver a poner los Chromecast rotando. Use cuando el usuario diga "exodia está apagado", "despierta exodia", "el quiosco no responde", "el Chromecast se quedó en negro", "no está rotando", "levanta producción", "wake on lan a exodia", o cuando /api/status muestre rotating=false o display_ready=false. NO usar para cambios de código, captura de screenshots, ni para configurar links (eso es trabajo normal del repo).
---

# Recuperar quiosco en exodia

Runbook de incidente para el host de producción. Verificado empíricamente el
2026-08-24 (ciclo completo apagado → WoL → servicio rotando).

## Hard rules

- **Triage antes de actuar.** Nunca mandes el magic packet sin comprobar primero
  si la máquina ya está encendida. Los tres síntomas se parecen desde la UI pero
  tienen causas distintas.
- **Manda el magic packet UNA vez y espera 4 minutos.** El POST del ThinkStation
  P510 tarda **2m37s** (memory training) antes de que el kernel arranque.
  Reintentar no acelera nada y hace creer que el WoL falló cuando no falló.
- **Captura la línea base antes de cualquier acción disruptiva**: guarda el
  `/api/status` completo, no solo el HTTP code. Sin el `rotating` previo no
  puedes saber si dejaste el servicio como estaba.
- **No apagues exodia** para probar nada salvo que el usuario lo pida explícito
  y confirme acceso físico. Es producción, y si el WoL fallara alguien tiene que
  ir al botón.
- **No hardcodees las IPs de los Chromecast.** Son DHCP y cambian (cc1 ha sido
  172.25.19.160 y 172.25.19.83). Léelas siempre de `/api/status`.

## Datos fijos

| Qué | Valor |
|---|---|
| Host | exodia — `ssh exodia`, `172.25.21.37`, user `cip-exodia`, hostname `a012413` |
| Hardware | Lenovo ThinkStation P510 (`30B4S1Q000`), Ubuntu 24.04 |
| NIC | `eno1`, MAC **`6c:0b:84:e2:5e:fc`**, driver `e1000e` |
| WoL | `Supports Wake-on: pumbg`, `Wake-on: g` — viene del firmware, **persiste** entre reboots |
| Servicio | `http://172.25.21.37:8000`, contenedor `quiosco-quiosco-1`, `restart=unless-stopped` |
| Chromecast | `cc1` (ITU_Chromecast 1), `cc2` (ITU CHROMECAST 3) |

**Requisito de red**: el magic packet solo funciona desde un host en
`172.25.21.0/24` (en este laptop, la interfaz USB `enx000e9a0afaa0` =
`172.25.21.36`). Desde el WiFi (`172.25.19.x`) **no** funciona: el router
descarta el broadcast dirigido cross-subnet.

## Paso 1 — Triage

```bash
ping -c 2 -W 2 172.25.21.37
curl -s -o /dev/null -m 8 -w "http=%{http_code}\n" http://172.25.21.37:8000/
```

| Ping | HTTP | Diagnóstico | Ir a |
|---|---|---|---|
| muerto | muerto | máquina apagada | Paso 2 (WoL) |
| OK | muerto | máquina arriba, contenedor caído | Paso 4 |
| OK | 200 | máquina y servicio OK; el problema es rotación/display | Paso 5 |

## Paso 2 — Despertar (solo si el ping está muerto)

El script aborta solo si no estás en el subnet correcto. Mándalo **una vez**:

```bash
python3 -c "
import socket,subprocess,sys
# La causa #1 de fallo es salir por el subnet equivocado: forzamos el origen.
out=subprocess.run(['ip','-4','-o','addr','show'],capture_output=True,text=True).stdout
src=next((l.split()[3].split('/')[0] for l in out.splitlines() if '172.25.21.' in l), None)
if not src:
    sys.exit('ABORTA: este host no tiene IP en 172.25.21.0/24 -> el paquete no llegaria. Conecta el ethernet USB.')
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
s.bind((src,0))
m=bytes.fromhex('6c0b84e25efc')
s.sendto(b'\xff'*6+m*16,('172.25.21.255',9))
print(f'magic packet enviado desde {src} -> 172.25.21.255:9')
"
```

## Paso 3 — Esperar el arranque (~4 min, es lo normal)

Cronología medida el 2026-08-24, desde el envío del paquete:

| t+ | Evento |
|---|---|
| 0s | magic packet → la máquina enciende |
| 0–2m37s | **firmware/POST** (memory training del P510) — silencio total, ni ping |
| 2m48s | kernel arranca |
| ~3m24s | responde al ping |
| ~4m | quiosco sirviendo en `:8000` |

`sleep` en foreground está bloqueado por el harness; usa un until-loop:

```bash
i=0; until ping -c 1 -W 1 172.25.21.37 >/dev/null 2>&1 || [ $i -ge 60 ]; do i=$((i+1)); sleep 5; done
ping -c 1 -W 1 172.25.21.37 >/dev/null 2>&1 && echo "arrancó en ~$((i*5))s" || echo "NO arrancó tras 5 min"
```

Si tras **5 minutos completos** no hay ping, ahí sí es fallo real. Ver
*Cuando el WoL de verdad falla*.

## Paso 4 — Contenedor (solo si el HTTP sigue muerto con ping OK)

```bash
ssh exodia 'docker ps -a --format "{{.Names}} {{.Status}}"; cd ~/quiosco && docker compose up -d'
```

## Paso 5 — Restaurar la rotación (siempre necesario tras un reboot)

**La rotación NO arranca sola.** No hay hook de startup en `main.py` que llame a
`start_rotation`; el `CastManager` pierde su estado en memoria y los Chromecast
quedan en negro aunque el contenedor esté arriba. Este es el síntoma más común
después de un reboot.

El endpoint correcto es `POST /api/chromecasts/{cc_id}/start` (no
`/api/start/{cc_id}`, que no existe). `start_rotation` hace `launch_display()`
y arranca el loop:

```bash
for cc in cc1 cc2; do
  echo "$cc: $(curl -s -m 20 -X POST http://172.25.21.37:8000/api/chromecasts/$cc/start)"
done
```

## Paso 6 — Verificar de verdad

Un `{"ok":true}` no prueba nada. Lo que prueba que quedó bien:

```bash
curl -s -m 10 http://172.25.21.37:8000/api/status | python3 -c "
import json,sys
for c in json.load(sys.stdin)['chromecasts']:
    print(f\"{c['id']} rot={c['rotating']} launched={c['display_launched']} ready={c['display_ready']} \"
          f\"fb={c['fallback_active']} fails={c['dashcast_failures']} idx={c['current_index']} \"
          f\"hb={c['heartbeat_age_seconds']}s {c['current_label']!r} err={c['last_error']!r}\")
"
```

Estado sano: `rot=True launched=True ready=True fb=False fails=0` y
`hb` por debajo de ~3s.

**Y confirma que el índice avanza** — `display_ready` solo dice que la página
carga, no que el loop corra. Con el intervalo por defecto de 120s hay que
esperar hasta 2 minutos para ver `idx` cambiar (`0 → 1`, "Servicios activos" →
"Uptime Robot"). Si necesitas la prueba rápida, `POST /api/chromecasts/{cc}/skip`
fuerza el salto.

## Casos de fallo del Wake-on-LAN

Cada caso está etiquetado por nivel de evidencia: **[obs]** ocurrió realmente en
el incidente del 2026-08-24, **[ver]** probado en directo, **[ded]** deducido del
mecanismo, sin probar. No trates un [ded] como hecho establecido.

| # | Fallo | Señal que ves | Evidencia |
|---|---|---|---|
| 1 | Te rindes antes de los 4 min | silencio total, ni ping | **[obs]** |
| 2 | Origen en el subnet equivocado | nada, y **ningún error** | **[ver]** |
| 3 | Ethernet USB desconectado | nada, o `ENETUNREACH` | **[ded]** |
| 4 | Unicast a la IP en vez de broadcast | nada | **[ver]** |
| 5 | MAC equivocada (`docker0`) | nada | **[ver]** |
| 6 | Esperar que "cualquier tráfico" despierte | nada | flag `Wake-on: g` |
| 7 | Flag WoL reseteado a `d` | nada | **[ded]** |
| 8 | BIOS: ErP / Deep Sleep activo | nada | **[ded]** |
| 9 | Sin corriente (no "apagado", *sin energía*) | nada, jamás | **[ded]** |
| 10 | DHCP movió a exodia | despertó, pero tu ping falla | **[ded]** |
| 11 | Despertó pero las pantallas siguen negras | ping y `:8000` OK | **[obs]** |

### 1. Rendirse antes de los 4 minutos — [obs], el más probable

Es el fallo que ocurrió de verdad. El POST del P510 tarda **2m37s** y durante ese
tiempo no hay ping, ni ARP, ni nada: es indistinguible de un WoL fallido. En el
incidente mandé un segundo paquete a los 3 min creyendo que había fallado; el
`systemd-analyze` demostró después que fue el **primero** el que la despertó.

Diagnóstico: ninguno. Espera. No reintentes antes de 5 minutos.

### 2. Origen en el subnet equivocado — [ver], el más peligroso

`sendto()` desde `172.25.19.187` (WiFi) hacia `172.25.21.255` **retorna con
éxito y sin excepción**. No hay ninguna señal de error: el paquete sale, el
router descarta el broadcast dirigido, y tú te quedas esperando un arranque que
nunca va a pasar.

Por eso el script del Paso 2 aborta si no encuentra IP en `172.25.21.0/24`. Es un
guard, no una comodidad: sin él la falla es indetectable.

### 3. Ethernet USB desconectado — [ded]

Con la USB conectada, el kernel enruta bien los tres destinos:

```text
172.25.21.255    -> dev enx000e9a0afaa0 src 172.25.21.36
255.255.255.255  -> dev enx000e9a0afaa0 src 172.25.21.36
```

Sin ella, el comportamiento se bifurca y es contraintuitivo:

- `172.25.21.255` pierde su ruta conectada → `sendto` **sí** falla (`ENETUNREACH`).
  Falla ruidosa, buena.
- `255.255.255.255` se va **silenciosamente por el WiFi** y nunca llega.

O sea: el broadcast limitado, que parece la variante más robusta, es la que
falla en silencio. Usa siempre el broadcast dirigido `172.25.21.255`.

### 4. Unicast a la IP en vez de broadcast — [ver]

Muchas herramientas y GUIs de WoL mandan por defecto a la IP/hostname. Con la
máquina apagada eso no puede funcionar: la entrada ARP caduca. Observado a los
~3s del apagado:

```text
172.25.21.37 dev enx000e9a0afaa0 INCOMPLETE
```

Sin ARP no hay MAC de destino y el paquete es inentregable. **Siempre broadcast.**

### 5. MAC equivocada — [ver]

La única MAC válida es la de `eno1`: **`6c:0b:84:e2:5e:fc`**. `docker0` es un
decoy activo: su MAC se **regenera en cada boot** (`d2:be:62:c0:f2:7c` antes del
reboot, `2a:87:70:f3:17:de` después). Si scriptreas "toma la primera MAC" o la
lees de un inventario viejo, puedes acabar con la de docker0 y no despertar nada.

### 6. Esperar que "cualquier tráfico" despierte la máquina

`Supports Wake-on: pumbg` pero el flag activo es solo **`g`** = magic packet.
Un ping, un ARP, tráfico unicast o un broadcast normal **no** la despiertan,
aunque el hardware sea capaz (`p`hy, `u`nicast, `m`ulticast, `b`roadcast).
Tiene que ser el frame de 102 bytes con la MAC repetida 16 veces.

### 7. Flag reseteado a `d` — [ded]

Viene del firmware, así que persiste; verificado que sobrevivió el salto de
kernel `6.14.0-27` → `7.0.0-30-generic`. Pero un cambio de driver o un
`ethtool -s eno1 wol d` explícito lo apagarían.

```bash
ssh -t exodia 'sudo ethtool eno1 | grep -i wake-on'   # debe decir: Wake-on: g
# si dice d:
ssh -t exodia 'sudo ethtool -s eno1 wol g'
```

`sudo -n` no funciona en exodia, pide password: este paso lo teclea el usuario.

### 8. BIOS: ErP / Deep Sleep — [ded]

En el P510, *Automatic Power On → Wake on LAN* debe estar activo y *ErP / Deep
Sleep* desactivado (ErP corta la alimentación en standby al NIC en S5). No se
puede leer ni cambiar por SSH: requiere presencia física y entrar al BIOS.

### 9. Sin corriente, que no es lo mismo que apagada — [ded]

El WoL necesita alimentación en standby para mantener el NIC escuchando. Si
exodia perdió corriente de verdad (corte eléctrico, cable desenchufado, UPS
agotada), el WoL es **imposible** por diseño, no importa la configuración.

Distinción operativa: *apagada por software* → WoL funciona. *Sin energía* →
alguien tiene que ir físicamente. Si el WoL falla y descartaste 1–5, este y el 8
son los candidatos, y ambos terminan en el mismo sitio: ir a la máquina.

### 10. DHCP movió a exodia — [ded]

El despertar va por MAC, así que **funciona** aunque la IP haya cambiado. Lo que
se rompe es tu verificación: sigues pingueando `172.25.21.37` y concluyes fallo
cuando la máquina está arriba en otra IP. Confirma por MAC, no por IP:

```bash
ip neigh | grep -i 6c:0b:84:e2:5e:fc
```

### 11. Despertó pero las pantallas siguen negras — [obs]

No es un fallo de WoL, pero el usuario lo reporta igual ("sigue sin funcionar").
Ping y `:8000` responden, y aun así los Chromecast están en negro porque la
rotación no arrancó sola. Es el Paso 5. Fue exactamente lo que pasó el
2026-08-24 tras el reboot.

## Notas de apagado

Por si alguna vez necesitas el ciclo completo: **no puedes apagar exodia por
SSH**. `systemctl poweroff` falla con `Interactive authentication required`
(polkit rechaza sesiones remotas) y `sudo` pide password. El usuario tiene que
teclearlo:

```bash
ssh -t exodia 'sudo poweroff'
```

Ten en cuenta que un reboot aplica kernels pendientes. El del 2026-08-24 saltó
`6.14.0-27` → `7.0.0-30-generic` tras 32 días de uptime; `e1000e` y el WoL
sobrevivieron, pero avisa al usuario del cambio de versión.

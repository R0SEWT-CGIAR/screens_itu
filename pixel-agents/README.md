# Suite de agentes (pixel-agents)

Configuracion versionada del slot **"Suite de agentes"** del quiosco
(`config.json`, entrada `http://172.25.21.37:3456/`).

Lo que sirve esa URL **no es codigo nuestro**: es el paquete npm de terceros
[`pixel-agents`](https://github.com/pixel-agents-hq/pixel-agents) (autor `pablodelucca`,
version en uso **1.4.0**), instalado global via nvm en la laptop de rody:

```
~/.nvm/versions/node/v22.22.0/lib/node_modules/pixel-agents/
```

Lo nuestro es unicamente lo que hay en esta carpeta: los units de systemd, la config,
el mapa de la oficina y el parche de compatibilidad para los Chromecast.
Antes vivia suelto en el home y sin backup.

## Arquitectura

```
Chromecast --> exodia 172.25.21.37:3456 --> [tunel ssh inverso] --> laptop 127.0.0.1:3456
```

El server corre en la **laptop** y escucha solo en loopback: el unico camino de entrada es el
tunel, asi nadie en la LAN ve la oficina de agentes. La conexion la inicia la laptop, por eso
su IP DHCP deja de importar y funciona igual por cableada o WiFi (esquiva el client isolation).

| Archivo | Destino en la laptop | Que hace |
|---|---|---|
| `systemd/pixel-agents.service` | `~/.config/systemd/user/` | server standalone en `127.0.0.1:3456` |
| `systemd/pixel-agents-tunnel.service` | `~/.config/systemd/user/` | `ssh -N -R 172.25.21.37:3456:127.0.0.1:3456 exodia` |
| `scripts/chromecast-compat.sh` | `~/.pixel-agents/` | `ExecStartPre`: transpila `dist/` a Chrome 70 |
| `config/config.json` | `~/.pixel-agents/` | `standalone.watchAllSessions: true` |
| `config/agent-seats.json` | `~/.pixel-agents/` | asiento y paleta de color por agente |
| `config/layout.json` | `~/.pixel-agents/` | **el mapa** de la oficina (tiles, muebles, areas) |

No se versiona `~/.pixel-agents/server.json`, `servers/` (pid + token de runtime) ni
`hooks/claude-hook.js` (lo regenera el propio paquete).

Requisitos fuera de esta carpeta:

- En exodia: `GatewayPorts clientspecified` en `/etc/ssh/sshd_config.d/60-gatewayports.conf`.
  Sin eso sshd **degrada el bind a loopback en silencio**: el tunel parece levantado y los
  Chromecast no llegan.
- `loginctl enable-linger rody` para que ambos services arranquen al boot sin login.
- Ruta de node clavada a **v22.22.0** en el `ExecStart`. Si nvm sube de version, actualizar el
  unit y hacer `daemon-reload`.

## Uso

```bash
# repo -> laptop (restaurar o aplicar cambios versionados)
./pixel-agents/scripts/install.sh          # no sobreescribe config/layout existentes
./pixel-agents/scripts/install.sh --force  # si tambien quieres pisar config y layout

# laptop -> repo (traer lo editado en vivo para commitear)
./pixel-agents/scripts/sync-from-live.sh
```

Se copia, no se hace symlink: un `git switch` a una rama sin esta carpeta borraria los units
en caliente y el quiosco se quedaria sin el slot tras el siguiente boot.

## Editar el mapa de la oficina

El mapa vive en `~/.pixel-agents/layout.json` y **se edita desde la propia web**, no a mano.
La vista del quiosco es un screenshot (`render_mode: live_screenshot`), asi que hay que abrir
la app en un navegador **de la laptop**: <http://127.0.0.1:3456/>.

1. Boton **"Edit office layout"** para entrar al editor.
2. Herramientas: pintar piso (`tile_paint`), pintar alfombra (`carpet_paint`, `P` copia
   variante y colores de un tile existente), colocar mueble (`furniture_place`) y
   copiar mueble (`furniture_pick`).
3. Al guardar, la web manda `saveLayout` por websocket y el server reescribe
   `~/.pixel-agents/layout.json` (escritura atomica via `.tmp` + rename).
4. Tambien hay **Import Layout** / export (`pixel-agents-layout.json`) para mover un mapa
   entre maquinas. Import avisa que pisa el mapa actual.
5. Cerrar con `./pixel-agents/scripts/sync-from-live.sh` + commit, o el cambio queda sin backup.

Estructura de `layout.json`: `cols`/`rows` (50x30 en el mapa actual), `tiles` como array plano
de `cols*rows` indices (`255` = vacio), mas `furniture`, `areas`, `areaTiles`, `tileColors`,
`pets` y `revision`. El default del paquete esta en `dist/assets/default-layout-1.json`
por si hace falta partir de cero.

Los asientos (`agent-seats.json`) se guardan aparte, con mensaje `saveAgentSeats`: mapean cada
agente a un `seatId` del layout (`conf-chair-1`, `off1-chair-a`, ...) con `palette` y `hueShift`.

## Editar / agregar assets

**No tocar `dist/assets/` del paquete**: `npm i -g pixel-agents` reescribe `dist/` y
`chromecast-compat.sh` tambien lo re-parchea. Los assets propios van en un *pack externo*,
que es lo que existe en `assets-pack/` de esta carpeta (ver su README para el formato).

Se registra una vez y queda en `~/.pixel-agents/config.json` (`externalAssetDirectories`):
la app manda `addExternalAssetDirectory` y el server recarga en caliente **characters, pets y
furniture**. Pisos, paredes y alfombras solo se releen al reiniciar el service.

Al arrancar, el server hace merge de `dist/assets` con cada directorio externo. En muebles el
pack externo **gana** si repite un `id`, asi que se puede reemplazar el `DESK` original sin
tocar el paquete.

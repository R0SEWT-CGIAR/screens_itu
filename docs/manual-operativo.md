# Manual operativo de Quiosco

Guia de instalacion, despliegue, operacion y troubleshooting para personal TI e infraestructura.

Este documento es la referencia operativa principal. El README queda como entrada rapida y mapa de navegacion.

## Guia de instalacion

### Objetivo

Preparar una copia funcional de Quiosco con dependencias, navegador de screenshots, Chromecasts descubiertos y `config.json` listo para despliegue.

### Prerrequisitos

Software:

- Python 3.13+
- `uv`
- Docker y Docker Compose para operacion
- Acceso de terminal al servidor donde correra Quiosco

Red:

- El servidor debe alcanzar los Chromecasts por TCP `8009`.
- El servidor debe alcanzar PRTG interno en `172.25.0.22:443` para tableros internos.
- Los Chromecasts deben poder abrir la URL definida en `PROXY_BASE`.
- El discovery por mDNS puede estar bloqueado en redes corporativas; la operacion normal usa IP directa desde `config.json`.

### 1. Instalar dependencias locales

```bash
uv sync
uv run playwright install chromium
```

Docker instala Chromium dentro de la imagen con `playwright install --with-deps chromium`. La instalacion local es necesaria para desarrollo, pruebas manuales y generacion local de screenshots.

### 2. Descubrir Chromecasts

```bash
uv run quiosco-discover
cat chromecast.json
```

`quiosco-discover` genera `chromecast.json` con `name`, `host`, `port` y `uuid` por dispositivo encontrado.

Si discovery no encuentra dispositivos, validar primero conectividad de red, VLAN, mDNS y acceso TCP `8009`. Para operacion estable se recomienda copiar la IP directa al `config.json`.

### 3. Completar `config.json`

Copiar al menos `host`, `port` y `uuid` de cada Chromecast descubierto.

Nota: `config.json` es solo la semilla. Cuando el watchdog redescubre un Chromecast en otra IP (DHCP), guarda el host/puerto en `data/runtime-state.json` (no versionado, montado como volumen en Docker). Ese estado tiene prioridad sobre `config.json` al arrancar; para forzar la IP de `config.json`, borrar `data/runtime-state.json`.

Ejemplo minimo:

```json
{
  "chromecasts": [
    {
      "id": "cc1",
      "name": "ITU_Chromecast 1",
      "host": "172.25.19.70",
      "port": 8009,
      "uuid": "add083f9-01dd-db70-7bea-a2bfb817c86e",
      "resolution": [1280, 720]
    }
  ],
  "links": [
    {
      "url": "https://172.25.0.22/public/mapshow.htm?id=5254&mapid=...",
      "label": "Servicios activos",
      "zoom": 1.0
    },
    {
      "url": "https://cipotato.org/",
      "label": "CIP landing",
      "zoom": 0.9
    }
  ],
  "default_interval_seconds": 5,
  "screenshot_gif_duration_seconds": 15
}
```

Campos principales:

| Campo | Requerido | Default | Uso |
| --- | --- | --- | --- |
| `chromecasts[].id` | Si | - | Identificador operativo (`cc1`, `cc2`) |
| `chromecasts[].name` | Si | - | Nombre visible en UI |
| `chromecasts[].host` | Si | - | IP del Chromecast |
| `chromecasts[].port` | No | `8009` | Puerto de control |
| `chromecasts[].uuid` | No | generado si falta | Identidad del dispositivo |
| `chromecasts[].resolution` | No | `[1920, 1080]` | Resolucion de salida |
| `links[].url` | Si | - | URL a mostrar |
| `links[].label` | Si | - | Etiqueta en UI |
| `links[].zoom` | No | `1.0` | Escala visual por pagina |
| `default_interval_seconds` | Si | - | Intervalo de rotacion, minimo 5s |
| `screenshot_gif_duration_seconds` | No | `60` | Duracion del GIF por URL screenshot |

### 4. Verificar instalacion local

```bash
uv run uvicorn quiosco.main:app --host 0.0.0.0 --port 8000 --reload
```

En otra terminal:

```bash
curl http://localhost:8000/api/status
```

Abrir la UI:

```text
http://localhost:8000
```

## Guia de despliegue

### Objetivo

Ejecutar Quiosco como servicio operativo con Docker Compose y una URL base alcanzable por los Chromecasts.

### Variable operativa obligatoria

`PROXY_BASE` define la URL que DashCast abre en cada Chromecast:

```bash
PROXY_BASE=http://<IP_DEL_SERVIDOR>:8000
```

Usar una IP o DNS que sea alcanzable desde la red de los Chromecasts. No usar `localhost` para despliegues reales porque el Chromecast resolveria `localhost` contra si mismo.

Si `PROXY_BASE` no se define, la app usa un fallback interno. En produccion debe declararse explicitamente para evitar despliegues dependientes de una IP accidental.

### Opcion A: Docker Compose directo

```bash
PROXY_BASE=http://<IP_DEL_SERVIDOR>:8000 docker compose up -d --build
```

Verificar:

```bash
docker compose logs -f quiosco
curl http://localhost:8000/api/status
```

Detener:

```bash
docker compose down
```

### Opcion B: Scripts operativos

Linux:

```bash
scripts/start.sh
scripts/stop.sh
```

Windows:

```bat
scripts\start.bat
scripts\stop.bat
```

Los scripts ejecutan `docker compose up -d --build` y `docker compose down` desde la raiz del repositorio. Definir `PROXY_BASE` en el entorno antes de iniciar el script.

### Opcion C: Cron en Linux

Editar crontab:

```bash
crontab -e
```

Ejemplo L-V:

```cron
30 7  * * 1-5  cd /ruta/quiosco && PROXY_BASE=http://<IP_DEL_SERVIDOR>:8000 docker compose up -d --build >> /var/log/quiosco_start.log 2>&1
30 16 * * 1-5  cd /ruta/quiosco && docker compose down >> /var/log/quiosco_stop.log 2>&1
```

### Opcion D: Task Scheduler en Windows

Crear dos tareas:

- 07:30: ejecutar `scripts/start.bat`
- 16:30: ejecutar `scripts/stop.bat`

Definir `PROXY_BASE` como variable de entorno del sistema o envolver el script de arranque con la asignacion equivalente.

### Opcion E: Windows con imagen de Docker Hub (recomendada para Windows)

No requiere clonar el repo ni buildear: usa la imagen publicada `cipotato/quiosco`.

1. Instalar Docker Desktop y habilitar "Start Docker Desktop when you sign in".
2. Crear una carpeta de despliegue (p.ej. `C:\quiosco\`) con:
   - `docker-compose.windows.yml` (de este repo)
   - `config.json` (IPs/UUIDs de los Chromecasts y links)
   - `.env` con `PROXY_BASE=http://<IP_LAN_DE_LA_COMPU>:8000`
3. Arrancar:

```bat
cd C:\quiosco
docker compose -f docker-compose.windows.yml up -d
```

4. Permitir el puerto 8000 entrante en el Firewall de Windows si los Chromecasts no cargan la display page.

Para actualizar a una nueva version de la imagen:

```bat
docker compose -f docker-compose.windows.yml pull
docker compose -f docker-compose.windows.yml up -d
```

Limitaciones en Windows (Docker Desktop):

- `network_mode: host` no existe; el compose de Windows mapea `8000:8000`.
- El discovery mDNS no funciona desde el contenedor. Desde la version 0.1.1 el recovery reencuentra por si solo a un Chromecast que cambio de IP (escaneo TCP del /24 de su ultima IP conocida); aun asi se recomienda reserva DHCP para los Chromecasts.

### Opcion F: Linux con imagen de Docker Hub (produccion actual: exodia)

Igual que la Opcion E pero en Linux, donde si existe `network_mode: host`. Es el despliegue actual de produccion en `exodia` (`172.25.21.37`, Ubuntu 24.04).

1. Instalar Docker (`sudo apt install docker.io docker-compose-v2`) y agregar el usuario operativo al grupo `docker`.
2. Crear una carpeta de despliegue (p.ej. `~/quiosco/`) con:
   - `docker-compose.exodia.yml` (de este repo; renombrable a `docker-compose.yml`)
   - `config.json` (IPs/UUIDs de los Chromecasts y links)
   - `static/screenshots/` con los GIFs seed (opcional; se regeneran solos)
3. Arrancar:

```bash
cd ~/quiosco
docker compose up -d
```

4. Si `ufw` esta activo, permitir el puerto 8000 entrante (`sudo ufw allow 8000/tcp`) cuando los Chromecasts no carguen la display page.

Para actualizar a una nueva version de la imagen:

```bash
docker compose pull
docker compose up -d
```

Tras cada reinicio del contenedor la rotacion arranca detenida: iniciar cada Chromecast desde la UI o con `POST /api/chromecasts/<id>/start`.

### Checklist posterior al despliegue

1. Abrir `http://<IP_DEL_SERVIDOR>:8000`.
2. Verificar `GET /api/status`.
3. Confirmar por Chromecast:
   - `connected = true`
   - `display_ready = true` despues del arranque
   - `last_error` vacio o `null`
4. Ejecutar `Debug interno` desde la UI.
5. Confirmar que las paginas avanzan y no quedan en blanco.

## Operacion diaria

### Arranque de turno

```bash
cd /ruta/quiosco
PROXY_BASE=http://<IP_DEL_SERVIDOR>:8000 docker compose up -d --build
```

Verificar estado:

```bash
curl http://localhost:8000/api/status
```

Abrir UI:

```text
http://<IP_DEL_SERVIDOR>:8000
```

### Acciones desde la UI

- Iniciar rotacion por pantalla.
- Detener rotacion por pantalla.
- Ajustar intervalo global de rotacion.
- Castear una URL puntual por pantalla.
- Ejecutar `Debug interno` (`/cast/startup-check`) para validar carga de todas las paginas.

### Monitoreo durante el turno

Revisar periodicamente:

- Dispositivos desconectados.
- `display_ready=false`.
- `reconnect_attempts` en aumento.
- Mensajes de `last_error`.
- Paginas en blanco o sin actualizacion.

Logs:

```bash
docker compose logs -f quiosco
```

### Cierre de turno

1. Confirmar estado final en UI y API.
2. Si corresponde apagar servicio:

```bash
cd /ruta/quiosco
docker compose down
```

3. Registrar incidencias con hora, pantalla afectada, URL implicada, accion aplicada y resultado.

## Troubleshooting

### Matriz de incidentes

| Sintoma | Causa probable | Accion inmediata | Escalar cuando |
| --- | --- | --- | --- |
| Chromecast aparece desconectado | IP/puerto incorrecto o red no accesible | Re-ejecutar `uv run quiosco-discover`, validar `config.json`, revisar TCP `8009` | No reconecta tras 2 ciclos de watchdog (~30s) |
| `display_ready=false` sostenido | DashCast no quedo activo o `PROXY_BASE` no es alcanzable | Verificar `PROXY_BASE`, abrir `/cast/display?cc_id=cc1`, revisar logs | Persiste luego de reiniciar el servicio |
| `fallback_active=true` en `/api/status` | DashCast no lanza (p.ej. receiver caido o sin internet); pantallas muestran GIFs via Default Media Receiver | Nada urgente: es el modo degradado esperado; revisar logs para la causa del fallo de DashCast | Sigue en fallback por horas o los GIFs quedan congelados |
| Dashboard PRTG en blanco | Falla de acceso a `172.25.0.22` o proxy | Ejecutar `Debug interno`, validar alcance a PRTG desde servidor | PRTG responde en red pero no renderiza via Quiosco |
| Sitio externo no carga en iframe | Restricciones `X-Frame-Options`, Cloudflare o CSP | Confirmar si esta en modo screenshot; evaluar reemplazo de URL | El sitio es critico y no existe alternativa |
| GIF de screenshot no cambia | Falla de captura Playwright, timeout o Chromium ausente | Revisar logs, comprobar `uv run playwright install chromium` local o imagen Docker actualizada | Falla continua en varios ciclos de captura |
| Rotacion no avanza | Intervalo invalido, estado detenido o error de current index | Revisar UI, `GET /api/status` y `GET /api/current/<id>` | El indice queda inconsistente tras reinicio |

### Comandos utiles

Estado global:

```bash
curl http://localhost:8000/api/status
```

Estado de pagina actual por Chromecast:

```bash
curl http://localhost:8000/api/current/cc1
```

Validacion visual:

```text
http://localhost:8000/cast/startup-check
```

Logs:

```bash
docker compose logs -f quiosco
```

### Criterios de escalamiento tecnico

Escalar al equipo de desarrollo cuando:

- Hay regresion reproducible en rutas `/proxy` o `/p`.
- Watchdog no recupera un dispositivo luego de reiniciar el servicio.
- Existen errores de rotacion con `current_index` inconsistente.
- Se requiere incorporar nuevos dominios en estrategia screenshot/proxy.
- Un cambio de configuracion afecta pantallas en produccion y no existe rollback claro.

## Referencia tecnica

### Modos de renderizado

| Tipo de URL | Deteccion | Modo de render | Ruta efectiva |
| --- | --- | --- | --- |
| Interna PRTG | Host `172.25.0.22` | `iframe` | `/proxy/{path}` |
| Externa proxyable | URL fuera de PRTG y fuera de lista screenshot | `iframe` | `/p/{origin_encoded}/{path}` |
| Externa no proxyable | Host en `SCREENSHOT_SITES` | `img` con GIF | `/static/screenshots/{asset}.gif` |

La lista `SCREENSHOT_SITES` vive en `src/quiosco/main.py`. Cualquier cambio requiere reiniciar la app.

### Flujo de casting

1. DashCast carga `GET /cast/display?cc_id=<id>`.
2. La display page contiene todos los `iframe` o `img` pre-cargados.
3. JavaScript consulta `GET /api/current/<id>` cada 2 segundos.
4. La rotacion actualiza `current_index`; no recarga DashCast en cada cambio.

### Recuperacion automatica

- Watchdog asincrono cada 15s (`WATCHDOG_INTERVAL_SECONDS`).
- Verifica socket, handshake y receiver activo.
- El poll de la display page a `/api/current` (cada 2s) actua como heartbeat: `display_ready=true` significa DashCast activo **y** pagina cargada latiendo. Si rota sin heartbeat por mas de 60s (`DISPLAY_HEARTBEAT_TIMEOUT_SECONDS`), cuenta como degradacion aunque DashCast corra (caso tipico: `PROXY_BASE` apunta a una IP muerta y el logo de DashCast queda pegado, incidente 2026-07-20).
- Si detecta degradacion, reconecta y relanza display page (con gracia de 45s tras cada lanzamiento de DashCast).
- Si la reconexion falla, busca al Chromecast por nombre via discovery mDNS (cooldown 60s). Como el mDNS no cruza subredes (el servidor puede estar en otra, p.ej. exodia en `172.25.21.0/24` y los Chromecasts en `172.25.19.0/24`), si no aparece escanea el /24 de su ultima IP conocida buscando el puerto 8009 y confirma identidad por nombre via `eureka_info` (puerto 8008, cooldown 120s). La IP nueva se persiste en `data/runtime-state.json`, nunca en `config.json` (incidente 2026-08-03: cc1 se mudo de `.160` a `.54` por DHCP).
- Si habia rotacion activa, la restablece manteniendo `current_index`.
- `GET /api/status` expone `heartbeat_age_seconds` por Chromecast (~2s en operacion sana; `null` si nunca cargo).

### Fallback a Default Media Receiver

Cuando DashCast falla 3 veces seguidas tras la gracia (`FALLBACK_AFTER_FAILURES`, p.ej. `CAST_INIT_TIMEOUT` como en el incidente del 2026-05-18), el watchdog activa modo fallback:

- La rotacion castea el GIF de screenshot del link actual con el Default Media Receiver (`CC1AD845`), que es el receiver oficial de Google y no depende del receiver de DashCast.
- Para que el fallback cubra tambien los dashboards PRTG, el ciclo de captura de GIFs incluye las URLs internas (con bypass de certificado); esos GIFs solo se usan como asset de respaldo, el modo normal sigue siendo iframe.
- Cada 5 min (`FALLBACK_DASHCAST_RETRY_SECONDS`) reintenta DashCast; sale del fallback solo cuando la display page vuelve a latir (heartbeat posterior al relanzamiento), no basta con que la app DashCast corra.
- `GET /api/status` expone `fallback_active` y `dashcast_failures` por Chromecast.

### Proxy PRTG

El proxy interno requiere tres capas:

1. Bypass SSL con `httpx` y `verify=False`.
2. Reescritura HTML/CSS de `href`, `src`, `action` y `url(...)` hacia `/proxy/...`.
3. Interceptor JS para reescribir `fetch()` y `XMLHttpRequest.open()` en runtime.

Sin la capa 3, PRTG pierde llamadas dinamicas y aparecen errores de conexion en pagina.

### API resumida

| Metodo | Ruta | Uso |
| --- | --- | --- |
| `GET` | `/api/status` | Estado global |
| `GET` | `/api/current/{id}` | URL/indice actual por Chromecast |
| `POST` | `/api/chromecasts/{id}/start` | Iniciar rotacion |
| `POST` | `/api/chromecasts/{id}/stop` | Detener rotacion |
| `POST` | `/api/chromecasts/{id}/cast` | Castear URL puntual |
| `PUT` | `/api/config/interval` | Cambiar intervalo global |
| `GET` | `/cast/display?cc_id=...` | Display page usada por DashCast |
| `GET` | `/cast/startup-check` | Validacion visual de URLs |
| `GET/POST/PUT` | `/proxy/{path}` | Proxy interno a PRTG |
| `GET/POST/PUT` | `/p/{origin}/{path}` | Proxy para externas proxyables |

### Cambios que requieren reinicio

Reiniciar servicio (`docker compose down` + `docker compose up -d --build`) cuando cambie alguno de estos elementos:

- `config.json`
- `PROXY_BASE`
- Lista `SCREENSHOT_SITES`
- Dependencias de captura Playwright/Chromium

### Pruebas de desarrollo

```bash
uv run python -m unittest discover -s tests -v
```

La suite actual cubre generacion de display page, metadatos de screenshot assets y comportamiento principal de watchdog. No cubre completamente proxy end-to-end ni casting con hardware real.

### Referencias

- `README.md`
- `docs/integrations/uptimerobot-data-contract.md`
- `docs/integrations/copilot-studio-uptimerobot-power-automate.md`
- `docs/adr/001-dashcast-para-casting-de-urls.md`
- `docs/adr/002-proxy-reverso-para-urls-internas.md`
- `docs/adr/003-enfoque-hibrido-internas-vs-externas.md`
- `docs/adr/004-conexion-chromecast-por-ip.md`

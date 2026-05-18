# Quiosco

Plataforma web para rotar tableros operativos y paginas institucionales en Chromecasts desde un punto central de control.

El sistema esta orientado a operacion TI: permite iniciar y detener rotacion, castear una URL puntual, validar carga de paginas internas y recuperar dispositivos de forma automatica cuando pierden conectividad.

## Inicio rapido

```bash
# 1) Instalar dependencias
uv sync
uv run playwright install chromium

# 2) Descubrir Chromecasts de la red
uv run python discover.py

# 3) Editar config.json con host/port/uuid y URLs a mostrar

# 4) Levantar en Docker (recomendado para operacion)
PROXY_BASE=http://<IP_DEL_SERVIDOR>:8000 docker compose up -d --build

# 5) Verificar estado general
curl http://localhost:8000/api/status
```

UI de control: `http://localhost:8000`

## Stack

- Backend: Python 3.13, FastAPI, Uvicorn
- Control Chromecast: pychromecast + DashCast
- Capturas para sitios no proxyables: Playwright + Pillow
- Frontend: HTML/JS estatico
- Ejecucion recomendada: Docker Compose

## Prerrequisitos

### Software

- Python 3.13+
- `uv`
- Docker + Docker Compose (produccion)

### Red

- Acceso del servidor a Chromecasts (puerto TCP 8009)
- Acceso del servidor a PRTG (`172.25.0.22:443`) para URLs internas
- Discovery por mDNS puede estar bloqueado en redes corporativas; la operacion normal usa IP directa desde `config.json`

## Configuracion inicial

### 1. Descubrir Chromecasts

```bash
uv run python discover.py
cat chromecast.json
```

`discover.py` genera `chromecast.json` con `name`, `host`, `port`, `uuid` por dispositivo.

### 2. Completar `config.json`

Copiar al menos `host`, `port` y `uuid` de cada Chromecast descubierto.

Ejemplo:

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

### Campos requeridos y opcionales

| Campo | Requerido | Default | Uso |
| --- | --- | --- | --- |
| `chromecasts[].id` | Si | - | Identificador operativo (`cc1`, `cc2`) |
| `chromecasts[].name` | Si | - | Nombre visible en UI |
| `chromecasts[].host` | Si | - | IP del Chromecast |
| `chromecasts[].port` | No | `8009` | Puerto de control |
| `chromecasts[].uuid` | No | generado si falta | Identidad del dispositivo |
| `chromecasts[].resolution` | No | `[1920, 1080]` | Resolucion de salida para display/screenshot |
| `links[].url` | Si | - | URL a mostrar |
| `links[].label` | Si | - | Etiqueta en UI |
| `links[].zoom` | No | `1.0` | Escala visual por pagina |
| `default_interval_seconds` | Si | - | Intervalo de rotacion (minimo 5s) |
| `screenshot_gif_duration_seconds` | No | `60` | Duracion del GIF por URL screenshot |

### Variable de entorno operativa

`PROXY_BASE` define la URL base que usa DashCast para abrir la display page:

```bash
PROXY_BASE=http://<IP_DEL_SERVIDOR>:8000
```

Si no se define, el sistema usa fallback (`http://172.25.19.179:8000`). En entornos institucionales se recomienda declarar `PROXY_BASE` de forma explicita.

## Modos de renderizado y enrutamiento de URLs

| Tipo de URL | Deteccion | Modo de render | Ruta efectiva |
| --- | --- | --- | --- |
| Interna PRTG | Host `172.25.0.22` | `iframe` | `/proxy/{path}` |
| Externa proxyable | URL fuera de PRTG y fuera de lista screenshot | `iframe` | `/p/{origin_encoded}/{path}` |
| Externa no proxyable | Host en `SCREENSHOT_SITES` | `img` con GIF | `/static/screenshots/{asset}.gif` |

Notas:

- La lista `SCREENSHOT_SITES` vive en `main.py` (`cipotato.org`, `cgiar.org`, `www.cgiar.org`, `stats.uptimerobot.com`).
- Cualquier cambio en `SCREENSHOT_SITES` requiere reiniciar la app.

## Operacion diaria

### Arranque

```bash
docker compose up -d --build
```

### Estado

```bash
curl http://localhost:8000/api/status
```

Revisar por Chromecast: `connected`, `rotating`, `display_ready`, `last_error`, `reconnect_attempts`.

### UI de control

En `http://localhost:8000`:

- Iniciar/detener rotacion por dispositivo
- Ajustar intervalo global
- Castear una URL puntual por dispositivo
- Ejecutar `Debug interno` (`/cast/startup-check`) para validar carga de todas las paginas

### Consulta desde Copilot Studio

Para que un agente responda preguntas como `OCS esta caido?` usando el estado publico de UptimeRobot, ver:

- `docs/integrations/uptimerobot-data-contract.md`
- `docs/integrations/copilot-studio-uptimerobot-power-automate.md`

### Detener

```bash
docker compose down
```

## Arquitectura operativa

### Flujo de casting

1. DashCast carga una sola vez `GET /cast/display?cc_id=<id>`.
2. La display page contiene todos los `iframe` o `img` pre-cargados.
3. JavaScript en la display page consulta `GET /api/current/<id>` cada 2 segundos.
4. La rotacion solo actualiza `current_index`; no recarga DashCast en cada cambio.

### Recuperacion automatica

- Watchdog asincrono cada 15s (`WATCHDOG_INTERVAL_SECONDS`)
- Verifica socket, handshake y receiver activo
- Si detecta degradacion, reconecta y relanza display page
- Si habia rotacion activa, la restablece manteniendo `current_index`

### Proxy PRTG (3 capas obligatorias)

1. Bypass SSL: `httpx` con `verify=False`
2. Reescritura HTML/CSS: rutas absolutas (`href`, `src`, `action`, `url(...)`) hacia `/proxy/...`
3. Interceptor JS: reescribe `fetch()` y `XMLHttpRequest.open()` en runtime

Sin la capa 3, PRTG pierde llamadas dinamicas y aparecen errores de conexion en pagina.

## API resumida

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

## Troubleshooting operativo

| Sintoma | Causa probable | Accion inmediata |
| --- | --- | --- |
| Chromecast aparece desconectado | IP/puerto incorrecto, red no accesible | Re-ejecutar `discover.py`, validar `config.json`, revisar conectividad TCP 8009 |
| `display_ready=false` sostenido | DashCast no quedo activo o `PROXY_BASE` no accesible | Verificar `PROXY_BASE`, abrir `/cast/display?cc_id=cc1` en navegador, revisar logs |
| Pagina PRTG en blanco | Problema de acceso a `172.25.0.22` o fallo de proxy | Probar `/cast/startup-check`, confirmar reachability a PRTG desde el servidor |
| Sitio externo no carga en iframe | `X-Frame-Options`/Cloudflare | Confirmar si esta en modo screenshot o ajustar estrategia de URL |
| GIF no se actualiza | Captura fallo (Playwright/timeout) | Revisar logs y verificar instalacion de Chromium |

Comando de logs en Docker:

```bash
docker compose logs -f quiosco
```

## Despliegue institucional

### Opcion A: Script operativo

- Linux: `scripts/start.sh`, `scripts/stop.sh`
- Windows: `scripts/start.bat`, `scripts/stop.bat`

### Opcion B: Cron (L-V)

```bash
crontab -e
```

```cron
30 7  * * 1-5  cd /ruta/quiosco && docker compose up -d --build >> /var/log/quiosco_start.log 2>&1
30 16 * * 1-5  cd /ruta/quiosco && docker compose down >> /var/log/quiosco_stop.log 2>&1
```

### Opcion C: Task Scheduler (Windows)

- 07:30: ejecutar `scripts/start.bat`
- 16:30: ejecutar `scripts/stop.bat`

## Pruebas

```bash
uv run python -m unittest discover -s tests -v
```

Cobertura actual enfocada en:

- Generacion de display page
- Metadatos de screenshot assets
- Comportamiento principal de watchdog

No cubre completamente proxy end-to-end ni casting con hardware real.

## Referencias

- Manual operativo: `docs/manual-operativo.md`
- ADRs de arquitectura: `docs/adr/`
  - `001-dashcast-para-casting-de-urls.md`
  - `002-proxy-reverso-para-urls-internas.md`
  - `003-enfoque-hibrido-internas-vs-externas.md`
  - `004-conexion-chromecast-por-ip.md`

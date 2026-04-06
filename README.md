# Quiosco

App web para mostrar dashboards y landing pages institucionales en pantallas con Chromecast, con rotacion automatica entre URLs.

## Stack

- **Backend:** Python 3.13 + FastAPI + uvicorn
- **Chromecast:** pychromecast + DashCast (app receiver para URLs arbitrarias)
- **Frontend:** HTML/JS estatico (sin framework)
- **Package manager:** uv

## Setup

```bash
uv sync
uv run playwright install chromium
```

## Uso

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Abrir `http://localhost:8000` para acceder a la UI de control.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

## Configuracion

Editar `config.json`:

```json
{
  "chromecasts": [
    {
      "id": "cc1",
      "name": "ITU_Chromecast 1",
      "host": "172.25.19.171",
      "port": 8009,
      "uuid": "add083f9-01dd-db70-7bea-a2bfb817c86e"
    }
  ],
  "links": [
    {
      "url": "https://www.cgiar.org/",
      "label": "CGIAR landing"
    }
  ],
  "default_interval_seconds": 30
}
```

- **chromecasts:** Lista de dispositivos. `host` y `uuid` se obtienen con `uv run python discover.py`.
- **links:** URLs a rotar. Soporta URLs internas (PRTG en 172.25.0.22) y externas.
- **default_interval_seconds:** Intervalo de rotacion por defecto (ajustable desde la UI, minimo 5s).

### Descubrir Chromecasts

```bash
uv run python discover.py
```

Genera `chromecast.json` con nombre, host, port y uuid de cada Chromecast en la red.

## Arquitectura

```
main.py              FastAPI app, endpoints API, proxy reverso, wrapper page
cast_manager.py      CastManager: conexion, rotacion, control de Chromecasts
config.json          Configuracion de Chromecasts, links e intervalo
static/index.html    UI web de control
discover.py          Script de descubrimiento de Chromecasts
```

### Flujo de casting

DashCast se carga **una sola vez** con una display page que contiene todos los links pre-cargados. La rotacion cambia la visibilidad CSS del frame activo sin recargar DashCast.

```
DashCast (una vez) → /cast/display?cc_id=cc1
                      ↓
                      N frames pre-cargados (iframes o screenshots)
                      ↓
                      Polling /api/current/cc1 cada 2s
                      ↓
                      Toggle display:block/none del frame activo
```

- URLs internas (172.x): iframes apuntan a `/proxy/{path}` (proxy reverso)
- URLs externas proxyables: iframes apuntan a `/p/{origin}/{path}`
- URLs marcadas como screenshot (`cgiar.org`, `cipotato.org`): la display page usa `<img>` y refresca el `src` con `?v={mtime_ns}` cuando cambia el PNG en disco
- Las capturas se indexan por URL completa: `hostname_sanitized + sha256(url)[:12]`

### Debug interno

La comprobacion de operatividad ahora es una herramienta manual para la persona que lanza la app:

- enlace: `/cast/startup-check`
- acceso: boton `Debug interno` en la UI principal
- contenido: todas las URLs configuradas
- secuencia: una pagina cada 10s, una sola vuelta completa
- final: la ultima pagina configurada queda visible
- panel: muestra el estado de cada pantalla (`Pendiente`, `Cargada`, `Sin respuesta`, `Error`)

La pagina es autonoma y no hace polling a `/api/current/{cc_id}`. Sirve para validar en navegador el pipeline real de cada pantalla antes de interactuar con los Chromecast.

### Endurance de Chromecast

Un watchdog asíncrono corre cada 15s y verifica por dispositivo:

- que exista cliente de Chromecast
- que el socket siga conectado
- que el handshake siga respondiendo
- que DashCast siga siendo el receiver activo cuando la display page está lanzada

Si detecta degradación, reintenta conexión y relanza la display page sin perder el `current_index`. Si la rotación estaba activa, la recupera con el mismo intervalo.

### Proxy reverso para PRTG (172.25.0.22)

Las paginas PRTG tienen cert SSL auto-firmado que el Chromecast no acepta. El proxy hace tres cosas criticas:

1. **Bypass SSL:** `httpx` con `verify=False` hace fetch a PRTG y sirve por HTTP plano
2. **Reescritura de rutas absolutas en HTML:** PRTG usa rutas absolutas (`/css/...`, `/javascript/...`, `/images/...`). El proxy las reescribe a `/proxy/css/...`, `/proxy/javascript/...`, etc. Las rutas relativas (como `mapshow_simple.htm`) no se tocan — resuelven correctamente relativo a la URL del documento.
3. **Interceptor JS (fetch/XHR):** Se inyecta un script que intercepta `fetch()` y `XMLHttpRequest.open()` para reescribir rutas absolutas en runtime (`/api/...` → `/proxy/api/...`). Sin esto, las APIs de auto-refresh de PRTG fallan con "Lost connection to PRTG server".

Ver [ADR-002](docs/adr/002-proxy-reverso-para-urls-internas.md) para detalle de enfoques que NO funcionaron (`<base>` tag, proxy universal).

### API

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/api/status` | Estado de Chromecasts, links e intervalo |
| GET | `/api/current/{id}` | Estado actual de render (`index`, `current_url`, `render_mode`, `asset_key`, `asset_revision`) |
| POST | `/api/chromecasts/{id}/start` | Iniciar rotacion automatica |
| POST | `/api/chromecasts/{id}/stop` | Detener rotacion |
| POST | `/api/chromecasts/{id}/cast` | Castear URL especifica (body: `{url, label}`) |
| PUT | `/api/config/interval` | Cambiar intervalo (body: `{seconds}`) |
| GET | `/cast/display?cc_id=...` | Display page con iframes (cargada por DashCast) |
| GET | `/cast/startup-check` | Pagina autonoma de debug para URLs internas |
| GET/POST/PUT | `/proxy/{path}` | Proxy reverso a 172.25.0.22 |

## Problemas conocidos

- **cipotato.org:** El Revolution Slider no renderiza completamente en el browser del Chromecast (limitacion del hardware/browser integrado).
- **Cloudflare:** Sitios protegidos por Cloudflare (cipotato.org) no se pueden proxear (challenge JS), van directo al Chromecast.
- **X-Frame-Options:** URLs externas con `X-Frame-Options: SAMEORIGIN` no cargan en los iframes de la display page.
- **proxy_base:** La IP del servidor (`172.25.19.179`) esta hardcodeada en `main.py`. Cambiar si la IP de la maquina cambia.

## ADRs

Ver [docs/adr/](docs/adr/) para decisiones de arquitectura.

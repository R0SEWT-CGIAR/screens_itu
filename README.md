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
```

## Uso

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Abrir `http://localhost:8000` para acceder a la UI de control.

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

DashCast se carga **una sola vez** con una display page que contiene todos los links en iframes pre-cargados. La rotacion cambia la visibilidad CSS del iframe activo (sin recargar DashCast).

```
DashCast (una vez) → /cast/display?cc_id=cc1
                      ↓
                      N iframes pre-cargados (uno por link)
                      ↓
                      Polling /api/current/cc1 cada 2s
                      ↓
                      Toggle display:block/none del iframe activo
```

- URLs internas (172.x): iframes apuntan a `/proxy/{path}` (proxy reverso)
- URLs externas: iframes apuntan directo (pueden fallar por X-Frame-Options)

### Proxy reverso para PRTG (172.25.0.22)

Las paginas PRTG tienen cert SSL auto-firmado que el Chromecast no acepta. El proxy hace tres cosas criticas:

1. **Bypass SSL:** `httpx` con `verify=False` hace fetch a PRTG y sirve por HTTP plano
2. **Reescritura de rutas absolutas en HTML:** PRTG usa rutas absolutas (`/css/...`, `/javascript/...`, `/images/...`). El proxy las reescribe a `/proxy/css/...`, `/proxy/javascript/...`, etc. Las rutas relativas (como `mapshow_simple.htm`) no se tocan — resuelven correctamente relativo a la URL del documento.
3. **Interceptor JS (fetch/XHR):** Se inyecta un script que intercepta `fetch()` y `XMLHttpRequest.open()` para reescribir rutas absolutas en runtime (`/api/...` → `/proxy/api/...`). Sin esto, las APIs de auto-refresh de PRTG fallan con "Lost connection to PRTG server".

Ver [ADR-002](docs/adr/002-proxy-reverso-para-urls-internas.md) para detalle de enfoques que NO funcionaron (`<base>` tag, proxy universal).

### API

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/api/status` | Estado de Chromecasts, links, intervalo |
| GET | `/api/current/{id}` | Index actual para polling de la display page |
| POST | `/api/chromecasts/{id}/start` | Iniciar rotacion automatica |
| POST | `/api/chromecasts/{id}/stop` | Detener rotacion |
| POST | `/api/chromecasts/{id}/cast` | Castear URL especifica (body: `{url, label}`) |
| PUT | `/api/config/interval` | Cambiar intervalo (body: `{seconds}`) |
| GET | `/cast/display?cc_id=...` | Display page con iframes (cargada por DashCast) |
| GET/POST/PUT | `/proxy/{path}` | Proxy reverso a 172.25.0.22 |

## Problemas conocidos

- **cipotato.org:** El Revolution Slider no renderiza completamente en el browser del Chromecast (limitacion del hardware/browser integrado).
- **Cloudflare:** Sitios protegidos por Cloudflare (cipotato.org) no se pueden proxear (challenge JS), van directo al Chromecast.
- **X-Frame-Options:** URLs externas con `X-Frame-Options: SAMEORIGIN` no cargan en los iframes de la display page.
- **proxy_base:** La IP del servidor (`172.25.19.179`) esta hardcodeada en `main.py`. Cambiar si la IP de la maquina cambia.

## ADRs

Ver [docs/adr/](docs/adr/) para decisiones de arquitectura.

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

```
UI (browser) → API → CastManager → pychromecast/DashCast → Chromecast
```

DashCast (app ID 84912283) es una app publica de Chromecast que renderiza cualquier URL en el browser integrado del dispositivo.

### Proxy reverso

Las URLs internas (172.25.0.22, servidor PRTG) tienen certificado SSL auto-firmado que el browser del Chromecast no acepta. Para resolverlo:

1. El CastManager reescribe URLs internas a una **wrapper page** servida por este servidor
2. La wrapper page tiene viewport fijo 1920x1080 y un iframe fullscreen
3. El iframe apunta a `/proxy/all?url=...` que hace fetch al origen sin verificar SSL
4. Las URLs absolutas en el HTML/CSS/JS se reescriben para pasar por el proxy

Las URLs externas (cgiar.org, cipotato.org) se envian directo al Chromecast con `force=True` en DashCast (bypass de X-Frame-Options). No se pueden proxear porque usan Cloudflare.

### API

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/api/status` | Estado de Chromecasts, links, intervalo |
| POST | `/api/chromecasts/{id}/start` | Iniciar rotacion automatica |
| POST | `/api/chromecasts/{id}/stop` | Detener rotacion |
| POST | `/api/chromecasts/{id}/cast` | Castear URL especifica (body: `{url, label}`) |
| PUT | `/api/config/interval` | Cambiar intervalo (body: `{seconds}`) |
| GET | `/cast/view?url=...` | Wrapper page para Chromecast (uso interno) |
| GET | `/proxy/all?url=...` | Proxy universal con reescritura de URLs |
| GET | `/proxy/{path}` | Proxy legacy para recursos internos con rutas relativas |

## Problemas conocidos

- **cipotato.org:** El Revolution Slider no renderiza completamente en el browser del Chromecast (limitacion del hardware/browser integrado).
- **Cloudflare:** Sitios protegidos por Cloudflare (cipotato.org) no se pueden proxear, van directo al Chromecast.
- **Viewport externas:** Las URLs externas cargan directo en el Chromecast sin control de viewport (no pasan por la wrapper page).
- **proxy_base:** La IP del servidor (`172.25.19.179`) esta hardcodeada en `main.py`. Cambiar si la IP de la maquina cambia.

## ADRs

Ver [docs/adr/](docs/adr/) para decisiones de arquitectura.

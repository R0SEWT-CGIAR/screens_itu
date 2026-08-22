# Quiosco

Quiosco es un servicio FastAPI para controlar la rotacion de tableros web en Chromecasts desde una UI central.

Esta pensado para operacion TI: permite iniciar y detener rotacion, castear una URL puntual, validar carga de paginas internas y recuperar dispositivos cuando pierden conectividad.

## Inicio rapido

```bash
# 1) Instalar dependencias locales
uv sync
uv run playwright install chromium

# 2) Descubrir Chromecasts de la red
uv run quiosco-discover

# 3) Completar config.json con host, port, uuid y URLs

# 4) Levantar en Docker para operacion
PROXY_BASE=http://<IP_DEL_SERVIDOR>:8000 docker compose up -d --build

# 5) Verificar estado
curl http://localhost:8000/api/status
```

UI de control:

```text
http://localhost:8000
```

## Guias operativas

La guia principal para infraestructura y soporte esta en [docs/manual-operativo.md](docs/manual-operativo.md).

| Necesidad | Donde leer |
| --- | --- |
| Instalar dependencias y preparar `config.json` | [Guia de instalacion](docs/manual-operativo.md#guia-de-instalacion) |
| Desplegar con Docker, scripts, cron o Task Scheduler | [Guia de despliegue](docs/manual-operativo.md#guia-de-despliegue) |
| Operar durante el turno | [Operacion diaria](docs/manual-operativo.md#operacion-diaria) |
| Resolver incidentes frecuentes | [Troubleshooting](docs/manual-operativo.md#troubleshooting) |
| Revisar rutas, reinicios y arquitectura | [Referencia tecnica](docs/manual-operativo.md#referencia-tecnica) |

## Stack

- Backend: Python 3.13, FastAPI, Uvicorn
- Control Chromecast: pychromecast + DashCast
- Capturas para sitios no proxyables: Playwright + Pillow
- Frontend: HTML/JS estatico
- Ejecucion recomendada: Docker Compose

## Configuracion esencial

El runtime se configura con `config.json` y `PROXY_BASE`.

Campos principales:

- `chromecasts[].id`: identificador operativo (`cc1`, `cc2`)
- `chromecasts[].host`: IP del Chromecast
- `chromecasts[].port`: puerto de control, normalmente `8009`
- `chromecasts[].uuid`: identidad del dispositivo
- `chromecasts[].resolution`: resolucion de salida
- `chromecasts[].playlist`: ids de los links que muestra esa pantalla; sin el, muestra todos
- `links[].id`: id estable derivado de la URL, lo asigna el servicio
- `links[].url`: URL a mostrar
- `links[].label`: etiqueta visible en la UI
- `links[].zoom`: escala visual por pagina
- `links[].enabled`: un link deshabilitado sale de la rotacion sin perder su configuracion
- `links[].render_mode`: usar `live_screenshot` para apps que requieren Chromium moderno
- `default_interval_seconds`: intervalo de rotacion, minimo 5 segundos
- `screenshot_gif_duration_seconds`: duracion de GIFs generados por screenshot
- `live_screenshot_interval_seconds`: frecuencia de PNGs en vivo; por defecto, 2 segundos

La consola web (`http://<servidor>:8000/`) edita `config.json` en caliente: links,
playlists por pantalla e intervalo. Guarda un backup en `data/config-backups/` antes de
cada escritura. Con el servicio arriba conviene configurar desde ahi y no por SSH, porque
el proceso tiene la configuracion en memoria y el proximo guardado sobreescribe el archivo.

`PROXY_BASE` debe apuntar a una URL alcanzable desde la red de los Chromecasts:

```bash
PROXY_BASE=http://<IP_DEL_SERVIDOR>:8000
```

En despliegues institucionales se recomienda declararlo siempre de forma explicita.

## Desarrollo

Levantar servidor local:

```bash
uv run uvicorn quiosco.main:app --host 0.0.0.0 --port 8000 --reload
```

Ejecutar pruebas:

```bash
uv run python -m unittest discover -s tests -v
```

Cobertura actual enfocada en:

- Generacion de display page
- Metadatos de screenshot assets
- Comportamiento principal de watchdog

No cubre completamente proxy end-to-end ni casting con hardware real.

## Arquitectura resumida

DashCast carga una sola pagina:

```text
/cast/display?cc_id=<chromecast-id>
```

Esa pagina contiene todos los `iframe` o `img` pre-cargados. La rotacion solo cambia `current_index`; no recarga DashCast en cada paso.

Tipos de render:

| Tipo de URL | Modo | Ruta efectiva |
| --- | --- | --- |
| PRTG interno (`172.25.0.22`) | `iframe` por proxy interno | `/proxy/{path}` |
| Externa proxyable | `iframe` por proxy externo | `/p/{origin}/{path}` |
| Externa no proxyable | `img` con GIF generado | `/static/screenshots/{asset}.gif` |
| App con `render_mode: live_screenshot` | `img` con PNG actualizado | `/static/screenshots/{asset}.png` |

Detalles estables de arquitectura:

- [DashCast para casting de URLs](docs/adr/001-dashcast-para-casting-de-urls.md)
- [Proxy reverso para URLs internas](docs/adr/002-proxy-reverso-para-urls-internas.md)
- [Enfoque hibrido internas vs externas](docs/adr/003-enfoque-hibrido-internas-vs-externas.md)
- [Conexion Chromecast por IP](docs/adr/004-conexion-chromecast-por-ip.md)

## Referencias

- Manual operativo: [docs/manual-operativo.md](docs/manual-operativo.md)
- Integracion UptimeRobot/Copilot Studio:
  - [Contrato de datos UptimeRobot](docs/integrations/uptimerobot-data-contract.md)
  - [Copilot Studio, UptimeRobot y Power Automate](docs/integrations/copilot-studio-uptimerobot-power-automate.md)

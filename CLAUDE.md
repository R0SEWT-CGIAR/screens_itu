# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Quiosco is a Python web app that rotates web pages (PRTG dashboards, institutional landing pages) on Chromecasts. It uses FastAPI for the backend/API, pychromecast with DashCast for Chromecast control, and a static HTML/JS frontend.

## Commands

```bash
uv sync                                                    # Install dependencies
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload  # Run dev server
uv run python discover.py                                  # Discover Chromecasts on LAN
```

No tests or linter configured.

## Architecture

### Casting flow (display page approach)

DashCast loads ONE page (`/cast/display?cc_id=cc1`) that contains all links as pre-loaded iframes. Rotation toggles CSS visibility — no DashCast reload, no loading screen.

The display page polls `/api/current/{cc_id}` every 2s to know which iframe to show. `CastManager._rotation_loop()` only increments `current_index` — it does NOT call `load_url()` on each rotation.

### PRTG proxy (critical — three layers needed)

Internal URLs (172.25.0.22) need a proxy because PRTG has an invalid SSL cert. The proxy at `/proxy/{path}` does three things that are ALL required:

1. **SSL bypass:** httpx fetches from PRTG with `verify=False`, serves via HTTP
2. **HTML URL rewriting:** Rewrites absolute paths in HTML attributes (`href="/css/..."` → `href="/proxy/css/..."`, same for `src`, `action`). Relative paths are NOT touched — they resolve correctly relative to the proxy URL.
3. **JS fetch/XHR interceptor:** Injected script that overrides `fetch()` and `XMLHttpRequest.open()` to rewrite `/path` → `/proxy/path`. Without this, PRTG's runtime API calls fail with "Lost connection to PRTG server".

**What does NOT work for PRTG** (documented in ADR-002):
- `<base href="/proxy/">` — only affects relative URLs in HTML, not absolute paths or JS requests
- Proxy universal (`/proxy/all?url=...`) — breaks relative URL resolution

### External URLs

Sites behind Cloudflare (cipotato.org) can't be proxied (403 JS challenge). They load directly in iframes on the display page — may fail if the site sets `X-Frame-Options: SAMEORIGIN`.

### Key classes

- **`CastManager`** (`cast_manager.py`): Manages Chromecast connections, rotation state, and display page launch. `launch_display()` loads DashCast once; `_rotation_loop()` only updates `current_index`.
- **`TimedDashCastController`** (`cast_manager.py`): Subclass of `DashCastController` with timing logs.

### Chromecast connection

pychromecast requires a `CastInfo` with at least one `HostServiceInfo(host, port)` in the `services` set. Without `HostServiceInfo`, `.wait()` times out silently. Host/port/uuid come from `config.json` (populated via `discover.py`).

### Configuration

`config.json` holds Chromecast devices (id, name, host, port, uuid), links (url, label), and default rotation interval. `PROXY_BASE` se lee de la env var (fallback: `http://172.25.19.179:8000`).

## Despliegue (TLDR)

```bash
# 1. Copiar repo a la maquina destino e instalar Docker
# 2. Crear .env con la IP de la maquina
echo 'PROXY_BASE=http://<TU_IP>:8000' > .env

# 3. Ajustar config.json con los Chromecasts de la red

# 4. Levantar
docker compose up -d --build

# 5. Verificar
curl http://localhost:8000/api/status

# 6. Parar
docker compose down
```

**Horario 7:30-16:30 (L-V):**

Linux (cron): `crontab -e`
```
30 7  * * 1-5  cd /ruta/quiosco && docker compose up -d
30 16 * * 1-5  cd /ruta/quiosco && docker compose down
```

Windows: Task Scheduler con `scripts/start.bat` (7:30) y `scripts/stop.bat` (16:30).

## ADRs

Architecture Decision Records in `docs/adr/` document key decisions: DashCast usage, reverse proxy for SSL, hybrid internal/external strategy, and IP-based Chromecast connection.

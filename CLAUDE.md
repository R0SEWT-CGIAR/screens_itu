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

### Casting flow

`UI → FastAPI API → CastManager → pychromecast/DashCast → Chromecast`

DashCast (app ID 84912283) is a public Chromecast receiver app that renders arbitrary URLs.

### Hybrid proxy strategy (critical to understand)

There are two types of URLs with different casting strategies:

- **Internal URLs (172.25.0.22, PRTG):** Have invalid self-signed SSL cert that the Chromecast browser rejects. These go through a wrapper page + reverse proxy: `CastManager._proxy_url()` rewrites to `/cast/view?url=...` → wrapper HTML with 1920x1080 iframe → `/proxy/all?url=...` fetches with `verify=False`. DashCast loads with `force=False`.

- **External URLs (cgiar.org, cipotato.org):** Protected by Cloudflare (can't proxy, returns 403). Sent directly to Chromecast via DashCast with `force=True` (bypasses X-Frame-Options). `TimedDashCastController` always uses `force_launch=True` to relaunch DashCast after force mode replaces the receiver.

### Key classes

- **`CastManager`** (`cast_manager.py`): Manages Chromecast connections, URL routing (proxy vs direct), and rotation via `asyncio.Task`. Connects using `CastInfo` + `HostServiceInfo` (not discovery).
- **`TimedDashCastController`** (`cast_manager.py`): Subclass of `DashCastController` that logs cast timing and handles the force/force_launch logic.

### Chromecast connection

pychromecast requires a `CastInfo` with at least one `HostServiceInfo(host, port)` in the `services` set. Without `HostServiceInfo`, `.wait()` times out silently. Host/port/uuid come from `config.json` (populated via `discover.py`).

### Configuration

`config.json` holds Chromecast devices (id, name, host, port, uuid), links (url, label), and default rotation interval. The `proxy_base` IP (`172.25.19.179`) is hardcoded in `main.py`.

## ADRs

Architecture Decision Records in `docs/adr/` document key decisions: DashCast usage, reverse proxy for SSL, hybrid internal/external strategy, and IP-based Chromecast connection.

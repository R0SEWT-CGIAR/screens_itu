# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

Quiosco is a Python 3.13 FastAPI service that rotates web pages on Chromecasts from a central control point. It renders internal PRTG dashboards, institutional pages, and selected external status pages through a hybrid iframe/proxy/screenshot approach.

Core runtime files (packaged under `src/quiosco/`):

- `src/quiosco/main.py`: FastAPI app, API routes, display pages, proxy routes, and static UI routes.
- `src/quiosco/cast_manager.py`: Chromecast connection state, DashCast launch, rotation state, and watchdog behavior.
- `src/quiosco/screenshot.py` / `src/quiosco/screenshot_assets.py`: Playwright/Pillow screenshot capture and stable asset naming.
- `src/quiosco/discover.py`: network discovery CLI (`quiosco-discover` entry point).
- `static/index.html`: browser control UI.
- `config.json`: runtime Chromecast and link configuration (lives at repo root).

## Source of Truth

Use these sources in order:

1. `CLAUDE.md` for Claude Code behavior.
2. `AGENTS.md` for general repository agent behavior.
3. `.beads/` for current tasks, status, decisions, and work tracking.
4. `docs/adr/` and `docs/manual-operativo.md` for stable architecture and operations.
5. `README.md` for human-facing project usage.

Do not create extra planning markdown files unless explicitly requested. Use Beads for durable task tracking.

## Beads Workflow

This repository uses **bd (Beads)** as the shared ticketing system.

Start substantial sessions with:

```bash
git status
bd prime
bd ready
```

For assigned or selected work:

```bash
bd show <id>
bd update <id> --claim
```

For new discovered work:

```bash
bd create "Title" --type task --priority 2
```

Before finishing:

```bash
uv run python -m unittest discover -s tests -v
bd close <id>
bd dolt status
bd dolt push
git status
```

Rules:

- Use `bd` for all durable task tracking.
- Use `bd remember` for persistent project knowledge; do not create `MEMORY.md`.
- Do not close a Beads issue without committed deliverables or an explicit handoff note.
- If a validation gate is not applicable, document why in the Beads issue or final handoff.

## Repository Strategy

Current branch strategy:

- `main`: stable deployable branch.
- `feature/<short-name>`: short-lived branch for focused work.

Completed work follows:

```text
feature/* -> main
```

Do not introduce a `dev` branch unless the team explicitly changes the workflow. Do not use branches as permanent project folders.

The worktree may contain unrelated user changes. Do not revert or overwrite them unless explicitly requested.

## Commands

```bash
uv sync
uv run playwright install chromium
uv run uvicorn quiosco.main:app --host 0.0.0.0 --port 8000 --reload
uv run quiosco-discover
uv run python -m unittest discover -s tests -v
PROXY_BASE=http://<server-ip>:8000 docker compose up -d --build
docker compose down
```

Testing uses the standard-library `unittest` suite in `tests/`. No linter is currently configured.

## Architecture

### Casting flow

DashCast loads one display page:

```text
/cast/display?cc_id=<chromecast-id>
```

That page contains all links as preloaded iframes or screenshot images. Rotation toggles CSS visibility, so DashCast does not reload on every step.

The display page polls:

```text
/api/current/{cc_id}
```

`CastManager._rotation_loop()` updates `current_index`; it does not call `load_url()` on each rotation.

### PRTG proxy (critical — three layers needed)

Internal PRTG URLs use host `172.25.0.22` and need a proxy because PRTG has an invalid SSL certificate. The `/proxy/{path}` route must preserve all three behaviors:

1. SSL bypass through `httpx` with `verify=False`.
2. HTML URL rewriting for absolute `href`, `src`, and `action` paths.
3. Injected JS `fetch()` and `XMLHttpRequest.open()` interception for runtime API calls.

Do not replace this with only `<base href="/proxy/">` or a universal query-param proxy; ADR-002 explains why those approaches fail.

### External URLs

External proxyable URLs use `/p/{origin_encoded}/{path}`. Some Cloudflare or frame-restricted sites cannot be proxied reliably and use screenshot assets instead.

The screenshot/proxy split is operationally sensitive. Changes to screenshot domains, generated GIF behavior, or proxy rewriting require focused tests and deployment notes.

### Chromecast connection

pychromecast requires a `CastInfo` with at least one `HostServiceInfo(host, port)` in the `services` set. Without `HostServiceInfo`, `.wait()` can time out silently.

Host, port, UUID, resolution, and link data come from `config.json`.

## Operational Notes

`PROXY_BASE` must point to a URL reachable from the Chromecast network. If missing, the app uses its configured fallback, but production deployments should set it explicitly.

The Copilot Studio / Power Automate / UptimeRobot docs in `docs/` are auxiliary external-integration material. They are not part of the core Chromecast runtime unless a Beads issue explicitly asks for implementation.

## ADRs

Architecture Decision Records in `docs/adr/` document key decisions:

- DashCast for arbitrary URL casting.
- Reverse proxy for internal SSL-invalid PRTG URLs.
- Hybrid internal/external rendering strategy.
- IP-based Chromecast connection.

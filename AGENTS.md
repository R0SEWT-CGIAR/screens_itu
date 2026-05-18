# Repository Guidelines

## Project

Quiosco is a Python 3.13 FastAPI service for controlling Chromecast dashboard rotation.

Core application code lives under `src/quiosco/`:

- `src/quiosco/main.py` exposes the API, proxy routes, display pages, and static UI routes.
- `src/quiosco/cast_manager.py` manages Chromecast connection state and rotation.
- `src/quiosco/screenshot.py` and `src/quiosco/screenshot_assets.py` handle screenshot-based rendering for pages that cannot be proxied.
- `src/quiosco/discover.py` is the network discovery CLI (also exposed as the `quiosco-discover` entry point).
- `static/index.html` is the browser UI.
- `docs/` contains operational docs and ADRs (`docs/integrations/` for external-integration material).
- `scripts/` contains startup helpers.
- `tests/` contains the standard-library `unittest` suite.

Runtime configuration is in `config.json`. Keep local secrets or deployment-specific values in `.env`, using `.env.example` as the template.

## Source of Truth

Use these sources in order:

1. `CLAUDE.md` for Claude Code-specific behavior.
2. `AGENTS.md` for general agent behavior in this repository.
3. `.beads/` for current tasks, status, decisions, and work tracking.
4. `docs/adr/` and `docs/manual-operativo.md` for stable technical and operational grounding.
5. `README.md` for human-facing project usage.

Do not create extra planning markdown files unless explicitly requested. Use Beads for task planning and tracking.

## Beads

This project uses **bd (Beads)** as the shared ticketing system.

Run `bd prime` for the current workflow reference at the start of substantial work.

Quick reference:

```bash
bd ready
bd show <id>
bd update <id> --claim
bd create "Title" --type task --priority 2
bd close <id>
bd dolt status
bd dolt pull
bd dolt push
```

Rules:

- Use `bd` for all durable task tracking.
- Do not use markdown TODO lists for project tracking.
- Use `bd remember` for persistent project knowledge; do not add `MEMORY.md` files.
- Do not close a Beads issue until the deliverable is committed or the remaining state is explicitly documented in the issue.
- If a task reveals unrelated work, create or update a Beads issue instead of expanding scope silently.

## Repository Strategy

Branches are temporary work streams, not permanent folders.

Current branch strategy:

- `main`: stable deliverable branch.
- `feature/<short-name>`: short-lived branch for focused work.

Completed work follows:

```text
feature/* -> main
```

Rules:

- Keep `main` stable and deployable.
- Do not introduce a `dev` branch unless the team explicitly changes the workflow.
- Do not use branches as permanent project folders.
- Prefer short-lived branches with narrow scope.
- Preserve unrelated user changes in the worktree; never revert them unless explicitly requested.

## Build, Test, and Development Commands

```bash
uv sync
uv run playwright install chromium
uv run uvicorn quiosco.main:app --host 0.0.0.0 --port 8000 --reload
uv run quiosco-discover
uv run python -m unittest discover -s tests -v
PROXY_BASE=http://<server-ip>:8000 docker compose up -d --build
docker compose down
```

Testing uses Python `unittest`; no linter is currently configured.

## Development Workflow

Before work:

```bash
git status
bd prime
bd ready
bd show <id>
bd update <id> --claim
```

For new work:

```bash
git checkout main
git pull --rebase
git checkout -b feature/<short-name>
```

Before finishing code changes:

```bash
uv run python -m unittest discover -s tests -v
bd close <id>
bd dolt status
bd dolt push
git status
```

If a validation gate is not applicable, state why in the Beads issue or session handoff.

## Coding Style and Testing

Use standard Python style with 4-space indentation, descriptive snake_case names for functions and variables, and PascalCase for classes.

Keep async code explicit and avoid blocking calls inside FastAPI routes or watchdog loops. Prefer structured JSON/config handling over ad hoc string parsing. Keep edits consistent with nearby code and avoid unrelated refactors.

Tests live in `tests/`. Name files `test_*.py`, test classes after the component under test, and test methods `test_<behavior>`. For code that touches Chromecast hardware, proxying, or screenshots, mock network/device boundaries and use temporary paths as existing tests do.

## Operational Boundaries

Quiosco's core purpose is Chromecast dashboard rotation. Documentation for Copilot Studio, Power Automate, and UptimeRobot is auxiliary external-integration material. Do not add runtime APIs or app behavior for that integration unless a Beads issue explicitly asks for it.

## Security and Configuration

Do not commit real credentials or site-specific secrets. Verify `PROXY_BASE` before deployment; DashCast must reach the service URL from the Chromecast network.

Treat `config.json` changes carefully because host, port, UUID, resolution, and URL settings affect live displays.

## Critical Rules

- Use Beads for durable task tracking.
- Do not invent monitored services, Chromecast devices, URLs, or data sources.
- Do not commit large generated artifacts unless they are intentionally part of the deliverable.
- Do not mix generated screenshots with code changes without calling it out.
- Do not modify final operational config without noting deployment impact.
- Do not close Beads issues without committed deliverables or an explicit handoff note.

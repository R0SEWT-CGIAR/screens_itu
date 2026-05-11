# Repository Guidelines

## Project Structure & Module Organization

Quiosco is a Python 3.13 FastAPI service for controlling Chromecast dashboard rotation.
Core application code lives at the repository root: `main.py` exposes the API and static UI routes, `cast_manager.py` manages Chromecast state and rotation, and `screenshot.py` / `screenshot_assets.py` handle screenshot-based rendering. The browser UI is in `static/index.html`. Operational docs and ADRs are in `docs/`, startup helpers are in `scripts/`, and tests are in `tests/`. Runtime configuration is in `config.json`; keep local secrets or deployment-specific values in `.env`, using `.env.example` as the template.

## Build, Test, and Development Commands

- `uv sync`: install Python dependencies from `pyproject.toml` and `uv.lock`.
- `uv run playwright install chromium`: install Chromium for screenshot capture.
- `uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload`: run the development server.
- `uv run python discover.py`: discover Chromecasts and write `chromecast.json`.
- `uv run python -m unittest discover -s tests -v`: run the test suite.
- `PROXY_BASE=http://<server-ip>:8000 docker compose up -d --build`: build and run the operational container.
- `docker compose down`: stop the container.

## Coding Style & Naming Conventions

Use standard Python style with 4-space indentation, descriptive snake_case names for functions and variables, and PascalCase for classes. Keep async code explicit and avoid blocking calls inside FastAPI routes or watchdog loops. Prefer structured JSON/config handling over ad hoc string parsing. There is no configured formatter or linter, so keep edits consistent with nearby code and avoid unrelated refactors.

## Testing Guidelines

Tests use the standard library `unittest` framework and live in `tests/`. Name files `test_*.py`, test classes after the component under test, and test methods `test_<behavior>`. For code that touches Chromecast hardware, proxying, or screenshots, mock network/device boundaries and use temporary paths as existing tests do.

## Commit & Pull Request Guidelines

Recent history uses short imperative or conventional-style subjects such as `fix race condition in set_interval` and `feat/ autodetect host ip...`. Keep commits focused and describe the operational impact when behavior changes. Pull requests should include a summary, test results, linked issue when applicable, and screenshots or API examples for UI/API changes. Call out configuration changes to `config.json`, `.env`, Docker, or Chromecast discovery.

## Security & Configuration Tips

Do not commit real credentials or site-specific secrets. Verify `PROXY_BASE` before deployment; DashCast must reach the service URL from the Chromecast network. Treat `config.json` changes carefully because host, port, UUID, resolution, and URL settings affect live displays.

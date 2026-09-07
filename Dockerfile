FROM python:3.13-slim

# System deps for Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Chromium (~1.4 GB) va ANTES de copiar cualquier archivo del proyecto.
# Antes esta capa venia despues del COPY de pyproject.toml, y como cada
# release sube la version ahi (y en uv.lock), el bump de 0.1.17 a 0.1.18
# invalidaba el cache y volvia a bajar el navegador entero. Resultado: los
# 18 tags publicados no compartian nada y costaban 1.7 GB de disco cada
# uno en vez de reusar esta capa.
#
# PLAYWRIGHT_VERSION tiene que seguir a la version de playwright en
# uv.lock: si se separan, el navegador que se instala aca no es el que
# busca el runtime y las capturas fallan al arrancar. El workflow de CI
# compara las dos y falla si no coinciden.
ARG PLAYWRIGHT_VERSION=1.58.0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uvx --from playwright==${PLAYWRIGHT_VERSION} playwright install --with-deps chromium

# Install Python dependencies (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy project
COPY . .
RUN uv sync --frozen --no-dev

# Ensure screenshots dir exists
RUN mkdir -p static/screenshots

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "quiosco.main:app", "--host", "0.0.0.0", "--port", "8000"]

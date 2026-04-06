import logging
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from cast_manager import CastManager

logging.basicConfig(level=logging.INFO)

manager = CastManager(proxy_base="http://172.25.19.179:8000")
proxy_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_client
    proxy_client = httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True)
    manager.connect()
    yield
    manager.disconnect()
    await proxy_client.aclose()


app = FastAPI(lifespan=lifespan)


# --- API ---

@app.get("/api/status")
def status():
    return manager.get_status()


@app.post("/api/chromecasts/{cc_id}/start")
async def start_rotation(cc_id: str):
    if not manager.start_rotation(cc_id):
        raise HTTPException(404, f"Chromecast '{cc_id}' no encontrado o no conectado")
    return {"ok": True}


@app.post("/api/chromecasts/{cc_id}/stop")
async def stop_rotation(cc_id: str):
    if not manager.stop_rotation(cc_id):
        raise HTTPException(404, f"Chromecast '{cc_id}' no encontrado")
    return {"ok": True}


class CastRequest(BaseModel):
    url: str
    label: str = ""


@app.post("/api/chromecasts/{cc_id}/cast")
def cast_url(cc_id: str, body: CastRequest):
    if not manager.cast_url(cc_id, body.url, body.label):
        raise HTTPException(404, f"Chromecast '{cc_id}' no encontrado o no conectado")
    return {"ok": True}


class IntervalRequest(BaseModel):
    seconds: float


@app.put("/api/config/interval")
async def set_interval(body: IntervalRequest):
    if body.seconds < 5:
        raise HTTPException(400, "El intervalo mínimo es 5 segundos")
    manager.set_interval(body.seconds)
    return {"ok": True, "interval_seconds": body.seconds}


# --- Proxy para URLs internas con cert inválido ---

INTERNAL_HOST = "172.25.0.22"


def to_proxy_url(url: str, request: Request | None = None, host_header: str = "") -> str:
    """Convierte una URL interna a su versión proxied."""
    if INTERNAL_HOST not in url:
        return url
    base = host_header or (str(request.base_url).rstrip("/") if request else "")
    return f"{base}/proxy/{quote(url, safe='')}"


@app.get("/proxy/{target_url:path}")
async def proxy(target_url: str):
    url = unquote(target_url)
    if INTERNAL_HOST not in url:
        raise HTTPException(400, "Solo se permite proxy a hosts internos")
    resp = await proxy_client.get(url)
    content_type = resp.headers.get("content-type", "text/html")
    body = resp.text
    # Reescribir referencias internas dentro del HTML para que pasen por el proxy
    if "text/html" in content_type:
        body = body.replace(f"https://{INTERNAL_HOST}", f"/proxy/{quote(f'https://{INTERNAL_HOST}', safe='')}")
    return HTMLResponse(content=body, status_code=resp.status_code)


# --- Static / UI ---

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from cast_manager import CastManager

logging.basicConfig(level=logging.INFO)

manager = CastManager(proxy_base="http://172.25.19.179:8000")
proxy_client: httpx.AsyncClient | None = None

INTERNAL_HOST = "172.25.0.22"


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


# --- Wrapper page para Chromecast ---

WRAPPER_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920,initial-scale=1">
<style>
  * { margin: 0; padding: 0; }
  html, body { width: 1920px; height: 1080px; overflow: hidden; background: #000; }
  iframe {
    width: 1920px;
    height: 1080px;
    border: none;
    overflow: hidden;
  }
</style>
</head>
<body>
<iframe src="{iframe_src}" scrolling="no" sandbox="allow-scripts allow-same-origin allow-forms"></iframe>
</body>
</html>"""


@app.get("/cast/view")
def cast_view(url: str):
    encoded = quote(url, safe="")
    iframe_src = f"/proxy/all?url={encoded}"
    html = WRAPPER_HTML.replace("{iframe_src}", iframe_src)
    return HTMLResponse(content=html)


# --- Proxy universal ---

@app.get("/proxy/all")
async def proxy_all(url: str, request: Request):
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(400, "URL inválida")

    resp = await proxy_client.get(url)
    content_type = resp.headers.get("content-type", "application/octet-stream")
    origin = f"{parsed.scheme}://{parsed.netloc}"

    body = resp.content
    if "text/html" in content_type:
        text = resp.text
        # Inyectar <base> para que URLs relativas resuelvan al origen original via proxy
        base_tag = f'<base href="/proxy/all?url={quote(origin, safe="")}" />'
        # Reescribir URLs absolutas al mismo host para que pasen por el proxy
        text = text.replace(f'href="/', f'href="/proxy/all?url={quote(origin + "/", safe="")}')
        text = text.replace(f"href='/", f"href='/proxy/all?url={quote(origin + '/', safe='')}")
        text = text.replace(f'src="/', f'src="/proxy/all?url={quote(origin + "/", safe="")}')
        text = text.replace(f"src='/", f"src='/proxy/all?url={quote(origin + '/', safe='')}")
        text = text.replace(f'action="/', f'action="/proxy/all?url={quote(origin + "/", safe="")}')
        # Reescribir URLs absolutas completas
        text = text.replace(origin + "/", f"/proxy/all?url={quote(origin + '/', safe='')}")
        text = text.replace(origin + '"', f'/proxy/all?url={quote(origin, safe="")}' + '"')
        # Inyectar viewport meta si no existe
        if '<meta name="viewport"' not in text.lower():
            text = text.replace("<head>", '<head><meta name="viewport" content="width=1920">', 1)
            text = text.replace("<HEAD>", '<HEAD><meta name="viewport" content="width=1920">', 1)
        body = text.encode("utf-8")
    elif "text/css" in content_type or "javascript" in content_type:
        text = resp.text
        text = text.replace(origin, f"/proxy/all?url={quote(origin, safe='')}")
        body = text.encode("utf-8")

    return Response(content=body, status_code=resp.status_code, media_type=content_type)


# --- Proxy interno legacy (para recursos con rutas relativas de 172.25.0.22) ---

@app.get("/proxy/{path:path}")
async def proxy_internal(path: str, request: Request):
    target = f"https://{INTERNAL_HOST}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    resp = await proxy_client.get(target)
    content_type = resp.headers.get("content-type", "application/octet-stream")
    return Response(content=resp.content, status_code=resp.status_code, media_type=content_type)


# --- Static / UI ---

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from cast_manager import CastManager

logging.basicConfig(level=logging.INFO)

INTERNAL_HOST = "172.25.0.22"
PROXY_BASE = "http://172.25.19.179:8000"

manager = CastManager(proxy_base=PROXY_BASE)
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


@app.get("/api/current/{cc_id}")
def current(cc_id: str):
    state = manager.states.get(cc_id)
    if not state:
        raise HTTPException(404)
    return {"index": state.current_index, "rotating": state.rotating}


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
        raise HTTPException(400, "El intervalo minimo es 5 segundos")
    manager.set_interval(body.seconds)
    return {"ok": True, "interval_seconds": body.seconds}


# --- Display page para Chromecast ---


def _iframe_src(url: str) -> str:
    """Genera el src del iframe: proxy para internas, directo para externas."""
    if INTERNAL_HOST in url:
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
        src = f"/proxy/{path}"
        if parsed.query:
            src += f"?{parsed.query}"
        return src
    return url


@app.get("/cast/display")
def cast_display(cc_id: str = "cc1"):
    links = manager.links
    iframes_html = ""
    for i, link in enumerate(links):
        src = _iframe_src(link["url"])
        iframes_html += f'  <iframe id="frame-{i}" src="{src}" class="frame" style="display:none"></iframe>\n'

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920,initial-scale=1">
<style>
  * {{ margin: 0; padding: 0; }}
  html, body {{ width: 1920px; height: 1080px; overflow: hidden; background: #000; }}
  .frame {{
    position: absolute;
    top: 0; left: 0;
    width: 1920px;
    height: 1080px;
    border: none;
  }}
</style>
</head>
<body>
{iframes_html}
<script>
  const ccId = "{cc_id}";
  const totalFrames = {len(links)};
  let currentIndex = -1;

  async function poll() {{
    try {{
      const res = await fetch("/api/current/" + ccId);
      const data = await res.json();
      if (data.index !== currentIndex) {{
        // Ocultar el frame actual
        if (currentIndex >= 0 && currentIndex < totalFrames) {{
          document.getElementById("frame-" + currentIndex).style.display = "none";
        }}
        // Mostrar el nuevo
        currentIndex = data.index;
        if (currentIndex >= 0 && currentIndex < totalFrames) {{
          document.getElementById("frame-" + currentIndex).style.display = "block";
        }}
      }}
    }} catch (e) {{
      console.error("Poll error:", e);
    }}
  }}

  // Mostrar el primer frame inmediatamente
  poll();
  setInterval(poll, 2000);
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


# --- Proxy path-based para 172.25.0.22 ---
# /proxy/path?query → https://172.25.0.22/path?query
# Inyecta <base> + override de fetch/XHR para que TODO pase por el proxy.

# JS que intercepta fetch() y XMLHttpRequest para redirigir por el proxy
PROXY_INTERCEPT_JS = """
<script>
(function() {
  function rewrite(url) {
    if (typeof url === 'string' && url.startsWith('/') && !url.startsWith('/proxy/')) {
      return '/proxy' + url;
    }
    return url;
  }
  var origFetch = window.fetch;
  window.fetch = function(input, init) {
    if (typeof input === 'string') input = rewrite(input);
    return origFetch.call(this, input, init);
  };
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    arguments[1] = rewrite(url);
    return origOpen.apply(this, arguments);
  };
})();
</script>
"""


async def _proxy_request(method: str, path: str, request: Request) -> Response:
    target = f"https://{INTERNAL_HOST}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    body = await request.body() if method in ("POST", "PUT", "PATCH") else None
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]

    resp = await proxy_client.request(method, target, content=body, headers=headers)
    content_type = resp.headers.get("content-type", "application/octet-stream")

    resp_body = resp.content
    if "text/html" in content_type:
        text = resp.text
        # Reescribir rutas absolutas en atributos HTML para que pasen por el proxy
        # /css/... → /proxy/css/..., /javascript/... → /proxy/javascript/..., etc.
        text = text.replace('href="/', 'href="/proxy/')
        text = text.replace("href='/", "href='/proxy/")
        text = text.replace('src="/', 'src="/proxy/')
        text = text.replace("src='/", "src='/proxy/")
        text = text.replace('action="/', 'action="/proxy/')
        # Inyectar interceptor JS para fetch/XHR en runtime
        if "<head>" in text:
            text = text.replace("<head>", f"<head>\n{PROXY_INTERCEPT_JS}", 1)
        elif "<HEAD>" in text:
            text = text.replace("<HEAD>", f"<HEAD>\n{PROXY_INTERCEPT_JS}", 1)
        resp_body = text.encode("utf-8")

    return Response(content=resp_body, status_code=resp.status_code, media_type=content_type)


@app.get("/proxy/{path:path}")
async def proxy_get(path: str, request: Request):
    return await _proxy_request("GET", path, request)


@app.post("/proxy/{path:path}")
async def proxy_post(path: str, request: Request):
    return await _proxy_request("POST", path, request)


@app.put("/proxy/{path:path}")
async def proxy_put(path: str, request: Request):
    return await _proxy_request("PUT", path, request)



# --- Static / UI ---

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from cast_manager import CastManager
from screenshot import start_screenshot_task

logging.basicConfig(level=logging.INFO)

PROXY_BASE = "http://172.25.19.179:8000"
PRTG_HOST = "172.25.0.22"
PRTG_ORIGIN = f"https://{PRTG_HOST}"

manager = CastManager(proxy_base=PROXY_BASE)
proxy_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_client
    proxy_client = httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True)
    manager.connect()
    # Start screenshot task for unproxyable URLs
    unproxyable_urls = [l["url"] for l in manager.links if _use_screenshot(l["url"])]
    screenshot_task = None
    if unproxyable_urls:
        screenshot_task = start_screenshot_task(unproxyable_urls, interval_seconds=300)
    yield
    if screenshot_task:
        screenshot_task.cancel()
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

# URLs que no se pueden proxear (Cloudflare JS challenge)
SCREENSHOT_SITES = {"cipotato.org", "www.cgiar.org", "cgiar.org"}


def _use_screenshot(url: str) -> bool:
    parsed = urlparse(url)
    return any(host in parsed.netloc for host in SCREENSHOT_SITES)


def _can_proxy(url: str) -> bool:
    return not _use_screenshot(url)


def _iframe_src(url: str) -> str:
    """Genera el src del iframe: /proxy/ para PRTG, /p/ para externas."""
    parsed = urlparse(url)

    # PRTG interno: usar /proxy/{path}
    if PRTG_HOST in parsed.netloc:
        path = parsed.path.lstrip("/")
        src = f"/proxy/{path}"
        if parsed.query:
            src += f"?{parsed.query}"
        return src

    # Externas proxyables: usar /p/{origin_encoded}/{path}
    origin = f"{parsed.scheme}://{parsed.netloc}"
    origin_encoded = quote(origin, safe="")
    path = parsed.path.lstrip("/")
    src = f"/p/{origin_encoded}/{path}"
    if parsed.query:
        src += f"?{parsed.query}"
    return src


@app.get("/cast/display")
def cast_display(cc_id: str = "cc1"):
    links = manager.links
    iframes_html = ""
    for i, link in enumerate(links):
        url = link["url"]
        if _use_screenshot(url):
            # Screenshot site: use static image
            parsed = urlparse(url)
            hostname = parsed.netloc.replace(".", "_")
            iframes_html += (
                f'  <img id="frame-{i}" src="/static/screenshots/{hostname}.png"'
                f' class="frame" style="display:none; width:1920px; height:1080px; object-fit:cover">\n'
            )
        else:
            src = _iframe_src(url)
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
  let currentIndex = -1;

  async function poll() {{
    try {{
      const res = await fetch("/api/current/" + ccId);
      const data = await res.json();
      if (data.index !== currentIndex) {{
        const oldFrame = document.getElementById("frame-" + currentIndex);
        if (oldFrame) oldFrame.style.display = "none";
        currentIndex = data.index;
        const newFrame = document.getElementById("frame-" + currentIndex);
        if (newFrame) newFrame.style.display = "block";
      }}
    }} catch (e) {{
      console.error("Poll error:", e);
    }}
  }}

  poll();
  setInterval(poll, 2000);
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


# --- Proxy PRTG (172.25.0.22) ---
# /proxy/{path}?query → https://172.25.0.22/{path}?query
# Reescribe rutas absolutas en HTML/CSS y stripea X-Frame-Options.

PRTG_INTERCEPT_JS = """
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


async def _prtg_proxy(path: str, method: str, request: Request) -> Response:
    target = f"{PRTG_ORIGIN}/{path}"
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
        # Reescribir rutas absolutas en HTML
        text = text.replace('href="/', 'href="/proxy/')
        text = text.replace("href='/", "href='/proxy/")
        text = text.replace('src="/', 'src="/proxy/')
        text = text.replace("src='/", "src='/proxy/")
        text = text.replace('action="/', 'action="/proxy/')
        # CSS url() en style inline
        text = text.replace('url("/', 'url("/proxy/')
        text = text.replace("url('/", "url('/proxy/")
        text = text.replace("url(/", "url(/proxy/")
        # Inyectar interceptor JS
        if "<head>" in text:
            text = text.replace("<head>", f"<head>\n{PRTG_INTERCEPT_JS}", 1)
        elif "<HEAD>" in text:
            text = text.replace("<HEAD>", f"<HEAD>\n{PRTG_INTERCEPT_JS}", 1)
        resp_body = text.encode("utf-8")
    elif "text/css" in content_type:
        text = resp.text
        text = text.replace('url("/', 'url("/proxy/')
        text = text.replace("url('/", "url('/proxy/")
        text = text.replace("url(/", "url(/proxy/")
        resp_body = text.encode("utf-8")

    return Response(content=resp_body, status_code=resp.status_code, media_type=content_type)


@app.get("/proxy/{path:path}")
async def prtg_proxy_get(path: str, request: Request):
    return await _prtg_proxy(path, "GET", request)


@app.post("/proxy/{path:path}")
async def prtg_proxy_post(path: str, request: Request):
    return await _prtg_proxy(path, "POST", request)


@app.put("/proxy/{path:path}")
async def prtg_proxy_put(path: str, request: Request):
    return await _prtg_proxy(path, "PUT", request)


# --- Proxy universal para externas ---
# /p/{origin_encoded}/{path}?query → {origin}/{path}?query
# Reescribe rutas absolutas en HTML/CSS y stripea X-Frame-Options.

PROXY_INTERCEPT_TPL = """
<script>
(function() {{
  var prefix = "{prefix}";
  function rewrite(url) {{
    if (typeof url === 'string' && url.startsWith('/') && !url.startsWith('/p/') && !url.startsWith('/api/')) {{
      return prefix + url;
    }}
    return url;
  }}
  var origFetch = window.fetch;
  window.fetch = function(input, init) {{
    if (typeof input === 'string') input = rewrite(input);
    return origFetch.call(this, input, init);
  }};
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {{
    arguments[1] = rewrite(url);
    return origOpen.apply(this, arguments);
  }};
}})();
</script>
"""


async def _proxy_request(origin: str, path: str, method: str, request: Request) -> Response:
    target = f"{origin}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    body = await request.body() if method in ("POST", "PUT", "PATCH") else None
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]

    resp = await proxy_client.request(method, target, content=body, headers=headers)
    content_type = resp.headers.get("content-type", "application/octet-stream")

    origin_encoded = quote(origin, safe="")
    prefix = f"/p/{origin_encoded}"

    resp_body = resp.content
    if "text/html" in content_type:
        text = resp.text
        text = text.replace('href="/', f'href="{prefix}/')
        text = text.replace("href='/", f"href='{prefix}/")
        text = text.replace('src="/', f'src="{prefix}/')
        text = text.replace("src='/", f"src='{prefix}/")
        text = text.replace('action="/', f'action="{prefix}/')
        text = text.replace('url("/', f'url("{prefix}/')
        text = text.replace("url('/", f"url('{prefix}/")
        text = text.replace("url(/", f"url({prefix}/")
        inject = PROXY_INTERCEPT_TPL.format(prefix=prefix)
        if "<head>" in text:
            text = text.replace("<head>", f"<head>\n{inject}", 1)
        elif "<HEAD>" in text:
            text = text.replace("<HEAD>", f"<HEAD>\n{inject}", 1)
        resp_body = text.encode("utf-8")
    elif "text/css" in content_type:
        text = resp.text
        text = text.replace('url("/', f'url("{prefix}/')
        text = text.replace("url('/", f"url('{prefix}/")
        text = text.replace("url(/", f"url({prefix}/")
        resp_body = text.encode("utf-8")

    return Response(content=resp_body, status_code=resp.status_code, media_type=content_type)


@app.get("/p/{origin_encoded}/{path:path}")
async def proxy_get(origin_encoded: str, path: str, request: Request):
    return await _proxy_request(unquote(origin_encoded), path, "GET", request)


@app.post("/p/{origin_encoded}/{path:path}")
async def proxy_post(origin_encoded: str, path: str, request: Request):
    return await _proxy_request(unquote(origin_encoded), path, "POST", request)


@app.put("/p/{origin_encoded}/{path:path}")
async def proxy_put(origin_encoded: str, path: str, request: Request):
    return await _proxy_request(unquote(origin_encoded), path, "PUT", request)


# --- Static / UI ---

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")

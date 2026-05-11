import asyncio
import json
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote, unquote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from cast_manager import CastManager, WATCHDOG_INTERVAL_SECONDS
from screenshot import start_screenshot_task
from screenshot_assets import screenshot_asset_key, screenshot_asset_revision

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROXY_FALLBACK = "http://172.25.19.179:8000"
PRTG_HOST = "172.25.0.22"
PRTG_ORIGIN = f"https://{PRTG_HOST}"


def _local_ip_for(target: str) -> Optional[str]:
    """Local IPv4 the kernel would use to reach `target` (no packet sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 1))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def _detect_local_ip(prefix: str, sentinels: list[str]) -> Optional[str]:
    """Return a local IPv4 starting with prefix, picking the interface that
    routes to one of the given sentinels (typically Chromecast IPs)."""
    for target in sentinels:
        ip = _local_ip_for(target)
        if ip and ip.startswith(prefix):
            return ip
    return None


def _resolve_proxy_base() -> str:
    env_value = os.environ.get("PROXY_BASE", "").strip()
    if env_value:
        logger.info("PROXY_BASE desde env: %s", env_value)
        return env_value

    try:
        with open("config.json") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    subnet = cfg.get("proxy_auto_subnet")
    if subnet:
        sentinels = [cc["host"] for cc in cfg.get("chromecasts", []) if cc.get("host")]
        # Last resort sentinel built from the prefix itself.
        octets = subnet.rstrip(".").split(".")
        while len(octets) < 4:
            octets.append("1")
        sentinels.append(".".join(octets[:4]))

        detected = _detect_local_ip(subnet, sentinels)
        if detected:
            url = f"http://{detected}:8000"
            logger.info("PROXY_BASE auto-detectado (%s): %s", subnet, url)
            return url
        logger.warning(
            "Auto-detect IP no encontró interfaz con prefijo '%s'. Usando fallback %s",
            subnet, PROXY_FALLBACK,
        )

    return PROXY_FALLBACK


PROXY_BASE = _resolve_proxy_base()

manager = CastManager(proxy_base=PROXY_BASE)
proxy_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_client
    proxy_client = httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True)
    manager.connect()
    # Start screenshot task for unproxyable URLs
    # Use first CC resolution as output size (all CCs typically share a screen res)
    first_state = next(iter(manager.states.values()), None)
    cast_w, cast_h = first_state.resolution if first_state else DEFAULT_RESOLUTION
    unproxyable_links = [l for l in manager.links if _use_screenshot(l["url"])]
    unproxyable_urls = [l["url"] for l in unproxyable_links]
    viewport_map = {
        l["url"]: (int(cast_w / l.get("zoom", 1.0)), int(cast_h / l.get("zoom", 1.0)))
        for l in unproxyable_links
    }
    with open("config.json") as f:
        _cfg = json.load(f)
    gif_duration = _cfg.get("screenshot_gif_duration_seconds", 60)
    screenshot_task = None
    if unproxyable_urls:
        screenshot_task = start_screenshot_task(
            unproxyable_urls, interval_seconds=300, gif_duration_seconds=gif_duration,
            viewport_map=viewport_map, output_size=(cast_w, cast_h),
        )
    watchdog_task = manager.start_watchdog_task(interval_seconds=WATCHDOG_INTERVAL_SECONDS)
    yield
    if screenshot_task:
        screenshot_task.cancel()
        await asyncio.gather(screenshot_task, return_exceptions=True)
    watchdog_task.cancel()
    await asyncio.gather(watchdog_task, return_exceptions=True)
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

    link = manager._current_link(state)
    current_url = state.current_url or (link["url"] if link else None)
    render_mode = "screenshot" if current_url and _use_screenshot(current_url) else "iframe"
    asset_key = screenshot_asset_key(current_url) if render_mode == "screenshot" and current_url else None
    asset_revision = screenshot_asset_revision(asset_key)
    return {
        "index": state.current_index,
        "rotating": state.rotating,
        "current_url": current_url,
        "render_mode": render_mode,
        "asset_key": asset_key,
        "asset_revision": asset_revision,
    }


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
DEFAULT_RESOLUTION = (1920, 1080)


def _cc_resolution(cc_id: str) -> tuple[int, int]:
    state = manager.states.get(cc_id)
    if state and hasattr(state, "resolution") and state.resolution:
        return state.resolution
    return DEFAULT_RESOLUTION


def _use_screenshot(url: str) -> bool:
    parsed = urlparse(url)
    return any(host in parsed.netloc for host in SCREENSHOT_SITES)


def _is_internal_url(url: str) -> bool:
    parsed = urlparse(url)
    return PRTG_HOST in parsed.netloc


def _internal_links(links: list[dict] | None = None) -> list[dict]:
    source_links = manager.links if links is None else links
    return [link for link in source_links if _is_internal_url(link["url"])]


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
    cast_w, cast_h = _cc_resolution(cc_id)
    links = manager.links
    iframes_html = ""
    for i, link in enumerate(links):
        url = link["url"]
        zoom = link.get("zoom", 1.0)
        # Viewport del contenido: resolución del CC ajustada por zoom
        vw = int(cast_w / zoom)
        vh = int(cast_h / zoom)
        # Scale para encajar en el cast
        sx = cast_w / vw
        sy = cast_h / vh
        if _use_screenshot(url):
            asset_key = screenshot_asset_key(url)
            iframes_html += (
                f'  <img id="frame-{i}" data-asset-key="{asset_key}"'
                f' class="frame screenshot-frame" style="display:none;'
                f' width:{vw}px; height:{vh}px;'
                f' transform:scale({sx},{sy}); transform-origin:top left;'
                f' object-fit:fill">\n'
            )
        else:
            src = _iframe_src(url)
            iframes_html += (
                f'  <iframe id="frame-{i}" src="{src}" class="frame"'
                f' style="display:none; width:{vw}px; height:{vh}px;'
                f' transform:scale({sx},{sy}); transform-origin:top left;"></iframe>\n'
            )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={cast_w},initial-scale=1">
<style>
  * {{ margin: 0; padding: 0; }}
  html, body {{ width: {cast_w}px; height: {cast_h}px; overflow: hidden; background: #000; }}
  .frame {{
    position: absolute;
    top: 0; left: 0;
    width: {cast_w}px;
    height: {cast_h}px;
    border: none;
  }}
</style>
</head>
<body>
{iframes_html}
<script>
  var ccId = "{cc_id}";
  var currentIndex = -1;
  var currentAssetKey = null;
  var currentAssetRevision = null;

  function screenshotSrc(assetKey, assetRevision) {{
    var version = assetRevision || "pending";
    return "/static/screenshots/" + assetKey + ".gif?v=" + version;
  }}

  function refreshScreenshotFrame(frame, assetKey, assetRevision) {{
    if (!frame || !assetKey) return;
    var nextSrc = screenshotSrc(assetKey, assetRevision);
    if (frame.getAttribute("src") !== nextSrc) {{
      frame.setAttribute("src", nextSrc);
    }}
  }}

  function poll() {{
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/api/current/" + ccId, true);
    xhr.onreadystatechange = function() {{
      if (xhr.readyState !== 4) return;
      if (xhr.status !== 200) return;
      try {{
        var data = JSON.parse(xhr.responseText);
        if (data.index !== currentIndex) {{
          var oldFrame = document.getElementById("frame-" + currentIndex);
          if (oldFrame) oldFrame.style.display = "none";
          currentIndex = data.index;
          var newFrame = document.getElementById("frame-" + currentIndex);
          if (data.render_mode === "screenshot") {{
            refreshScreenshotFrame(newFrame, data.asset_key, data.asset_revision);
            currentAssetKey = data.asset_key;
            currentAssetRevision = data.asset_revision;
          }} else {{
            currentAssetKey = null;
            currentAssetRevision = null;
          }}
          if (newFrame) newFrame.style.display = "block";
        }} else if (
          data.render_mode === "screenshot" &&
          (data.asset_key !== currentAssetKey || data.asset_revision !== currentAssetRevision)
        ) {{
          var curFrame = document.getElementById("frame-" + currentIndex);
          refreshScreenshotFrame(curFrame, data.asset_key, data.asset_revision);
          currentAssetKey = data.asset_key;
          currentAssetRevision = data.asset_revision;
        }} else if (data.render_mode !== "screenshot") {{
          currentAssetKey = null;
          currentAssetRevision = null;
        }}
      }} catch (e) {{
        /* ignore parse errors */
      }}
    }};
    xhr.send();
  }}

  poll();
  setInterval(poll, 2000);
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/cast/startup-check")
def cast_startup_check(cc_id: str = "cc1"):
    cast_w, cast_h = _cc_resolution(cc_id)
    links = manager.links
    if not links:
        return HTMLResponse(
            content=f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={cast_w},initial-scale=1">
<style>
  html, body {{
    width: {cast_w}px;
    height: {cast_h}px;
    margin: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #000;
    color: #fff;
    font-family: system-ui, sans-serif;
  }}
</style>
</head>
<body>No hay paginas configuradas para la comprobacion</body>
</html>""",
        )

    frames_html = ""
    labels = []
    urls = []
    for i, link in enumerate(links):
        url = link["url"]
        zoom = link.get("zoom", 1.0)
        vw = int(cast_w / zoom)
        vh = int(cast_h / zoom)
        sx = cast_w / vw
        sy = cast_h / vh
        if _use_screenshot(url):
            asset_key = screenshot_asset_key(url)
            asset_revision = screenshot_asset_revision(asset_key)
            asset_version = asset_revision if asset_revision is not None else "pending"
            src = f"/static/screenshots/{asset_key}.gif?v={asset_version}"
            frames_html += (
                f'  <img id="startup-frame-{i}" src="{src}"'
                f' class="frame" style="display:none;'
                f' width:{vw}px; height:{vh}px;'
                f' transform:scale({sx},{sy}); transform-origin:top left;'
                f' object-fit:fill">\n'
            )
        else:
            src = _iframe_src(url)
            frames_html += (
                f'  <iframe id="startup-frame-{i}" src="{src}" class="frame"'
                f' style="display:none; width:{vw}px; height:{vh}px;'
                f' transform:scale({sx},{sy}); transform-origin:top left;"></iframe>\n'
            )
        labels.append(link["label"])
        urls.append(url)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={cast_w},initial-scale=1">
<style>
  * {{ margin: 0; padding: 0; }}
  html, body {{ width: {cast_w}px; height: {cast_h}px; overflow: hidden; background: #000; }}
  .frame {{
    position: absolute;
    top: 0; left: 0;
    width: {cast_w}px;
    height: {cast_h}px;
    border: none;
  }}
  .debug-overlay {{
    position: absolute;
    top: 24px;
    left: 24px;
    z-index: 10;
    min-width: 440px;
    max-width: 760px;
    padding: 16px 18px;
    border-radius: 12px;
    background: rgba(15, 23, 42, 0.88);
    color: #f8fafc;
    font-family: system-ui, sans-serif;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.28);
  }}
  .debug-kicker {{
    font-size: 14px;
    color: #93c5fd;
    margin-bottom: 8px;
    letter-spacing: 0.03em;
    text-transform: uppercase;
  }}
  .debug-title {{
    font-size: 28px;
    font-weight: 700;
    line-height: 1.2;
    margin-bottom: 6px;
  }}
  .debug-meta {{
    font-size: 16px;
    color: #cbd5e1;
    margin-bottom: 6px;
  }}
  .debug-url {{
    font-size: 14px;
    color: #94a3b8;
    word-break: break-all;
  }}
  .debug-list {{
    margin-top: 14px;
    display: grid;
    gap: 8px;
  }}
  .debug-item {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 10px;
    border-radius: 8px;
    background: rgba(30, 41, 59, 0.9);
  }}
  .debug-item.active {{
    outline: 1px solid rgba(147, 197, 253, 0.8);
    background: rgba(30, 64, 175, 0.28);
  }}
  .debug-item-name {{
    font-size: 14px;
    color: #e2e8f0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .debug-badge {{
    flex-shrink: 0;
    padding: 4px 8px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }}
  .debug-badge.pending {{
    background: rgba(245, 158, 11, 0.18);
    color: #fbbf24;
  }}
  .debug-badge.loaded {{
    background: rgba(34, 197, 94, 0.18);
    color: #86efac;
  }}
  .debug-badge.timeout {{
    background: rgba(239, 68, 68, 0.18);
    color: #fda4af;
  }}
  .debug-badge.error {{
    background: rgba(190, 24, 93, 0.22);
    color: #f9a8d4;
  }}
</style>
</head>
<body>
{frames_html}
<div class="debug-overlay">
  <div class="debug-kicker">Debug interno</div>
  <div class="debug-title" id="debugTitle">Inicializando comprobacion</div>
  <div class="debug-meta" id="debugMeta">Preparando rotacion de paginas configuradas</div>
  <div class="debug-url" id="debugUrl"></div>
  <div class="debug-list" id="debugList"></div>
</div>
<script>
  const frameCount = {len(links)};
  const stepMs = 10000;
  const loadTimeoutMs = 30000;
  const labels = {json.dumps(labels, ensure_ascii=True)};
  const urls = {json.dumps(urls, ensure_ascii=True)};
  const frames = Array.from({{ length: frameCount }}, (_, index) =>
    document.getElementById("startup-frame-" + index)
  );
  const frameStates = Array.from({{ length: frameCount }}, () => "pendiente");
  let currentIndex = -1;

  function statusLabel(status) {{
    if (status === "loaded") return "Cargada";
    if (status === "timeout") return "Sin respuesta";
    if (status === "error") return "Error";
    return "Pendiente";
  }}

  function renderDebugList() {{
    const list = document.getElementById("debugList");
    list.innerHTML = "";

    labels.forEach((label, index) => {{
      const item = document.createElement("div");
      item.className = "debug-item" + (index === currentIndex ? " active" : "");

      const name = document.createElement("div");
      name.className = "debug-item-name";
      name.textContent = `${{index + 1}}. ${{label}}`;

      const badge = document.createElement("div");
      badge.className = "debug-badge " + frameStates[index];
      badge.textContent = statusLabel(frameStates[index]);

      item.appendChild(name);
      item.appendChild(badge);
      list.appendChild(item);
    }});
  }}

  function showFrame(index) {{
    const oldFrame = document.getElementById("startup-frame-" + currentIndex);
    if (oldFrame) oldFrame.style.display = "none";
    currentIndex = index;
    const frame = document.getElementById("startup-frame-" + currentIndex);
    if (frame) frame.style.display = "block";
    document.getElementById("debugTitle").textContent = labels[index] || `Pagina ${{index + 1}}`;
    document.getElementById("debugMeta").textContent =
      `Paso ${{index + 1}} de ${{frameCount}} · cambio cada ${{stepMs / 1000}}s`;
    document.getElementById("debugUrl").textContent = urls[index] || "";
    renderDebugList();
  }}

  function finishSequence() {{
    document.getElementById("debugMeta").textContent =
      `Comprobacion finalizada · ultima pagina visible`;
    renderDebugList();
  }}

  function advance(nextIndex) {{
    if (nextIndex >= frameCount) {{
      finishSequence();
      return;
    }}

    setTimeout(() => {{
      showFrame(nextIndex);
      advance(nextIndex + 1);
    }}, stepMs);
  }}

  frames.forEach((frame, index) => {{
    const timeoutId = setTimeout(() => {{
      if (frameStates[index] === "pendiente") {{
        frameStates[index] = "timeout";
        renderDebugList();
      }}
    }}, loadTimeoutMs);

    frame.addEventListener("load", () => {{
      clearTimeout(timeoutId);
      frameStates[index] = "loaded";
      renderDebugList();
    }});

    frame.addEventListener("error", () => {{
      clearTimeout(timeoutId);
      frameStates[index] = "error";
      renderDebugList();
    }});
  }});

  renderDebugList();
  showFrame(0);
  advance(1);
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

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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from pathlib import Path

from . import config_store, health
from .cast_manager import CastManager, WATCHDOG_INTERVAL_SECONDS
from .runtime_monitor import start_runtime_monitor_task
from .screenshot import start_live_screenshot_task, start_screenshot_task
from .screenshot_assets import screenshot_asset_key, screenshot_asset_revision

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"

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
        with open(_CONFIG_PATH) as f:
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

manager = CastManager(config_path=str(_CONFIG_PATH), proxy_base=PROXY_BASE)
proxy_client: httpx.AsyncClient | None = None
# Pedidos de recaptura de GIF desde la consola; la crea el lifespan.
recapture_queue: "asyncio.Queue[str] | None" = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_client
    proxy_client = httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True)
    manager.connect()
    # Sin esto un reboot deja los Chromecast conectados pero en negro: connect()
    # no rota. Los que no esten listos aun los recoge el watchdog.
    for cc_id in manager.states:
        manager.maybe_autostart_rotation(cc_id)
    # Start screenshot task for unproxyable URLs
    # Use first CC resolution as output size (all CCs typically share a screen res)
    first_state = next(iter(manager.states.values()), None)
    cast_w, cast_h = first_state.resolution if first_state else DEFAULT_RESOLUTION
    gif_duration = manager.config.get("screenshot_gif_duration_seconds", 60)
    live_interval = manager.config.get("live_screenshot_interval_seconds", 2)

    def _capture_targets() -> tuple[list[str], dict[str, tuple[int, int]]]:
        return gif_capture_targets(manager.links, cast_w, cast_h)

    global recapture_queue
    recapture_queue = asyncio.Queue()
    initial_urls, initial_viewports = _capture_targets()
    # Arranca siempre, incluso sin targets: el tecnico puede agregar un link que
    # necesite screenshot en cualquier momento.
    screenshot_task = start_screenshot_task(
        initial_urls, interval_seconds=300, gif_duration_seconds=gif_duration,
        viewport_map=initial_viewports, output_size=(cast_w, cast_h),
        link_source=_capture_targets, recapture_queue=recapture_queue,
    )

    # La captura en vivo mantiene una pagina abierta por link, asi que su lista
    # se fija al arrancar: dar de alta un link live_screenshot desde la consola
    # todavia necesita reiniciar el servicio.
    live_screenshot_task = None
    live_urls, live_viewport_map = live_capture_targets(manager.links, cast_w, cast_h)
    if live_urls:
        live_screenshot_task = start_live_screenshot_task(
            live_urls,
            interval_seconds=live_interval,
            viewport_map=live_viewport_map,
            output_size=(cast_w, cast_h),
        )
    watchdog_task = manager.start_watchdog_task(interval_seconds=WATCHDOG_INTERVAL_SECONDS)
    runtime_monitor_task = start_runtime_monitor_task()
    yield
    if screenshot_task:
        screenshot_task.cancel()
        await asyncio.gather(screenshot_task, return_exceptions=True)
    if live_screenshot_task:
        live_screenshot_task.cancel()
        await asyncio.gather(live_screenshot_task, return_exceptions=True)
    watchdog_task.cancel()
    await asyncio.gather(watchdog_task, return_exceptions=True)
    runtime_monitor_task.cancel()
    await asyncio.gather(runtime_monitor_task, return_exceptions=True)
    manager.disconnect()
    await proxy_client.aclose()


app = FastAPI(lifespan=lifespan)


# --- API ---

@app.exception_handler(config_store.ConfigError)
async def config_error_handler(request: Request, exc: config_store.ConfigError):
    """Los mensajes de config_store estan escritos para que los lea el tecnico."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def _link_view(link: dict) -> dict:
    """Copia del link con como lo renderizaria la display page.

    La consola dibuja el preview con el mismo src que el Chromecast, asi que lo
    que se ve al ajustar el zoom es lo que va a salir en pantalla. Es una copia:
    manager.links son los dicts que se serializan a config.json.
    """
    view = dict(link)
    if _link_uses_screenshot(link):
        asset_key = screenshot_asset_key(link["url"])
        extension = _screenshot_extension(link)
        revision = screenshot_asset_revision(asset_key, extension)
        view["preview_mode"] = "screenshot"
        view["preview_src"] = (
            f"/static/screenshots/{asset_key}.{extension}?v={revision or 'pending'}"
        )
    else:
        view["preview_mode"] = "iframe"
        view["preview_src"] = _iframe_src(link["url"], direct=link.get("direct", False))
    return view


@app.get("/api/status")
def status():
    payload = manager.get_status()
    for cc in payload["chromecasts"]:
        cc["health"] = health.diagnose(cc, payload["interval_seconds"])
    payload["links"] = [_link_view(link) for link in payload["links"]]
    return payload


@app.get("/api/current/{cc_id}")
def current(cc_id: str, preview: bool = False):
    state = manager.states.get(cc_id)
    if not state:
        raise HTTPException(404)

    # El poll de la display page (cada 2s) es el heartbeat del watchdog.
    # El espejo de la consola pide preview=1 justamente para no contarlo: si lo
    # contara, una pestana abierta en la oficina mantendria display_ready en
    # verde aunque el Chromecast estuviera muerto.
    if not preview:
        manager.note_display_heartbeat(cc_id)

    link = manager._current_link(state)
    current_url = state.current_url or (link["url"] if link else None)
    uses_screenshot = bool(
        current_url
        and (
            (link and link.get("url") == current_url and _link_uses_screenshot(link))
            or _use_screenshot(current_url)
        )
    )
    render_mode = "screenshot" if uses_screenshot else "iframe"
    asset_extension = (
        _screenshot_extension(link)
        if uses_screenshot and link and link.get("url") == current_url
        else ("gif" if uses_screenshot else None)
    )
    asset_key = screenshot_asset_key(current_url) if render_mode == "screenshot" and current_url else None
    asset_revision = screenshot_asset_revision(asset_key, asset_extension or "gif")
    return {
        "index": state.current_index,
        # La display page keyea sus frames por id de link y se recarga sola
        # cuando config_revision cambia (links o playlists editados).
        "link_id": link["id"] if link else None,
        "config_revision": manager.config_revision,
        "rotating": state.rotating,
        "current_url": current_url,
        "render_mode": render_mode,
        "asset_key": asset_key,
        "asset_extension": asset_extension,
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
    # Persiste en config.json: antes el cambio se perdia en cada reinicio.
    seconds = manager.set_interval(body.seconds)
    return {"ok": True, "interval_seconds": seconds}


# --- Configuracion de links (consola de tecnicos) ---

class LinkCreate(BaseModel):
    url: str
    label: str = ""
    zoom: float = 1.0
    optional: bool = False
    direct: bool = False
    enabled: bool = True
    render_mode: str = config_store.DEFAULT_RENDER_MODE


class LinkUpdate(BaseModel):
    url: Optional[str] = None
    label: Optional[str] = None
    zoom: Optional[float] = None
    optional: Optional[bool] = None
    direct: Optional[bool] = None
    enabled: Optional[bool] = None
    render_mode: Optional[str] = None


class LinkOrderRequest(BaseModel):
    link_ids: list[str]


class PlaylistRequest(BaseModel):
    # null devuelve la pantalla a "todos los links habilitados".
    link_ids: Optional[list[str]] = None


class SkipRequest(BaseModel):
    step: int = 1


@app.get("/api/links")
def list_links():
    return {"links": manager.links, "config_revision": manager.config_revision}


@app.post("/api/links", status_code=201)
def create_link(body: LinkCreate):
    link = manager.add_link(body.model_dump())
    return {"ok": True, "link": link, "config_revision": manager.config_revision}


@app.patch("/api/links/{link_id}")
def patch_link(link_id: str, body: LinkUpdate):
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(400, "No se envio ningun campo para actualizar")
    link = manager.update_link(link_id, payload)
    return {"ok": True, "link": link, "config_revision": manager.config_revision}


@app.delete("/api/links/{link_id}")
def remove_link(link_id: str):
    manager.delete_link(link_id)
    return {"ok": True, "config_revision": manager.config_revision}


@app.put("/api/links/order")
def reorder_links(body: LinkOrderRequest):
    links = manager.reorder_links(body.link_ids)
    return {"ok": True, "links": links, "config_revision": manager.config_revision}


@app.post("/api/links/{link_id}/recapture")
def recapture_link(link_id: str):
    """Pide el GIF de un link ahora, sin esperar los 300s del ciclo."""
    link = next((l for l in manager.links if l["id"] == link_id), None)
    if not link:
        raise HTTPException(404, f"No existe el link '{link_id}'")
    if not (_use_screenshot(link["url"]) or _is_internal_url(link["url"])):
        raise HTTPException(
            400,
            f"'{link['label']}' se muestra por iframe y no usa GIF; no hay nada que recapturar",
        )
    if recapture_queue is None:
        raise HTTPException(503, "La captura de screenshots todavia no esta activa")
    recapture_queue.put_nowait(link["url"])
    return {"ok": True, "queued": link["url"]}


@app.put("/api/chromecasts/{cc_id}/playlist")
def set_playlist(cc_id: str, body: PlaylistRequest):
    playlist = manager.set_playlist(cc_id, body.link_ids)
    return {"ok": True, "playlist": playlist, "config_revision": manager.config_revision}


# --- Acciones de recuperacion ---

@app.post("/api/chromecasts/{cc_id}/relaunch")
def relaunch_display(cc_id: str):
    """Fuerza recargar la display page: el arreglo mas comun cuando queda
    trabada con el logo de DashCast."""
    if cc_id not in manager.states:
        raise HTTPException(404, f"Chromecast '{cc_id}' no encontrado")
    manager._relaunch_display(cc_id)
    state = manager.states[cc_id]
    if not state.display_launched:
        raise HTTPException(502, state.last_error or "No se pudo relanzar la display page")
    return {"ok": True}


@app.post("/api/chromecasts/{cc_id}/skip")
def skip_link(cc_id: str, body: SkipRequest):
    if cc_id not in manager.states:
        raise HTTPException(404, f"Chromecast '{cc_id}' no encontrado")
    if not manager.skip(cc_id, body.step):
        raise HTTPException(409, "La pantalla no tiene links para recorrer")
    state = manager.states[cc_id]
    return {"ok": True, "current_label": state.current_label, "current_url": state.current_url}


# --- Display page para Chromecast ---

# URLs que no se pueden proxear (Cloudflare JS challenge)
SCREENSHOT_SITES = {"cipotato.org", "www.cgiar.org", "cgiar.org", "stats.uptimerobot.com"}
DEFAULT_RESOLUTION = (1920, 1080)


def _cc_resolution(cc_id: str) -> tuple[int, int]:
    state = manager.states.get(cc_id)
    if state and hasattr(state, "resolution") and state.resolution:
        return state.resolution
    return DEFAULT_RESOLUTION


def _use_screenshot(url: str) -> bool:
    parsed = urlparse(url)
    return any(host in parsed.netloc for host in SCREENSHOT_SITES)


def _is_live_screenshot(link: dict) -> bool:
    return link.get("render_mode") == "live_screenshot"


def _link_uses_screenshot(link: dict) -> bool:
    return _is_live_screenshot(link) or _use_screenshot(link["url"])


def _screenshot_extension(link: dict) -> str:
    return "png" if _is_live_screenshot(link) else "gif"


def _viewport_map(links: list[dict], cast_w: int, cast_h: int) -> dict[str, tuple[int, int]]:
    return {
        l["url"]: (int(cast_w / l.get("zoom", 1.0)), int(cast_h / l.get("zoom", 1.0)))
        for l in links
    }


def gif_capture_targets(
    links: list[dict], cast_w: int, cast_h: int
) -> tuple[list[str], dict[str, tuple[int, int]]]:
    """Links que necesitan GIF, re-resueltos en cada ciclo de captura.

    Capturamos las unproxyables (se muestran como screenshot) y tambien las
    internas PRTG: esas siguen renderizando como iframe, pero su GIF sirve de
    asset para el fallback via Default Media Receiver. Se re-resuelve en cada
    ciclo para que un link agregado desde la consola obtenga su GIF sin
    reiniciar el servicio. Los de captura en vivo quedan fuera: esos los atiende
    el loop de PNG, que mantiene su propia pagina abierta.
    """
    selected = [
        l
        for l in links
        if l.get("enabled", True)
        and not _is_live_screenshot(l)
        and (_use_screenshot(l["url"]) or _is_internal_url(l["url"]))
    ]
    return [l["url"] for l in selected], _viewport_map(selected, cast_w, cast_h)


def live_capture_targets(
    links: list[dict], cast_w: int, cast_h: int
) -> tuple[list[str], dict[str, tuple[int, int]]]:
    """Links de captura en vivo. Se resuelven una sola vez, al arrancar."""
    selected = [l for l in links if l.get("enabled", True) and _is_live_screenshot(l)]
    return [l["url"] for l in selected], _viewport_map(selected, cast_w, cast_h)


def _is_internal_url(url: str) -> bool:
    parsed = urlparse(url)
    return PRTG_HOST in parsed.netloc


def _internal_links(links: list[dict] | None = None) -> list[dict]:
    source_links = manager.links if links is None else links
    return [link for link in source_links if _is_internal_url(link["url"])]


def _can_proxy(url: str) -> bool:
    return not _use_screenshot(url)


def _iframe_src(url: str, direct: bool = False) -> str:
    """Genera el src del iframe: /proxy/ para PRTG, /p/ para externas.

    Con direct=True devuelve la URL tal cual: para apps de la misma red que los
    Chromecasts (SPA con assets/websockets que el proxy /p/ no soporta)."""
    if direct:
        return url

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
def cast_display(cc_id: str = "cc1", preview: bool = False):
    """La pagina que carga DashCast. Con preview=1 es el espejo de la consola:
    identica, pero su poll no cuenta como heartbeat del watchdog."""
    cast_w, cast_h = _cc_resolution(cc_id)
    # Solo los links de esta pantalla: con playlists, cada Chromecast puede
    # tener una seleccion y un orden propios.
    links = manager.links_for(cc_id)
    config_revision = manager.config_revision
    iframes_html = ""
    for link in links:
        # Los frames se keyean por id de link, no por posicion: asi reordenar o
        # borrar un link no reasigna los frames de los demas.
        frame_id = link["id"]
        url = link["url"]
        zoom = link.get("zoom", 1.0)
        # Viewport del contenido: resolución del CC ajustada por zoom
        vw = int(cast_w / zoom)
        vh = int(cast_h / zoom)
        # Scale para encajar en el cast
        sx = cast_w / vw
        sy = cast_h / vh
        if _link_uses_screenshot(link):
            asset_key = screenshot_asset_key(url)
            asset_extension = _screenshot_extension(link)
            # src precargado: el Chromecast descarga y decodifica el asset al
            # cargar la pagina, no en frio durante su slot de rotacion (quiosco-av6)
            asset_revision = screenshot_asset_revision(asset_key, asset_extension)
            asset_version = asset_revision if asset_revision is not None else "pending"
            src = f"/static/screenshots/{asset_key}.{asset_extension}?v={asset_version}"
            iframes_html += (
                f'  <img id="frame-{frame_id}" data-asset-key="{asset_key}"'
                f' data-asset-extension="{asset_extension}" src="{src}"'
                f' class="frame screenshot-frame" style="display:none;'
                f' width:{vw}px; height:{vh}px;'
                f' transform:scale({sx},{sy}); transform-origin:top left;'
                f' object-fit:fill">\n'
            )
        else:
            src = _iframe_src(url, direct=link.get("direct", False))
            if link.get("optional"):
                # No precargar: puede estar caido al abrir la display page.
                # El JS le pone src al mostrarlo (recarga fresca en cada slot).
                iframes_html += (
                    f'  <iframe id="frame-{frame_id}" src="about:blank" data-lazy-src="{src}"'
                    f' class="frame" style="display:none; width:{vw}px; height:{vh}px;'
                    f' transform:scale({sx},{sy}); transform-origin:top left;"></iframe>\n'
                )
            else:
                iframes_html += (
                    f'  <iframe id="frame-{frame_id}" src="{src}" class="frame"'
                    f' style="display:none; width:{vw}px; height:{vh}px;'
                    f' transform:scale({sx},{sy}); transform-origin:top left;"></iframe>\n'
                )

    current_query = "?preview=1" if preview else ""
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
  .notice {{
    position: absolute;
    top: 0; left: 0;
    width: {cast_w}px;
    height: {cast_h}px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #64748b;
    font-family: system-ui, sans-serif;
    font-size: {max(16, int(cast_h / 24))}px;
  }}
</style>
</head>
<body>
{iframes_html}<div id="empty-notice" class="notice" style="display:none">Pantalla sin links configurados</div>
<script>
  var ccId = "{cc_id}";
  var currentQuery = "{current_query}";
  var configRevision = {config_revision};
  var currentLinkId = null;
  var currentAssetKey = null;
  var currentAssetExtension = null;
  var currentAssetRevision = null;

  function screenshotSrc(assetKey, assetRevision, assetExtension) {{
    var version = assetRevision || "pending";
    var extension = assetExtension || "gif";
    return "/static/screenshots/" + assetKey + "." + extension + "?v=" + version;
  }}

  function refreshScreenshotFrame(frame, assetKey, assetRevision, assetExtension) {{
    if (!frame || !assetKey) return;
    var nextSrc = screenshotSrc(assetKey, assetRevision, assetExtension);
    if (frame.getAttribute("src") !== nextSrc) {{
      frame.setAttribute("src", nextSrc);
    }}
  }}

  function poll() {{
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/api/current/" + ccId + currentQuery, true);
    xhr.onreadystatechange = function() {{
      if (xhr.readyState !== 4) return;
      if (xhr.status !== 200) return;
      try {{
        var data = JSON.parse(xhr.responseText);
        if (data.config_revision !== configRevision) {{
          // El tecnico edito links o playlists: los frames horneados en esta
          // pagina ya no corresponden, hay que reconstruirla. Esto evita tener
          // que relanzar DashCast en cada cambio de configuracion.
          window.location.reload();
          return;
        }}
        var notice = document.getElementById("empty-notice");
        if (notice) notice.style.display = data.link_id ? "none" : "flex";
        if (data.link_id !== currentLinkId) {{
          var oldFrame = document.getElementById("frame-" + currentLinkId);
          if (oldFrame) {{
            oldFrame.style.display = "none";
            if (oldFrame.getAttribute("data-lazy-src")) {{
              oldFrame.setAttribute("src", "about:blank");
            }}
          }}
          currentLinkId = data.link_id;
          var newFrame = document.getElementById("frame-" + currentLinkId);
          if (newFrame && newFrame.getAttribute("data-lazy-src")) {{
            newFrame.setAttribute("src", newFrame.getAttribute("data-lazy-src"));
          }}
          if (data.render_mode === "screenshot") {{
            refreshScreenshotFrame(
              newFrame, data.asset_key, data.asset_revision, data.asset_extension
            );
            currentAssetKey = data.asset_key;
            currentAssetExtension = data.asset_extension;
            currentAssetRevision = data.asset_revision;
          }} else {{
            currentAssetKey = null;
            currentAssetExtension = null;
            currentAssetRevision = null;
          }}
          if (newFrame) newFrame.style.display = "block";
          // Embebida en la consola: avisarle del cambio para que su pie y su
          // barra de progreso no queden esperando al poll de /api/status.
          if (window.parent !== window) {{
            try {{
              window.parent.postMessage(
                {{quiosco: "current", ccId: ccId, linkId: data.link_id, rotating: data.rotating}},
                window.location.origin
              );
            }} catch (e) {{ /* la consola puede haberse ido */ }}
          }}
        }} else if (
          data.render_mode === "screenshot" &&
          (data.asset_key !== currentAssetKey ||
           data.asset_extension !== currentAssetExtension ||
           data.asset_revision !== currentAssetRevision)
        ) {{
          var curFrame = document.getElementById("frame-" + currentLinkId);
          refreshScreenshotFrame(
            curFrame, data.asset_key, data.asset_revision, data.asset_extension
          );
          currentAssetKey = data.asset_key;
          currentAssetExtension = data.asset_extension;
          currentAssetRevision = data.asset_revision;
        }} else if (data.render_mode !== "screenshot") {{
          currentAssetKey = null;
          currentAssetExtension = null;
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
    links = manager.links_for(cc_id)
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
        if _link_uses_screenshot(link):
            asset_key = screenshot_asset_key(url)
            asset_extension = _screenshot_extension(link)
            asset_revision = screenshot_asset_revision(asset_key, asset_extension)
            asset_version = asset_revision if asset_revision is not None else "pending"
            src = f"/static/screenshots/{asset_key}.{asset_extension}?v={asset_version}"
            frames_html += (
                f'  <img id="startup-frame-{i}" src="{src}"'
                f' class="frame" style="display:none;'
                f' width:{vw}px; height:{vh}px;'
                f' transform:scale({sx},{sy}); transform-origin:top left;'
                f' object-fit:fill">\n'
            )
        else:
            src = _iframe_src(url, direct=link.get("direct", False))
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

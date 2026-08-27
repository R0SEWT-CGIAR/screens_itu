"""Panel propio de estado de servicios, sobre el JSON publico de UptimeRobot.

Por que no mostrar la status page de UptimeRobot tal cual: no se deja enmarcar,
asi que caia en SCREENSHOT_SITES y llegaba a la pantalla como un GIF del tope de
la pagina. En 1280x720 eso son 2 de 8 monitores, con el hero "All systems
Operational" ocupando ~40% del alto y sin scroll posible. La status page es solo
una SPA sobre un endpoint JSON publico (sin API key), asi que consumimos ese
JSON y pintamos el panel nosotros: mismo origen que la display page, iframe
directo, datos en vivo y control total del layout.

Decisiones de presentacion (ver guia dataviz):

- El strip agrupa los 90 dias en celdas de 3 y muestra el PEOR de cada trio. A
  4 metros una barra de 5px no se ve; promediar escondería una caida de un dia
  entre dos dias limpios, quedarse con el peor no.
- Paleta de estado fija, y nunca color solo: good (#0ca30c) y critical
  (#d03b3b) miden dE 4.1 en deuteranopia, o sea que un daltonico no distingue
  "operativo" de "caido" por el color. Cada fila lleva etiqueta de texto, el
  marcador cambia de forma y los dias con incidencias sobresalen en altura.
- El porcentaje se trunca, no se redondea: 99.996 redondeado da "100.00" y el
  panel estaria afirmando un 100% que no ocurrio.
- Sin capa de hover: el destino es un Chromecast, no hay puntero.
"""

import asyncio
import html
import json
import logging
import math
import time
from datetime import datetime
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_URL = "https://stats.uptimerobot.com/api/getMonitorList/26r4CjSckG"
DEFAULT_REFRESH_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 10

DAYS_PER_CELL = 3
MAX_CELLS = 30

# Umbral -> banda de la paleta de estado. Ordenado de mejor a peor.
BANDS = ((100.0, "ok"), (99.0, "leve"), (95.0, "grave"), (0.0, "caida"))


# --- Configuracion ---

def panel_settings(config: dict) -> dict:
    """Bloque 'uptime_panel' del config.json, con defaults.

    aliases mapea el nombre del monitor en UptimeRobot al texto que se muestra;
    hidden lista monitores que no salen en el panel (por nombre o por id). La
    pantalla de la entrada la ve cualquier visitante, y los nombres crudos son
    nomenclatura interna de infraestructura.
    """
    raw = config.get("uptime_panel") or {}
    if not isinstance(raw, dict):
        raw = {}

    aliases = raw.get("aliases") or {}
    aliases = {str(k): str(v) for k, v in aliases.items()} if isinstance(aliases, dict) else {}

    hidden = raw.get("hidden") or []
    hidden = [str(h) for h in hidden] if isinstance(hidden, list) else []

    try:
        refresh = float(raw.get("refresh_seconds", DEFAULT_REFRESH_SECONDS))
    except (TypeError, ValueError):
        refresh = DEFAULT_REFRESH_SECONDS

    return {
        "source_url": str(raw.get("source_url") or DEFAULT_SOURCE_URL),
        "refresh_seconds": max(15.0, refresh),
        "aliases": aliases,
        "hidden": hidden,
    }


# --- Normalizacion (puro, sin red) ---

def band_for(ratio: float) -> str:
    for threshold, name in BANDS:
        if ratio >= threshold:
            return name
    return "caida"


def ratio_text(value: float) -> str:
    """100 exacto se escribe corto; el resto TRUNCADO a dos decimales.

    A 4 metros '100.000' son seis digitos que nadie parsea. Y truncar en vez de
    redondear evita que 99.996 se muestre como '100.00' junto a una nota que
    dice 'con incidencias': en fiabilidad el error seguro va hacia abajo.
    """
    if value >= 100.0:
        return "100"
    return f"{math.floor(value * 100) / 100:.2f}"


def _cells(daily_ratios: list[dict]) -> list[float]:
    """90 ratios diarios -> hasta 30 celdas, cada una el peor de sus 3 dias."""
    out: list[float] = []
    for i in range(0, len(daily_ratios), DAYS_PER_CELL):
        chunk = daily_ratios[i:i + DAYS_PER_CELL]
        if not chunk:
            continue
        try:
            out.append(min(float(d["ratio"]) for d in chunk))
        except (KeyError, TypeError, ValueError):
            continue
    return out[-MAX_CELLS:]


def _duration_text(seconds: int) -> str:
    hours, minutes = divmod(int(seconds) // 60, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def _relative_days(then: datetime, now: datetime) -> str:
    days = (now - then).days
    if days <= 0:
        return "hoy"
    if days == 1:
        return "ayer"
    return f"hace {days} días"


def _is_hidden(monitor: dict, hidden: list[str]) -> bool:
    if not hidden:
        return False
    name = str(monitor.get("name", "")).casefold()
    monitor_id = str(monitor.get("monitorId", ""))
    return any(h.casefold() == name or h == monitor_id for h in hidden)


def build_view(
    raw: dict,
    *,
    aliases: Optional[dict] = None,
    hidden: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Modelo de vista del panel a partir del JSON crudo de la status page."""
    aliases = aliases or {}
    hidden = hidden or []
    now = now or datetime.now()

    monitors_raw = raw.get("data")
    if not isinstance(monitors_raw, list):
        raise ValueError("El JSON de la status page no trae una lista 'data'")

    monitors = []
    for m in monitors_raw:
        if not isinstance(m, dict) or _is_hidden(m, hidden):
            continue
        name = str(m.get("name", "")).strip() or "(sin nombre)"
        # statusClass viene 'success' cuando esta arriba; cualquier otra cosa
        # (danger, warning, paused) se trata como no operativo.
        down = m.get("statusClass") != "success"
        cells = _cells(m.get("dailyRatios") or [])
        try:
            ratio90 = float((m.get("90dRatio") or {}).get("ratio"))
        except (TypeError, ValueError):
            ratio90 = 100.0 if not cells else min(cells)

        monitors.append({
            "name": name,
            "label": aliases.get(name, name),
            "down": down,
            "ratio90": ratio90,
            "ratio_text": ratio_text(ratio90),
            "cells": [{"band": band_for(r)} for r in cells],
            "bad_cells": sum(1 for r in cells if r < 100.0),
        })

    # Lo roto arriba: en un panel de recepcion no se busca, se ve. Dentro de
    # cada grupo, alfabetico, para que el orden no baile entre refrescos.
    monitors.sort(key=lambda m: (not m["down"], m["label"].casefold()))

    total = len(monitors)
    down = sum(1 for m in monitors if m["down"])

    incident = None
    stamps = []
    for m in monitors_raw:
        last = m.get("lastDowntime") if isinstance(m, dict) else None
        if isinstance(last, dict) and last.get("date"):
            stamps.append(last)
    if stamps:
        latest = max(stamps, key=lambda x: x["date"])
        try:
            when = datetime.strptime(latest["date"], "%Y-%m-%d %H:%M:%S")
            incident = {
                "when": _relative_days(when, now),
                "duration": _duration_text(latest.get("duration") or 0),
            }
        except (TypeError, ValueError):
            incident = None

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "clock": now.strftime("%H:%M"),
        "total": total,
        "down": down,
        "problem": down > 0,
        "headline": (
            f"{down} de {total} caído{'s' if down != 1 else ''}" if down
            else f"{total} de {total} operativos"
        ),
        "incident": incident,
        "monitors": monitors,
    }


# --- Cache con red ---

class PanelCache:
    """Una sola lectura upstream por ventana de refresco, compartida.

    Tres Chromecast mas los espejos de la consola piden el panel a la vez; sin
    cache eso serian varias llamadas por minuto a UptimeRobot para pintar
    exactamente lo mismo. Ante un fallo se sirve la ultima vista buena marcada
    como stale: una pantalla de recepcion nunca debe quedarse en blanco por un
    timeout.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_REFRESH_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._view: Optional[dict] = None
        self._fetched_monotonic: Optional[float] = None
        self._lock = asyncio.Lock()
        self.last_error: Optional[str] = None

    def _age(self) -> Optional[float]:
        if self._fetched_monotonic is None:
            return None
        return time.monotonic() - self._fetched_monotonic

    async def get(self, settings: dict, client: httpx.AsyncClient) -> dict:
        async with self._lock:
            age = self._age()
            if self._view is not None and age is not None and age < self.ttl_seconds:
                return self._decorate(self._view, age)

            try:
                response = await client.get(
                    settings["source_url"], timeout=DEFAULT_TIMEOUT_SECONDS
                )
                response.raise_for_status()
                view = build_view(
                    response.json(),
                    aliases=settings["aliases"],
                    hidden=settings["hidden"],
                )
            except Exception as exc:  # noqa: BLE001 - cualquier fallo cae a stale
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self._view is None:
                    logger.warning("Panel de uptime sin datos todavia: %s", self.last_error)
                    raise
                logger.warning(
                    "Panel de uptime: fallo el refresco (%s); se sirve la vista de hace %.0fs",
                    self.last_error, age or 0,
                )
                return self._decorate(self._view, age)

            self.last_error = None
            self._view = view
            self._fetched_monotonic = time.monotonic()
            return self._decorate(view, 0.0)

    def _decorate(self, view: dict, age: Optional[float]) -> dict:
        out = dict(view)
        age_seconds = int(age or 0)
        out["age_seconds"] = age_seconds
        # Se avisa recien pasada una ventana entera de refresco: parpadear un
        # aviso en cada ciclo normal seria ruido.
        out["stale"] = age_seconds > self.ttl_seconds * 2
        return out


# --- Render ---

_STYLE = """
  * { margin:0; padding:0; box-sizing:border-box; }
  :root {
    --superficie:#1a1a19; --plano:#0d0d0d;
    --tinta:#ffffff; --tinta-2:#c3c2b7; --tinta-3:#898781; --linea:#2c2c2a;
    --ok:#0ca30c; --leve:#fab219; --grave:#ec835a; --caida:#d03b3b;
  }
  html,body { width:100%; height:100%; overflow:hidden; }
  body {
    background:var(--plano); color:var(--tinta);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
    display:flex; flex-direction:column; padding:18px 26px 14px;
  }
  header { display:flex; align-items:baseline; justify-content:space-between; margin-bottom:12px; }
  h1 { font-size:23px; font-weight:600; letter-spacing:-.01em; }
  .reloj { font-size:16px; color:var(--tinta-3); font-variant-numeric:tabular-nums; }
  .reloj .stale { color:var(--leve); }

  .hero {
    background:var(--superficie); border-radius:12px; padding:15px 24px; margin-bottom:12px;
    display:flex; align-items:center; gap:15px; border-left:5px solid var(--ok);
  }
  .hero.caida { border-left-color:var(--caida); background:#241715; }
  .hero-icono { font-size:25px; line-height:1; color:var(--ok); }
  .hero.caida .hero-icono { color:var(--caida); }
  .hero-txt { font-size:33px; font-weight:650; letter-spacing:-.02em; }
  .hero-sub { margin-left:auto; font-size:16px; color:var(--tinta-3); }

  table { width:100%; border-collapse:collapse; flex:1; }
  td, th { padding:0 8px; vertical-align:middle; }
  .eje th { font-size:12px; font-weight:400; color:var(--tinta-3); padding-bottom:5px; }
  .eje .strip span { display:flex; justify-content:space-between; }

  .fila { border-top:1px solid var(--linea); }
  .fila.caida { background:#241715; }
  .fila.caida td:first-child { box-shadow:inset 4px 0 0 var(--caida); }

  .estado { width:150px; padding-left:12px; white-space:nowrap; }
  .punto {
    display:inline-block; width:11px; height:11px; border-radius:50%;
    background:var(--ok); margin-right:9px; vertical-align:middle;
  }
  /* Rombo, no circulo: la forma distingue caido de operativo sin el color. */
  .fila.caida .punto { background:var(--caida); border-radius:2px; transform:rotate(45deg); }
  .etiqueta { font-size:16px; color:var(--tinta-2); vertical-align:middle; }
  .fila.caida .etiqueta { color:var(--caida); font-weight:650; }

  .nombre { font-size:24px; font-weight:550; letter-spacing:-.01em; line-height:1.15; }
  .nota { display:block; font-size:14px; color:var(--tinta-3); font-weight:400; margin-top:2px; }

  .strip { width:434px; }
  .celdas { display:flex; align-items:flex-end; gap:2px; height:34px; }
  .celda { width:12px; border-radius:3px; background:var(--ok); }
  .celda.leve { background:var(--leve); }
  .celda.grave { background:var(--grave); }
  .celda.caida { background:var(--caida); }

  .pct {
    width:104px; text-align:right; padding-right:0;
    font-size:23px; font-variant-numeric:tabular-nums; color:var(--tinta-2);
  }
  .fila.caida .pct { color:var(--tinta); }
  .signo { font-size:15px; color:var(--tinta-3); margin-left:2px; }

  footer {
    display:flex; align-items:center; gap:20px; margin-top:9px; padding-top:10px;
    border-top:1px solid var(--linea); font-size:14px; color:var(--tinta-3);
  }
  .leyenda { display:flex; align-items:center; gap:15px; }
  .clave { display:flex; align-items:center; gap:6px; }
  .clave i { width:11px; border-radius:2px; display:inline-block; }
  .fuente { margin-left:auto; }
  .aviso { color:var(--leve); }
"""

# El render vive solo en JS y corre tambien en la primera pintura, sobre el JSON
# embebido: si el servidor tambien supiera pintar filas habria dos plantillas
# que mantener sincronizadas. Asi la pagina aparece completa sin esperar al
# primer fetch, y un fallo de red posterior no la deja en blanco.
_SCRIPT = """
const ALTO_OK = 22, ALTO_MAL = 34;

function pintar(v) {
  document.getElementById('reloj').innerHTML = v.stale
    ? `<span class="stale">datos de hace ${Math.round(v.age_seconds / 60)} min</span>`
    : `actualizado ${v.clock}`;

  const hero = document.getElementById('hero');
  hero.className = 'hero ' + (v.problem ? 'caida' : 'ok');
  document.getElementById('hero-icono').textContent = v.problem ? '\\u25B2' : '\\u25CF';
  document.getElementById('hero-txt').textContent = v.headline;
  document.getElementById('hero-sub').textContent = v.incident
    ? `último incidente ${v.incident.when} \\u00B7 ${v.incident.duration}`
    : 'sin incidentes registrados';

  const filas = v.monitors.map(m => {
    const celdas = m.cells.map(c =>
      `<i class="celda ${c.band}" style="height:${c.band === 'ok' ? ALTO_OK : ALTO_MAL}px"></i>`
    ).join('');
    const nota = m.bad_cells
      ? `<span class="nota">${m.bad_cells} día${m.bad_cells === 1 ? '' : 's'} con incidencias</span>`
      : '';
    return `<tr class="fila ${m.down ? 'caida' : 'ok'}">
      <td class="estado"><span class="punto"></span><span class="etiqueta">${m.down ? 'Caído' : 'Operativo'}</span></td>
      <td class="nombre">${escapar(m.label)}${nota}</td>
      <td class="strip"><span class="celdas">${celdas}</span></td>
      <td class="pct">${m.ratio_text}<span class="signo">%</span></td>
    </tr>`;
  }).join('');

  document.getElementById('filas').innerHTML = filas;
}

function escapar(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function refrescar() {
  try {
    const r = await fetch('/api/uptime-panel', { cache: 'no-store' });
    if (r.ok) pintar(await r.json());
  } catch (e) {
    // Se mantiene la pintura anterior: mejor un dato de hace un rato que un
    // parpadeo o una pantalla vacia en recepcion.
  }
}

pintar(JSON.parse(document.getElementById('datos').textContent));
setInterval(refrescar, REFRESH_MS);
"""


def render_html(view: dict, *, refresh_seconds: float) -> str:
    """Pagina completa del panel, con la vista inicial embebida."""
    datos = html.escape(json.dumps(view, ensure_ascii=False), quote=False)
    script = _SCRIPT.replace("REFRESH_MS", str(int(refresh_seconds * 1000)))
    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Estado de servicios</title>
<style>{_STYLE}</style>
</head>
<body>
  <header>
    <h1>Estado de servicios</h1>
    <span class="reloj" id="reloj"></span>
  </header>

  <div class="hero" id="hero">
    <span class="hero-icono" id="hero-icono"></span>
    <span class="hero-txt" id="hero-txt"></span>
    <span class="hero-sub" id="hero-sub"></span>
  </div>

  <table>
    <tr class="eje">
      <th></th><th></th>
      <th class="strip"><span><em>hace 90 días</em><em>hoy</em></span></th>
      <th class="pct" style="text-align:right">90 días</th>
    </tr>
    <tbody id="filas"></tbody>
  </table>

  <footer>
    <span class="leyenda">
      <span class="clave"><i style="background:var(--ok);height:9px"></i>sin incidencias</span>
      <span class="clave"><i style="background:var(--leve);height:14px"></i>leve</span>
      <span class="clave"><i style="background:var(--grave);height:14px"></i>grave</span>
      <span class="clave"><i style="background:var(--caida);height:14px"></i>caída</span>
    </span>
    <span class="fuente">cada celda = 3 días, se muestra el peor · fuente UptimeRobot</span>
  </footer>

<script type="application/json" id="datos">{datos}</script>
<script>{script}</script>
</body></html>"""


def render_error_html(message: str) -> str:
    """Pagina de ultimo recurso: no hay ni una vista buena en cache."""
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>Estado de servicios</title>
<style>{_STYLE}
  .vacio {{ margin:auto; text-align:center; color:var(--tinta-3); }}
  .vacio strong {{ display:block; font-size:28px; color:var(--tinta-2); margin-bottom:8px; font-weight:600; }}
</style></head>
<body>
  <div class="vacio">
    <strong>Estado de servicios no disponible</strong>
    {html.escape(message)}
  </div>
</body></html>"""

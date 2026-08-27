"""Panel propio de estado de la red, sobre la API de PRTG.

Reemplaza al mapa publico de PRTG que se casteaba como live_screenshot: una
pagina de Chromium abierta de forma permanente y fotografiada cada 2s. Leyendo
la API directamente el panel queda en vivo de verdad y se apaga el proceso mas
caro del contenedor.

Decisiones de presentacion (guia dataviz), tomadas sobre los datos reales del
PRTG de CIP (412 sensores):

- NO se listan los 412 ni los 31 que no estan en verde. 23 de esos 31 son
  "Unusual" con el mismo mensaje ("1 day interval average of < 0.01 Mbit/s" en
  puertos de switch): listarlos ahogaria al unico caido. La historia es "que
  esta roto", asi que la forma correcta es enfasis, no enumeracion. Unusual se
  cuenta en la fila de totales pero no se lista, salvo que se pida.
- Los pausados se muestran SIEMPRE aunque no sean un problema. Un tercio del
  arbol esta pausado; un panel que dijera "todo bien" callando eso estaria
  mintiendo por omision, porque un sensor pausado no se esta vigilando.
- Paleta de estado fija y nunca color solo: cada fila lleva su etiqueta de texto
  y el marcador cambia de forma segun el estado (ver panel_ui).
- Unusual y Pausado usan tinta neutra, no un color de estado: los cuatro colores
  de estado quedan reservados para lo que realmente exige accion.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional

import httpx

from . import panel_ui

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://172.25.0.22"
DEFAULT_REFRESH_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_ISSUES = 8

# Columnas que pide el panel. PRTG devuelve cada una dos veces: la version
# renderizada (con HTML adentro) y la _raw. Para estado y numeros se usan las
# _raw; 'message' sin sufijo viene envuelto en <div class="status">.
COLUMNS = (
    "objid,probe,group,device,sensor,status,message,lastvalue,priority,downtimesince"
)

# status_raw de PRTG -> (banda de color, etiqueta, categoria de conteo).
#
# La banda y la categoria son cosas distintas a proposito. La banda es color; la
# categoria es lo que afirma el titular. Mezclarlas hacia que "2 sensores sin
# datos" se sumaran a "1 caido" y el panel anunciara "3 sensores caidos", que es
# falso. Los cuatro colores de estado se reservan para lo accionable.
STATUS = {
    0:  ("neutro",  "Sin definir",        "otro"),
    1:  ("grave",   "Sin datos",          "sindatos"),
    2:  ("neutro",  "Escaneando",         "otro"),
    3:  ("ok",      "Operativo",          "ok"),
    4:  ("leve",    "Alerta",             "alerta"),
    5:  ("caida",   "Caído",              "caido"),
    6:  ("caida",   "Sonda caída",        "caido"),
    7:  ("neutro",  "Pausado",            "pausado"),
    8:  ("neutro",  "Pausado",            "pausado"),
    9:  ("neutro",  "Pausado",            "pausado"),
    10: ("inusual", "Inusual",            "inusual"),
    11: ("neutro",  "Sin licencia",       "otro"),
    12: ("neutro",  "Pausado",            "pausado"),
    13: ("grave",   "Caído (reconocido)", "caido"),
    14: ("grave",   "Caído parcial",      "caido"),
}
UNKNOWN_STATUS = ("neutro", "Desconocido", "otro")

# Categorias que ameritan una fila en la tabla.
LISTABLES = ("caido", "sindatos", "alerta")

# Orden de severidad para ordenar la tabla y elegir el titular.
SEVERITY = {"caida": 0, "grave": 1, "leve": 2, "inusual": 3, "neutro": 4, "ok": 5}


# --- Configuracion ---

def panel_settings(config: dict) -> dict:
    """Bloque 'prtg_panel' del config.json mas las credenciales del entorno.

    Las credenciales NO viven en config.json: van en el .env de la maquina
    (PRTG_USER, PRTG_PASSHASH) porque config.json se lee por SSH, se respalda y
    se edita desde la consola web. Se guarda el passhash, no la contrasena.
    """
    raw = config.get("prtg_panel") or {}
    if not isinstance(raw, dict):
        raw = {}

    try:
        refresh = float(raw.get("refresh_seconds", DEFAULT_REFRESH_SECONDS))
    except (TypeError, ValueError):
        refresh = DEFAULT_REFRESH_SECONDS

    try:
        max_issues = int(raw.get("max_issues", DEFAULT_MAX_ISSUES))
    except (TypeError, ValueError):
        max_issues = DEFAULT_MAX_ISSUES

    hidden_groups = raw.get("hidden_groups") or []
    hidden_groups = ([str(g) for g in hidden_groups]
                     if isinstance(hidden_groups, list) else [])

    return {
        "base_url": str(raw.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        "refresh_seconds": max(15.0, refresh),
        "max_issues": max(1, max_issues),
        "show_unusual": bool(raw.get("show_unusual", False)),
        "hidden_groups": hidden_groups,
        "username": os.environ.get("PRTG_USER", ""),
        "passhash": os.environ.get("PRTG_PASSHASH", ""),
    }


# --- Normalizacion (puro, sin red) ---

def _clean(value) -> str:
    return str(value or "").strip()


def _status_of(sensor: dict) -> tuple[str, str, str]:
    try:
        raw = int(sensor.get("status_raw"))
    except (TypeError, ValueError):
        return UNKNOWN_STATUS
    return STATUS.get(raw, UNKNOWN_STATUS)


def build_view(
    raw: dict,
    *,
    max_issues: int = DEFAULT_MAX_ISSUES,
    show_unusual: bool = False,
    hidden_groups: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Modelo de vista del panel a partir de la tabla de sensores de PRTG."""
    hidden = {g.casefold() for g in (hidden_groups or [])}
    now = now or datetime.now()

    sensors_raw = raw.get("sensors")
    if not isinstance(sensors_raw, list):
        raise ValueError("La respuesta de PRTG no trae una lista 'sensors'")

    counts = {"ok": 0, "alerta": 0, "caido": 0, "sindatos": 0,
              "inusual": 0, "pausado": 0, "otro": 0}
    issues = []
    total = 0

    for s in sensors_raw:
        if not isinstance(s, dict):
            continue
        group = _clean(s.get("group"))
        if group.casefold() in hidden:
            continue
        total += 1

        band, label, category = _status_of(s)
        counts[category] = counts.get(category, 0) + 1

        if category in LISTABLES or (category == "inusual" and show_unusual):
            issues.append({
                "band": band,
                "status": label,
                "group": group,
                "device": _clean(s.get("device")),
                "sensor": _clean(s.get("sensor")),
                # message_raw es el texto plano; 'message' viene con HTML dentro.
                "message": _clean(s.get("message_raw") or s.get("message")),
                "since": _clean(s.get("downtimesince_raw") or s.get("downtimesince")),
            })

    # Lo mas grave arriba; dentro de cada banda, alfabetico, para que el orden no
    # baile entre refrescos.
    issues.sort(key=lambda i: (SEVERITY.get(i["band"], 9),
                               i["device"].casefold(), i["sensor"].casefold()))
    shown, omitted = issues[:max_issues], max(0, len(issues) - max_issues)

    down, nodata, warn = counts["caido"], counts["sindatos"], counts["alerta"]
    paused = counts["pausado"]
    plural = lambda n: "es" if n != 1 else ""

    # El titular nombra UNA categoria, la mas grave que exista, y con su numero
    # exacto. El resto se enumera en la linea de apoyo.
    if down:
        severity = "caida"
        headline = f"{down} sensor{plural(down)} caído{'s' if down != 1 else ''}"
    elif nodata:
        severity = "caida"
        headline = f"{nodata} sensor{plural(nodata)} sin datos"
    elif warn:
        severity = "leve"
        headline = f"{warn} sensor{plural(warn)} en alerta"
    else:
        severity, headline = "ok", "Sin incidencias"

    resto = []
    if down and nodata:
        resto.append(f"{nodata} sin datos")
    if (down or nodata) and warn:
        resto.append(f"{warn} en alerta")
    if counts["inusual"]:
        resto.append(f"{counts['inusual']} inusual{plural(counts['inusual'])}")

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "clock": now.strftime("%H:%M"),
        "total": total,
        "counts": counts,
        "severity": severity,
        "problem": severity != "ok",
        "headline": headline,
        "subline": " · ".join(resto) or f"{counts['ok']} de {total} sensores operativos",
        "issues": shown,
        "omitted": omitted,
        "paused": paused,
        # Un sensor pausado no se esta vigilando. Con un tercio del arbol
        # pausado, callarlo seria mentir por omision.
        "paused_note": (
            f"{paused} sensor{plural(paused)} pausado"
            f"{'s' if paused != 1 else ''} sin vigilar" if paused else ""
        ),
    }


# --- Cache con red ---

class PanelCache:
    """Una sola lectura de PRTG por ventana de refresco, compartida.

    Mismo contrato que la del panel de uptime: ante un fallo se sirve la ultima
    vista buena marcada como stale, porque una pantalla de recepcion no puede
    quedarse en blanco por un timeout.
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
                if not settings["username"] or not settings["passhash"]:
                    raise RuntimeError(
                        "Faltan PRTG_USER y PRTG_PASSHASH en el entorno del contenedor"
                    )
                response = await client.get(
                    f"{settings['base_url']}/api/table.json",
                    params={
                        "content": "sensors",
                        "columns": COLUMNS,
                        "count": "50000",
                        "username": settings["username"],
                        "passhash": settings["passhash"],
                    },
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                view = build_view(
                    response.json(),
                    max_issues=settings["max_issues"],
                    show_unusual=settings["show_unusual"],
                    hidden_groups=settings["hidden_groups"],
                )
            except Exception as exc:  # noqa: BLE001 - cualquier fallo cae a stale
                # El passhash va en la query, asi que un error de httpx puede
                # traer la URL completa: se recorta al tipo y no se loguea la URL.
                self.last_error = type(exc).__name__
                if isinstance(exc, (ValueError, RuntimeError)):
                    self.last_error = f"{type(exc).__name__}: {exc}"
                if self._view is None:
                    logger.warning("Panel de PRTG sin datos todavia: %s", self.last_error)
                    raise
                logger.warning(
                    "Panel de PRTG: fallo el refresco (%s); se sirve la vista de hace %.0fs",
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
        out["stale"] = age_seconds > self.ttl_seconds * 2
        return out


# --- Render ---

_STYLE = """
  .kpis { display:flex; gap:10px; margin-bottom:13px; }
  .kpi {
    flex:1; background:var(--superficie); border-radius:10px; padding:10px 14px;
    border-top:3px solid var(--neutro);
  }
  .kpi.ok { border-top-color:var(--ok); }
  .kpi.leve { border-top-color:var(--leve); }
  .kpi.caida { border-top-color:var(--caida); }
  .kpi.grave { border-top-color:var(--grave); }
  .kpi-n { font-size:31px; font-weight:650; letter-spacing:-.02em; line-height:1.05; }
  .kpi.cero .kpi-n { color:var(--tinta-3); }
  .kpi-t { font-size:14px; color:var(--tinta-3); margin-top:1px; }

  table { flex:1; }
  .estado { width:158px; }
  .que { font-size:21px; font-weight:550; letter-spacing:-.01em; line-height:1.2; }
  .donde { display:block; font-size:13px; color:var(--tinta-3); font-weight:400; margin-top:2px; }
  .msg {
    font-size:15px; color:var(--tinta-2); text-align:right; padding-right:0;
    max-width:430px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .mas { font-size:14px; color:var(--tinta-3); padding:9px 0 0 12px; }
  .limpio { margin:auto; text-align:center; color:var(--tinta-3); font-size:19px; }
"""

_SCRIPT = """
function pintar(v) {
  pintarReloj(v);

  const hero = document.getElementById('hero');
  hero.className = 'hero ' + v.severity;
  document.getElementById('hero-icono').textContent =
    v.severity === 'caida' ? '\\u25B2' : (v.severity === 'leve' ? '\\u25A0' : '\\u25CF');
  document.getElementById('hero-txt').textContent = v.headline;
  document.getElementById('hero-sub').textContent = v.subline;

  const KPIS = [
    ['ok',      'Operativos', v.counts.ok],
    ['leve',    'En alerta',  v.counts.alerta],
    ['caida',   'Caídos',     v.counts.caido],
    ['grave',   'Sin datos',  v.counts.sindatos],
    ['neutro',  'Inusual',    v.counts.inusual],
    ['neutro',  'Pausados',   v.paused],
  ];
  document.getElementById('kpis').innerHTML = KPIS.map(([c, t, n]) =>
    `<div class="kpi ${c}${n ? '' : ' cero'}"><div class="kpi-n">${n}</div><div class="kpi-t">${t}</div></div>`
  ).join('');

  const cuerpo = document.getElementById('filas');
  if (!v.issues.length) {
    cuerpo.innerHTML = `<tr><td colspan="3" class="limpio">
      Ningún sensor requiere atención</td></tr>`;
  } else {
    cuerpo.innerHTML = v.issues.map(i => `<tr class="fila ${i.band}">
      <td class="estado"><span class="punto"></span><span class="etiqueta">${escapar(i.status)}</span></td>
      <td class="que">${escapar(i.sensor)}<span class="donde">${escapar(i.device)} \\u00B7 ${escapar(i.group)}</span></td>
      <td class="msg">${escapar(i.message)}</td>
    </tr>`).join('');
  }

  document.getElementById('mas').textContent =
    v.omitted ? `y ${v.omitted} más que no caben en pantalla` : '';
  document.getElementById('pausados').textContent = v.paused_note;
}

pintar(JSON.parse(document.getElementById('datos').textContent));
setInterval(() => refrescar('/api/prtg-panel'), REFRESH_MS);
"""

_BODY = """
  <header>
    <h1>Estado de la red</h1>
    <span class="reloj" id="reloj"></span>
  </header>

  <div class="hero" id="hero">
    <span class="hero-icono" id="hero-icono"></span>
    <span class="hero-txt" id="hero-txt"></span>
    <span class="hero-sub" id="hero-sub"></span>
  </div>

  <div class="kpis" id="kpis"></div>

  <table><tbody id="filas"></tbody></table>
  <div class="mas" id="mas"></div>

  <footer>
    <span class="aviso" id="pausados"></span>
    <span class="fuente">fuente PRTG · CIPMONITOR</span>
  </footer>
"""


def render_html(view: dict, *, refresh_seconds: float) -> str:
    """Pagina completa del panel, con la vista inicial embebida."""
    return panel_ui.page(
        title="Estado de la red",
        style=_STYLE,
        body=_BODY,
        view=view,
        script=panel_ui.SCRIPT_COMUN + _SCRIPT,
        refresh_seconds=refresh_seconds,
    )


def render_error_html(message: str) -> str:
    return panel_ui.error_page("Estado de la red", message)

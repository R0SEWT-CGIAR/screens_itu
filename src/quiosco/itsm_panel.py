"""Panel de tickets por brechearse, sobre el ITSM de CIP.

A diferencia de los otros dos paneles, este NO habla con su fuente: exodia no
tiene credenciales de SharePoint ni de Power Platform, y no hay consentimiento
de admin disponible. Un flow de Power Automate con trigger HTTP publica el
agregado ya calculado y quiosco solo lo consume. La URL con SAS de ese flow ES
la credencial, asi que vive en el entorno del contenedor y se consume
server-side: si el fetch saliera del navegador, la URL terminaria en el DOM de
una pantalla que la gente mira.

Que muestra y por que (decidido con datos reales, ver quiosco-fdc y la consulta
a g2-senior):

- La lista es de tickets POR brechearse, no de brecheados. Con el reloj corriendo
  el panel es un pedido de auxilio y el que aparece todavia puede salir; una
  lista de ya brecheados solo reparte culpa sobre algo que ya no tiene arreglo.
- Los ya vencidos se muestran como TOTAL DEL EQUIPO, sin desglose por persona.
  No es pudor: medido el 2026-08-28, 20 de 35 vencidos eran de un solo
  asignatario, y en un equipo de ~10 cualquier corte —incluso iniciales— lo
  identifica igual que el nombre. Ademas el patron (tasa de cierre baja + pila
  de New) es indistinguible de estar sobrecargado o de licencia, o sea lo
  contrario de una falla de desempeño.
- Los pausados (esperando al usuario / on hold) NO entran en la lista. El flow
  de SLA Pause Accrual corre TimeToFixDate hacia adelante mientras el ticket
  esta pausado, asi que el deadline ya trae la pausa descontada y un ticket
  pausado se aleja solo. La mecanica justa ya estaba construida upstream.
- La cuenta regresiva se recalcula en cada render contra el reloj actual, no se
  toma del agregado. Asi el flow se llama cada pocos minutos pero el "2 min" de
  la pantalla es exacto.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from . import panel_ui

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 300      # ventana de cache contra el flow
DEFAULT_POLL_SECONDS = 60          # cada cuanto repinta la pagina
DEFAULT_TIMEOUT_SECONDS = 30
# Red de seguridad, no criterio de seleccion: quien decide que esta "por
# brechearse" es el flow, y lo hace en DIAS HABILES, no en horas de pared. Un
# viernes 14:00, las proximas 48 h de pared llegan al domingo, donde no vence
# nada: el panel se vaciaria el fin de semana afirmando que no hay riesgo. Este
# tope solo evita que un flow con un bug ponga en pantalla algo de dentro de un
# mes; si recortara antes que el flow, estaria escondiendo lo que el flow eligio.
DEFAULT_HORIZON_HOURS = 168
DEFAULT_MAX_ROWS = 8

LIMA = timezone(timedelta(hours=-5))


# --- Configuracion ---

def panel_settings(config: dict) -> dict:
    """Bloque 'itsm_panel' del config.json mas la URL del flow desde el entorno.

    La URL NO va en config.json: lleva el SAS y ese archivo se lee por SSH, se
    respalda y lo reescribe la consola web.
    """
    raw = config.get("itsm_panel") or {}
    if not isinstance(raw, dict):
        raw = {}

    def _num(clave, defecto, minimo):
        try:
            return max(minimo, float(raw.get(clave, defecto)))
        except (TypeError, ValueError):
            return defecto

    return {
        "flow_url": os.environ.get("ITSM_PANEL_FLOW_URL", ""),
        "refresh_seconds": _num("refresh_seconds", DEFAULT_REFRESH_SECONDS, 60),
        "poll_seconds": _num("poll_seconds", DEFAULT_POLL_SECONDS, 15),
        "horizon_hours": _num("horizon_hours", DEFAULT_HORIZON_HOURS, 1),
        "max_rows": int(_num("max_rows", DEFAULT_MAX_ROWS, 1)),
        "show_people": bool(raw.get("show_people", True)),
    }


# --- Normalizacion (puro, sin red) ---

def _parse(stamp) -> Optional[datetime]:
    """ISO 8601 a datetime con tz. El flow emite UTC; se exige tz explicita."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Un stamp sin zona se asume UTC: es lo que emite SharePoint en raw, y
    # tratarlo como hora local correria el reloj 5 h (ver 0.1.10).
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def remaining_text(hours: float) -> str:
    """Cuanto falta, en la unidad que se lee de un vistazo a 4 metros."""
    if hours < 1 / 60:
        return "ahora"
    if hours < 1:
        return f"{int(round(hours * 60))} min"
    if hours < 2:
        return f"{hours:.1f} h"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} d"


def band_for(hours: float) -> str:
    """Banda de color. Los cuatro colores de estado se reservan para lo urgente."""
    if hours < 1:
        return "caida"
    if hours < 4:
        return "leve"
    return "neutro"


def initials(name: str) -> str:
    """Iniciales para quien no tiene foto: el conector devuelve 404 y sin esto
    la fila quedaria coja."""
    partes = [p for p in str(name or "").replace(",", " ").split() if p[:1].isalpha()]
    if not partes:
        return "?"
    if len(partes) == 1:
        return partes[0][:2].upper()
    return (partes[0][:1] + partes[-1][:1]).upper()


def build_view(
    payload: dict,
    *,
    now: Optional[datetime] = None,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    max_rows: int = DEFAULT_MAX_ROWS,
    show_people: bool = True,
) -> dict:
    """Modelo de vista a partir del agregado que publica el flow.

    Contrato esperado (lo define quiosco, lo cumple el flow):

        {
          "generated_at": "2026-08-31T16:11:00Z",
          # Inicio de la semana EN CURSO: el ultimo miercoles 12:30Z (= 07:30
          # Lima, el corte de la reunion) que sea <= ahora. Nunca uno futuro:
          # un "% de la semana" necesita un inicio en el pasado que contar.
          "week_start":   "2026-08-26T12:30:00Z",
          "at_risk": [ {"id": 65036, "kind": "TTR",
                        "due": "2026-08-28T16:15:00Z", "priority": 5,
                        "assignee": {"id": 6728, "name": "...",
                                     "photo": "data:image/jpeg;base64,..."}} ],
          "counts": {"breached_ttf": 35, "breached_ttr": 30,
                     "untriaged": 26, "waiting_user": 64, "active": 69}
        }

    'at_risk' NUNCA trae titulo ni solicitante: ese contrato vive en el flow, no
    en la disciplina de este modulo (pii_scrub no limpia nombres de personas de
    los titulos — bead cip-it-analytics-ff1).
    """
    now = now or datetime.now(timezone.utc)

    crudas = payload.get("at_risk")
    if not isinstance(crudas, list):
        raise ValueError("El agregado del ITSM no trae una lista 'at_risk'")

    filas = []
    for t in crudas:
        if not isinstance(t, dict):
            continue
        vence = _parse(t.get("due"))
        if vence is None:
            continue
        horas = (vence - now).total_seconds() / 3600
        # Lo ya vencido no se lista: el panel es de los que todavia se pueden
        # salvar. Los vencidos van en los contadores del equipo.
        if horas < 0 or horas > horizon_hours:  # el tope es red de seguridad
            continue

        quien = t.get("assignee") or {}
        nombre = str(quien.get("name") or "").strip()
        filas.append({
            "id": t.get("id"),
            "kind": str(t.get("kind") or "").upper()[:3],
            "hours": horas,
            "remaining": remaining_text(horas),
            "band": band_for(horas),
            "priority": t.get("priority"),
            "name": nombre if show_people else "",
            "initials": initials(nombre) if (show_people and nombre) else "",
            "photo": (quien.get("photo") or "") if show_people else "",
            "unassigned": not quien.get("id"),
        })

    filas.sort(key=lambda f: f["hours"])
    mostradas, omitidas = filas[:max_rows], max(0, len(filas) - max_rows)

    c = payload.get("counts") or {}
    def _c(k):
        try:
            return int(c.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    n = len(filas)
    urgentes = sum(1 for f in filas if f["hours"] < 1)
    if urgentes:
        severity = "caida"
    elif n:
        severity = "leve"
    else:
        severity = "ok"

    semana = _parse(payload.get("week_start"))
    return {
        "clock": now.astimezone(LIMA).strftime("%H:%M"),
        "week_label": (
            f"semana desde {semana.astimezone(LIMA):%a %-d, %H:%M}" if semana else ""
        ),
        "severity": severity,
        "problem": severity != "ok",
        "headline": (
            f"{n} por brechearse" if n else "Nada por brechearse"
        ),
        "subline": (
            f"el más urgente en {mostradas[0]['remaining']}" if mostradas
            else "todos con holgura"
        ),
        "rows": mostradas,
        "omitted": omitidas,
        "counts": {
            "breached_ttf": _c("breached_ttf"),
            "breached_ttr": _c("breached_ttr"),
            "untriaged": _c("untriaged"),
            "waiting_user": _c("waiting_user"),
        },
    }


# --- Cache con red ---

class PanelCache:
    """Cachea el AGREGADO, no la vista.

    Diferencia con los otros dos paneles: aqui la vista se reconstruye en cada
    lectura aunque el agregado venga de cache, porque la cuenta regresiva tiene
    que ser exacta contra el reloj de ahora. El flow se llama cada
    refresh_seconds; el panel repinta cada poll_seconds.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_REFRESH_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._payload: Optional[dict] = None
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
            if self._payload is None or age is None or age >= self.ttl_seconds:
                try:
                    if not settings["flow_url"]:
                        raise RuntimeError(
                            "Falta ITSM_PANEL_FLOW_URL en el entorno del contenedor"
                        )
                    # GET, no POST: el trigger del flow es de lectura. Lo fija
                    # un test porque cambiarlo por analogia con otros flows del
                    # environment (CreateQaTicketFromApi si es POST) rompe en
                    # silencio con un 4xx que se veria como "fuente caida".
                    r = await client.get(
                        settings["flow_url"], timeout=DEFAULT_TIMEOUT_SECONDS
                    )
                    r.raise_for_status()
                    payload = r.json()
                    # Se valida antes de cachear: un agregado malformado no debe
                    # desplazar al ultimo bueno.
                    build_view(payload)
                except Exception as exc:  # noqa: BLE001
                    # La URL lleva el SAS, asi que nunca se loguea el detalle de
                    # httpx (puede traer la URL completa).
                    self.last_error = (f"{type(exc).__name__}: {exc}"
                                       if isinstance(exc, (ValueError, RuntimeError))
                                       else type(exc).__name__)
                    if self._payload is None:
                        logger.warning("Panel ITSM sin datos todavia: %s", self.last_error)
                        raise
                    logger.warning(
                        "Panel ITSM: fallo el refresco (%s); se sirve el agregado de hace %.0fs",
                        self.last_error, age or 0,
                    )
                else:
                    self.last_error = None
                    self._payload = payload
                    self._fetched_monotonic = time.monotonic()
                    age = 0.0

            # La vista se reconstruye siempre, aunque el agregado venga de cache:
            # la cuenta regresiva se mide contra ahora, no contra la lectura.
            view = build_view(
                self._payload,
                horizon_hours=settings["horizon_hours"],
                max_rows=settings["max_rows"],
                show_people=settings["show_people"],
            )
            edad = int(age or 0)
            view["age_seconds"] = edad
            view["stale"] = edad > self.ttl_seconds * 2
            return view


# --- Render ---

_STYLE = """
  table { flex:1; }
  .cuenta { width:210px; padding-left:14px; }
  .cuenta .t { font-size:33px; font-weight:650; letter-spacing:-.02em; color:var(--tinta-3); }
  .fila.caida .cuenta .t { color:var(--caida); }
  .fila.leve  .cuenta .t { color:var(--leve); }
  .tk { font-size:29px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-.01em; }
  .tipo { font-size:14px; font-weight:650; margin-left:12px; padding:2px 7px;
          border-radius:4px; vertical-align:middle; letter-spacing:.03em; }
  .tipo.ttr { background:#2d2416; color:var(--leve); }
  .tipo.ttf { background:#2a1a18; color:var(--grave); }
  .pri { font-size:14px; color:var(--tinta-3); margin-left:8px; font-weight:400;
         border:1px solid var(--linea); border-radius:4px; padding:1px 6px; vertical-align:middle; }
  .quien { text-align:right; padding-right:2px; white-space:nowrap; }
  .av { display:inline-flex; align-items:center; justify-content:center; width:38px; height:38px;
        border-radius:50%; font-size:15px; font-weight:650; color:#fff; vertical-align:middle;
        background:#3a3a37; object-fit:cover; }
  .av.sin { background:transparent; border:2px dashed var(--linea); color:var(--tinta-3); }
  .nom { font-size:16px; color:var(--tinta-2); margin-left:11px; vertical-align:middle; }
  .contexto { display:flex; gap:11px; margin-top:11px; }
  .cx { flex:1; background:var(--superficie); border-radius:9px; padding:9px 14px;
        border-top:3px solid var(--neutro); }
  .cx.mal { border-top-color:var(--caida); }
  .cx-n { font-size:25px; font-weight:650; letter-spacing:-.02em; }
  .cx.cero .cx-n { color:var(--tinta-3); }
  .cx-t { font-size:13px; color:var(--tinta-3); margin-top:1px; }
  .mas { font-size:14px; color:var(--tinta-3); padding:8px 0 0 14px; }
  .limpio { margin:auto; text-align:center; color:var(--tinta-3); font-size:19px; padding:30px 0; }
"""

_SCRIPT = """
function pintar(v) {
  document.getElementById('reloj').innerHTML =
    (v.stale ? `<span class="stale">datos de hace ${Math.round(v.age_seconds/60)} min</span>`
             : `actualizado ${v.clock}`) + (v.week_label ? ` \\u00B7 ${v.week_label}` : '');

  const hero = document.getElementById('hero');
  hero.className = 'hero ' + v.severity;
  document.getElementById('hero-icono').textContent =
    v.severity === 'caida' ? '\\u25B2' : (v.severity === 'leve' ? '\\u25A0' : '\\u25CF');
  document.getElementById('hero-txt').textContent = v.headline;
  document.getElementById('hero-sub').textContent = v.subline;

  const cuerpo = document.getElementById('filas');
  if (!v.rows.length) {
    cuerpo.innerHTML = `<tr><td class="limpio">Ningún ticket a punto de brechear</td></tr>`;
  } else {
    cuerpo.innerHTML = v.rows.map(r => {
      let quien = '';
      if (r.unassigned) {
        quien = `<td class="quien"><span class="av sin">—</span><span class="nom">sin asignar</span></td>`;
      } else if (r.name) {
        // La foto puede faltar (el conector da 404 para quien no la tiene): las
        // iniciales no son decoracion, son el camino normal para varios.
        const av = r.photo
          ? `<img class="av" src="${r.photo}" alt="">`
          : `<span class="av">${escapar(r.initials)}</span>`;
        quien = `<td class="quien">${av}<span class="nom">${escapar(r.name)}</span></td>`;
      }
      return `<tr class="fila ${r.band}">
        <td class="cuenta"><span class="t">${escapar(r.remaining)}</span></td>
        <td class="tk">#${escapar(String(r.id))}<span class="tipo ${r.kind.toLowerCase()}">${escapar(r.kind)}</span>
            <span class="pri">P${escapar(String(r.priority))}</span></td>
        ${quien}
      </tr>`;
    }).join('');
  }

  document.getElementById('mas').textContent =
    v.omitted ? `y ${v.omitted} más que no caben en pantalla` : '';

  const C = [
    ['mal', v.counts.breached_ttf, 'ya vencidos en TTF'],
    ['mal', v.counts.breached_ttr, 'ya vencidos en TTR'],
    ['',    v.counts.untriaged,    'sin triar'],
    ['',    v.counts.waiting_user, 'esperando al usuario'],
  ];
  document.getElementById('contexto').innerHTML = C.map(([c, n, t]) =>
    `<div class="cx ${c}${n ? '' : ' cero'}"><div class="cx-n">${n}</div><div class="cx-t">${t}</div></div>`
  ).join('');
}

pintar(JSON.parse(document.getElementById('datos').textContent));
setInterval(() => refrescar('/api/itsm-panel'), REFRESH_MS);
"""

_BODY = """
  <header>
    <h1>Tickets por brechearse</h1>
    <span class="reloj" id="reloj"></span>
  </header>

  <div class="hero" id="hero">
    <span class="hero-icono" id="hero-icono"></span>
    <span class="hero-txt" id="hero-txt"></span>
    <span class="hero-sub" id="hero-sub"></span>
  </div>

  <table><tbody id="filas"></tbody></table>
  <div class="mas" id="mas"></div>
  <div class="contexto" id="contexto"></div>

  <footer>
    <span class="leyenda">
      <span class="clave"><i style="background:var(--caida);height:11px"></i>menos de 1 h</span>
      <span class="clave"><i style="background:var(--leve);height:11px"></i>menos de 4 h</span>
    </span>
    <span class="fuente">fuente ITSM · CIP-IT</span>
  </footer>
"""


def render_html(view: dict, *, poll_seconds: float) -> str:
    return panel_ui.page(
        title="Tickets por brechearse",
        style=_STYLE,
        body=_BODY,
        view=view,
        script=panel_ui.SCRIPT_COMUN + _SCRIPT,
        refresh_seconds=poll_seconds,
    )


def render_error_html(message: str) -> str:
    return panel_ui.error_page("Tickets por brechearse", message)

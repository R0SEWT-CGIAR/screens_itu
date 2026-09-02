"""Panel de cumplimiento semanal por persona (quiosco-2jp.3).

Lo publica un flow distinto del que alimenta el panel de tickets: 'OPS - ITSM
Weekly Compliance', con su propia URL en el .env de exodia
(ITSM_WEEKLY_FLOW_URL). Trae la semana en curso, la anterior completa, y la
anterior RECORTADA A ESTA MISMA ALTURA, que es la comparacion honesta un
miercoles a media mañana.

TRES DECISIONES QUE NO SON DE ESTILO, SINO DE NO MENTIR EN UNA PARED:

1. EL TTR QUE SE MUESTRA ES UN PISO, NO LA MEDIA, y el panel lo dice. Un ticket
   viejo cerrado esta semana entra al TTR con su plazo de respuesta vencido hace
   meses, probablemente antes de que quien lo cerro lo tuviera asignado: solo
   puede entrar como incumplido. Se mantiene la definicion del reporte ITSM360
   por paridad —un panel que muestre 82 % donde el reporte sancionado muestra
   70 % hace que la reunion discuta cual miente en vez de que hacer— pero el
   sesgo tiene signo fijo y se declara en pantalla (bead quiosco-rfv).

2. SIN PORCENTAJE CON POCA MASA. El corte de semana es el miercoles 12:30Z, asi
   que cada miercoles a media mañana la semana en curso lleva minutos y sus
   conteos son 0. Un porcentaje sobre n=2 es ruido con aires de metrica: por
   debajo del umbral se muestran los conteos crudos y se rotula la semana como
   recien iniciada.

3. MIENTRAS LA SEMANA ESTA VERDE SE MUESTRA LA ANTERIOR, ROTULADA. Una tabla de
   ceros no informa a nadie; la semana cerrada si, siempre que la pantalla diga
   cual esta mirando.

Y una que si es de estilo, de la guia dataviz: la barra de cada persona es
cumplido contra incumplido, dos segmentos con 2 px de superficie entre ellos y
los conteos escritos al lado. Sin hover —el destino es un Chromecast— y sin
paleta categorica: los dos colores son ESTADO (--ok, --caida), reservados y
siempre con etiqueta de texto.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from . import panel_ui
from .itsm_panel import (DIAS, LIMA, _parse, available_photos, display_name)

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 600.0     # la semana no se mueve rapido
DEFAULT_POLL_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MIN_N = 8                   # masa minima para publicar un porcentaje
DEFAULT_MAX_ROWS = 8                # personas que caben en 720 px


def panel_settings(config: dict) -> dict:
    """Bloque 'weekly_panel' del config.json mas la URL del flow del entorno.

    La URL lleva SAS y por eso no vive en config.json, que se lee por SSH y lo
    reescribe la consola web. Es OTRA distinta de la del panel de tickets.
    """
    raw = config.get("weekly_panel") or {}
    if not isinstance(raw, dict):
        raw = {}

    def _num(clave, defecto, minimo):
        try:
            return max(minimo, float(raw.get(clave, defecto)))
        except (TypeError, ValueError):
            return defecto

    return {
        "flow_url": os.environ.get("ITSM_WEEKLY_FLOW_URL", ""),
        "refresh_seconds": _num("refresh_seconds", DEFAULT_REFRESH_SECONDS, 60),
        "poll_seconds": _num("poll_seconds", DEFAULT_POLL_SECONDS, 15),
        "min_n": int(_num("min_n", DEFAULT_MIN_N, 1)),
        "max_rows": int(_num("max_rows", DEFAULT_MAX_ROWS, 1)),
        "show_people": bool(raw.get("show_people", True)),
    }


# --- Normalizacion (puro, sin red) ---

def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _reloj_par(bloque: dict, clave: str) -> dict:
    """El par cumplido/incumplido de un reloj, mas su total evaluable.

    'total' NO es la cantidad de cerrados: un ticket sin deadline conocido no
    cuenta ni a favor ni en contra, y meterlo en el denominador hundiria el
    porcentaje por una ausencia de dato.
    """
    par = bloque.get(clave) if isinstance(bloque, dict) else None
    if not isinstance(par, dict):
        par = {}
    ok, bad = _int(par.get("ok")), _int(par.get("bad"))
    return {"ok": ok, "bad": bad, "total": ok + bad}


def pct(ok: int, total: int, min_n: int) -> Optional[int]:
    """Porcentaje solo con masa suficiente; si no, None y se pintan conteos."""
    if total < min_n or total <= 0:
        return None
    return round(100 * ok / total)


def _bloque(raw, min_n: int) -> dict:
    """Un bloque del agregado normalizado. Lo que llegue mal vale cero.

    Mismo criterio que los otros paneles: un agregado torcido degrada a ceros
    visibles, no a un 500 que en un Chromecast se ve como pantalla en blanco.
    """
    if not isinstance(raw, dict):
        raw = {}
    ttf = _reloj_par(raw, "ttf")
    ttr = _reloj_par(raw, "ttr")
    return {
        "closed": _int(raw.get("closed")),
        "no_deadline": _int(raw.get("no_deadline")),
        "ttf": {**ttf, "pct": pct(ttf["ok"], ttf["total"], min_n)},
        "ttr": {**ttr, "pct": pct(ttr["ok"], ttr["total"], min_n)},
        "from": raw.get("from") if isinstance(raw.get("from"), str) else "",
        "to": raw.get("to") if isinstance(raw.get("to"), str) else "",
    }


def _etiqueta_semana(desde: str) -> str:
    """'semana desde mié 26 07:30', en hora de Lima."""
    d = _parse(desde)
    if d is None:
        return ""
    lima = d.astimezone(LIMA)
    return f"semana desde {DIAS[lima.weekday()]} {lima.day} {lima:%H:%M}"


def build_view(
    payload: dict,
    *,
    now: Optional[datetime] = None,
    min_n: int = DEFAULT_MIN_N,
    max_rows: int = DEFAULT_MAX_ROWS,
    show_people: bool = True,
    photo_ids: frozenset[str] = frozenset(),
) -> dict:
    """Modelo de vista del agregado semanal.

    Contrato esperado (lo publica el flow; ver docs/changes del lado de
    CIP_AGENTS):

        {
          "generated_at": "2026-09-02T15:17:55Z",
          "current": {"from","to","closed","ttf":{"ok","bad"},
                      "ttr":{"ok","bad"},"no_deadline",
                      "by_person":[{"id","name",
                                    "current":{...},"previous":{...},
                                    "previous_to_date":{...}}]},
          "previous":         {"from","to","closed","ttf","ttr","no_deadline"},
          "previous_to_date": {"from","to","closed","ttf","ttr","no_deadline"}
        }

    'previous_to_date.to' llega con SIETE decimales de segundo
    (2026-08-26T15:18:02.0000000Z) porque es un campo calculado del flow, no un
    campo de lista. fromisoformat lo acepta desde 3.11; hay test para que un
    cambio de intérprete no lo rompa en silencio.
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict) or not isinstance(payload.get("current"), dict):
        raise ValueError("El agregado semanal no trae el bloque 'current'")

    crudo_actual = payload["current"]
    actual = _bloque(crudo_actual, min_n)
    anterior = _bloque(payload.get("previous"), min_n)
    hasta_aqui = _bloque(payload.get("previous_to_date"), min_n)

    # Con la semana recien abierta los conteos son 0 y una tabla de ceros no
    # informa: se muestra la semana cerrada, rotulada como tal.
    masa_actual = actual["ttf"]["total"] + actual["ttr"]["total"]
    verde = masa_actual < min_n
    modo = "previous" if verde else "current"

    filas = []
    if show_people:
        for p in crudo_actual.get("by_person") or []:
            if not isinstance(p, dict):
                continue
            bloque = _bloque(p.get(modo), min_n)
            pid = str(p.get("id") or "")
            nombre = display_name(p.get("name"))
            filas.append({
                "id": p.get("id"),
                "name": nombre,
                "initials": "".join(x[0] for x in nombre.split()[:2]).upper(),
                "photo": f"/static/photos/{pid}.jpg" if pid in photo_ids else "",
                "closed": bloque["closed"],
                "ttf": bloque["ttf"],
                "ttr": bloque["ttr"],
                "no_deadline": bloque["no_deadline"],
            })
        # Por volumen cerrado, no por porcentaje: ordenar por cumplimiento
        # convierte un sesgo conocido en un ranking, y el que sale ultimo suele
        # ser quien vacio backlog.
        filas.sort(key=lambda f: (-f["closed"], f["name"]))

    mostradas, omitidas = filas[:max_rows], max(0, len(filas) - max_rows)
    referencia = anterior if modo == "previous" else hasta_aqui

    return {
        "clock": now.astimezone(LIMA).strftime("%H:%M"),
        "week_label": _etiqueta_semana(actual["from"]),
        "severity": "ok",
        "mode": modo,
        "fresh_week": verde,
        "table_label": ("semana anterior, ya cerrada" if modo == "previous"
                        else "semana en curso"),
        "headline": (f"{actual['closed']} cerrados esta semana"
                     if not verde else "La semana acaba de empezar"),
        "subline": (f"a esta altura la semana pasada: {hasta_aqui['closed']}"
                    if hasta_aqui["closed"] or verde
                    else f"la semana pasada cerro {anterior['closed']}"),
        "current": actual,
        "previous": anterior,
        "previous_to_date": hasta_aqui,
        "reference": referencia,
        "rows": mostradas,
        "omitted": omitidas,
        "min_n": min_n,
    }


# --- Cache ---

class PanelCache:
    """Una lectura por ventana de refresco, compartida por las pantallas.

    Mismo criterio que los otros paneles: ante un fallo se sirve la ultima
    vista buena marcada como stale, porque una pantalla en blanco no se
    distingue de un cuelgue. Y el detalle del error nunca se loguea: la URL
    lleva SAS y httpx la incluye en sus mensajes.
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
                if not settings["flow_url"]:
                    raise RuntimeError(
                        "Falta ITSM_WEEKLY_FLOW_URL en el entorno del contenedor")
                # file:// sirve un agregado guardado en disco, igual que en el
                # panel de tickets: se itera el diseño en la laptop sin
                # llevarse la URL con SAS, y el dataset queda estable.
                if settings["flow_url"].startswith("file://"):
                    crudo = json.loads(Path(settings["flow_url"][7:]).read_text())
                else:
                    r = await client.get(settings["flow_url"],
                                         timeout=DEFAULT_TIMEOUT_SECONDS)
                    r.raise_for_status()
                    crudo = r.json()
                view = build_view(
                    crudo,
                    min_n=settings["min_n"],
                    max_rows=settings["max_rows"],
                    show_people=settings["show_people"],
                    photo_ids=available_photos(),
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = (f"{type(exc).__name__}: {exc}"
                                   if isinstance(exc, (ValueError, RuntimeError))
                                   else type(exc).__name__)
                if self._view is None:
                    logger.warning("Panel semanal sin datos todavia: %s", self.last_error)
                    raise
                logger.warning(
                    "Panel semanal: fallo el refresco (%s); se sirve la vista de hace %.0fs",
                    self.last_error, age or 0,
                )
                return self._decorate(self._view, age)

            self.last_error = None
            self._view = view
            self._fetched_monotonic = time.monotonic()
            return self._decorate(view, 0.0)

    def _decorate(self, view: dict, age: Optional[float]) -> dict:
        out = dict(view)
        edad = int(age or 0)
        out["age_seconds"] = edad
        out["stale"] = edad > self.ttl_seconds * 2
        return out


# --- Render ---

_STYLE = """
  :root {
    --ok:#01b8aa; --caida:#fd625e; --leve:#f2c80f; --neutro:#5a5a56;
  }
  .hero { border-left-color:var(--ok); }
  .hero-chip { margin-left:16px; font-size:15px; color:var(--leve);
               border:1px solid var(--leve); border-radius:20px; padding:3px 11px; }
  /* Sin rotulo no queda la pildora vacia flotando al lado del titular. */
  .hero-chip:empty { display:none; }

  .equipo { display:flex; gap:12px; margin-bottom:10px; }
  .eq { flex:1; background:var(--superficie); border-radius:10px; padding:9px 16px;
        border-top:3px solid var(--neutro); }
  .eq-t { font-size:13px; color:var(--tinta-3); }
  .eq-n { font-size:27px; font-weight:650; letter-spacing:-.02em;
          font-variant-numeric:tabular-nums; }
  .eq-sub { font-size:13px; color:var(--tinta-3); }

  /* El pie lleva la leyenda y el aviso del piso del TTR, asi que la tabla se
     recorta antes que empujarlo fuera de los 720 px. Las filas van justas por
     lo mismo: con ocho personas y el bloque de equipo, el alto no sobra. */
  table { flex:1; min-height:0; overflow:hidden; }
  .cab th { font-size:13px; font-weight:400; color:var(--tinta-3);
            text-align:left; padding-bottom:6px; }
  .cab th.num { text-align:right; }
  .fila td { padding:5px 8px; }
  .quien { white-space:nowrap; }
  .av { display:inline-flex; align-items:center; justify-content:center;
        width:34px; height:34px; border-radius:50%; font-size:13px; font-weight:650;
        color:#fff; background:#3a3a37; vertical-align:middle; object-fit:cover; }
  .nom { font-size:18px; color:var(--tinta-2); margin-left:11px; vertical-align:middle; }
  .cerrados { width:110px; text-align:right; font-size:21px;
              font-variant-numeric:tabular-nums; }

  /* Dos segmentos con 2 px de superficie entre ellos, y los conteos escritos
     al lado: el color no es el unico canal. */
  .reloj-celda { width:270px; }
  .barra { display:flex; gap:2px; height:13px; border-radius:3px; overflow:hidden;
           background:#232320; }
  .barra i { display:block; height:100%; }
  .barra .b-ok { background:var(--ok); }
  .barra .b-mal { background:var(--caida); }
  .barra.vacia { background:#232320; }
  .cuenta { font-size:13px; color:var(--tinta-3); margin-top:3px;
            font-variant-numeric:tabular-nums; }
  .cuenta b { color:var(--tinta-2); font-weight:600; }
  /* Un cero no se pinta de alarma: 'ninguno incumplido' es la buena noticia
     (mismo criterio que los contadores del panel de tickets). */
  .cuenta .mal { color:var(--caida); }
  /* 'n=6' no es un fallo, es masa insuficiente: en tinta apagada y no en la de
     caida, que se leeria como alarma. */
  .cuenta .poco { color:var(--tinta-3); }

  .mas { font-size:14px; color:var(--tinta-3); padding:6px 0 0 8px; }
  .vacio { align-self:start; text-align:center; color:var(--tinta-3);
           font-size:19px; padding:26px 0; }
  .piso { color:var(--leve); }
  body footer { margin-top:auto; }
"""

_SCRIPT = """
function barra(par, min_n) {
  if (!par.total) {
    return `<div class="barra vacia"></div>
            <div class="cuenta">sin tickets con plazo</div>`;
  }
  const ok = Math.round(100 * par.ok / par.total);
  const pct = par.pct === null
    ? `<span class="poco">n=${par.total}</span>`
    : `<b>${par.pct}%</b>`;
  return `<div class="barra">
      <i class="b-ok" style="width:${ok}%"></i>
      <i class="b-mal" style="width:${100 - ok}%"></i>
    </div>
    <div class="cuenta">${pct} · ${par.ok} cumplidos, <span class="${
      par.bad ? 'mal' : ''}">${par.bad}</span> no</div>`;
}

function pintar(v) {
  document.getElementById('reloj').innerHTML =
    (v.stale ? `<span class="stale">datos de hace ${Math.round(v.age_seconds/60)} min</span>`
             : `actualizado ${v.clock}`) + (v.week_label ? ` \\u00B7 ${v.week_label}` : '');

  document.getElementById('hero-txt').textContent = v.headline;
  document.getElementById('hero-sub').textContent = v.subline;
  document.getElementById('hero-chip').textContent =
    v.fresh_week ? 'semana recién iniciada' : '';

  const b = v[v.mode === 'previous' ? 'previous' : 'current'];
  const ref = v.reference;
  document.getElementById('equipo').innerHTML = `
    <div class="eq"><div class="eq-t">cerrados · ${escapar(v.table_label)}</div>
      <div class="eq-n">${b.closed}</div>
      <div class="eq-sub">${v.mode === 'previous'
        ? 'semana completa' : 'a esta altura la pasada: ' + ref.closed}</div></div>
    <div class="eq"><div class="eq-t">TTF del equipo</div>
      <div class="eq-n">${b.ttf.pct === null ? '\\u2014' : b.ttf.pct + '%'}</div>
      <div class="eq-sub">${b.ttf.ok} cumplidos, ${b.ttf.bad} no</div></div>
    <div class="eq"><div class="eq-t">TTR del equipo</div>
      <div class="eq-n">${b.ttr.pct === null ? '\\u2014' : b.ttr.pct + '%'}</div>
      <div class="eq-sub">${b.ttr.ok} cumplidos, ${b.ttr.bad} no</div></div>
    <div class="eq"><div class="eq-t">sin plazo conocido</div>
      <div class="eq-n">${b.no_deadline}</div>
      <div class="eq-sub">fuera de los dos ratios</div></div>`;

  const cuerpo = document.getElementById('filas');
  if (!v.rows.length) {
    cuerpo.innerHTML = `<tr><td class="vacio" colspan="4">
      El agregado no trae personas<small></small></td></tr>`;
  } else {
    cuerpo.innerHTML = v.rows.map(r => {
      const av = r.photo ? `<img class="av" src="${r.photo}" alt="">`
                         : `<span class="av">${escapar(r.initials)}</span>`;
      return `<tr class="fila">
        <td class="quien">${av}<span class="nom">${escapar(r.name)}</span></td>
        <td class="cerrados">${r.closed}</td>
        <td class="reloj-celda">${barra(r.ttf, v.min_n)}</td>
        <td class="reloj-celda">${barra(r.ttr, v.min_n)}</td>
      </tr>`;
    }).join('');
  }
  document.getElementById('mas').textContent =
    v.omitted ? `y ${v.omitted} más que no caben en pantalla` : '';
  document.getElementById('cabecera').textContent = v.table_label;
}

pintar(JSON.parse(document.getElementById('datos').textContent));
setInterval(() => refrescar('/api/weekly-panel'), REFRESH_MS);
"""

_BODY = """
  <header>
    <h1>Cumplimiento de la semana</h1>
    <span class="reloj" id="reloj"></span>
  </header>

  <div class="hero" id="hero">
    <span class="hero-txt" id="hero-txt"></span>
    <span class="hero-chip" id="hero-chip"></span>
    <span class="hero-sub" id="hero-sub"></span>
  </div>

  <div class="equipo" id="equipo"></div>

  <table>
    <thead><tr class="cab">
      <th id="cabecera"></th>
      <th class="num">cerrados</th>
      <th>TTF · tiempo de solución</th>
      <th>TTR · primera respuesta</th>
    </tr></thead>
    <tbody id="filas"></tbody>
  </table>
  <div class="mas" id="mas"></div>

  <footer>
    <span class="leyenda">
      <span class="clave"><i style="background:var(--ok);height:11px"></i>cumplido</span>
      <span class="clave"><i style="background:var(--caida);height:11px"></i>no cumplido</span>
      <span class="clave piso">el TTR es un piso: un ticket viejo cerrado hoy entra ya vencido</span>
    </span>
    <span class="fuente">fuente ITSM · CIP-IT</span>
  </footer>
"""


def render_html(view: dict, *, poll_seconds: float) -> str:
    return panel_ui.page(
        title="Cumplimiento de la semana",
        style=_STYLE,
        body=_BODY,
        view=view,
        script=panel_ui.SCRIPT_COMUN + _SCRIPT,
        refresh_seconds=poll_seconds,
    )


def render_error_html(message: str) -> str:
    return panel_ui.error_page("Cumplimiento de la semana", message)

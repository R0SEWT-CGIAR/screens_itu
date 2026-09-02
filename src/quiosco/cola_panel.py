"""Panel de como va la cola de tickets: los contadores del ITSM en el tiempo.

El agregado del ITSM trae cinco contadores del equipo cada vez que se lee, y
hasta ahora el panel de tickets los pintaba y los olvidaba. Un 63 en vencidos
no dice nada por si solo: no se sabe si veniamos de 40 o de 90. Esa es la
pregunta que una pantalla de pared puede contestar y una tabla no, porque lo
unico que hace falta es mirarla dos veces en la semana.

Dos piezas:

- ColaHistory guarda una muestra cada pocos minutos en el volumen montado
  (data/, junto al config.json), con poda por antiguedad. Sobrevive reinicios
  del contenedor porque una serie que se borra en cada despliegue no sirve.
- build_view convierte esa serie en cuatro tarjetas —valor de ahora, cuanto se
  movio, y la curva— mas el total de activos en el hero.

Decisiones de dataviz, siguiendo la guia y lo que ya hace panel_ui:

- CUATRO PEQUENOS MULTIPLOS, no un grafico de cuatro series. Cuatro lineas
  encimadas en una pantalla que se mira de lejos y de paso obligan a una
  leyenda; cuatro tarjetas con una curva cada una se leen de un vistazo y el
  numero grande sigue siendo el protagonista.
- EL COLOR ES ESTADO, NO IDENTIDAD. Los dos contadores de vencidos van en la
  tinta de caida y los otros dos en tinta neutra, con su etiqueta al lado. No
  hay paleta categorica que validar: no hay cuatro colores compitiendo por
  significar cuatro cosas.
- SIN HOVER. El destino es un Chromecast y no hay puntero, asi que todo valor
  que importe esta escrito: el de ahora, el delta y los extremos de la ventana.
- El par que si comparte grafico (linea de caida contra linea neutra) se midio
  con el validador de la guia: dE 13.0 en deuteranopia y 22.1 en vision normal,
  ambos sobre el minimo de 8. Las dos marcas FAIL que devuelve son de banda de
  luminosidad y piso de croma, que aplican a paletas categoricas; aqui una de
  las dos tintas tiene que leerse gris a proposito.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import panel_ui
from .itsm_panel import (DEFAULT_WEEK_TIME, DEFAULT_WEEK_WEEKDAY, DIAS, LIMA,
                         week_start)

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_SECONDS = 300.0      # cada cuanto se anota una muestra
DEFAULT_POLL_SECONDS = 60.0         # cada cuanto repinta la pagina
DEFAULT_WINDOW_HOURS = 24.0         # que tramo de la serie se dibuja
DEFAULT_RETENTION_DAYS = 30.0       # cuanto se guarda en disco
HISTORY_FILENAME = "cola-history.jsonl"

# Las cuatro tarjetas, en orden de lectura. La banda decide el color de la
# curva: 'mal' es un contador que solo puede ser malas noticias.
TARJETAS = (
    ("breached_ttf", "vencidos en TTF", "mal"),
    ("breached_ttr", "vencidos en TTR", "mal"),
    ("untriaged", "sin triar", ""),
    ("waiting_user", "esperando al usuario", ""),
)

# viewBox de la curva. La relacion 250x90 la repite el CSS con aspect-ratio
# para que el SVG escale UNIFORME: con preserveAspectRatio='none' el trazo
# sale de grosor distinto en cada eje y el punto del final queda ovalado.
SPARK_W, SPARK_H = 250, 90
MAX_SPARK_POINTS = 120              # mas puntos que eso no se distinguen


def panel_settings(config: dict) -> dict:
    """Bloque 'cola_panel' del config.json, con pisos para no dispararse."""
    raw = config.get("cola_panel") or {}
    if not isinstance(raw, dict):
        raw = {}

    def _num(clave, defecto, minimo):
        try:
            return max(minimo, float(raw.get(clave, defecto)))
        except (TypeError, ValueError):
            return defecto

    return {
        "sample_seconds": _num("sample_seconds", DEFAULT_SAMPLE_SECONDS, 60),
        "poll_seconds": _num("poll_seconds", DEFAULT_POLL_SECONDS, 15),
        "window_hours": _num("window_hours", DEFAULT_WINDOW_HOURS, 1),
        "retention_days": _num("retention_days", DEFAULT_RETENTION_DAYS, 1),
    }


def history_path(config_path: str | os.PathLike) -> Path:
    """Junto al config.json, en data/, que es el volumen montado en produccion."""
    return Path(config_path).resolve().parent / "data" / HISTORY_FILENAME


def aviso_si_no_hay_volumen(
    path: Path,
    *,
    en_contenedor: Optional[bool] = None,
    raiz_app: Path = Path("/app"),
) -> Optional[str]:
    """Grita si la serie va a caer DENTRO de la imagen en vez del volumen.

    Este fallo pertenece a la peor familia: la que se degrada a algo que parece
    normal. Sin el volumen montado el panel funciona, muestra su curva, y en el
    siguiente despliegue la serie desaparece sin que nada falle a la vista —
    justo lo contrario de la variable de entorno que falto hoy, que al menos
    dijo su nombre en pantalla. Los fallos que gritan se arreglan solos.

    Se comprueba solo dentro del contenedor y comparando dispositivos: si data/
    esta en el mismo sistema de archivos que la raiz de la app, no hay bind
    mount. En la laptop no aplica y no se avisa nada.
    """
    if en_contenedor is None:
        en_contenedor = Path("/.dockerenv").exists()
    if not en_contenedor:
        return None
    try:
        dev_datos = path.parent.stat().st_dev
        dev_app = raiz_app.stat().st_dev
    except OSError:
        return None
    if dev_datos != dev_app:
        return None
    mensaje = (
        f"La serie de la cola vive en {path.parent}, que NO es un volumen "
        "montado: cada despliegue la va a borrar y el panel seguira "
        "pintandose como si nada. Revisar el bind mount ./data:/app/data."
    )
    logger.error(mensaje)
    return mensaje


# --- La serie ---

class ColaHistory:
    """Muestras en memoria y en un jsonl, con poda por antiguedad.

    Se mantiene la lista en memoria y el archivo se usa para sobrevivir
    reinicios: cada peticion del panel lee de la lista, no del disco. Con una
    muestra cada 5 minutos y 30 dias de retencion son ~8.600 filas, que caben
    de sobra.

    Una linea ilegible se ignora en vez de reventar la carga: el archivo se
    escribe con append y un contenedor muerto a media escritura deja una linea
    partida. Perder esa muestra es aceptable; perder la serie no.
    """

    def __init__(self, path: str | os.PathLike, retention_days: float = DEFAULT_RETENTION_DAYS):
        self.path = Path(path)
        self.retention_days = retention_days
        self.samples: list[dict] = []

    def _corte(self, now: datetime) -> datetime:
        return now - timedelta(days=self.retention_days)

    def load(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        corte = self._corte(now)
        leidas, malas = [], 0
        if self.path.exists():
            for linea in self.path.read_text(errors="replace").splitlines():
                linea = linea.strip()
                if not linea:
                    continue
                muestra = _parse_sample(linea)
                if muestra is None:
                    malas += 1
                    continue
                if muestra["at"] >= corte:
                    leidas.append(muestra)
        leidas.sort(key=lambda m: m["at"])
        self.samples = leidas
        if malas:
            logger.warning("Serie de la cola: %d lineas ilegibles ignoradas", malas)
        return len(self.samples)

    def append(self, counts: dict, now: Optional[datetime] = None) -> dict:
        now = now or datetime.now(timezone.utc)
        muestra = {"at": now.replace(microsecond=0), **{
            k: int(v) for k, v in counts.items() if isinstance(v, (int, float))
        }}
        self.samples.append(muestra)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_to_json(muestra), ensure_ascii=False) + "\n")
        if self.samples and self.samples[0]["at"] < self._corte(now):
            self.prune(now)
        return muestra

    def prune(self, now: Optional[datetime] = None) -> int:
        """Reescribe el archivo sin lo que ya paso la retencion.

        Reescritura atomica: un corte de luz a media poda no debe dejar el
        archivo a medias, que es peor que tenerlo largo.
        """
        now = now or datetime.now(timezone.utc)
        corte = self._corte(now)
        quedan = [m for m in self.samples if m["at"] >= corte]
        borradas = len(self.samples) - len(quedan)
        self.samples = quedan
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            "".join(json.dumps(_to_json(m), ensure_ascii=False) + "\n" for m in quedan),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        if borradas:
            logger.info("Serie de la cola: %d muestras podadas", borradas)
        return borradas

    def window(self, hours: float, now: Optional[datetime] = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        desde = now - timedelta(hours=hours)
        return [m for m in self.samples if m["at"] >= desde]


def _parse_sample(linea: str) -> Optional[dict]:
    try:
        d = json.loads(linea)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    stamp = d.get("at")
    if not isinstance(stamp, str):
        return None
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    muestra = {"at": at if at.tzinfo else at.replace(tzinfo=timezone.utc)}
    for k, v in d.items():
        if k == "at":
            continue
        try:
            muestra[k] = int(v)
        except (TypeError, ValueError):
            continue
    return muestra


def _to_json(muestra: dict) -> dict:
    d = {k: v for k, v in muestra.items() if k != "at"}
    return {"at": muestra["at"].astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"), **d}


# --- Vista ---

def _curva(valores: list[int]) -> dict:
    """Puntos de la curva en el viewBox, mas los extremos para etiquetarlos.

    La escala arranca en el minimo de la ventana y no en cero: lo que interesa
    de un contador de cola es el movimiento, y con base cero un backlog que se
    mueve entre 60 y 65 se ve como una linea recta. El precio es que la altura
    no es comparable entre tarjetas, y por eso cada una escribe sus extremos.
    """
    if not valores:
        return {"puntos": "", "lo": None, "hi": None, "ultimo_x": None, "ultimo_y": None}
    lo, hi = min(valores), max(valores)
    rango = (hi - lo) or 1
    # Margenes por los dos ejes: el punto del final se dibuja centrado en la
    # ultima x, asi que con la curva pegada al borde del viewBox el SVG lo
    # recorta por la mitad y parece una marca a medio pintar.
    margen_x, margen_y = 8, 10
    ancho = SPARK_W - margen_x * 2
    alto = SPARK_H - margen_y * 2
    paso = ancho / max(1, len(valores) - 1) if len(valores) > 1 else 0
    pares = []
    for i, v in enumerate(valores):
        x = margen_x + i * paso if len(valores) > 1 else SPARK_W / 2
        y = margen_y + alto - (v - lo) / rango * alto
        pares.append((round(x, 1), round(y, 1)))
    return {
        "puntos": " ".join(f"{x},{y}" for x, y in pares),
        "lo": lo, "hi": hi,
        "ultimo_x": pares[-1][0], "ultimo_y": pares[-1][1],
    }


def _submuestrear(muestras: list[dict], tope: int = MAX_SPARK_POINTS) -> list[dict]:
    """Deja como maximo `tope` puntos, conservando siempre el ultimo.

    Con la ventana en dias la serie llega a miles de muestras y en 250 px no se
    distinguen: dibujarlas todas solo engorda el HTML. El ultimo punto se
    conserva a mano porque es el que lleva la etiqueta del valor de ahora.
    """
    if len(muestras) <= tope:
        return muestras
    paso = len(muestras) / tope
    elegidas = [muestras[int(i * paso)] for i in range(tope)]
    if elegidas[-1] is not muestras[-1]:
        elegidas[-1] = muestras[-1]
    return elegidas


def build_view(
    muestras: list[dict],
    *,
    now: Optional[datetime] = None,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    week_weekday: int = DEFAULT_WEEK_WEEKDAY,
    week_time: str = DEFAULT_WEEK_TIME,
) -> dict:
    """Modelo de vista a partir de la serie ya filtrada a la ventana."""
    now = now or datetime.now(timezone.utc)
    muestras = sorted(muestras, key=lambda m: m["at"])
    ultima = muestras[-1] if muestras else None
    primera = muestras[0] if muestras else None

    # Una serie que dejo de crecer es un fallo del muestreador, no calma: se
    # avisa igual que en los otros paneles en vez de pintar la ultima curva
    # como si fuera de ahora.
    edad = (now - ultima["at"]).total_seconds() if ultima else None
    parada = edad is not None and edad > sample_seconds * 3

    tarjetas = []
    for clave, etiqueta, banda in TARJETAS:
        serie = [m[clave] for m in muestras if clave in m]
        valor = serie[-1] if serie else None
        delta = (serie[-1] - serie[0]) if len(serie) > 1 else None
        tarjetas.append({
            "key": clave,
            "label": etiqueta,
            "band": banda,
            "value": valor,
            "delta": delta,
            "spark": _curva([m[clave] for m in _submuestrear(muestras) if clave in m]),
            "points": len(serie),
        })

    activos = ultima.get("active") if ultima else None
    sin_fecha = ultima.get("no_deadline") if ultima else None

    # El corte de semana se lee de la config del panel de tickets: dos paneles
    # del mismo dominio que discutan cuando empieza la semana serian peores que
    # cualquiera de los dos solo.
    semana = week_start(now, week_weekday, week_time)

    return {
        "clock": now.astimezone(LIMA).strftime("%H:%M"),
        "week_label": (f"semana desde {DIAS[semana.weekday()]} {semana.day} "
                       f"{semana:%H:%M}"),
        "severity": "leve" if _empeoro(tarjetas) else "ok",
        "headline": (f"{activos} tickets activos" if activos is not None
                     else ("Serie en blanco" if not muestras else "La cola")),
        "subline": _subline(muestras, window_hours, sample_seconds, parada),
        "cards": tarjetas,
        "window_hours": window_hours,
        "span_label": _span(primera, ultima),
        "points": len(muestras),
        "no_deadline": sin_fecha,
        "stalled": parada,
        "age_seconds": int(edad) if edad is not None else None,
        "stale": bool(parada),
    }


def _span(primera: Optional[dict], ultima: Optional[dict]) -> str:
    """De cuando a cuando va la curva.

    Con el dia por delante cuando la ventana cruza la medianoche: '08:33 →
    08:28' a secas se lee como si fuera al reves.
    """
    if not primera or not ultima or primera is ultima:
        return ""
    a = primera["at"].astimezone(LIMA)
    b = ultima["at"].astimezone(LIMA)
    if a.date() == b.date():
        return f"{a:%H:%M} → {b:%H:%M}"
    return f"{DIAS[a.weekday()]} {a:%H:%M} → {DIAS[b.weekday()]} {b:%H:%M}"


def _empeoro(tarjetas: list[dict]) -> bool:
    """Aviso solo si un contador de vencidos crecio en la ventana."""
    return any(t["band"] == "mal" and (t["delta"] or 0) > 0 for t in tarjetas)


def _subline(muestras, window_hours, sample_seconds, parada) -> str:
    if parada:
        return "el muestreador dejo de anotar"
    if not muestras:
        return f"la primera muestra entra en {int(sample_seconds / 60)} min"
    if len(muestras) == 1:
        return "una sola muestra: la curva aparece en unos minutos"
    ventana = (f"{int(window_hours)} h" if window_hours < 48
               else f"{int(window_hours / 24)} d")
    return f"ultimas {ventana} · {len(muestras)} muestras"


# --- Render ---

_STYLE = """
  :root {
    /* Misma paleta del reporte ITSM360 que usa el panel de tickets: es el
       mismo dominio y conviene que un vencido se vea del mismo color en las
       dos pantallas. */
    --ok:#01b8aa; --caida:#fd625e; --leve:#f2c80f; --neutro:#5a5a56;
  }
  .hero { border-left-color:var(--ok); }
  .hero.leve { border-left-color:var(--leve); background:#221f10; }
  .hero.leve .hero-icono { color:var(--leve); }

  /* La grilla se queda con el alto que sobra: cuatro tarjetas apretadas
     arriba dejaban 300 px de negro debajo, que es el mismo defecto que se
     arreglo en el panel de tickets. De paso la curva gana altura, que es lo
     que le faltaba para distinguir un movimiento de otro. */
  .grilla { flex:1; display:grid; grid-template-columns:1fr 1fr;
            grid-template-rows:1fr 1fr; gap:12px; min-height:0; }
  .tar { background:var(--superficie); border-radius:11px; padding:14px 18px;
         border-top:3px solid var(--neutro); display:flex; align-items:center;
         gap:18px; min-height:0; }
  .tar.mal { border-top-color:var(--caida); }
  .tar-txt { min-width:186px; }
  .tar-n { font-size:52px; font-weight:650; letter-spacing:-.02em; line-height:1.05;
           font-variant-numeric:tabular-nums; }
  .tar-t { font-size:15px; color:var(--tinta-3); margin-top:2px; }
  /* El delta lleva signo y palabra: 'sube' y 'baja' no dependen del color ni
     de que se distinga un + de un menos a cuatro metros. */
  .delta { font-size:16px; margin-top:6px; color:var(--tinta-2);
           font-variant-numeric:tabular-nums; }
  .delta.mal { color:var(--caida); }
  .delta.bien { color:var(--ok); }
  .delta.igual { color:var(--tinta-3); }

  .spark { flex:1; min-width:0; }
  .spark svg { width:100%; aspect-ratio:250/90; max-height:100%; display:block; }
  .spark .linea { fill:none; stroke:var(--tinta-2); stroke-width:2;
                  stroke-linejoin:round; stroke-linecap:round; }
  .tar.mal .spark .linea { stroke:var(--caida); }
  .spark .fin { fill:var(--tinta); }
  .tar.mal .spark .fin { fill:var(--caida); }
  .spark .ext { font-size:10px; fill:var(--tinta-3); }

  .sin-serie { align-self:start; text-align:center; color:var(--tinta-3);
               font-size:19px; padding:26px 0 6px; }
  .sin-serie strong { display:block; font-size:26px; color:var(--tinta-2);
                      font-weight:600; margin-bottom:7px; }
  body footer { margin-top:auto; }
"""

_SCRIPT = """
function curva(t) {
  const s = t.spark;
  if (!s.puntos) return `<div class="spark"></div>`;
  // Con un solo punto no hay curva que pintar y el hero ya explica por que:
  // repetirlo en las cuatro tarjetas es ruido.
  if (t.points < 2) return `<div class="spark"></div>`;
  // Los extremos van escritos: sin ellos la curva no dice de cuanto a cuanto,
  // y en una pantalla sin puntero no hay tooltip que lo cuente.
  const ext = s.hi === s.lo
    ? `<text class="ext" x="0" y="12">sin cambios en ${s.hi}</text>`
    : `<text class="ext" x="0" y="9">${s.hi}</text>
       <text class="ext" x="0" y="88">${s.lo}</text>`;
  return `<div class="spark"><svg viewBox="0 0 250 90">
    ${ext}
    <polyline class="linea" points="${s.puntos}"></polyline>
    <circle class="fin" cx="${s.ultimo_x}" cy="${s.ultimo_y}" r="4"></circle>
  </svg></div>`;
}

function delta(t) {
  if (t.delta === null || t.delta === undefined) return '';
  if (t.delta === 0) return `<div class="delta igual">sin cambio</div>`;
  const sube = t.delta > 0;
  // Que suban los vencidos es malo; que suban los que esperan al usuario, no.
  const clase = t.band === 'mal' ? (sube ? 'mal' : 'bien') : '';
  const palabra = sube ? 'sube' : 'baja';
  return `<div class="delta ${clase}">${palabra} ${Math.abs(t.delta)}</div>`;
}

function pintar(v) {
  document.getElementById('reloj').innerHTML =
    (v.stalled ? `<span class="stale">sin muestras nuevas hace ${Math.round(v.age_seconds/60)} min</span>`
               : `actualizado ${v.clock}`) + (v.week_label ? ` \\u00B7 ${v.week_label}` : '');

  const hero = document.getElementById('hero');
  hero.className = 'hero ' + v.severity;
  document.getElementById('hero-icono').textContent = v.severity === 'leve' ? '\\u25A0' : '\\u25CF';
  document.getElementById('hero-txt').textContent = v.headline;
  document.getElementById('hero-sub').textContent = v.subline;

  const grilla = document.getElementById('grilla');
  if (!v.points) {
    grilla.innerHTML = `<div class="sin-serie" style="grid-column:1/-1">
      <strong>Todavía no hay serie</strong>
      el panel anota los contadores cada pocos minutos; en cuanto haya dos muestras aparece la curva</div>`;
  } else {
    grilla.innerHTML = v.cards.map(t => `<div class="tar ${t.band}">
      <div class="tar-txt">
        <div class="tar-n">${t.value === null ? '\\u2014' : t.value}</div>
        <div class="tar-t">${escapar(t.label)}</div>
        ${delta(t)}
      </div>
      ${curva(t)}
    </div>`).join('');
  }

  document.getElementById('pie').textContent =
    [v.span_label, v.no_deadline ? `${v.no_deadline} sin fecha de SLA` : ''].filter(Boolean).join(' \\u00B7 ');
}

pintar(JSON.parse(document.getElementById('datos').textContent));
setInterval(() => refrescar('/api/cola-panel'), REFRESH_MS);
"""

_BODY = """
  <header>
    <h1>Cómo va la cola</h1>
    <span class="reloj" id="reloj"></span>
  </header>

  <div class="hero" id="hero">
    <span class="hero-icono" id="hero-icono"></span>
    <span class="hero-txt" id="hero-txt"></span>
    <span class="hero-sub" id="hero-sub"></span>
  </div>

  <div class="grilla" id="grilla"></div>

  <footer>
    <span class="leyenda" id="pie"></span>
    <span class="fuente">fuente ITSM · CIP-IT</span>
  </footer>
"""


def render_html(view: dict, *, poll_seconds: float) -> str:
    return panel_ui.page(
        title="Cómo va la cola",
        style=_STYLE,
        body=_BODY,
        view=view,
        script=panel_ui.SCRIPT_COMUN + _SCRIPT,
        refresh_seconds=poll_seconds,
    )


def render_error_html(message: str) -> str:
    return panel_ui.error_page("Cómo va la cola", message)


# --- Muestreador ---

async def sampler_loop(
    history: ColaHistory,
    counts_source: Callable[[], "object"],
    interval_seconds: float = DEFAULT_SAMPLE_SECONDS,
) -> None:
    """Anota una muestra cada `interval_seconds`.

    Vive en el lifespan y no en la peticion del panel a proposito: si la serie
    dependiera de que alguien mire la pagina, no habria historia justo cuando
    la pantalla estuvo apagada, que es cuando mas interesa saber que paso.

    `counts_source` es una corrutina que devuelve los contadores del agregado
    (o None si todavia no hay ninguno bueno). Se le pide a el la lectura para
    no tener dos caminos distintos hacia el flow: el cache del panel de tickets
    ya sabe cuando refrescar y cuando servir lo de hace un rato.
    """
    while True:
        try:
            counts = await counts_source()
            if counts:
                history.append(counts)
            else:
                logger.warning("Serie de la cola: el agregado no dio contadores")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Serie de la cola: fallo la muestra (%s)", type(exc).__name__)
        await asyncio.sleep(interval_seconds)


def start_sampler_task(
    history: ColaHistory,
    counts_source: Callable[[], "object"],
    interval_seconds: float = DEFAULT_SAMPLE_SECONDS,
):
    return asyncio.create_task(sampler_loop(history, counts_source, interval_seconds))

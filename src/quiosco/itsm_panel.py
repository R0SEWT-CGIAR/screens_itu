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
import json
import logging
import os
import time
from pathlib import Path
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
DEFAULT_MAX_ROWS = 6
# Title es Text(255) en SharePoint y el ancho util son ~44 caracteres a 22px.
MAX_SUBJECT = 44
# Corte de la semana: miercoles 07:30 Lima, que es cuando el equipo se reune a
# revisar estas metricas. Vive aqui y no en el flow a proposito: no es un hecho
# de los datos como el calendario habil, es cuando se junta la gente. Moverlo
# debe ser editar una linea de config, no un PATCH al clientdata de un flow en
# produccion.
DEFAULT_WEEK_WEEKDAY = 3           # ISO: 1=lunes ... 7=domingo
DEFAULT_WEEK_TIME = "07:30"

# Las fotos son datos estaticos de ~12 personas que cambian cuando entra o sale
# alguien, no cada 5 minutos. Se bajan una vez por Graph a 48x48 y viven en
# disco; meterlas en el agregado seria mandar 19 KB de binario en cada llamada.
# Sin manifiesto a proposito: el archivo <id>.jpg existe o no, asi no hay indice
# que se desincronice de los archivos.
PHOTO_DIR = Path("static/photos")
PHOTO_URL = "/static/photos/{}.jpg"

LIMA = timezone(timedelta(hours=-5))
# strftime("%a") usa el locale del proceso, que en el contenedor es C: escribia
# "Wed" en un panel en español. Se traduce a mano en vez de depender de que la
# imagen tenga el locale es_PE generado.
DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


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
        "week_weekday": int(_num("week_weekday", DEFAULT_WEEK_WEEKDAY, 1)),
        "week_time": str(raw.get("week_time") or DEFAULT_WEEK_TIME),
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


def _reloj(hours):
    """Un reloj de la fila, o None si ese deadline no aplica."""
    if hours is None:
        return None
    return {"remaining": remaining_text(hours), "band": band_for(hours)}


def band_for(hours: float) -> str:
    """Banda de color. Los cuatro colores de estado se reservan para lo urgente."""
    if hours < 1:
        return "caida"
    if hours < 4:
        return "leve"
    return "neutro"


_aviso_fotos = False


def available_photos(directory: Path = PHOTO_DIR) -> frozenset[str]:
    """Ids de asignatario que tienen foto en disco.

    La cobertura no es total y no es un caso de borde: hoy 12 de 13 personas con
    cuenta activa. Las iniciales son camino principal para el resto.

    Si NO hay ninguna foto se avisa una vez. El motivo es que este fallo se
    degrada a algo que parece una decision de diseño: sin el volumen montado
    —que es justo lo que paso— todas las filas caen a iniciales y no falla nada
    visible. Los fallos que gritan se arreglan solos; los que se ven normales
    necesitan que alguien vaya a mirar.
    """
    global _aviso_fotos
    try:
        ids = frozenset(p.stem for p in directory.glob("*.jpg"))
    except OSError:
        ids = frozenset()
    if not ids and not _aviso_fotos:
        _aviso_fotos = True
        logger.warning(
            "Panel ITSM: 0 fotos en %s (existe=%s). Todas las filas van a caer a "
            "iniciales; si no es lo esperado, revisar el volumen static/photos.",
            directory, directory.is_dir(),
        )
    return ids


def week_start(now: datetime, weekday: int = DEFAULT_WEEK_WEEKDAY,
               hhmm: str = DEFAULT_WEEK_TIME) -> datetime:
    """Inicio de la semana EN CURSO: el ultimo corte <= ahora, en hora de Lima.

    Nunca uno futuro: un "cumplimiento de la semana" necesita un inicio en el
    pasado que contar. Si hoy es el dia del corte pero todavia no es la hora, la
    semana en curso empezo hace siete dias.
    """
    local = now.astimezone(LIMA)
    try:
        hora, minuto = (int(x) for x in hhmm.split(":", 1))
    except (ValueError, AttributeError):
        hora, minuto = 7, 30
    weekday = min(7, max(1, weekday))

    corte = local.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    corte -= timedelta(days=(local.isoweekday() - weekday) % 7)
    if corte > local:
        corte -= timedelta(days=7)
    return corte


def display_name(raw: str) -> str:
    """El nombre como se lee en una pared, no como lo guarda SharePoint.

    Llega "Rodriguez, Saul  (CIP)": doble espacio, apellido primero y sufijo de
    organizacion que no aporta nada en una pantalla interna. Se voltea solo si
    hay UNA coma, que es lo que hace seguro el caso peruano de dos apellidos
    ("Garcia Perez, Juan Carlos" -> "Juan Carlos Garcia Perez").
    """
    limpio = " ".join(str(raw or "").split())
    # Sufijos de organizacion entre parentesis al final: "(CIP)", "(CGIAR)".
    while limpio.endswith(")") and "(" in limpio:
        limpio = limpio[:limpio.rindex("(")].rstrip()
    if limpio.count(",") == 1:
        apellido, nombre = (p.strip() for p in limpio.split(","))
        if apellido and nombre:
            return f"{nombre} {apellido}"
    return limpio


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
    week_weekday: int = DEFAULT_WEEK_WEEKDAY,
    week_time: str = DEFAULT_WEEK_TIME,
    photo_ids: frozenset[str] = frozenset(),
) -> dict:
    """Modelo de vista a partir del agregado que publica el flow.

    Contrato esperado (lo define quiosco, lo cumple el flow):

        {
          "generated_at": "2026-08-31T16:11:00Z",
          # El flow todavia emite "week_start", pero se IGNORA: lo calcula
          # week_start() desde la config de este lado.
          # assignee NO trae foto: esas viven en disco (ver PHOTO_DIR).
          "at_risk": [ {"id": 65036, "priority": 5,
                        "subject": "...", "requester": "...",
                        "due_ttf": "2026-08-28T16:15:00Z",
                        # due_ttr null significa dos cosas distintas y por eso
                        # viene acompañado: con ttr_responded true ya respondio
                        # un tecnico (se pinta —), con false no hay deadline
                        # conocido (se pinta ? en tinta de aviso). Pintar lo
                        # mismo para ambos escondia un fallo detras de un estado
                        # normal.
                        "due_ttr": None, "ttr_responded": True,
                        "assignee": {"id": 6728, "name": "..."}} ],
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
    sin_fecha = 0
    for t in crudas:
        if not isinstance(t, dict):
            sin_fecha += 1
            continue

        relojes = {}
        for clave, campo in (("ttf", "due_ttf"), ("ttr", "due_ttr")):
            vence = _parse(t.get(campo))
            if vence is not None:
                relojes[clave] = (vence - now).total_seconds() / 3600

        # Sin ningun deadline conocido no hay nada contra que contar.
        if not relojes:
            sin_fecha += 1
            continue
        # Un ticket con CUALQUIER deadline ya vencido sale de la lista: el panel
        # es de los que todavia se pueden salvar, y los vencidos van en los
        # contadores del equipo, sin desglose por persona.
        if any(h < 0 for h in relojes.values()):
            continue
        proximo = min(relojes.values())
        if proximo > horizon_hours:      # el tope es red de seguridad, no criterio
            continue

        quien = t.get("assignee") or {}
        nombre = display_name(quien.get("name"))
        pid = str(quien.get("id") or "")
        asunto = " ".join(str(t.get("subject") or "").split())
        if len(asunto) > MAX_SUBJECT:
            asunto = asunto[:MAX_SUBJECT - 1].rstrip() + "…"

        filas.append({
            "id": t.get("id"),
            "hours": proximo,
            "band": band_for(proximo),
            "priority": t.get("priority"),
            "subject": asunto,
            # Un ticket sin asunto deja la fila con el numero y nada mas, que se
            # lee como render roto. Se marca para pintarlo como ausencia.
            "subject_missing": not asunto,
            "requester": display_name(t.get("requester")),
            "ttf": _reloj(relojes.get("ttf")),
            "ttr": _reloj(relojes.get("ttr")),
            # Sin due_ttr, el motivo importa: respondido es normal, sin deadline
            # es una rareza que tiene que verse como tal.
            "ttr_state": ("" if "ttr" in relojes
                          else ("respondido" if t.get("ttr_responded") else "desconocido")),
            "name": nombre if show_people else "",
            "initials": initials(nombre) if (show_people and nombre) else "",
            "photo": PHOTO_URL.format(pid) if (show_people and pid in photo_ids) else "",
            "unassigned": not pid,
        })

    # Que el agregado traiga filas y NINGUNA tenga fecha legible no es un estado
    # de negocio, es un desajuste de contrato — y se disfraza del estado mas
    # tranquilizador que existe: "nada por brechearse". Paso de verdad cuando el
    # flow empezo a mandar due_ttf/due_ttr y el panel todavia leia due.
    contrato_roto = bool(crudas) and sin_fecha == len(crudas)
    if contrato_roto:
        logger.error(
            "Panel ITSM: el agregado trae %d filas y ninguna con fecha legible. "
            "Probable desajuste de contrato con el flow; el panel NO va a decir "
            "que no hay nada en riesgo.", len(crudas),
        )

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
    if contrato_roto:
        severity = "leve"
    elif urgentes:
        severity = "caida"
    elif n:
        severity = "leve"
    else:
        severity = "ok"

    # week_start se calcula aqui, no se lee del payload: el flow lo sigue
    # emitiendo pero no lo consume nadie, asi que es peso muerto hasta la proxima
    # edicion del clientdata por un motivo real.
    semana = week_start(now, week_weekday, week_time)
    return {
        "clock": now.astimezone(LIMA).strftime("%H:%M"),
        "week_label": (f"semana desde {DIAS[semana.weekday()]} {semana.day} "
                       f"{semana:%H:%M}"),
        "severity": severity,
        "problem": severity != "ok",
        "broken_contract": contrato_roto,
        # Cuantas filas llegaron sin una sola fecha legible. Con el
        # contrato roto es el dato que hace concreto el aviso.
        "unreadable_rows": sin_fecha,
        "headline": (
            "Datos ilegibles" if contrato_roto
            else (f"{n} por brechearse" if n else "Nada por brechearse")
        ),
        "subline": ("el agregado no trae fechas que el panel entienda"
                    if contrato_roto else " · ".join(filter(None, [
            f"el más urgente en {remaining_text(mostradas[0]['hours'])}" if mostradas
            else "todos con holgura",
            (lambda n: f"{n} sin asignar" if n else "")(
                sum(1 for f in mostradas if f["unassigned"])),
        ]))),
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
                    # file:// sirve un agregado guardado en disco. Es para
                    # iterar el diseño en la laptop sin llevarse la URL con SAS
                    # del flow, y ademas da un dataset estable: los datos no se
                    # mueven bajo los pies mientras se ajusta el layout.
                    if settings["flow_url"].startswith("file://"):
                        payload = json.loads(
                            Path(settings["flow_url"][7:]).read_text()
                        )
                    else:
                        # GET, no POST: el trigger del flow es de lectura. Lo
                        # fija un test porque cambiarlo por analogia con otros
                        # flows del environment (CreateQaTicketFromApi si es
                        # POST) rompe en silencio con un 4xx que se veria como
                        # "fuente caida".
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
                week_weekday=settings["week_weekday"],
                week_time=settings["week_time"],
                photo_ids=available_photos(),
            )
            edad = int(age or 0)
            view["age_seconds"] = edad
            view["stale"] = edad > self.ttl_seconds * 2
            return view


# --- Render ---

_STYLE = """
  :root {
    /* Paleta del reporte ITSM360, que es el que el equipo ya mira cada semana.
       Ademas de ser la que pidieron, separa mejor: teal contra salmon mide
       dE 11.6 en deuteranopia, contra los 4.1 del verde/rojo. Casi 3x. */
    --ok:#01b8aa; --caida:#fd625e; --leve:#f2c80f; --neutro:#5a5a56;
  }
  .hero { border-left-color:var(--ok); }
  .hero.leve { border-left-color:var(--leve); background:#221f10; }
  .hero.leve .hero-icono { color:var(--leve); }

  table { flex:0 1 auto; }
  td { padding:8px 8px; }
  .fila { border-top:1px solid var(--linea); }
  .fila.caida { background:#2a1618; }
  .fila.caida td:first-child { box-shadow:inset 4px 0 0 var(--caida); }

  .relojes { width:196px; padding-left:14px; white-space:nowrap; }
  .chip { display:inline-block; font-size:19px; font-weight:650; padding:4px 9px;
          border-radius:6px; background:#232320; color:var(--tinta-2);
          font-variant-numeric:tabular-nums; margin-right:7px; }
  .chip b { font-size:11px; font-weight:700; color:var(--tinta-3); display:block;
            letter-spacing:.08em; margin-bottom:-1px; }
  .chip.caida { background:#3d1c1e; color:var(--caida); }
  .chip.caida b { color:var(--caida); opacity:.75; }
  .chip.leve { background:#332d12; color:var(--leve); }
  .chip.leve b { color:var(--leve); opacity:.75; }
  .chip.nula { color:var(--tinta-3); }
  /* Sin deadline conocido no es lo mismo que ya respondido: se pinta en tinta
     de aviso para que se lea como rareza y no como estado normal. */
  .chip.raro { color:var(--leve); }

  .tk { font-size:16px; font-weight:600; color:var(--tinta-3);
        font-variant-numeric:tabular-nums; margin-right:11px; }
  .asunto { font-size:22px; font-weight:550; letter-spacing:-.01em; }
  .asunto.falta { color:var(--tinta-3); font-weight:450; font-style:italic; }
  .sol { display:block; font-size:14px; color:var(--tinta-3); margin-top:3px; }

  .quien { width:250px; text-align:right; white-space:nowrap; }
  .av { display:inline-flex; align-items:center; justify-content:center; width:36px; height:36px;
        border-radius:50%; font-size:14px; font-weight:650; color:#fff;
        background:#3a3a37; vertical-align:middle; object-fit:cover; }
  .av.sin { background:transparent; border:2px dashed var(--linea); color:var(--tinta-3); }
  .nom { font-size:17px; color:var(--tinta-2); margin-left:11px; vertical-align:middle; }
  .nom.sin { color:var(--tinta-3); }

  /* El sobrante cae SIEMPRE contra el pie. Con tres o cuatro filas —el caso
     comun, y el que mas se mira— un hueco entre la ultima fila y los
     contadores se lee como que la pagina no termino de cargar; el mismo hueco
     abajo se lee como que sobra sitio. Es la regla que ya seguia el estado
     vacio, ahora para todos los estados. */
  .contexto { display:flex; gap:11px; padding-top:11px; }
  body footer { margin-top:auto; }
  .cx { flex:1; background:var(--superficie); border-radius:9px; padding:9px 14px;
        border-top:3px solid var(--neutro); }
  .cx.mal { border-top-color:var(--caida); }
  .cx.cero { border-top-color:var(--neutro); }
  .cx-n { font-size:25px; font-weight:650; letter-spacing:-.02em; }
  .cx.cero .cx-n { color:var(--tinta-3); }
  .cx-t { font-size:13px; color:var(--tinta-3); margin-top:1px; }
  .mas { font-size:14px; color:var(--tinta-3); padding:8px 0 0 14px; }
  .limpio { text-align:center; color:var(--ok); font-size:23px; font-weight:550; padding:26px 0 6px; }
  .limpio.roto { color:var(--leve); }
  .limpio small { display:block; font-size:15px; color:var(--tinta-3); font-weight:400; margin-top:5px; }
  /* Con la lista vacia el espacio sobrante se lo quedan los contadores, que
     pasan a ser el contenido en vez de un pie de pagina. */
  body.sin-filas .contexto { gap:14px; margin-top:20px; }
  body.sin-filas .cx { padding:22px 20px; }
  body.sin-filas .cx-n { font-size:44px; }
  body.sin-filas .cx-t { font-size:15px; margin-top:3px; }
"""

_SCRIPT = """
function chip(tipo, reloj, estado) {
  if (reloj) {
    return `<span class="chip ${reloj.band}"><b>${tipo}</b>${escapar(reloj.remaining)}</span>`;
  }
  // "—" es ya respondido; "?" es no sabemos. Pintar lo mismo para ambos
  // escondia un fallo detras de un estado normal.
  const raro = estado === 'desconocido';
  return `<span class="chip ${raro ? 'raro' : 'nula'}"><b>${tipo}</b>${raro ? '?' : '\\u2014'}</span>`;
}

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
  document.body.classList.toggle('sin-filas', !v.rows.length);
  if (!v.rows.length) {
    // El titular ya avisa que los datos son ilegibles; el cuerpo decia igual
    // "Ningún ticket a punto de brechear", en el teal de todo bien. Media
    // pantalla desmentia a la otra media, y la mitad tranquilizadora era la
    // que mas saltaba a la vista.
    cuerpo.innerHTML = v.broken_contract
      ? `<tr><td class="limpio roto">${v.unreadable_rows} filas sin una fecha legible
        <small>el panel no puede decir qué está en riesgo; los contadores vienen del mismo agregado</small></td></tr>`
      : `<tr><td class="limpio">Ningún ticket a punto de brechear
        <small>lo que sigue es el estado de la cola</small></td></tr>`;
  } else {
    cuerpo.innerHTML = v.rows.map(r => {
      let quien = `<span class="av sin">\\u2014</span><span class="nom sin">sin asignar</span>`;
      if (!r.unassigned && r.name) {
        const av = r.photo ? `<img class="av" src="${r.photo}" alt="">`
                           : `<span class="av">${escapar(r.initials)}</span>`;
        quien = `${av}<span class="nom">${escapar(r.name)}</span>`;
      }
      const sol = r.requester ? `<span class="sol">solicita ${escapar(r.requester)}</span>` : '';
      const asunto = r.subject_missing
        ? `<span class="asunto falta">sin asunto</span>`
        : `<span class="asunto">${escapar(r.subject)}</span>`;
      return `<tr class="fila ${r.band}">
        <td class="relojes">${chip('TTF', r.ttf, '')}${chip('TTR', r.ttr, r.ttr_state)}</td>
        <td class="que"><span class="tk">#${escapar(String(r.id))}</span>
            ${asunto}${sol}</td>
        <td class="quien">${quien}</td>
      </tr>`;
    }).join('');
  }

  document.getElementById('mas').textContent =
    v.omitted ? `y ${v.omitted} más que no caben en pantalla` : '';

  const C = [
    ['mal', v.counts.breached_ttf, 'vencidos en TTF'],
    ['mal', v.counts.breached_ttr, 'vencidos en TTR'],
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

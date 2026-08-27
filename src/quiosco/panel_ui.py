"""Chrome compartido de los paneles que sirve el quiosco.

Todos los paneles (/panel/uptime, /panel/prtg) son la misma pieza: una pagina
a pantalla completa que se castea a un Chromecast de 1280x720 y se lee de
lejos. Comparten tokens de color, encabezado, hero, filas y pie; lo unico
propio de cada uno es el cuerpo.

Reglas que valen para todos, de la guia dataviz:

- Paleta de estado fija, nunca tematizada, y NUNCA color solo. good (#0ca30c) y
  critical (#d03b3b) miden dE 4.1 en deuteranopia: un daltonico no los
  distingue. Cada estado va con etiqueta de texto, y el marcador cambia de forma
  ademas de color.
- Sin capa de hover ni tooltips: el destino es un Chromecast, no hay puntero.
  Todo valor tiene que estar visible sin interaccion.
- Marcas finas, rejilla discreta, sin bordes alrededor de las marcas.
"""

import html
import json

# Paleta de estado (fija) y tinta, sobre la superficie oscura #1a1a19 documentada
# en la guia. Los cuatro estados superan 3:1 de contraste contra esa superficie.
TOKENS = """
  :root {
    --superficie:#1a1a19; --plano:#0d0d0d;
    --tinta:#ffffff; --tinta-2:#c3c2b7; --tinta-3:#898781; --linea:#2c2c2a;
    --ok:#0ca30c; --leve:#fab219; --grave:#ec835a; --caida:#d03b3b;
    --neutro:#6b6a66;
  }
"""

BASE = """
  * { margin:0; padding:0; box-sizing:border-box; }
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
  .hero.leve  { border-left-color:var(--leve); background:#221d10; }
  .hero-icono { font-size:25px; line-height:1; color:var(--ok); }
  .hero.caida .hero-icono { color:var(--caida); }
  .hero.leve .hero-icono { color:var(--leve); }
  .hero-txt { font-size:33px; font-weight:650; letter-spacing:-.02em; }
  .hero-sub { margin-left:auto; font-size:16px; color:var(--tinta-3); text-align:right; }

  table { width:100%; border-collapse:collapse; }
  td, th { padding:0 8px; vertical-align:middle; }
  .eje th { font-size:12px; font-weight:400; color:var(--tinta-3); padding-bottom:5px; }

  .fila { border-top:1px solid var(--linea); }
  .fila.caida { background:#241715; }
  .fila.caida td:first-child { box-shadow:inset 4px 0 0 var(--caida); }

  .estado { white-space:nowrap; padding-left:12px; }
  .punto {
    display:inline-block; width:11px; height:11px; border-radius:50%;
    background:var(--ok); margin-right:9px; vertical-align:middle;
  }
  /* La FORMA distingue los estados, no solo el color: rombo para lo caido,
     cuadrado para lo que avisa, circulo para lo sano. Se lee en escala de
     grises y con deuteranopia. */
  .fila.caida .punto { background:var(--caida); border-radius:2px; transform:rotate(45deg); }
  .fila.leve .punto  { background:var(--leve); border-radius:2px; }
  .fila.grave .punto { background:var(--grave); border-radius:2px; transform:rotate(45deg); }
  .fila.neutro .punto { background:var(--neutro); }
  .etiqueta { font-size:16px; color:var(--tinta-2); vertical-align:middle; }
  .fila.caida .etiqueta { color:var(--caida); font-weight:650; }
  .fila.leve .etiqueta  { color:var(--leve); }
  .fila.grave .etiqueta { color:var(--grave); }

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


def page(*, title: str, style: str, body: str, view: dict, script: str,
         refresh_seconds: float) -> str:
    """Documento completo con la vista inicial embebida.

    El render vive solo en JS y corre tambien en la primera pintura, sobre el
    JSON embebido: si el servidor tambien supiera pintar filas habria dos
    plantillas que mantener sincronizadas. Asi la pagina aparece completa sin
    esperar al primer fetch, y un fallo de red posterior no la deja en blanco.
    """
    datos = html.escape(json.dumps(view, ensure_ascii=False), quote=False)
    script = script.replace("REFRESH_MS", str(int(refresh_seconds * 1000)))
    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{TOKENS}{BASE}{style}</style>
</head>
<body>
{body}
<script type="application/json" id="datos">{datos}</script>
<script>{script}</script>
</body></html>"""


def error_page(title: str, message: str) -> str:
    """Pagina de ultimo recurso: no hay ni una vista buena en cache.

    Explica en pantalla en vez de devolver un 500, que en un Chromecast se veria
    como una pantalla en blanco indistinguible de un cuelgue."""
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{TOKENS}{BASE}
  .vacio {{ margin:auto; text-align:center; color:var(--tinta-3); max-width:70%; }}
  .vacio strong {{ display:block; font-size:28px; color:var(--tinta-2); margin-bottom:8px; font-weight:600; }}
</style></head>
<body>
  <div class="vacio">
    <strong>{html.escape(title)} no disponible</strong>
    {html.escape(message)}
  </div>
</body></html>"""


# Fragmentos de JS que comparten los paneles. Se concatenan al script propio.
SCRIPT_COMUN = """
function escapar(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function pintarReloj(v) {
  document.getElementById('reloj').innerHTML = v.stale
    ? `<span class="stale">datos de hace ${Math.round(v.age_seconds / 60)} min</span>`
    : `actualizado ${v.clock}`;
}

async function refrescar(endpoint) {
  try {
    const r = await fetch(endpoint, { cache: 'no-store' });
    if (r.ok) pintar(await r.json());
  } catch (e) {
    // Se mantiene la pintura anterior: mejor un dato de hace un rato que un
    // parpadeo o una pantalla vacia en recepcion.
  }
}
"""

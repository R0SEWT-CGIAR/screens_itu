#!/usr/bin/env bash
# Hace compatible el webview de pixel-agents con los Chromecast del quiosco.
#
# Los dos Chromecast de ITU corren Chrome 70 (CrKey/1.36.157768, armv7l, 1280x720).
# El bundle de pixel-agents usa `?.` y `??` (Chrome 80+), asi que el Chromecast
# descarga el JS y muere al parsearlo: la pantalla queda en blanco. Ademas usa
# structuredClone (Chrome 98+), que no es cuestion de sintaxis y hay que polyfillear.
#
# Este script transpila el bundle a chrome70 y le prepende el polyfill, dejando el
# original en *.js.orig. Es idempotente: siempre parte del .orig.
#
# RE-EJECUTAR DESPUES DE CADA `npm i -g pixel-agents` (el update reescribe dist/).
set -euo pipefail

PKG="${1:-/home/rody/.nvm/versions/node/v22.22.0/lib/node_modules/pixel-agents}"
ASSETS="$PKG/dist/webview/assets"

[[ -d "$ASSETS" ]] || { echo "no existe $ASSETS" >&2; exit 1; }
command -v esbuild >/dev/null || { echo "falta esbuild (npm i -g esbuild)" >&2; exit 1; }

shopt -s nullglob
patched=0
for js in "$ASSETS"/index-*.js; do
  case "$js" in *.orig) continue ;; esac
  orig="$js.orig"
  [[ -f "$orig" ]] || cp "$js" "$orig"
  tmp="$(mktemp)"
  # Por stdin + --loader=js: el backup se llama .orig y esbuild elige loader
  # por extension, que ahi no le dice nada.
  esbuild --loader=js --target=chrome70 --format=esm --legal-comments=none < "$orig" > "$tmp"
  {
    cat <<'POLY'
/* polyfill para el Chrome 70 del Chromecast: structuredClone es Chrome 98+ */
if (typeof structuredClone !== "function") {
  self.structuredClone = function sc(v, seen) {
    seen = seen || new Map();
    if (v === null || typeof v !== "object") return v;
    if (seen.has(v)) return seen.get(v);
    if (v instanceof Date) return new Date(v.getTime());
    if (v instanceof RegExp) return new RegExp(v.source, v.flags);
    var out;
    if (Array.isArray(v)) {
      out = []; seen.set(v, out);
      for (var i = 0; i < v.length; i++) out[i] = sc(v[i], seen);
      return out;
    }
    if (v instanceof Map) {
      out = new Map(); seen.set(v, out);
      v.forEach(function (val, k) { out.set(sc(k, seen), sc(val, seen)); });
      return out;
    }
    if (v instanceof Set) {
      out = new Set(); seen.set(v, out);
      v.forEach(function (val) { out.add(sc(val, seen)); });
      return out;
    }
    out = {}; seen.set(v, out);
    for (var k in v) if (Object.prototype.hasOwnProperty.call(v, k)) out[k] = sc(v[k], seen);
    return out;
  };
}
POLY
    cat "$tmp"
  } > "$js"
  rm -f "$tmp"
  echo "parcheado $(basename "$js"): $(wc -c < "$orig") -> $(wc -c < "$js") bytes"
  patched=$((patched + 1))
done

[[ "$patched" -gt 0 ]] || { echo "no encontre index-*.js en $ASSETS" >&2; exit 1; }
echo "listo. reiniciar el server no es necesario (son archivos estaticos),"
echo "pero el Chromecast recarga fresco en cada slot de la rotacion."

# ADR-003: Enfoque hibrido para URLs internas vs externas

## Estado
Aceptado

## Contexto
Tenemos dos tipos de URLs:
- **Internas (172.25.0.22):** Cert SSL invalido, sin Cloudflare, sin X-Frame-Options
- **Externas (cgiar.org, cipotato.org):** Cert valido, protegidas por Cloudflare, con X-Frame-Options: SAMEORIGIN

Inicialmente se intento un proxy universal para todas las URLs, pero las externas con Cloudflare devuelven 403 (challenge JS que requiere browser real).

## Alternativas consideradas

1. **Proxy universal:** Proxear todo. Falla con Cloudflare (403 Forbidden, "Just a moment..." challenge).
2. **Todo directo con DashCast force=True:** Funciona para externas, falla para internas (cert invalido).
3. **Hibrido:** Internas via wrapper+proxy, externas directo con DashCast.

## Decision
Enfoque hibrido:
- **URLs internas (172.25.0.22):** `_proxy_url()` las convierte a `http://servidor:8000/cast/view?url=...`. DashCast las carga con `force=False` (nuestra wrapper page no bloquea iframes). La wrapper page tiene viewport 1920x1080 y un iframe que carga via `/proxy/all`.
- **URLs externas:** Se envian directo al Chromecast. DashCast las carga con `force=True` (bypass X-Frame-Options). Sin control de viewport.

## Consecuencias
- (+) Ambos tipos de URL funcionan
- (+) URLs internas tienen viewport controlado (1920x1080)
- (-) URLs externas no tienen control de viewport — dependen del browser del Chromecast
- (-) Logica condicional en `TimedDashCastController.load_url()` decide `force` segun si la URL es wrapper o no
- (-) Si cipotato.org o cgiar.org cambian su proteccion Cloudflare, podrian dejar de funcionar

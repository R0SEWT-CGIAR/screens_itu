# ADR-003: Enfoque hibrido para URLs internas vs externas

## Estado
Aceptado (actualizado)

## Contexto
El sistema debe mostrar en Chromecast tres escenarios reales:

- **Internas PRTG (172.25.0.22):** certificado SSL invalido, requieren proxy local
- **Externas proxyables:** pueden pasar por proxy sin romper contenido
- **Externas no proxyables (Cloudflare/challenge):** no se pueden proxear de forma confiable

Adicionalmente, se requiere evitar recargas continuas de DashCast para no mostrar pantallas de carga en cada rotacion.

## Alternativas consideradas

1. **Proxy universal para todo**
	 - Ventaja: una sola ruta de render
	 - Problema: sitios con Cloudflare devuelven 403/challenge y no cargan bien

2. **Cast directo URL por URL con DashCast**
	 - Ventaja: flujo simple
	 - Problema: rotacion con recargas de receiver, peor experiencia visual y sin control uniforme de layout

3. **Enfoque hibrido con display page unica (decision actual)**
	 - Una display page concentra iframes y screenshots
	 - La rotacion solo cambia indice visible

## Decision
Se adopta un enfoque hibrido centrado en una **display page unica** cargada una sola vez por Chromecast.

### Reglas de enrutamiento

- **Internas PRTG (`172.25.0.22`)**
	- Render en `iframe`
	- Ruta: `/proxy/{path}`
	- Proxy con bypass SSL, reescritura HTML/CSS e interceptor JS (ver ADR-002)

- **Externas proxyables**
	- Render en `iframe`
	- Ruta: `/p/{origin_encoded}/{path}`

- **Externas no proxyables**
	- Render como `img` con GIF periodico
	- Ruta de asset: `/static/screenshots/{asset}.gif`
	- Seleccion segun `SCREENSHOT_SITES`

### Rotacion

- DashCast carga `GET /cast/display?cc_id=<id>` una sola vez
- La display page consulta `GET /api/current/<id>` cada 2s
- `CastManager._rotation_loop()` solo incrementa `current_index`
- No se llama `load_url()` en cada salto de rotacion

## Consecuencias

- (+) Se soportan internas, externas proxyables y externas no proxyables en un solo flujo
- (+) La rotacion es estable y sin recarga de receiver en cada cambio
- (+) Se mantiene control de layout por resolucion/zoom dentro de la display page
- (-) `SCREENSHOT_SITES` es una lista en codigo y requiere reinicio al cambiarla
- (-) Las URLs en screenshot no son tiempo real continuo; dependen del ciclo de captura
- (-) El sistema depende de que `PROXY_BASE` sea accesible desde la red del Chromecast

## Notas de implementacion

- El flujo legacy basado en wrapper `cast/view` y proxy universal `proxy/all` ya no es la implementacion vigente.
- La implementacion actual se apoya en:
	- `src/quiosco/main.py`: `/cast/display`, `/api/current/{id}`, `/proxy/{path}`, `/p/{origin}/{path}`
	- `src/quiosco/cast_manager.py`: `launch_display()`, `_rotation_loop()`, watchdog y recuperacion

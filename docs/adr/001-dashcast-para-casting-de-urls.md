# ADR-001: DashCast para casting de URLs arbitrarias

## Estado
Aceptado

## Contexto
Necesitamos mostrar paginas web arbitrarias (dashboards PRTG, landing pages) en Chromecasts. El Chromecast no tiene browser nativo accesible — solo puede ejecutar "receiver apps" registradas en Google.

## Alternativas consideradas

1. **Custom receiver app:** Registrar una app propia en Google Cast Developer Console. Requiere cuenta de desarrollador ($5), proceso de registro, y mantener un receiver HTML hosteado.
2. **DashCast:** App publica existente (ID 84912283) que acepta cualquier URL y la renderiza en el Chromecast. pychromecast incluye `DashCastController` con soporte nativo.
3. **Screen mirroring:** Castear pantalla completa desde el servidor. Requiere Chrome corriendo con interfaz grafica.

## Decision
Usar DashCast via `pychromecast.controllers.dashcast.DashCastController`.

## Consecuencias
- (+) Sin necesidad de registrar app en Google ni mantener receiver
- (+) API simple: `load_url(url, force=True/False)`
- (-) Con `force=False` usa iframe — sitios con `X-Frame-Options: SAMEORIGIN` no cargan
- (-) Con `force=True` reemplaza el receiver, requiere `force_launch=True` para cambiar URL
- (-) El browser del Chromecast es limitado: JS pesado (Revolution Slider) puede no funcionar

## Notas
- `force=True` se usa para URLs externas (bypass X-Frame-Options)
- `force=False` se usa para URLs internas que pasan por nuestra wrapper page (sin restriccion iframe)
- `force_launch=True` siempre activo para garantizar que DashCast se relance tras force=True

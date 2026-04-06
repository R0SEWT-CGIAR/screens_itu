# ADR-002: Proxy reverso para URLs internas con SSL invalido

## Estado
Aceptado (actualizado)

## Contexto
El servidor PRTG (172.25.0.22) usa certificado SSL auto-firmado. El browser del Chromecast no puede aceptar certs invalidos (no hay forma de hacer click en "continuar de todas formas"). Las paginas cargan en blanco.

## Alternativas consideradas

1. **Instalar cert valido en PRTG:** Requiere acceso admin al servidor PRTG y un certificado (Let's Encrypt o CA interna). No siempre posible.
2. **Proxy reverso en nuestra app:** FastAPI proxea las requests a PRTG sin verificar SSL (`verify=False`). El Chromecast accede a nuestro servidor por HTTP plano.
3. **nginx como reverse proxy:** Mas robusto, pero agrega una dependencia y complejidad de deployment.

## Decision
Proxy reverso integrado en FastAPI usando httpx con `verify=False`.

## Implementacion

### Proxy path-based
`/proxy/{path}?query` → `https://172.25.0.22/{path}?query`

Soporta GET, POST y PUT (PRTG usa POST para algunas APIs).

### Reescritura de URLs en HTML (critico)
PRTG usa rutas absolutas para recursos (`/css/prtg0.css`, `/javascript/lib/jquery.js`, `/images/refresh.png`). Sin reescritura, estas rutas apuntan a nuestro servidor y dan 404.

La reescritura en el proxy transforma:
- `href="/css/..."` → `href="/proxy/css/..."`
- `src="/javascript/..."` → `src="/proxy/javascript/..."`
- `src="/images/..."` → `src="/proxy/images/..."`

Las rutas relativas (como `mapshow_simple.htm` dentro de `mapshow.htm`) NO se tocan — resuelven correctamente relativo a la URL del documento (`/proxy/public/mapshow.htm` → `/proxy/public/mapshow_simple.htm`).

### Interceptor JS para fetch/XHR (critico)
La reescritura HTML solo cubre atributos en tags. El JavaScript de PRTG tambien hace requests en runtime con rutas absolutas (`fetch("/api/...")`, `$.ajax("/api/...")`). Para cubrirlos, se inyecta un script que intercepta `window.fetch()` y `XMLHttpRequest.prototype.open()` y reescribe rutas absolutas (`/path` → `/proxy/path`).

### Enfoques descartados que NO funcionaron
1. **`<base href="/proxy/">`** — Solo afecta URLs relativas en atributos HTML. No afecta rutas absolutas (`/css/...`) ni requests JS. PRTG usa mayoritariamente rutas absolutas.
2. **`<base href="/proxy/public/">`** — Mismo problema. Ademas rompia rutas absolutas de otros directorios (`/css/`, `/javascript/`).
3. **Proxy universal (`/proxy/all?url=...`)** — No permite que rutas relativas resuelvan naturalmente. Requiere reescritura completa de todas las URLs en HTML/CSS/JS.

## Consecuencias
- (+) PRTG carga completamente: HTML, CSS, JS, imagenes, APIs de refresh
- (+) Los mapas PRTG mantienen auto-refresh funcional (las APIs pasan por el proxy)
- (+) Las rutas relativas resuelven automaticamente (sin reescritura)
- (-) Agrega latencia (doble hop: Chromecast → servidor → PRTG)
- (-) Solo funciona si el servidor puede alcanzar 172.25.0.22 (misma red)
- (-) Si PRTG construye URLs con `window.location.origin` en vez de rutas relativas/absolutas, esas requests no se interceptan

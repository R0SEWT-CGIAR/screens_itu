# ADR-002: Proxy reverso para URLs internas con SSL invalido

## Estado
Aceptado

## Contexto
El servidor PRTG (172.25.0.22) usa certificado SSL auto-firmado. El browser del Chromecast no puede aceptar certs invalidos (no hay forma de hacer click en "continuar de todas formas"). Las paginas cargan en blanco.

## Alternativas consideradas

1. **Instalar cert valido en PRTG:** Requiere acceso admin al servidor PRTG y un certificado (Let's Encrypt o CA interna). No siempre posible.
2. **Proxy reverso en nuestra app:** FastAPI proxea las requests a PRTG sin verificar SSL (`verify=False`). El Chromecast accede a nuestro servidor por HTTP plano.
3. **nginx como reverse proxy:** Mas robusto, pero agrega una dependencia y complejidad de deployment.

## Decision
Proxy reverso integrado en FastAPI usando httpx con `verify=False`.

## Implementacion
- `/proxy/all?url=<url>` — Proxy universal, acepta cualquier URL
- `/proxy/{path}` — Proxy path-based para 172.25.0.22, las rutas relativas resuelven automaticamente
- HTML: reescribe URLs absolutas (`href="/..."`, `src="/..."`) para pasar por el proxy
- CSS/JS: reescribe URLs absolutas al mismo host
- Otros recursos (imagenes, fonts): pass-through sin modificacion

## Consecuencias
- (+) El Chromecast accede a contenido interno sin problemas de SSL
- (+) Las rutas relativas en el HTML resuelven correctamente via `/proxy/{path}`
- (-) Agrega latencia (doble hop: Chromecast → servidor → PRTG)
- (-) La reescritura de URLs es fragil — URLs generadas por JS en runtime no se reescriben
- (-) Solo funciona si el servidor puede alcanzar 172.25.0.22 (misma red)

# Manual operativo de Quiosco

Guia de operacion para personal TI y soporte del Centro Internacional de la Papa.

Este manual complementa el README con procedimientos de turno, verificacion operativa y atencion de incidentes frecuentes.

## 1. Alcance

Incluye:

- Arranque y parada del servicio
- Verificacion de estado de Chromecasts y paginas
- Respuesta inicial ante incidentes operativos
- Criterios de escalamiento tecnico

No incluye:

- Cambios de codigo
- Reingenieria de arquitectura

## 2. Requisitos operativos

### 2.1 Requisitos tecnicos

- Docker y Docker Compose instalados en el servidor
- Archivo `config.json` actualizado con Chromecasts y links vigentes
- Variable `PROXY_BASE` definida con IP/host alcanzable desde Chromecasts
- Acceso de red a:
  - Chromecasts (TCP 8009)
  - PRTG interno (`172.25.0.22:443`) para paneles internos

### 2.2 Archivos de referencia

- `config.json`
- `docker-compose.yml`
- `scripts/start.sh`, `scripts/stop.sh`
- `scripts/start.bat`, `scripts/stop.bat`

## 3. Inicio de turno

### 3.1 Arranque del servicio

Linux:

```bash
cd /ruta/quiosco
docker compose up -d --build
```

Windows (PowerShell o CMD):

```bat
cd /d C:\ruta\quiosco
docker compose up -d --build
```

### 3.2 Verificacion inicial (obligatoria)

1. Verificar estado global:

```bash
curl http://localhost:8000/api/status
```

2. Abrir UI:

- `http://<IP_SERVIDOR>:8000`

3. Confirmar por Chromecast:

- `connected = true`
- `display_ready = true` despues del arranque
- `last_error` vacio o `null`

4. Ejecutar validacion visual:

- Boton `Debug interno` en la UI
- Validar que la secuencia avance por todas las paginas
- Revisar estados de carga: `Cargada`, `Sin respuesta`, `Error`

## 4. Operacion durante el turno

### 4.1 Acciones operativas en UI

- Iniciar rotacion por pantalla
- Detener rotacion por pantalla
- Ajustar intervalo global de rotacion
- Castear URL puntual por pantalla

### 4.2 Criterios de monitoreo continuo

Revisar periodicamente:

- Dispositivos desconectados
- `reconnect_attempts` en aumento
- Mensajes de `last_error`
- Paginas en blanco o sin actualizacion

Comando de logs recomendado:

```bash
docker compose logs -f quiosco
```

## 5. Matriz de incidentes

| Sintoma | Causa probable | Accion inmediata | Escalar cuando |
| --- | --- | --- | --- |
| Chromecast desconectado | Host/puerto incorrecto o red caida | Re-ejecutar `discover.py`, actualizar `config.json`, verificar conectividad | No reconecta tras 2 ciclos de watchdog (~30s) |
| `display_ready=false` sostenido | DashCast no activo o `PROXY_BASE` inaccesible | Verificar `PROXY_BASE`, abrir `/cast/display?cc_id=cc1`, revisar logs | Persiste luego de reinicio de servicio |
| Dashboard PRTG en blanco | Falla de acceso a `172.25.0.22` o proxy | Ejecutar `Debug interno`, validar alcance a PRTG desde servidor | PRTG responde en red pero no renderiza |
| Sitio externo no carga | Restricciones `X-Frame-Options`/Cloudflare | Verificar si cae en modo screenshot; evaluar reemplazo de URL | El sitio es critico y no existe alternativa |
| GIF de screenshot no cambia | Falla de captura Playwright o timeout | Revisar logs, comprobar Chromium instalado y recursos del host | Falla continua en varios ciclos de captura |

## 6. Cambios que requieren reinicio

Reiniciar servicio (`docker compose down` + `docker compose up -d --build`) cuando cambie alguno de estos elementos:

- `config.json`
- `PROXY_BASE`
- Lista `SCREENSHOT_SITES` en `main.py`
- Dependencias de captura (Playwright/Chromium)

## 7. Cierre de turno

1. Confirmar estado final en UI y API.
2. Si corresponde apagar servicio:

```bash
cd /ruta/quiosco
docker compose down
```

3. Registrar incidencias del turno con:

- hora
- pantalla afectada
- URL implicada
- accion aplicada
- resultado

## 8. Operacion programada

### 8.1 Linux (cron)

```cron
30 7  * * 1-5  cd /ruta/quiosco && docker compose up -d --build >> /var/log/quiosco_start.log 2>&1
30 16 * * 1-5  cd /ruta/quiosco && docker compose down >> /var/log/quiosco_stop.log 2>&1
```

### 8.2 Windows (Task Scheduler)

- 07:30: ejecutar `scripts/start.bat`
- 16:30: ejecutar `scripts/stop.bat`

## 9. Escalamiento tecnico

Escalar al equipo de desarrollo cuando:

- Hay regresion reproducible en rutas `/proxy` o `/p`
- Watchdog no recupera un dispositivo luego de reinicio del servicio
- Existen errores de rotacion con `current_index` inconsistente
- Se requiere incorporar nuevos dominios en estrategia screenshot/proxy

## 10. Referencias

- `README.md`
- `docs/uptimerobot-data-contract.md`
- `docs/copilot-studio-uptimerobot-power-automate.md`
- `docs/adr/001-dashcast-para-casting-de-urls.md`
- `docs/adr/002-proxy-reverso-para-urls-internas.md`
- `docs/adr/003-enfoque-hibrido-internas-vs-externas.md`
- `docs/adr/004-conexion-chromecast-por-ip.md`

# Contrato de datos UptimeRobot para agentes

Contrato y profiling del endpoint publico usado por la status page de UptimeRobot. Este documento define que datos consumir, que datos ignorar, como normalizarlos y que forma deberia recibir un agente de Copilot Studio.

La logica concreta de Power Automate se define despues. Este contrato es la base estable para esa implementacion.

## Fuente

Endpoint:

```text
GET https://stats.uptimerobot.com/api/getMonitorList/26r4CjSckG
```

Caracteristicas observadas el 2026-05-11:

| Metrica | Valor |
| --- | ---: |
| Content-Type | `application/json` |
| Tamano de respuesta | 118724 bytes |
| Tiempo de descarga observado | 1.63s |
| Monitores | 8 |
| Dias historicos | 90 |
| Zona horaria | `-05:00` |
| Autenticacion | No requerida |

## Perfil estructural

Claves raiz observadas:

```json
["data", "days", "psp", "statistics", "status"]
```

Uso recomendado:

| Campo | Uso |
| --- | --- |
| `status` | Validar respuesta upstream. Esperado: `ok`. |
| `data` | Fuente canonica de monitores. |
| `days` | Ventana historica disponible; 90 fechas. |
| `statistics` | Estado agregado de toda la pagina. Opcional para respuestas globales. |
| `psp` | Metadata de pagina; `psp.monitors` duplica exactamente `data`. |

Regla de contrato:

```text
Consumir data[] como fuente canonica. No consumir psp.monitors para evitar duplicidad.
```

## Contrato upstream minimo

Schema minimo que debe soportar el consumidor:

```json
{
  "type": "object",
  "required": ["status", "data"],
  "properties": {
    "status": { "type": "string" },
    "data": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["monitorId", "name", "statusClass", "type"],
        "properties": {
          "monitorId": { "type": "integer" },
          "createdAt": { "type": "string" },
          "statusClass": { "type": "string" },
          "name": { "type": "string" },
          "url": { "type": ["string", "null"] },
          "type": { "type": "string" },
          "groupId": { "type": "integer" },
          "groupName": { "type": "string" },
          "dailyRatios": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "date": { "type": "string" },
                "ratio": { "type": "string" },
                "label": { "type": "string" },
                "color": { "type": "string" }
              }
            }
          },
          "ratio": {
            "type": "object",
            "properties": {
              "ratio": { "type": "string" },
              "label": { "type": "string" },
              "color": { "type": "string" }
            }
          },
          "30dRatio": {
            "type": "object",
            "properties": {
              "ratio": { "type": "string" },
              "label": { "type": "string" },
              "color": { "type": "string" }
            }
          },
          "90dRatio": {
            "type": "object",
            "properties": {
              "ratio": { "type": "string" },
              "label": { "type": "string" },
              "color": { "type": "string" }
            }
          },
          "hasIncidentComments": { "type": "boolean" },
          "lastDowntime": {
            "type": ["object", "null"],
            "properties": {
              "date": { "type": "string" },
              "duration": { "type": "integer" },
              "reason": { "type": "string" }
            }
          }
        }
      }
    },
    "days": {
      "type": "array",
      "items": { "type": "string" }
    },
    "statistics": { "type": "object" },
    "psp": { "type": "object" }
  }
}
```

Notas de tipo:

- Los porcentajes (`ratio.ratio`, `30dRatio.ratio`, `90dRatio.ratio`, `dailyRatios[].ratio`) llegan como string decimal, no como numero.
- `lastDowntime.duration` llega como entero en segundos.
- `lastDowntime` puede ser `null`.
- `url` puede ser `null`; en el perfil actual es `null` para todos los monitores.
- Las fechas llegan como texto. `lastDowntime.date` incluye hora; `dailyRatios[].date` solo fecha.

## Nulabilidad y cardinalidad observada

| Campo | Perfil observado |
| --- | --- |
| `data[]` | 8 elementos |
| `data[].dailyRatios[]` | 90 elementos por monitor |
| `days[]` | 90 elementos, de `2026-02-11` a `2026-05-11` |
| `data[].url` | 8 nulos de 8 |
| `data[].lastDowntime` | 1 nulo de 8 |
| `data[].statusClass` | 8 `success` de 8 |
| `data[].type` | 7 `HTTP(s)`, 1 `Keyword` |
| `data[].groupName` | 8 `Monitors (default)` |
| `psp.monitors` | Igual a `data` |

## Servicios observados

| `monitorId` | `name` | `type` | `statusClass` | `ratio` | `30dRatio` | `90dRatio` | `lastDowntime` |
| ---: | --- | --- | --- | ---: | ---: | ---: | --- |
| 791317712 | Cipotato.org | HTTP(s) | success | 99.992 | 99.992 | 99.988 | 2026-04-24 13:31:15, 219s |
| 791359952 | cipotato.org deface | Keyword | success | 99.995 | 99.995 | 99.963 | 2026-04-24 13:57:33, 130s |
| 796788388 | Genebank web for request germplasm | HTTP(s) | success | 99.958 | 99.958 | 99.950 | 2026-04-24 13:56:13, 195s |
| 796788410 | Genebank website | HTTP(s) | success | 99.966 | 99.966 | 99.984 | 2026-04-24 14:21:11, 300s |
| 791317722 | KM Hub/cipweb2 | HTTP(s) | success | 100.000 | 100.000 | 99.978 | 2026-02-27 04:03:42, 425s |
| 796788405 | New CIPGADC - Distribution | HTTP(s) | success | 100.000 | 100.000 | 99.973 | 2026-02-27 04:05:02, 207s |
| 791333173 | OCS | HTTP(s) | success | 100.000 | 100.000 | 99.344 | 2026-03-23 00:44:44, 162s |
| 791317811 | SharePoint Online | HTTP(s) | success | 100.000 | 100.000 | 100.000 | null |

## Estado normalizado

El agente no deberia exponer `statusClass` directamente salvo para diagnostico. Debe normalizarlo:

| Upstream `statusClass` | `status` normalizado | `isOperational` | Mensaje base |
| --- | --- | --- | --- |
| `success` | `up` | `true` | Operativo |
| `danger` | `down` | `false` | Caido o con problemas |
| `paused` | `paused` | `false` | Monitoreo pausado |
| otro valor o vacio | `unknown` | `false` | Estado no determinado |

Los valores observados actualmente solo incluyen `success`, pero el contrato debe aceptar los demas estados para no romperse cuando cambie el estado real del servicio.

## Matching de servicio

Entrada esperada desde el agente:

```json
{
  "serviceQuery": "ocs"
}
```

Reglas:

1. Normalizar `serviceQuery` con trim y minusculas.
2. Buscar coincidencia exacta contra `lower(name)`.
3. Si no hay exacta, buscar coincidencia parcial con `contains(lower(name), serviceQuery)`.
4. Si hay cero coincidencias, devolver `matchStatus = "not_found"`.
5. Si hay una coincidencia, devolver `matchStatus = "matched"`.
6. Si hay mas de una coincidencia, devolver `matchStatus = "ambiguous"` y candidatos.

Claves canonicas actuales:

| Servicio | `matchKey` |
| --- | --- |
| Cipotato.org | `cipotato.org` |
| cipotato.org deface | `cipotato.org deface` |
| Genebank web for request germplasm | `genebank web for request germplasm` |
| Genebank website | `genebank website` |
| KM Hub/cipweb2 | `km hub/cipweb2` |
| New CIPGADC - Distribution | `new cipgadc - distribution` |
| OCS | `ocs` |
| SharePoint Online | `sharepoint online` |

Alias recomendados para conversacion:

| Alias de usuario | Resolver a |
| --- | --- |
| `sharepoint` | SharePoint Online |
| `km hub` | KM Hub/cipweb2 |
| `cipgadc` | New CIPGADC - Distribution |
| `genebank request` | Genebank web for request germplasm |
| `genebank website` | Genebank website |
| `ocs` | OCS |

Si el usuario dice solo `genebank`, el resultado debe ser ambiguo porque hay dos servicios Genebank.

## Contrato normalizado para el agente

Power Automate, o cualquier capa intermedia futura, deberia devolver un objeto compacto con esta forma conceptual. Si Copilot Studio solo acepta strings como output directo, este objeto se puede serializar como texto JSON y ademas devolver `responseText`.

Caso encontrado:

```json
{
  "source": "uptimerobot_public_status_page",
  "sourceUrl": "https://stats.uptimerobot.com/26r4CjSckG",
  "serviceQuery": "ocs",
  "matchStatus": "matched",
  "matchType": "exact",
  "matchedCount": 1,
  "service": {
    "monitorId": 791333173,
    "name": "OCS",
    "type": "HTTP(s)",
    "rawStatusClass": "success",
    "status": "up",
    "isOperational": true,
    "uptime": {
      "current": "100.000",
      "last30Days": "100.000",
      "last90Days": "99.344",
      "last90DaysLabel": "good"
    },
    "lastDowntime": {
      "date": "2026-03-23 00:44:44",
      "durationSeconds": 162,
      "reason": "Incident detected"
    }
  },
  "responseText": "OCS no esta caido. UptimeRobot lo reporta como operativo. Uptime actual: 100.000%, ultimos 30 dias: 100.000%, ultimos 90 dias: 99.344%. El ultimo downtime registrado fue el 2026-03-23 00:44:44 y duro 162 segundos."
}
```

Caso ambiguo:

```json
{
  "source": "uptimerobot_public_status_page",
  "serviceQuery": "genebank",
  "matchStatus": "ambiguous",
  "matchType": "partial",
  "matchedCount": 2,
  "candidates": [
    "Genebank web for request germplasm",
    "Genebank website"
  ],
  "responseText": "Encontre mas de un servicio Genebank. Indica si quieres revisar Genebank web for request germplasm o Genebank website."
}
```

Caso no encontrado:

```json
{
  "source": "uptimerobot_public_status_page",
  "serviceQuery": "correo",
  "matchStatus": "not_found",
  "matchType": "none",
  "matchedCount": 0,
  "candidates": [],
  "responseText": "No encontre un servicio llamado correo en UptimeRobot."
}
```

Caso error upstream:

```json
{
  "source": "uptimerobot_public_status_page",
  "serviceQuery": "ocs",
  "matchStatus": "error",
  "errorType": "upstream_unavailable",
  "responseText": "No pude consultar UptimeRobot en este momento. Intenta nuevamente o revisa la status page directamente."
}
```

## Reglas de respuesta

Para preguntas de estado puntual, la respuesta debe priorizar:

1. Estado actual normalizado.
2. Nombre exacto del servicio usado para evitar confusiones.
3. Uptime actual o 30 dias.
4. Ultimo downtime si existe.
5. Advertencia si el matching fue parcial.

No incluir el historial diario completo en la respuesta del agente. Los 90 dias de `dailyRatios` son utiles para analisis, pero son demasiado verbosos para una conversacion normal.

## Profiling reproducible

Comandos usados para perfilar:

```bash
curl -fsSL -o /tmp/uptimerobot-profile.json \
  -w 'size_download=%{size_download}\ntime_total=%{time_total}\ncontent_type=%{content_type}\n' \
  https://stats.uptimerobot.com/api/getMonitorList/26r4CjSckG

jq -r '{root_keys: keys, status, monitor_count: (.data|length), days_count: (.days|length), psp_keys: (.psp|keys), statistics_keys: (.statistics|keys)}' /tmp/uptimerobot-profile.json

jq -r '{data_equals_psp_monitors: (.data == .psp.monitors), psp_totalMonitors: .psp.totalMonitors, psp_perPage: .psp.perPage, psp_timezone: .psp.timezone, days_first: .days[0], days_last: .days[-1], daily_min: ([.data[].dailyRatios|length] | min), daily_max: ([.data[].dailyRatios|length] | max)}' /tmp/uptimerobot-profile.json

jq -r '.data[] | [.monitorId, .name, .type, .statusClass, (if .url == null then "null" else .url end), (.dailyRatios|length), (.lastDowntime|type)] | @tsv' /tmp/uptimerobot-profile.json
```

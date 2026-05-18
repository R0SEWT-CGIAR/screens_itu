# Copilot Studio y UptimeRobot con Power Automate

Guia para que un agente de Copilot Studio responda preguntas como `OCS esta caido?` consultando la status page publica de UptimeRobot.

No se crea una API nueva en Quiosco. Power Automate consulta directamente el endpoint publico de UptimeRobot, filtra el servicio solicitado y devuelve una respuesta corta al agente.

Antes de implementar el flujo, usar como contrato base:

- `docs/integrations/uptimerobot-data-contract.md`

## Fuente de datos

Status page:

```text
https://stats.uptimerobot.com/26r4CjSckG
```

Endpoint JSON usado por la pagina:

```text
https://stats.uptimerobot.com/api/getMonitorList/26r4CjSckG
```

Campos utiles por monitor:

- `monitorId`
- `name`
- `type`
- `statusClass`
- `ratio.ratio`
- `30dRatio.ratio`
- `90dRatio.ratio`
- `lastDowntime.date`
- `lastDowntime.duration`
- `lastDowntime.reason`

El campo `url` aparece como `null` en la respuesta publica. Esto alcanza para responder estado, uptime e incidentes, pero no para conocer la URL real configurada dentro de UptimeRobot.

## Servicios actuales

Extraccion realizada el 2026-05-11 desde el endpoint publico.

| Servicio | Tipo | Estado | Uptime actual | 30d | 90d | Ultimo downtime |
| --- | --- | --- | ---: | ---: | ---: | --- |
| Cipotato.org | HTTP(s) | success | 99.992 | 99.992 | 99.988 | 2026-04-24 13:31:15, 219s |
| cipotato.org deface | Keyword | success | 99.995 | 99.995 | 99.963 | 2026-04-24 13:57:33, 130s |
| Genebank web for request germplasm | HTTP(s) | success | 99.958 | 99.958 | 99.950 | 2026-04-24 13:56:13, 195s |
| Genebank website | HTTP(s) | success | 99.966 | 99.966 | 99.984 | 2026-04-24 14:21:11, 300s |
| KM Hub/cipweb2 | HTTP(s) | success | 100.000 | 100.000 | 99.978 | 2026-02-27 04:03:42, 425s |
| New CIPGADC - Distribution | HTTP(s) | success | 100.000 | 100.000 | 99.973 | 2026-02-27 04:05:02, 207s |
| OCS | HTTP(s) | success | 100.000 | 100.000 | 99.344 | 2026-03-23 00:44:44, 162s |
| SharePoint Online | HTTP(s) | success | 100.000 | 100.000 | 100.000 | Sin downtime registrado |

Interpretacion recomendada:

| `statusClass` | Respuesta al usuario |
| --- | --- |
| `success` | Operativo |
| `danger` | Caido o con problemas |
| `paused` | Monitoreo pausado |
| otro valor o vacio | Estado no determinado |

## Flujo en Power Automate

Nombre recomendado del flujo:

```text
Consultar estado de servicio
```

### 1. Trigger

Usar el trigger:

```text
When an agent calls the flow
```

Agregar input:

| Nombre | Tipo | Uso |
| --- | --- | --- |
| `ServiceName` | Text | Nombre detectado por el agente, por ejemplo `OCS` |

### 2. Normalizar entrada

Agregar una accion `Compose` llamada `Normalize_ServiceName`:

```text
toLower(trim(triggerBody()?['ServiceName']))
```

Si el nombre exacto del input se muestra distinto en Power Automate, seleccionar el valor dinamico `ServiceName` desde el panel y envolverlo con `toLower(trim(...))`.

### 3. Consultar UptimeRobot

Agregar accion `HTTP`:

| Campo | Valor |
| --- | --- |
| Method | `GET` |
| URI | `https://stats.uptimerobot.com/api/getMonitorList/26r4CjSckG` |

No requiere headers ni autenticacion.

### 4. Parsear JSON

Agregar `Parse JSON` con:

| Campo | Valor |
| --- | --- |
| Content | `Body` de la accion HTTP |

Usar este schema minimo:

```json
{
  "type": "object",
  "properties": {
    "status": { "type": "string" },
    "data": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "monitorId": { "type": "integer" },
          "name": { "type": "string" },
          "type": { "type": "string" },
          "statusClass": { "type": "string" },
          "ratio": {
            "type": "object",
            "properties": {
              "ratio": { "type": "string" }
            }
          },
          "30dRatio": {
            "type": "object",
            "properties": {
              "ratio": { "type": "string" }
            }
          },
          "90dRatio": {
            "type": "object",
            "properties": {
              "ratio": { "type": "string" }
            }
          },
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
    "statistics": { "type": "object" }
  }
}
```

### 5. Buscar coincidencia exacta

Agregar `Filter array` llamado `Filter_Exact_Match`.

From:

```text
body('Parse_JSON')?['data']
```

Advanced mode:

```text
@equals(toLower(item()?['name']), outputs('Normalize_ServiceName'))
```

### 6. Buscar coincidencia parcial

Agregar `Filter array` llamado `Filter_Partial_Match`.

From:

```text
body('Parse_JSON')?['data']
```

Advanced mode:

```text
@contains(toLower(item()?['name']), outputs('Normalize_ServiceName'))
```

### 7. Seleccionar monitor

Agregar una condicion:

```text
length(body('Filter_Exact_Match')) is greater than 0
```

Si es verdadero, usar el primer elemento exacto:

```text
first(body('Filter_Exact_Match'))
```

Si es falso, evaluar:

```text
length(body('Filter_Partial_Match'))
```

Casos:

- `0`: no se encontro el servicio.
- `1`: usar `first(body('Filter_Partial_Match'))`.
- mayor que `1`: devolver candidatos y pedir que el usuario sea mas especifico.

Para candidatos, agregar una accion `Select` sobre `body('Filter_Partial_Match')` que deje solo `name`, y luego usar `join(...)` para construir un texto. Si Power Automate no permite unir directamente objetos, responder con una frase fija que liste los servicios disponibles desde la tabla de esta guia.

### 8. Componer mensaje final

Crear un `Compose` o variable `StatusMessage` segun el caso.

Si no hay coincidencias:

```text
No encontre un servicio llamado "<ServiceName>". Servicios disponibles: Cipotato.org, cipotato.org deface, Genebank web for request germplasm, Genebank website, KM Hub/cipweb2, New CIPGADC - Distribution, OCS, SharePoint Online.
```

Si hay multiples coincidencias:

```text
Encontre varios servicios que coinciden con "<ServiceName>". Indica uno mas especifico: Cipotato.org, cipotato.org deface, Genebank web for request germplasm, Genebank website, KM Hub/cipweb2, New CIPGADC - Distribution, OCS, SharePoint Online.
```

Si el servicio esta operativo:

```text
<Servicio> no esta caido. UptimeRobot lo reporta como operativo. Uptime actual: <ratio>%, ultimos 30 dias: <30d>%, ultimos 90 dias: <90d>%. Ultimo downtime: <fecha y duracion> o sin downtime registrado.
```

Si el servicio tiene problemas:

```text
<Servicio> parece tener problemas. UptimeRobot lo reporta con estado <statusClass>. Uptime actual: <ratio>%, ultimos 30 dias: <30d>%, ultimos 90 dias: <90d>%.
```

Si el servicio esta pausado:

```text
<Servicio> esta pausado en UptimeRobot. No puedo confirmar su estado real ahora.
```

Ejemplo para `OCS` con los datos actuales:

```text
OCS no esta caido. UptimeRobot lo reporta como operativo. Uptime actual: 100.000%, ultimos 30 dias: 100.000%, ultimos 90 dias: 99.344%. El ultimo downtime registrado fue el 2026-03-23 00:44:44 y duro 162 segundos.
```

### 9. Responder al agente

Agregar `Respond to the agent` con outputs:

| Nombre | Tipo | Valor |
| --- | --- | --- |
| `StatusMessage` | Text | Mensaje final compuesto |
| `Found` | Boolean | `true` si se encontro un unico servicio |
| `MatchedService` | Text | Nombre exacto del servicio encontrado, vacio si no aplica |
| `RawStatus` | Text | Valor original de `statusClass`, vacio si no aplica |

Mantener `Asynchronous response` en `Off`, porque los flujos llamados por agentes deben responder de forma sincronica.

## Configuracion en Copilot Studio

Crear un tema o una herramienta con frases de activacion:

```text
OCS esta caido?
estado de SharePoint
revisa Genebank website
hay problemas con KM Hub?
```

Flujo recomendado:

1. Capturar el nombre del servicio mencionado por el usuario.
2. Si no hay un nombre claro, preguntar: `Que servicio quieres revisar?`
3. Llamar el flujo `Consultar estado de servicio`.
4. Enviar `ServiceName` con el texto capturado.
5. Responder al usuario con `StatusMessage`.

Ejemplo de interaccion:

```text
Usuario: OCS esta caido?
Agente: OCS no esta caido. UptimeRobot lo reporta como operativo. Uptime actual: 100.000%, ultimos 30 dias: 100.000%, ultimos 90 dias: 99.344%. El ultimo downtime registrado fue el 2026-03-23 00:44:44 y duro 162 segundos.
```

## Consideraciones operativas

- La consulta es publica y no usa API key.
- Si Copilot Studio o el conector devuelve error por tamano de respuesta, mantener el filtrado dentro de Power Automate y devolver solo `StatusMessage`.
- Si se agregan monitores en UptimeRobot, no hace falta cambiar el endpoint. Solo actualizar esta guia si se quiere mantener la tabla de servicios vigente.
- Si se necesita saber la URL real monitoreada, usar la API autenticada de UptimeRobot o mantener una tabla manual de equivalencias.

## Referencias

- HTTP request en Copilot Studio: https://learn.microsoft.com/microsoft-copilot-studio/authoring-http-node
- Inputs y outputs de flujos para agentes: https://learn.microsoft.com/microsoft-copilot-studio/advanced-flow-input-output

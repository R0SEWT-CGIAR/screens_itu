# ADR-004: Conexion a Chromecasts por IP directa

## Estado
Aceptado

## Contexto
pychromecast ofrece dos formas de conectar:
1. `get_chromecasts()` — Descubrimiento automatico por mDNS/zeroconf. Lento (5-10s), requiere multicast en la red.
2. `Chromecast(CastInfo(...))` — Conexion directa por IP. Requiere conocer host, port y uuid de antemano.

## Decision
Conexion directa por IP usando `CastInfo` + `HostServiceInfo`. Los datos (host, port, uuid) se almacenan en `config.json` y se obtienen previamente con `discover.py`.

## Detalles
pychromecast.Chromecast() requiere un objeto `CastInfo` con un `services` set que contenga al menos un `HostServiceInfo(host, port)`. Sin el `HostServiceInfo`, el constructor acepta el `CastInfo` pero `.wait()` falla con timeout (el socket no sabe a donde conectar).

```python
from pychromecast.models import CastInfo, HostServiceInfo

svc = HostServiceInfo(host, port)
cast_info = CastInfo(services={svc}, uuid=..., host=host, port=port, ...)
cc = pychromecast.Chromecast(cast_info)
cc.wait(timeout=10)
```

## Consecuencias
- (+) Conexion rapida (~1s vs ~10s con discovery)
- (+) No depende de mDNS/multicast (puede estar bloqueado en redes corporativas)
- (-) Si la IP del Chromecast cambia (DHCP), hay que actualizar config.json y re-correr discover.py

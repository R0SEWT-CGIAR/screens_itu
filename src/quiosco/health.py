"""Traduce el estado crudo de una pantalla a un diagnostico para el tecnico.

/api/status ya expone heartbeat_age_seconds, fallback_active, dashcast_failures
y last_error, pero eso solo sirve a quien conoce el codigo. Aqui se convierte en
tres frases: que pasa, por que, y que hacer.
"""

from typing import Optional

from .cast_manager import (
    DISPLAY_HEARTBEAT_TIMEOUT_SECONDS,
    FALLBACK_AFTER_FAILURES,
)

LEVEL_OK = "ok"
LEVEL_WARN = "atencion"
LEVEL_ERROR = "error"


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def diagnose(cc: dict, interval_seconds: float) -> dict:
    """Diagnostico de una entrada de CastManager.get_status()['chromecasts']."""
    link_count = len(cc.get("playlist_link_ids") or [])
    heartbeat_age: Optional[float] = cc.get("heartbeat_age_seconds")
    stale_heartbeat = (
        heartbeat_age is None or heartbeat_age > DISPLAY_HEARTBEAT_TIMEOUT_SECONDS
    )

    if not cc.get("connected"):
        return {
            "level": LEVEL_ERROR,
            "summary": "Sin conexion con el Chromecast",
            "cause": cc.get("last_error") or "El dispositivo no responde en su IP",
            "action": (
                "Verificar que el Chromecast este encendido y en la red. El watchdog "
                "reintenta cada 15s y, si cambio de IP, escanea la subred para "
                "reencontrarlo."
            ),
        }

    if cc.get("fallback_active"):
        return {
            "level": LEVEL_ERROR,
            "summary": "Modo degradado: se castean imagenes en vez de las paginas",
            "cause": (
                f"DashCast no logro cargar la display page en "
                f"{_plural(cc.get('dashcast_failures') or FALLBACK_AFTER_FAILURES, 'chequeo', 'chequeos')} "
                "seguidos, asi que la rotacion usa los GIF por el receptor de Google."
            ),
            "action": (
                "Revisar que PROXY_BASE apunte a una IP de este servidor alcanzable "
                "desde la red del Chromecast. Reintenta DashCast solo cada 5 minutos; "
                "'Relanzar pantalla' fuerza el intento ahora."
            ),
        }

    if cc.get("display_launched") and stale_heartbeat:
        return {
            "level": LEVEL_ERROR,
            "summary": "DashCast corriendo pero la pagina no carga",
            "cause": (
                "El Chromecast acepto la app pero la display page nunca hizo su poll, "
                + (
                    "no llego ningun latido."
                    if heartbeat_age is None
                    else f"ultimo latido hace {int(heartbeat_age)}s."
                )
            ),
            "action": (
                "Es el sintoma clasico de un PROXY_BASE que el Chromecast no alcanza: "
                "queda el logo de DashCast fijo. Verificar la IP configurada y que el "
                "puerto 8000 responda desde la red del dispositivo."
            ),
        }

    if link_count == 0:
        return {
            "level": LEVEL_WARN,
            "summary": "Pantalla sin links para mostrar",
            "cause": (
                "Su playlist esta vacia o todos sus links estan deshabilitados."
            ),
            "action": "Habilitar algun link o agregarlo a la playlist de esta pantalla.",
        }

    if not cc.get("rotating"):
        return {
            "level": LEVEL_WARN,
            "summary": "Conectado, rotacion detenida",
            "cause": "Nadie inicio la rotacion en esta pantalla.",
            "action": f"Iniciar rotacion para recorrer sus {link_count} links.",
        }

    interval = int(interval_seconds) if float(interval_seconds).is_integer() else interval_seconds
    return {
        "level": LEVEL_OK,
        "summary": f"Rotando {_plural(link_count, 'link', 'links')} cada {interval}s",
        "cause": "",
        "action": "",
    }

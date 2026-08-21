"""Lectura y escritura del config.json que edita el tecnico desde la consola.

config.json es la fuente de verdad de lo que el tecnico configura: links,
playlists por pantalla e intervalo de rotacion. Las IPs y puertos que el
watchdog redescubre en runtime NO se escriben aqui: van a
data/runtime-state.json y se aplican como overlay al arrancar (ver CLAUDE.md).
"""

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = "config-backups"
BACKUP_RETENTION = 20
MIN_INTERVAL_SECONDS = 5
MIN_ZOOM = 0.1
MAX_ZOOM = 4.0
ALLOWED_SCHEMES = ("http", "https")


class ConfigError(ValueError):
    """Configuracion invalida. El mensaje se le muestra tal cual al tecnico."""


# --- Ids estables de link ---

def link_id_for(url: str) -> str:
    """Id derivado de la URL: estable al reordenar o borrar otros links."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]


def _unique_id(url: str, taken: set[str]) -> str:
    base = link_id_for(url)
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


# --- Normalizacion ---

def normalize_link(raw: dict, taken: Optional[set[str]] = None) -> dict:
    """Devuelve un link con claves canonicas, validado y con id asignado."""
    if not isinstance(raw, dict):
        raise ConfigError("Cada link debe ser un objeto")

    url = str(raw.get("url") or "").strip()
    validate_url(url)

    zoom = raw.get("zoom", 1.0)
    try:
        zoom = float(zoom)
    except (TypeError, ValueError):
        raise ConfigError(f"El zoom de '{url}' no es un numero")
    if not MIN_ZOOM <= zoom <= MAX_ZOOM:
        raise ConfigError(f"El zoom debe estar entre {MIN_ZOOM} y {MAX_ZOOM} (recibido {zoom})")

    taken = taken if taken is not None else set()
    link_id = str(raw.get("id") or "").strip() or _unique_id(url, taken)

    link = {
        "id": link_id,
        "url": url,
        "label": str(raw.get("label") or "").strip() or url,
        "zoom": zoom,
    }
    # optional/direct solo se escriben cuando estan activos, para no ensuciar
    # el config.json que el tecnico tambien puede leer por SSH.
    if raw.get("optional"):
        link["optional"] = True
    if raw.get("direct"):
        link["direct"] = True
    link["enabled"] = bool(raw.get("enabled", True))
    return link


def validate_url(url: str) -> None:
    if not url:
        raise ConfigError("La URL del link no puede estar vacia")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ConfigError(f"La URL debe empezar con http:// o https:// (recibido '{url}')")
    if not parsed.netloc:
        raise ConfigError(f"La URL no tiene host: '{url}'")


def normalize_links(raw_links: list) -> list[dict]:
    if not isinstance(raw_links, list):
        raise ConfigError("'links' debe ser una lista")
    taken: set[str] = set()
    normalized: list[dict] = []
    for raw in raw_links:
        link = normalize_link(raw, taken)
        if link["id"] in taken:
            # Dos entradas trajeron el mismo id explicito: se reasigna el segundo.
            link["id"] = _unique_id(link["url"], taken)
        taken.add(link["id"])
        normalized.append(link)
    return normalized


def normalize(cfg: dict) -> dict:
    """Normaliza el config completo sin escribirlo a disco."""
    if not isinstance(cfg, dict):
        raise ConfigError("El config debe ser un objeto JSON")

    normalized = dict(cfg)
    normalized["links"] = normalize_links(cfg.get("links", []))
    known_ids = {link["id"] for link in normalized["links"]}

    chromecasts = cfg.get("chromecasts", [])
    if not isinstance(chromecasts, list):
        raise ConfigError("'chromecasts' debe ser una lista")
    normalized["chromecasts"] = [
        _normalize_chromecast(cc, known_ids) for cc in chromecasts
    ]

    interval = cfg.get("default_interval_seconds", MIN_INTERVAL_SECONDS)
    normalized["default_interval_seconds"] = validate_interval(interval)
    return normalized


def _normalize_chromecast(raw: dict, known_ids: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("Cada chromecast debe ser un objeto")
    cc = dict(raw)
    playlist = raw.get("playlist")
    if playlist is None:
        cc.pop("playlist", None)
        return cc
    if not isinstance(playlist, list):
        raise ConfigError(f"La playlist de '{raw.get('id')}' debe ser una lista de ids")
    # Se descartan ids desconocidos y duplicados: un link borrado no debe dejar
    # la pantalla en un estado invalido.
    seen: set[str] = set()
    cleaned = []
    for lid in playlist:
        lid = str(lid)
        if lid in known_ids and lid not in seen:
            cleaned.append(lid)
            seen.add(lid)
    cc["playlist"] = cleaned
    return cc


def validate_interval(seconds) -> float:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        raise ConfigError("El intervalo debe ser un numero de segundos")
    if value < MIN_INTERVAL_SECONDS:
        raise ConfigError(f"El intervalo minimo es {MIN_INTERVAL_SECONDS} segundos")
    return value


# --- Resolucion por pantalla ---

def resolve_playlist(links: list[dict], playlist: Optional[list[str]]) -> list[dict]:
    """Links efectivos de una pantalla: filtro de playlist + solo habilitados.

    playlist None equivale a 'todos los links habilitados', que es el
    comportamiento que tenia el quiosco antes de las playlists por pantalla.
    """
    enabled = [link for link in links if link.get("enabled", True)]
    if playlist is None:
        return enabled
    by_id = {link["id"]: link for link in enabled}
    return [by_id[lid] for lid in playlist if lid in by_id]


# --- Persistencia ---

def load(config_path) -> dict:
    with open(config_path) as fh:
        return normalize(json.load(fh))


def save(config_path, cfg: dict, *, backup_dir=None, timestamp: Optional[str] = None) -> dict:
    """Guarda el config normalizado. Devuelve el config tal como quedo escrito."""
    path = Path(config_path)
    normalized = normalize(cfg)
    if backup_dir is None:
        backup_dir = path.resolve().parent / "data" / BACKUP_DIRNAME
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    _write_backup(path, Path(backup_dir), timestamp)
    payload = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    _write_config(path, payload)
    return normalized


def _write_config(path: Path, payload: str) -> None:
    """Escribe el config de forma atomica, con fallback a escritura in place.

    En produccion config.json llega al contenedor como bind-mount de un solo
    archivo (docker-compose.yml). Sobre ese mount os.replace falla con EBUSY,
    porque cambiar el inodo romperia el mount. Ahi se escribe in place: menos
    seguro ante un corte a mitad de escritura, pero el backup que se acaba de
    tomar deja el estado anterior recuperable.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".config-", suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(tmp_name, path)
            return
        except OSError as exc:
            logger.info(
                "os.replace no disponible sobre %s (%s); se escribe in place", path, exc
            )
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())


def _write_backup(path: Path, backup_dir: Path, timestamp: str) -> Optional[Path]:
    if not path.exists():
        return None
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"config-{timestamp}.json"
        if target.exists():
            # Varios guardados en el mismo segundo: se conserva el primero, que
            # es el estado al que el tecnico querria volver.
            return target
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        _prune_backups(backup_dir)
        return target
    except OSError as exc:
        # Un backup fallido no debe impedir el cambio que pidio el tecnico.
        logger.warning("No se pudo guardar backup de config en %s: %s", backup_dir, exc)
        return None


def _prune_backups(backup_dir: Path) -> None:
    backups = sorted(backup_dir.glob("config-*.json"))
    for stale in backups[:-BACKUP_RETENTION]:
        try:
            stale.unlink()
        except OSError:
            pass

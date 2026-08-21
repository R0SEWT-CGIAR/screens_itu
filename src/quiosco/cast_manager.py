import asyncio
import ipaddress
import json
import logging
import socket
import time
import urllib.request
import uuid as uuid_mod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import pychromecast
from pychromecast.controllers.dashcast import DashCastController
from pychromecast.generated.cast_channel_pb2 import CastMessage
from pychromecast.models import CastInfo, HostServiceInfo

from . import config_store
from .screenshot_assets import screenshot_asset_path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"

RUNTIME_STATE_FILENAME = "runtime-state.json"

DASHCAST_APP_ID = "84912283"
WATCHDOG_INTERVAL_SECONDS = 15.0
DISCOVERY_COOLDOWN_SECONDS = 60.0
SUBNET_SCAN_COOLDOWN_SECONDS = 120.0
SUBNET_SCAN_CONNECT_TIMEOUT_SECONDS = 1.0
SUBNET_SCAN_WORKERS = 32
EUREKA_PORT = 8008
EUREKA_TIMEOUT_SECONDS = 3.0
LINK_AVAILABILITY_TTL_SECONDS = 30.0
LINK_AVAILABILITY_TIMEOUT_SECONDS = 3.0
DASHCAST_LAUNCH_GRACE_SECONDS = 45.0
DISPLAY_HEARTBEAT_TIMEOUT_SECONDS = 60.0
FALLBACK_AFTER_FAILURES = 3
FALLBACK_DASHCAST_RETRY_SECONDS = 300.0


class TimedDashCastController(DashCastController):
    """DashCast con logging de tiempos."""

    def __init__(self, cc_name: str = ""):
        super().__init__()
        self.cc_name = cc_name
        self._send_time: float = 0

    def load_url(self, url: str, **kwargs) -> None:
        self._send_time = time.monotonic()
        logger.info("[%s] Enviando URL: %s", self.cc_name, url)
        super().load_url(url, **kwargs)

    def launch(self, *, callback_function=None, force_launch=False):
        # Siempre force_launch para poder relanzar tras cast directo
        cc_name = self.cc_name
        if callback_function is not None:
            _orig = callback_function
            def _logged(success: bool, response) -> None:
                if success:
                    logger.info("[%s] RECEIVER_STATUS recibido — enviando URL a DashCast", cc_name)
                else:
                    logger.warning("[%s] Fallo al lanzar DashCast: %s", cc_name, response)
                _orig(success, response)
            callback_function = _logged
        super().launch(callback_function=callback_function, force_launch=True)

    def receive_message(self, message: CastMessage, data: dict) -> bool:
        elapsed = time.monotonic() - self._send_time if self._send_time else 0
        logger.info(
            "[%s] Respuesta DashCast en %.2fs - data: %s",
            self.cc_name, elapsed, data,
        )
        return True


@dataclass
class CastState:
    id: str
    name: str
    host: str
    port: int
    uuid: str = ""
    current_index: int = 0
    rotating: bool = False
    current_url: Optional[str] = None
    current_label: Optional[str] = None
    connected: bool = False
    display_launched: bool = False
    display_ready: bool = False
    last_seen_at: Optional[str] = None
    last_error: Optional[str] = None
    reconnect_attempts: int = 0
    resolution: tuple[int, int] = (1920, 1080)
    last_display_launch_monotonic: Optional[float] = None
    last_heartbeat_monotonic: Optional[float] = None
    dashcast_failures: int = 0
    fallback_active: bool = False
    last_fallback_retry_monotonic: Optional[float] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    _chromecast: Optional[object] = field(default=None, repr=False)
    _dashcast: Optional[DashCastController] = field(default=None, repr=False)


class CastManager:
    def __init__(
        self,
        config_path: Optional[str] = None,
        proxy_base: str = "",
        runtime_state_path: Optional[str] = None,
    ):
        if config_path is None:
            config_path = str(_DEFAULT_CONFIG_PATH)
        cfg = config_store.load(config_path)

        self.config_path: str = config_path
        if runtime_state_path is None:
            self.runtime_state_path = (
                Path(config_path).resolve().parent / "data" / RUNTIME_STATE_FILENAME
            )
        else:
            self.runtime_state_path = Path(runtime_state_path)
        # config es la fuente de verdad de lo que se persiste; links/interval son
        # las vistas calientes que lee el loop de rotacion.
        self.config: dict = cfg
        self.links: list[dict] = cfg["links"]
        self.interval: float = cfg["default_interval_seconds"]
        self._playlists: dict[str, Optional[list[str]]] = {
            cc["id"]: cc.get("playlist") for cc in cfg["chromecasts"]
        }
        # La display page hornea esta revision al renderizar y se recarga sola
        # cuando el poll a /api/current devuelve una distinta.
        self.config_revision: int = 1
        self.proxy_base: str = proxy_base
        self._last_discovery_time: float = 0.0
        self._last_subnet_scan_time: float = 0.0
        self._link_availability: dict[str, tuple[float, bool]] = {}
        self.states: dict[str, CastState] = {}
        overrides = self._load_runtime_state()
        for cc in cfg["chromecasts"]:
            res = cc.get("resolution", [1920, 1080])
            override = overrides.get(cc["id"], {})
            self.states[cc["id"]] = CastState(
                id=cc["id"],
                name=cc["name"],
                host=override.get("host", cc["host"]),
                port=override.get("port", cc.get("port", 8009)),
                uuid=cc.get("uuid", ""),
                resolution=(int(res[0]), int(res[1])),
            )

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

    def links_for(self, cc_id: str) -> list[dict]:
        """Links efectivos de una pantalla: su playlist, solo los habilitados.

        Sin playlist configurada la pantalla rota todos los links habilitados,
        que es como se comportaba el quiosco antes de las playlists.
        """
        return config_store.resolve_playlist(self.links, self._playlists.get(cc_id))

    def playlist_for(self, cc_id: str) -> Optional[list[str]]:
        return self._playlists.get(cc_id)

    def _current_link(self, state: CastState) -> Optional[dict]:
        links = self.links_for(state.id)
        if not links:
            return None
        return links[state.current_index % len(links)]

    def _probe_link(self, url: str) -> bool:
        try:
            with httpx.Client(
                verify=False,
                timeout=LINK_AVAILABILITY_TIMEOUT_SECONDS,
                follow_redirects=True,
            ) as client:
                resp = client.get(url)
            return resp.status_code < 500
        except Exception:
            return False

    def _link_available(self, link: Optional[dict]) -> bool:
        """True salvo que el link sea optional y su probe (cacheado por TTL) falle."""
        if not link or not link.get("optional"):
            return True
        url = link["url"]
        cached = self._link_availability.get(url)
        if cached and time.monotonic() - cached[0] < LINK_AVAILABILITY_TTL_SECONDS:
            return cached[1]
        available = self._probe_link(url)
        self._link_availability[url] = (time.monotonic(), available)
        if cached is None or cached[1] != available:
            logger.info(
                "Link opcional '%s': %s",
                link.get("label", url),
                "disponible" if available else "no disponible, se salta en rotacion",
            )
        return available

    def _advance_index(self, state: CastState, step_size: int = 1) -> None:
        """Avanza al siguiente link disponible; si ninguno lo esta, avanza normal.

        step_size -1 retrocede, para el boton de link anterior de la consola.
        """
        links = self.links_for(state.id)
        n = len(links)
        if not n:
            # Un tecnico puede dejar una pantalla sin links (todos deshabilitados
            # o playlist vacia); antes esto reventaba con division por cero.
            state.current_index = 0
            return
        direction = 1 if step_size >= 0 else -1
        for step in range(1, n + 1):
            candidate = (state.current_index + step * direction) % n
            if self._link_available(links[candidate]):
                state.current_index = candidate
                return
        state.current_index = (state.current_index + direction) % n

    def _sync_current_link_state(self, state: CastState) -> None:
        link = self._current_link(state)
        if not link:
            state.current_url = None
            state.current_label = None
            return

        state.current_url = link["url"]
        state.current_label = link["label"]

    def _build_cast_info(self, state: CastState) -> CastInfo:
        svc = HostServiceInfo(state.host, state.port)
        return CastInfo(
            services={svc},
            uuid=uuid_mod.UUID(state.uuid) if state.uuid else uuid_mod.uuid4(),
            model_name="Chromecast",
            friendly_name=state.name,
            host=state.host,
            port=state.port,
            cast_type="cast",
            manufacturer="Google Inc.",
        )

    def _cleanup_chromecast(self, state: CastState) -> None:
        chromecast = state._chromecast
        state._chromecast = None
        state._dashcast = None
        state.connected = False
        state.display_launched = False
        state.display_ready = False

        if chromecast:
            try:
                chromecast.disconnect(timeout=1)
            except TypeError:
                chromecast.disconnect()
            except Exception:
                pass

    def _connect_state(self, state: CastState) -> bool:
        self._cleanup_chromecast(state)

        try:
            cc = pychromecast.Chromecast(self._build_cast_info(state))
            cc.wait(timeout=10)
            dashcast = TimedDashCastController(cc_name=state.name)
            cc.register_handler(dashcast)
            state._chromecast = cc
            state._dashcast = dashcast
            state.connected = True
            state.display_ready = cc.app_id == DASHCAST_APP_ID
            state.last_seen_at = self._utcnow()
            state.last_error = None
            state.reconnect_attempts = 0
            logger.info("Conectado a %s (%s)", state.name, state.host)
            return True
        except Exception as exc:
            self._cleanup_chromecast(state)
            state.last_error = str(exc)
            logger.error("Error conectando a %s: %s", state.name, exc)
            return False

    def connect(self) -> None:
        for state in self.states.values():
            self._connect_state(state)

    def disconnect(self) -> None:
        for state in self.states.values():
            if state.task and not state.task.done():
                state.task.cancel()
            state.task = None
            self._cleanup_chromecast(state)

    def launch_display(self, cc_id: str) -> bool:
        """Carga la display page en el Chromecast."""
        state = self.states.get(cc_id)
        if not state or not state.connected or not state._dashcast or not state._chromecast:
            return False

        if not state.current_url:
            self._sync_current_link_state(state)

        display_url = f"{self.proxy_base}/cast/display?cc_id={cc_id}"
        logger.info("Lanzando display page en %s: %s", state.name, display_url)
        try:
            state._dashcast.load_url(display_url)
        except Exception as exc:
            state.display_launched = False
            state.display_ready = False
            state.last_error = str(exc)
            logger.error("Error lanzando display page en %s: %s", state.name, exc)
            return False

        state.display_launched = True
        state.display_ready = state._chromecast.app_id == DASHCAST_APP_ID
        state.last_display_launch_monotonic = time.monotonic()
        state.last_error = None
        return True

    def _relaunch_display(self, cc_id: str) -> None:
        """Fuerza relanzar la display page (tras un cast directo)."""
        state = self.states.get(cc_id)
        if not state:
            return
        state.display_launched = False
        state.display_ready = False
        self.launch_display(cc_id)

    def cast_direct(self, cc_id: str, url: str) -> bool:
        """Cast directo con DashCast force=True (para externas sin iframe)."""
        state = self.states.get(cc_id)
        if not state or not state.connected or not state._dashcast:
            return False
        logger.info("Cast directo (force) a %s: %s", state.name, url)
        try:
            state._dashcast.load_url(url, force=True)
        except Exception as exc:
            state.last_error = str(exc)
            logger.error("Error en cast directo a %s: %s", state.name, exc)
            return False
        state.display_launched = False  # La display page fue reemplazada
        state.display_ready = False
        state.last_error = None
        return True

    def note_display_heartbeat(self, cc_id: str) -> None:
        """La display page hace poll a /api/current cada 2s; eso es el heartbeat."""
        state = self.states.get(cc_id)
        if state:
            state.last_heartbeat_monotonic = time.monotonic()

    def _fallback_media_url(self, state: CastState) -> Optional[str]:
        """URL del GIF de screenshot del link actual, si existe el asset."""
        link = self._current_link(state)
        if not link:
            return None
        asset_path = screenshot_asset_path(link["url"])
        try:
            revision = asset_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        return f"{self.proxy_base}/static/screenshots/{asset_path.name}?v={revision}"

    def cast_fallback_media(self, cc_id: str) -> bool:
        """Castea el screenshot del link actual con el Default Media Receiver.

        Modo degradado cuando DashCast no logra lanzar (p.ej. CAST_INIT_TIMEOUT):
        el receiver oficial de Google si puede reproducir los GIF locales.
        """
        state = self.states.get(cc_id)
        if not state or not state.connected or not state._chromecast:
            return False

        media_url = self._fallback_media_url(state)
        if not media_url:
            logger.warning(
                "[%s] Fallback sin asset de screenshot para %s; se mantiene el anterior",
                state.name, state.current_label,
            )
            return False

        logger.info("[%s] Fallback: casteando %s", state.name, media_url)
        try:
            mc = state._chromecast.media_controller
            mc.play_media(media_url, "image/gif")
            mc.block_until_active(timeout=10)
        except Exception as exc:
            state.last_error = f"Fallback media fallo: {exc}"
            logger.error("[%s] Error casteando fallback: %s", state.name, exc)
            return False
        state.last_error = None
        return True

    def cast_url(self, cc_id: str, url: str, label: str = "") -> bool:
        """Cast manual: busca el link por URL y actualiza current_index."""
        state = self.states.get(cc_id)
        if not state or not state.connected:
            return False
        # El indice es relativo a la playlist de esta pantalla, no al catalogo.
        for i, link in enumerate(self.links_for(cc_id)):
            if link["url"] == url:
                state.current_index = i
                state.current_url = url
                state.current_label = label or link["label"]
                return self.launch_display(cc_id)
        return False

    async def _rotation_loop(self, cc_id: str) -> None:
        """Loop de rotacion: todo via display page."""
        state = self.states[cc_id]
        logger.info("Rotacion iniciada para %s", state.name)

        if not self.links_for(cc_id):
            logger.warning("Rotacion omitida para %s: no hay links configurados", state.name)
            state.rotating = False
            state.task = None
            return

        try:
            while state.rotating:
                if not self.links_for(cc_id):
                    # El tecnico dejo la pantalla sin links en caliente. No se
                    # mata la rotacion: al rehabilitar un link se reanuda sola.
                    state.current_url = None
                    state.current_label = None
                    await asyncio.sleep(self.interval)
                    continue

                if not await asyncio.to_thread(self._link_available, self._current_link(state)):
                    await asyncio.to_thread(self._advance_index, state)
                self._sync_current_link_state(state)
                logger.info(
                    "Rotando a [%d] %s en %s",
                    state.current_index,
                    state.current_label,
                    state.name,
                )

                if state.fallback_active:
                    # No pisar un reintento de DashCast en curso con el media cast
                    retrying_dashcast = (
                        state.last_display_launch_monotonic is not None
                        and time.monotonic() - state.last_display_launch_monotonic
                        < DASHCAST_LAUNCH_GRACE_SECONDS
                    )
                    if not retrying_dashcast:
                        await asyncio.to_thread(self.cast_fallback_media, cc_id)
                elif not state.display_launched:
                    self._relaunch_display(cc_id)

                await asyncio.sleep(self.interval)
                await asyncio.to_thread(self._advance_index, state)
        except asyncio.CancelledError:
            logger.info("Rotacion cancelada para %s", state.name)
        except Exception as exc:
            logger.exception("Error en rotacion de %s: %s", state.name, exc)
            state.last_error = str(exc)
            state.rotating = False
        finally:
            if state.task is asyncio.current_task():
                state.task = None

    def start_rotation(self, cc_id: str) -> bool:
        state = self.states.get(cc_id)
        if not state or not state.connected or not self.links_for(cc_id):
            return False

        self._sync_current_link_state(state)
        self.launch_display(cc_id)

        if state.rotating:
            return True

        state.rotating = True
        state.task = asyncio.create_task(self._rotation_loop(cc_id))
        return True

    def stop_rotation(self, cc_id: str) -> bool:
        state = self.states.get(cc_id)
        if not state:
            return False
        state.rotating = False
        if state.task and not state.task.done():
            state.task.cancel()
        state.task = None
        return True

    def skip(self, cc_id: str, step_size: int = 1) -> bool:
        """Salta al link siguiente (o anterior) sin esperar el intervalo."""
        state = self.states.get(cc_id)
        if not state or not self.links_for(cc_id):
            return False
        self._advance_index(state, step_size)
        self._sync_current_link_state(state)
        if state.fallback_active:
            self.cast_fallback_media(cc_id)
        return True

    # --- Mutaciones de configuracion (consola de tecnicos) ---

    def _working_config(self) -> dict:
        """Copia editable del config, con las vistas calientes ya volcadas."""
        cfg = dict(self.config)
        cfg["links"] = [dict(link) for link in self.links]
        cfg["default_interval_seconds"] = self.interval
        # Los chromecast se copian del config original a proposito: host y port
        # pueden haber sido redescubiertos en runtime y esos no van a config.json
        # (ver CLAUDE.md); solo la playlist se edita desde aqui.
        cfg["chromecasts"] = [dict(cc) for cc in self.config["chromecasts"]]
        return cfg

    def _apply_config(self, cfg: dict) -> dict:
        """Persiste cfg, adopta el resultado normalizado y sube la revision."""
        showing = {
            cc_id: (self._current_link(state) or {}).get("id")
            for cc_id, state in self.states.items()
        }
        saved = config_store.save(self.config_path, cfg)

        self.config = saved
        self.links = saved["links"]
        self.interval = saved["default_interval_seconds"]
        self._playlists = {cc["id"]: cc.get("playlist") for cc in saved["chromecasts"]}
        self.config_revision += 1
        self._restore_indices(showing)
        return saved

    def _restore_indices(self, showing: dict[str, Optional[str]]) -> None:
        """Deja cada pantalla en el mismo link que mostraba antes del cambio.

        Sin esto, editar el ultimo link de la lista hace saltar a las pantallas
        que estaban en cualquier otra posicion.
        """
        for cc_id, state in self.states.items():
            links = self.links_for(cc_id)
            if not links:
                state.current_index = 0
            else:
                previous_id = showing.get(cc_id)
                position = next(
                    (i for i, link in enumerate(links) if link["id"] == previous_id), None
                )
                state.current_index = (
                    position if position is not None else state.current_index % len(links)
                )
            self._sync_current_link_state(state)

    def _find_link(self, cfg: dict, link_id: str) -> dict:
        for link in cfg["links"]:
            if link.get("id") == link_id:
                return link
        raise config_store.ConfigError(f"No existe el link '{link_id}'")

    def add_link(self, payload: dict) -> dict:
        cfg = self._working_config()
        taken = {link["id"] for link in cfg["links"]}
        new_link = config_store.normalize_link({**payload, "id": None}, taken)
        cfg["links"].append(new_link)
        saved = self._apply_config(cfg)
        logger.info("Link agregado: %s (%s)", new_link["label"], new_link["id"])
        return next(link for link in saved["links"] if link["id"] == new_link["id"])

    def update_link(self, link_id: str, payload: dict) -> dict:
        cfg = self._working_config()
        current = self._find_link(cfg, link_id)
        merged = {**current, **payload, "id": link_id}
        # optional/direct se omiten del config cuando son falsos, asi que un
        # merge plano no puede apagarlos: hay que borrar la clave a mano.
        for flag in ("optional", "direct"):
            if flag in payload and not payload[flag]:
                merged.pop(flag, None)
        index = cfg["links"].index(current)
        cfg["links"][index] = config_store.normalize_link(merged)
        saved = self._apply_config(cfg)
        logger.info("Link actualizado: %s", link_id)
        return next(link for link in saved["links"] if link["id"] == link_id)

    def set_link_enabled(self, link_id: str, enabled: bool) -> dict:
        return self.update_link(link_id, {"enabled": enabled})

    def delete_link(self, link_id: str) -> None:
        cfg = self._working_config()
        current = self._find_link(cfg, link_id)
        cfg["links"] = [link for link in cfg["links"] if link["id"] != link_id]
        for cc in cfg["chromecasts"]:
            if isinstance(cc.get("playlist"), list):
                cc["playlist"] = [lid for lid in cc["playlist"] if lid != link_id]
        self._apply_config(cfg)
        logger.info("Link borrado: %s (%s)", current.get("label"), link_id)

    def reorder_links(self, link_ids: list[str]) -> list[dict]:
        cfg = self._working_config()
        by_id = {link["id"]: link for link in cfg["links"]}
        if sorted(link_ids) != sorted(by_id):
            raise config_store.ConfigError(
                "El nuevo orden debe incluir exactamente los links existentes"
            )
        cfg["links"] = [by_id[lid] for lid in link_ids]
        saved = self._apply_config(cfg)
        return saved["links"]

    def set_playlist(self, cc_id: str, link_ids: Optional[list[str]]) -> Optional[list[str]]:
        if cc_id not in self.states:
            raise config_store.ConfigError(f"No existe el chromecast '{cc_id}'")
        cfg = self._working_config()
        for cc in cfg["chromecasts"]:
            if cc.get("id") != cc_id:
                continue
            if link_ids is None:
                cc.pop("playlist", None)
            else:
                cc["playlist"] = list(link_ids)
            break
        else:
            raise config_store.ConfigError(f"No existe el chromecast '{cc_id}'")
        self._apply_config(cfg)
        logger.info("Playlist de %s actualizada: %s", cc_id, self._playlists.get(cc_id))
        return self._playlists.get(cc_id)

    def set_interval(self, seconds: float, persist: bool = True) -> float:
        """Cambia el intervalo de rotacion. Persiste por defecto: antes se
        perdia en cada reinicio del contenedor."""
        seconds = config_store.validate_interval(seconds)
        if persist:
            cfg = self._working_config()
            cfg["default_interval_seconds"] = seconds
            self._apply_config(cfg)
        else:
            self.interval = seconds
        for cc_id, state in self.states.items():
            if state.rotating:
                self.stop_rotation(cc_id)
                self.start_rotation(cc_id)
        return self.interval

    def _check_connection_health(self, state: CastState, timeout: float = 5) -> tuple[bool, Optional[str]]:
        if not state._chromecast or not state._dashcast:
            return False, "Chromecast client no inicializado"

        socket_client = getattr(state._chromecast, "socket_client", None)
        if not socket_client or not socket_client.is_connected:
            return False, "Socket desconectado"

        try:
            state._chromecast.wait(timeout=timeout)
        except Exception as exc:
            return False, f"Handshake fallido: {exc}"

        state.last_seen_at = self._utcnow()
        return True, None

    def _discover_by_name(self, name: str) -> Optional[tuple[str, int]]:
        if time.monotonic() - self._last_discovery_time < DISCOVERY_COOLDOWN_SECONDS:
            return None

        logger.info("Iniciando discovery mDNS buscando '%s'...", name)
        try:
            chromecasts, browser = pychromecast.get_chromecasts(timeout=8)
            pychromecast.discovery.stop_discovery(browser)
            self._last_discovery_time = time.monotonic()
        except Exception as exc:
            logger.error("Discovery mDNS falló: %s", exc)
            self._last_discovery_time = time.monotonic()
            return None

        for cc in chromecasts:
            if cc.name.lower() == name.lower():
                return (cc.cast_info.host, cc.cast_info.port)

        logger.info("Discovery no encontró '%s' en la red", name)
        return None

    def _probe_cast_port(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection(
                (host, port), timeout=SUBNET_SCAN_CONNECT_TIMEOUT_SECONDS
            ):
                return True
        except OSError:
            return False

    def _device_name(self, host: str) -> Optional[str]:
        """Nombre del dispositivo vía la API de setup del Chromecast (puerto 8008)."""
        url = f"http://{host}:{EUREKA_PORT}/setup/eureka_info?params=name"
        try:
            with urllib.request.urlopen(url, timeout=EUREKA_TIMEOUT_SECONDS) as resp:
                return json.load(resp).get("name")
        except Exception:
            return None

    def _scan_subnet_for_device(
        self, name: str, last_host: str, port: int
    ) -> Optional[tuple[str, int]]:
        """Fallback del mDNS: barre el /24 de la última IP conocida buscando el puerto
        de Cast y confirma identidad por eureka_info. Necesario porque el mDNS es
        multicast de segmento y no cruza subredes (el servidor puede estar en otra)."""
        if time.monotonic() - self._last_subnet_scan_time < SUBNET_SCAN_COOLDOWN_SECONDS:
            return None
        self._last_subnet_scan_time = time.monotonic()

        try:
            if ipaddress.ip_address(last_host).version != 4:
                return None
            network = ipaddress.ip_network(f"{last_host}/24", strict=False)
        except ValueError:
            logger.error("IP inválida para escaneo de subred: %s", last_host)
            return None

        logger.info("Escaneando %s buscando '%s' (puerto %d)...", network, name, port)
        hosts = [str(h) for h in network.hosts()]
        with ThreadPoolExecutor(max_workers=SUBNET_SCAN_WORKERS) as pool:
            open_flags = list(pool.map(lambda h: self._probe_cast_port(h, port), hosts))
        candidates = [h for h, is_open in zip(hosts, open_flags) if is_open]

        for host in candidates:
            found = self._device_name(host)
            if found and found.lower() == name.lower():
                logger.info("Escaneo de subred encontró '%s' en %s:%d", name, host, port)
                return (host, port)

        logger.info("Escaneo de subred no encontró '%s' en %s", name, network)
        return None

    def _load_runtime_state(self) -> dict:
        """Lee hosts/puertos descubiertos previamente (overlay sobre config.json)."""
        try:
            with open(self.runtime_state_path) as f:
                data = json.load(f)
            chromecasts = data.get("chromecasts", {})
            if isinstance(chromecasts, dict):
                return chromecasts
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(
                "Estado runtime ilegible (%s), se ignora: %s", self.runtime_state_path, exc
            )
        return {}

    def _persist_host_update(self, state: CastState) -> None:
        """Guarda la IP/puerto descubiertos en el estado runtime, no en config.json."""
        try:
            try:
                with open(self.runtime_state_path) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            chromecasts = data.setdefault("chromecasts", {})
            chromecasts[state.id] = {"host": state.host, "port": state.port}
            self.runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.runtime_state_path, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            logger.info(
                "runtime-state actualizado: %s -> %s:%d", state.name, state.host, state.port
            )
        except Exception as exc:
            logger.error("Error actualizando estado runtime para %s: %s", state.name, exc)

    async def _recover_state(self, state: CastState, reason: Optional[str]) -> None:
        should_restore_display = state.display_launched or state.rotating or state.current_url is not None
        should_resume_rotation = state.rotating
        needs_rotation_task = state.task is None or state.task.done()

        state.reconnect_attempts += 1
        state.last_error = reason or "Conexion perdida"
        logger.warning(
            "Recuperando %s (%s), intento %d: %s",
            state.name,
            state.host,
            state.reconnect_attempts,
            state.last_error,
        )

        connected = await asyncio.to_thread(self._connect_state, state)
        if not connected:
            result = await asyncio.to_thread(self._discover_by_name, state.name)
            if result is None:
                result = await asyncio.to_thread(
                    self._scan_subnet_for_device, state.name, state.host, state.port
                )
            if result:
                new_host, new_port = result
                if new_host != state.host or new_port != state.port:
                    logger.info(
                        "Descubierto %s en nueva IP %s:%d (era %s:%d)",
                        state.name, new_host, new_port, state.host, state.port,
                    )
                    state.host = new_host
                    state.port = new_port
                    await asyncio.to_thread(self._persist_host_update, state)
                    connected = await asyncio.to_thread(self._connect_state, state)
            if not connected:
                state.reconnect_attempts = max(state.reconnect_attempts, 1)
                return

        if should_restore_display:
            self._sync_current_link_state(state)
            self.launch_display(state.id)

        if should_resume_rotation and needs_rotation_task:
            state.rotating = True
            state.task = asyncio.create_task(self._rotation_loop(state.id))

    async def ensure_device(self, cc_id: str) -> None:
        state = self.states.get(cc_id)
        if not state:
            return

        healthy, error = await asyncio.to_thread(self._check_connection_health, state)
        if not healthy:
            await self._recover_state(state, error)
            return

        state.connected = True
        state.last_error = None

        chromecast = state._chromecast
        now = time.monotonic()
        dashcast_running = bool(chromecast and chromecast.app_id == DASHCAST_APP_ID)
        heartbeat_fresh = (
            state.last_heartbeat_monotonic is not None
            and now - state.last_heartbeat_monotonic <= DISPLAY_HEARTBEAT_TIMEOUT_SECONDS
        )
        in_launch_grace = (
            state.last_display_launch_monotonic is not None
            and now - state.last_display_launch_monotonic < DASHCAST_LAUNCH_GRACE_SECONDS
        )

        if state.fallback_active:
            # Salir del fallback exige que la display page haya cargado de verdad
            # (heartbeat posterior al ultimo launch), no solo que DashCast corra.
            page_loaded = (
                dashcast_running
                and state.last_heartbeat_monotonic is not None
                and state.last_display_launch_monotonic is not None
                and state.last_heartbeat_monotonic >= state.last_display_launch_monotonic
            )
            if page_loaded:
                state.fallback_active = False
                state.dashcast_failures = 0
                state.display_ready = True
                logger.info(
                    "[%s] DashCast y display page recuperados; saliendo de fallback", state.name
                )
                return

            state.display_ready = False
            if not state.rotating:
                return

            if (
                state.last_fallback_retry_monotonic is None
                or now - state.last_fallback_retry_monotonic >= FALLBACK_DASHCAST_RETRY_SECONDS
            ):
                state.last_fallback_retry_monotonic = now
                logger.info("[%s] Reintentando DashCast desde fallback", state.name)
                self._relaunch_display(cc_id)
            return

        # Degradado si DashCast no corre, o si rota sin heartbeat de la display
        # page (DashCast puede correr con el logo pegado si la pagina no carga).
        display_degraded = state.display_launched and (
            not dashcast_running or (state.rotating and not heartbeat_fresh)
        )
        if display_degraded:
            if not state.rotating:
                state.display_ready = False
                return

            if in_launch_grace:
                state.display_ready = False
                return

            state.display_ready = False
            state.last_error = (
                "DashCast no activo" if not dashcast_running else "Display page sin heartbeat"
            )
            state.dashcast_failures += 1

            if state.dashcast_failures >= FALLBACK_AFTER_FAILURES:
                state.fallback_active = True
                state.last_fallback_retry_monotonic = now
                logger.warning(
                    "[%s] DashCast degradado %d veces seguidas (%s); fallback a Default Media Receiver",
                    state.name, state.dashcast_failures, state.last_error,
                )
                await asyncio.to_thread(self.cast_fallback_media, cc_id)
                return

            logger.warning(
                "[%s] Receiver degradado (%d/%d): app_id=%s heartbeat_fresh=%s",
                state.name, state.dashcast_failures, FALLBACK_AFTER_FAILURES,
                chromecast.app_id if chromecast else None, heartbeat_fresh,
            )
            self._relaunch_display(cc_id)
            return

        state.display_ready = bool(
            state.display_launched and dashcast_running and heartbeat_fresh
        )
        if state.display_ready:
            state.dashcast_failures = 0

    async def watchdog_loop(self, interval_seconds: float = WATCHDOG_INTERVAL_SECONDS) -> None:
        logger.info("Watchdog de Chromecast iniciado (intervalo=%ss)", interval_seconds)
        try:
            while True:
                for cc_id in self.states:
                    try:
                        await self.ensure_device(cc_id)
                    except Exception as exc:
                        state = self.states[cc_id]
                        state.last_error = str(exc)
                        logger.exception("Error en watchdog de %s: %s", state.name, exc)
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Watchdog de Chromecast cancelado")

    def start_watchdog_task(self, interval_seconds: float = WATCHDOG_INTERVAL_SECONDS) -> asyncio.Task:
        return asyncio.create_task(self.watchdog_loop(interval_seconds))

    def get_status(self) -> dict:
        return {
            "interval_seconds": self.interval,
            "links": self.links,
            "config_revision": self.config_revision,
            "chromecasts": [
                {
                    "id": s.id,
                    "name": s.name,
                    "host": s.host,
                    "playlist": self._playlists.get(s.id),
                    "playlist_link_ids": [link["id"] for link in self.links_for(s.id)],
                    "connected": s.connected,
                    "rotating": s.rotating,
                    "current_url": s.current_url,
                    "current_label": s.current_label,
                    "current_index": s.current_index,
                    "display_launched": s.display_launched,
                    "display_ready": s.display_ready,
                    "fallback_active": s.fallback_active,
                    "dashcast_failures": s.dashcast_failures,
                    "heartbeat_age_seconds": (
                        round(time.monotonic() - s.last_heartbeat_monotonic, 1)
                        if s.last_heartbeat_monotonic is not None
                        else None
                    ),
                    "last_seen_at": s.last_seen_at,
                    "last_error": s.last_error,
                    "reconnect_attempts": s.reconnect_attempts,
                }
                for s in self.states.values()
            ],
        }

import asyncio
import json
import logging
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pychromecast
from pychromecast.controllers.dashcast import DashCastController
from pychromecast.generated.cast_channel_pb2 import CastMessage
from pychromecast.models import CastInfo, HostServiceInfo

logger = logging.getLogger(__name__)

DASHCAST_APP_ID = "84912283"
WATCHDOG_INTERVAL_SECONDS = 15.0
DISCOVERY_COOLDOWN_SECONDS = 60.0


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
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    _chromecast: Optional[object] = field(default=None, repr=False)
    _dashcast: Optional[DashCastController] = field(default=None, repr=False)


class CastManager:
    def __init__(self, config_path: str = "config.json", proxy_base: str = ""):
        with open(config_path) as f:
            cfg = json.load(f)

        self.config_path: str = config_path
        self.links: list[dict] = cfg["links"]
        self.interval: float = cfg["default_interval_seconds"]
        self.proxy_base: str = proxy_base
        self._last_discovery_time: float = 0.0
        self.states: dict[str, CastState] = {}
        for cc in cfg["chromecasts"]:
            res = cc.get("resolution", [1920, 1080])
            self.states[cc["id"]] = CastState(
                id=cc["id"],
                name=cc["name"],
                host=cc["host"],
                port=cc.get("port", 8009),
                uuid=cc.get("uuid", ""),
                resolution=(int(res[0]), int(res[1])),
            )

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _current_link(self, state: CastState) -> Optional[dict]:
        if not self.links:
            return None
        return self.links[state.current_index % len(self.links)]

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

    def cast_url(self, cc_id: str, url: str, label: str = "") -> bool:
        """Cast manual: busca el link por URL y actualiza current_index."""
        state = self.states.get(cc_id)
        if not state or not state.connected:
            return False
        for i, link in enumerate(self.links):
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

        if not self.links:
            logger.warning("Rotacion omitida para %s: no hay links configurados", state.name)
            state.rotating = False
            state.task = None
            return

        try:
            while state.rotating:
                self._sync_current_link_state(state)
                logger.info(
                    "Rotando a [%d] %s en %s",
                    state.current_index,
                    state.current_label,
                    state.name,
                )

                if not state.display_launched:
                    self._relaunch_display(cc_id)

                await asyncio.sleep(self.interval)
                state.current_index = (state.current_index + 1) % len(self.links)
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
        if not state or not state.connected or not self.links:
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

    def set_interval(self, seconds: float) -> None:
        self.interval = seconds
        for cc_id, state in self.states.items():
            if state.rotating:
                self.stop_rotation(cc_id)
                self.start_rotation(cc_id)

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

    def _persist_host_update(self, state: CastState) -> None:
        try:
            with open(self.config_path) as f:
                cfg = json.load(f)
            for entry in cfg["chromecasts"]:
                if entry["id"] == state.id:
                    entry["host"] = state.host
                    entry["port"] = state.port
                    break
            with open(self.config_path, "w") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")
            logger.info("config.json actualizado: %s -> %s:%d", state.name, state.host, state.port)
        except Exception as exc:
            logger.error("Error actualizando config.json para %s: %s", state.name, exc)

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
        if state.display_launched and chromecast and chromecast.app_id != DASHCAST_APP_ID:
            state.display_ready = False
            state.last_error = "DashCast no activo"
            logger.warning("[%s] Receiver degradado: app_id=%s", state.name, chromecast.app_id)
            self._relaunch_display(cc_id)
            return

        state.display_ready = bool(
            state.display_launched and chromecast and chromecast.app_id == DASHCAST_APP_ID
        )

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
            "chromecasts": [
                {
                    "id": s.id,
                    "name": s.name,
                    "host": s.host,
                    "connected": s.connected,
                    "rotating": s.rotating,
                    "current_url": s.current_url,
                    "current_label": s.current_label,
                    "current_index": s.current_index,
                    "display_launched": s.display_launched,
                    "display_ready": s.display_ready,
                    "last_seen_at": s.last_seen_at,
                    "last_error": s.last_error,
                    "reconnect_attempts": s.reconnect_attempts,
                }
                for s in self.states.values()
            ],
        }

import asyncio
import json
import logging
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from typing import Optional

import pychromecast
from pychromecast.controllers.dashcast import DashCastController
from pychromecast.generated.cast_channel_pb2 import CastMessage
from pychromecast.models import CastInfo, HostServiceInfo

logger = logging.getLogger(__name__)

INTERNAL_HOST = "172.25.0.22"


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
            "[%s] Respuesta DashCast en %.2fs — data: %s",
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
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    _chromecast: Optional[object] = field(default=None, repr=False)
    _dashcast: Optional[DashCastController] = field(default=None, repr=False)


class CastManager:
    def __init__(self, config_path: str = "config.json", proxy_base: str = ""):
        with open(config_path) as f:
            cfg = json.load(f)

        self.links: list[dict] = cfg["links"]
        self.interval: float = cfg["default_interval_seconds"]
        self.proxy_base: str = proxy_base
        self.states: dict[str, CastState] = {}
        for cc in cfg["chromecasts"]:
            self.states[cc["id"]] = CastState(
                id=cc["id"],
                name=cc["name"],
                host=cc["host"],
                port=cc.get("port", 8009),
                uuid=cc.get("uuid", ""),
            )

    def connect(self) -> None:
        for state in self.states.values():
            try:
                svc = HostServiceInfo(state.host, state.port)
                cast_info = CastInfo(
                    services={svc},
                    uuid=uuid_mod.UUID(state.uuid) if state.uuid else uuid_mod.uuid4(),
                    model_name="Chromecast",
                    friendly_name=state.name,
                    host=state.host,
                    port=state.port,
                    cast_type="cast",
                    manufacturer="Google Inc.",
                )
                cc = pychromecast.Chromecast(cast_info)
                cc.wait(timeout=10)
                dashcast = TimedDashCastController(cc_name=state.name)
                cc.register_handler(dashcast)
                state._chromecast = cc
                state._dashcast = dashcast
                state.connected = True
                logger.info("Conectado a %s (%s)", state.name, state.host)
            except Exception as e:
                logger.error("Error conectando a %s: %s", state.name, e)
                state.connected = False

    def disconnect(self) -> None:
        for state in self.states.values():
            if state.task and not state.task.done():
                state.task.cancel()
            if state._chromecast:
                try:
                    state._chromecast.disconnect()
                except Exception:
                    pass

    def launch_display(self, cc_id: str) -> bool:
        """Carga la display page en el Chromecast."""
        state = self.states.get(cc_id)
        if not state or not state.connected or not state._dashcast:
            return False
        if state.display_launched:
            return True
        display_url = f"{self.proxy_base}/cast/display?cc_id={cc_id}"
        logger.info("Lanzando display page en %s: %s", state.name, display_url)
        state._dashcast.load_url(display_url)
        state.display_launched = True
        return True

    def _relaunch_display(self, cc_id: str) -> None:
        """Fuerza relanzar la display page (tras un cast directo)."""
        state = self.states.get(cc_id)
        if not state:
            return
        state.display_launched = False
        self.launch_display(cc_id)

    def cast_direct(self, cc_id: str, url: str) -> bool:
        """Cast directo con DashCast force=True (para externas sin iframe)."""
        state = self.states.get(cc_id)
        if not state or not state.connected or not state._dashcast:
            return False
        logger.info("Cast directo (force) a %s: %s", state.name, url)
        state._dashcast.load_url(url, force=True)
        state.display_launched = False  # La display page fue reemplazada
        return True

    def cast_url(self, cc_id: str, url: str, label: str = "") -> bool:
        """Cast manual: busca el link por URL y actualiza current_index."""
        state = self.states.get(cc_id)
        if not state or not state.connected:
            return False
        # Buscar el index del link
        for i, link in enumerate(self.links):
            if link["url"] == url:
                state.current_index = i
                state.current_url = url
                state.current_label = label
                # Asegurarse de que la display page este cargada
                self.launch_display(cc_id)
                return True
        return False

    def _can_proxy(self, url: str) -> bool:
        """URLs que no se pueden proxear (Cloudflare JS challenge)."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        unproxyable = {"cipotato.org"}
        return not any(host in parsed.netloc for host in unproxyable)

    async def _rotation_loop(self, cc_id: str) -> None:
        """Loop de rotacion: todo via display page (iframes para proxyables, screenshots para el resto)."""
        state = self.states[cc_id]
        logger.info("Rotacion iniciada para %s", state.name)
        try:
            while state.rotating:
                link = self.links[state.current_index]
                state.current_url = link["url"]
                state.current_label = link["label"]
                logger.info("Rotando a [%d] %s en %s", state.current_index, link["label"], state.name)

                # Asegurar display page cargada (todo pasa por ella)
                if not state.display_launched:
                    self._relaunch_display(cc_id)

                await asyncio.sleep(self.interval)
                state.current_index = (state.current_index + 1) % len(self.links)
        except asyncio.CancelledError:
            logger.info("Rotacion cancelada para %s", state.name)
        except Exception as e:
            logger.exception("Error en rotacion de %s: %s", state.name, e)
            state.rotating = False

    def start_rotation(self, cc_id: str) -> bool:
        state = self.states.get(cc_id)
        if not state or not state.connected:
            return False
        # Lanzar display page si no esta cargada
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
                }
                for s in self.states.values()
            ],
        }

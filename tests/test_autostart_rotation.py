"""Auto-start de rotacion: un reboot no debe dejar las pantallas en negro.

El lifespan solo hace connect(), que no rota. Sin auto-start, tras un reinicio
los Chromecast quedan connected=True / rotating=False y alguien tiene que pulsar
Start a mano. Estos tests fijan ese contrato y su excepcion: si un tecnico paro
la rotacion adrede, nadie se la vuelve a arrancar por detras.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quiosco.cast_manager import CastManager


def write_config(config_path: Path, *, auto_start=None) -> None:
    cfg = {
        "chromecasts": [
            {
                "id": "cc1",
                "name": "CC Uno",
                "host": "127.0.0.1",
                "port": 8009,
                "uuid": "00000000-0000-0000-0000-000000000001",
            },
        ],
        "links": [
            {"url": "https://a.example/", "label": "A"},
            {"url": "https://b.example/", "label": "B"},
        ],
        "default_interval_seconds": 15,
        "proxy_auto_subnet": "172.25.",
    }
    if auto_start is not None:
        cfg["auto_start_rotation"] = auto_start
    config_path.write_text(json.dumps(cfg), encoding="utf-8")


class AutostartTestCase(unittest.IsolatedAsyncioTestCase):
    def _manager(self, *, auto_start=None, connected=True) -> CastManager:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = Path(tmp.name) / "config.json"
        write_config(config_path, auto_start=auto_start)
        manager = CastManager(config_path=str(config_path))
        manager.states["cc1"].connected = connected
        # launch_display habla con el Chromecast real; el loop de rotacion solo
        # nos interesa por el efecto de arrancarlo, no por lo que hace dentro.
        self.enterContext(patch.object(CastManager, "launch_display"))
        self.enterContext(patch.object(CastManager, "_rotation_loop", new=self._noop))
        return manager

    @staticmethod
    async def _noop(*args, **kwargs):
        return None

    async def test_arranca_sola_en_arranque_en_frio(self):
        manager = self._manager()
        state = manager.states["cc1"]
        self.assertFalse(state.rotating)

        self.assertTrue(manager.maybe_autostart_rotation("cc1"))
        self.assertTrue(state.rotating)

    async def test_respeta_el_stop_del_tecnico(self):
        manager = self._manager()
        manager.start_rotation("cc1")
        manager.stop_rotation("cc1")
        state = manager.states["cc1"]
        self.assertTrue(state.stopped_by_user)

        # El watchdog llama a esto en cada ciclo: no debe pelearse con el tecnico.
        self.assertFalse(manager.maybe_autostart_rotation("cc1"))
        self.assertFalse(state.rotating)

    async def test_start_manual_limpia_la_marca_de_stop(self):
        manager = self._manager()
        manager.stop_rotation("cc1")
        manager.start_rotation("cc1")
        self.assertFalse(manager.states["cc1"].stopped_by_user)

    async def test_desactivable_por_config(self):
        manager = self._manager(auto_start=False)
        self.assertFalse(manager.autostart_rotation_enabled)
        self.assertFalse(manager.maybe_autostart_rotation("cc1"))
        self.assertFalse(manager.states["cc1"].rotating)

    async def test_activado_por_defecto_sin_la_clave(self):
        manager = self._manager()
        self.assertTrue(manager.autostart_rotation_enabled)

    async def test_no_arranca_sin_conexion(self):
        # Caso del schedule: exodia arranca antes de que la tele responda.
        manager = self._manager(connected=False)
        self.assertFalse(manager.maybe_autostart_rotation("cc1"))

        # ...y cuando el Chromecast aparece, el watchdog lo recoge.
        manager.states["cc1"].connected = True
        self.assertTrue(manager.maybe_autostart_rotation("cc1"))

    async def test_no_arranca_dos_veces(self):
        manager = self._manager()
        self.assertTrue(manager.maybe_autostart_rotation("cc1"))
        self.assertFalse(manager.maybe_autostart_rotation("cc1"))

    async def test_cambio_de_intervalo_no_cuenta_como_stop_humano(self):
        # set_interval reinicia la rotacion por dentro; si eso marcara
        # stopped_by_user, el auto-start quedaria inhabilitado para siempre.
        manager = self._manager()
        manager.start_rotation("cc1")
        manager.set_interval(30, persist=False)
        self.assertFalse(manager.states["cc1"].stopped_by_user)
        self.assertTrue(manager.states["cc1"].rotating)

    async def test_no_arranca_una_pantalla_ya_lanzada(self):
        # display_launched=True significa que no es un arranque en frio: puede
        # estar parada por un tecnico o en tratamiento por el watchdog. El
        # auto-start no debe pisar ninguno de los dos casos.
        manager = self._manager()
        manager.states["cc1"].display_launched = True
        self.assertFalse(manager.maybe_autostart_rotation("cc1"))
        self.assertFalse(manager.states["cc1"].rotating)

    async def test_status_expone_la_marca(self):
        manager = self._manager()
        manager.stop_rotation("cc1")
        cc = manager.get_status()["chromecasts"][0]
        self.assertTrue(cc["stopped_by_user"])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quiosco.cast_manager import CastManager, DASHCAST_APP_ID


class FakeChromecast:
    def __init__(self, app_id=DASHCAST_APP_ID, is_connected=True):
        self._app_id = app_id
        self.socket_client = SimpleNamespace(is_connected=is_connected)
        self.disconnected = False

    @property
    def app_id(self):
        return self._app_id

    def wait(self, timeout=None):
        return None

    def disconnect(self, timeout=None):
        self.disconnected = True


def write_config(config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "chromecasts": [
                    {
                        "id": "cc1",
                        "name": "Test Chromecast",
                        "host": "127.0.0.1",
                        "port": 8009,
                        "uuid": "00000000-0000-0000-0000-000000000001",
                    }
                ],
                "links": [
                    {"url": "https://example.com/a", "label": "A"},
                    {"url": "https://example.com/b", "label": "B"},
                ],
                "default_interval_seconds": 30,
            }
        ),
        encoding="utf-8",
    )


class CastManagerWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        config_path = Path(self.tmpdir.name) / "config.json"
        write_config(config_path)
        self.manager = CastManager(config_path=str(config_path), proxy_base="http://testserver")
        self.state = self.manager.states["cc1"]

    def tearDown(self):
        self.tmpdir.cleanup()

    async def test_watchdog_reconnects_when_socket_is_down(self):
        self.state.connected = True
        self.state.display_launched = True
        self.state.current_url = self.manager.links[0]["url"]
        self.state._chromecast = FakeChromecast(is_connected=False)
        self.state._dashcast = object()

        launch_calls = []

        def fake_connect(state):
            state._chromecast = FakeChromecast()
            state._dashcast = object()
            state.connected = True
            state.display_launched = False
            state.display_ready = False
            state.last_error = None
            state.reconnect_attempts = 0
            return True

        def fake_launch(cc_id):
            launch_calls.append(cc_id)
            self.state.display_launched = True
            return True

        self.manager._connect_state = fake_connect
        self.manager.launch_display = fake_launch

        await self.manager.ensure_device("cc1")

        self.assertTrue(self.state.connected)
        self.assertEqual(launch_calls, ["cc1"])

    async def test_watchdog_relaunches_when_dashcast_is_not_active(self):
        self.state.connected = True
        self.state.display_launched = True
        self.state._chromecast = FakeChromecast(app_id="OTHER_APP", is_connected=True)
        self.state._dashcast = object()

        launch_calls = []

        def fake_launch(cc_id):
            launch_calls.append(cc_id)
            self.state.display_launched = True
            return True

        self.manager.launch_display = fake_launch

        await self.manager.ensure_device("cc1")

        self.assertEqual(launch_calls, ["cc1"])

    async def test_watchdog_preserves_index_when_resuming_rotation(self):
        self.state.connected = True
        self.state.rotating = True
        self.state.display_launched = True
        self.state.current_index = 1
        self.state.current_url = self.manager.links[1]["url"]
        self.state.current_label = self.manager.links[1]["label"]
        self.state._chromecast = FakeChromecast(is_connected=False)
        self.state._dashcast = object()

        def fake_connect(state):
            state._chromecast = FakeChromecast()
            state._dashcast = object()
            state.connected = True
            state.display_launched = False
            state.display_ready = False
            state.last_error = None
            state.reconnect_attempts = 0
            return True

        def fake_launch(cc_id):
            self.state.display_launched = True
            return True

        def fake_create_task(coro):
            coro.close()
            return object()

        self.manager._connect_state = fake_connect
        self.manager.launch_display = fake_launch

        with patch("quiosco.cast_manager.asyncio.create_task", side_effect=fake_create_task):
            await self.manager.ensure_device("cc1")

        self.assertEqual(self.state.current_index, 1)
        self.assertIsNotNone(self.state.task)

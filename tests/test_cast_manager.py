import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quiosco.cast_manager import (
    CastManager,
    DASHCAST_APP_ID,
    DASHCAST_LAUNCH_GRACE_SECONDS,
    FALLBACK_AFTER_FAILURES,
    FALLBACK_DASHCAST_RETRY_SECONDS,
)


class FakeMediaController:
    def __init__(self):
        self.played = []

    def play_media(self, url, content_type):
        self.played.append((url, content_type))

    def block_until_active(self, timeout=None):
        return None


class FakeChromecast:
    def __init__(self, app_id=DASHCAST_APP_ID, is_connected=True):
        self._app_id = app_id
        self.socket_client = SimpleNamespace(is_connected=is_connected)
        self.media_controller = FakeMediaController()
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


class CastManagerRuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmpdir.name) / "config.json"
        write_config(self.config_path)
        self.state_path = Path(self.tmpdir.name) / "data" / "runtime-state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_runtime_state_overrides_config_host(self):
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(
            json.dumps({"chromecasts": {"cc1": {"host": "10.0.0.9", "port": 8010}}}),
            encoding="utf-8",
        )

        manager = CastManager(config_path=str(self.config_path), proxy_base="http://testserver")

        self.assertEqual(manager.states["cc1"].host, "10.0.0.9")
        self.assertEqual(manager.states["cc1"].port, 8010)

    def test_persist_host_update_writes_runtime_state_not_config(self):
        manager = CastManager(config_path=str(self.config_path), proxy_base="http://testserver")
        config_before = self.config_path.read_text(encoding="utf-8")

        state = manager.states["cc1"]
        state.host = "10.0.0.7"
        state.port = 8009
        manager._persist_host_update(state)

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), config_before)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["chromecasts"]["cc1"], {"host": "10.0.0.7", "port": 8009})

    def test_corrupt_runtime_state_is_ignored(self):
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text("{not json", encoding="utf-8")

        manager = CastManager(config_path=str(self.config_path), proxy_base="http://testserver")

        self.assertEqual(manager.states["cc1"].host, "127.0.0.1")


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
        self.state.rotating = True
        self.state.display_launched = True
        self.state.last_display_launch_monotonic = (
            time.monotonic() - DASHCAST_LAUNCH_GRACE_SECONDS - 1
        )
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

    async def test_watchdog_waits_for_dashcast_launch_grace_period(self):
        self.state.connected = True
        self.state.rotating = True
        self.state.display_launched = True
        self.state.last_display_launch_monotonic = time.monotonic()
        self.state._chromecast = FakeChromecast(app_id=None, is_connected=True)
        self.state._dashcast = object()

        launch_calls = []

        def fake_launch(cc_id):
            launch_calls.append(cc_id)
            return True

        self.manager.launch_display = fake_launch

        await self.manager.ensure_device("cc1")

        self.assertEqual(launch_calls, [])
        self.assertFalse(self.state.display_ready)
        self.assertIsNone(self.state.last_error)

    async def test_watchdog_does_not_relaunch_when_rotation_is_stopped(self):
        self.state.connected = True
        self.state.rotating = False
        self.state.display_launched = True
        self.state.last_display_launch_monotonic = (
            time.monotonic() - DASHCAST_LAUNCH_GRACE_SECONDS - 1
        )
        self.state._chromecast = FakeChromecast(app_id="CC1AD845", is_connected=True)
        self.state._dashcast = object()

        launch_calls = []

        def fake_launch(cc_id):
            launch_calls.append(cc_id)
            return True

        self.manager.launch_display = fake_launch

        await self.manager.ensure_device("cc1")

        self.assertEqual(launch_calls, [])
        self.assertFalse(self.state.display_ready)

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


class CastManagerFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        config_path = Path(self.tmpdir.name) / "config.json"
        write_config(config_path)
        self.manager = CastManager(config_path=str(config_path), proxy_base="http://testserver")
        self.state = self.manager.states["cc1"]
        self.asset_path = Path(self.tmpdir.name) / "example_com_abc123def456.gif"
        self.asset_path.write_bytes(b"GIF89a")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _degrade_state(self):
        self.state.connected = True
        self.state.rotating = True
        self.state.display_launched = True
        self.state.last_display_launch_monotonic = (
            time.monotonic() - DASHCAST_LAUNCH_GRACE_SECONDS - 1
        )
        self.state._chromecast = FakeChromecast(app_id="OTHER_APP", is_connected=True)
        self.state._dashcast = object()

    async def test_watchdog_activates_fallback_after_consecutive_failures(self):
        self._degrade_state()

        launch_calls = []

        def fake_launch(cc_id):
            launch_calls.append(cc_id)
            self.state.display_launched = True
            return True

        self.manager.launch_display = fake_launch

        with patch("quiosco.cast_manager.screenshot_asset_path", return_value=self.asset_path):
            for _ in range(FALLBACK_AFTER_FAILURES):
                await self.manager.ensure_device("cc1")

        self.assertTrue(self.state.fallback_active)
        self.assertEqual(self.state.dashcast_failures, FALLBACK_AFTER_FAILURES)
        # Solo relanza DashCast en los intentos previos al fallback
        self.assertEqual(len(launch_calls), FALLBACK_AFTER_FAILURES - 1)
        played = self.state._chromecast.media_controller.played
        self.assertEqual(len(played), 1)
        url, content_type = played[0]
        self.assertEqual(content_type, "image/gif")
        self.assertIn("http://testserver/static/screenshots/", url)
        self.assertIn(self.asset_path.name, url)

    async def test_fallback_does_not_retry_dashcast_within_window(self):
        self._degrade_state()
        self.state.fallback_active = True
        self.state.last_fallback_retry_monotonic = time.monotonic()

        launch_calls = []
        self.manager.launch_display = lambda cc_id: launch_calls.append(cc_id) or True

        await self.manager.ensure_device("cc1")

        self.assertEqual(launch_calls, [])
        self.assertTrue(self.state.fallback_active)

    async def test_fallback_retries_dashcast_after_retry_window(self):
        self._degrade_state()
        self.state.fallback_active = True
        self.state.last_fallback_retry_monotonic = (
            time.monotonic() - FALLBACK_DASHCAST_RETRY_SECONDS - 1
        )

        launch_calls = []

        def fake_launch(cc_id):
            launch_calls.append(cc_id)
            self.state.display_launched = True
            return True

        self.manager.launch_display = fake_launch

        before = self.state.last_fallback_retry_monotonic
        await self.manager.ensure_device("cc1")

        self.assertEqual(launch_calls, ["cc1"])
        self.assertTrue(self.state.fallback_active)
        self.assertGreater(self.state.last_fallback_retry_monotonic, before)

    async def test_fallback_clears_when_dashcast_recovers(self):
        self._degrade_state()
        self.state.fallback_active = True
        self.state.dashcast_failures = 5
        self.state._chromecast = FakeChromecast(app_id=DASHCAST_APP_ID, is_connected=True)

        await self.manager.ensure_device("cc1")

        self.assertFalse(self.state.fallback_active)
        self.assertEqual(self.state.dashcast_failures, 0)
        self.assertTrue(self.state.display_ready)

    async def test_cast_fallback_media_without_asset_returns_false(self):
        self.state.connected = True
        self.state._chromecast = FakeChromecast(is_connected=True)
        self.manager._sync_current_link_state(self.state)

        result = self.manager.cast_fallback_media("cc1")

        self.assertFalse(result)
        self.assertEqual(self.state._chromecast.media_controller.played, [])

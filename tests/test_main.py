import json
import tempfile
import unittest
from pathlib import Path

import main
import screenshot_assets
from cast_manager import CastManager


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
                    {"url": "https://www.cgiar.org/landing", "label": "Screenshot"},
                    {"url": "https://example.com/dashboard", "label": "Iframe"},
                    {
                        "url": "https://172.25.0.22/public/mapshow.htm?id=1",
                        "label": "Internal 1",
                    },
                    {
                        "url": "https://172.25.0.22/public/mapshow.htm?id=2",
                        "label": "Internal 2",
                    },
                ],
                "default_interval_seconds": 30,
            }
        ),
        encoding="utf-8",
    )


class MainRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_manager = main.manager
        self.original_dir = screenshot_assets.SCREENSHOT_DIR

        screenshot_assets.SCREENSHOT_DIR = Path(self.tmpdir.name) / "screenshots"
        screenshot_assets.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

        config_path = Path(self.tmpdir.name) / "config.json"
        write_config(config_path)
        main.manager = CastManager(config_path=str(config_path), proxy_base="http://testserver")

    def tearDown(self):
        main.manager = self.original_manager
        screenshot_assets.SCREENSHOT_DIR = self.original_dir
        self.tmpdir.cleanup()

    def test_current_returns_screenshot_metadata(self):
        state = main.manager.states["cc1"]
        state.current_index = 0

        asset_key = screenshot_assets.screenshot_asset_key(main.manager.links[0]["url"])
        asset_path = screenshot_assets.screenshot_asset_path_for_key(asset_key)
        asset_path.write_bytes(b"png")
        expected_revision = asset_path.stat().st_mtime_ns

        payload = main.current("cc1")

        self.assertEqual(payload["index"], 0)
        self.assertEqual(payload["current_url"], main.manager.links[0]["url"])
        self.assertEqual(payload["render_mode"], "screenshot")
        self.assertEqual(payload["asset_key"], asset_key)
        self.assertEqual(payload["asset_revision"], expected_revision)

    def test_current_returns_iframe_metadata(self):
        state = main.manager.states["cc1"]
        state.current_index = 1

        payload = main.current("cc1")

        self.assertEqual(payload["index"], 1)
        self.assertEqual(payload["current_url"], main.manager.links[1]["url"])
        self.assertEqual(payload["render_mode"], "iframe")
        self.assertIsNone(payload["asset_key"])
        self.assertIsNone(payload["asset_revision"])

    def test_cast_display_uses_dynamic_screenshot_assets(self):
        response = main.cast_display("cc1")
        html = response.body.decode("utf-8")
        screenshot_key = screenshot_assets.screenshot_asset_key(main.manager.links[0]["url"])

        self.assertIn(f'data-asset-key="{screenshot_key}"', html)
        self.assertIn("refreshScreenshotFrame", html)
        self.assertIn("/static/screenshots/${assetKey}.png?v=${version}", html)
        self.assertIn('<iframe id="frame-1"', html)

    def test_cast_startup_check_uses_all_configured_links(self):
        response = main.cast_startup_check("cc1")
        html = response.body.decode("utf-8")

        self.assertIn('startup-frame-0', html)
        self.assertIn('startup-frame-3', html)
        screenshot_key = screenshot_assets.screenshot_asset_key(main.manager.links[0]["url"])
        self.assertIn(f"/static/screenshots/{screenshot_key}.png?v=", html)
        self.assertIn("/p/https%3A%2F%2Fexample.com/dashboard", html)
        self.assertIn("/proxy/public/mapshow.htm?id=1", html)
        self.assertIn("/proxy/public/mapshow.htm?id=2", html)
        self.assertIn("const stepMs = 10000;", html)
        self.assertIn("const loadTimeoutMs = 30000;", html)
        self.assertIn("Debug interno", html)
        self.assertIn('id="debugList"', html)
        self.assertIn("renderDebugList", html)
        self.assertIn("Cargada", html)
        self.assertIn("Sin respuesta", html)
        self.assertIn("Pendiente", html)
        self.assertIn("Comprobacion finalizada", html)
        self.assertIn("ultima pagina visible", html)
        self.assertIn("Screenshot", html)
        self.assertIn("Iframe", html)
        self.assertIn("Internal 1", html)
        self.assertIn("Internal 2", html)
        self.assertNotIn("/api/current/", html)
        self.assertNotIn("startup-check-complete", html)

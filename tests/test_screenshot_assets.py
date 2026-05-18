import tempfile
import unittest
from pathlib import Path

from quiosco import screenshot_assets


class ScreenshotAssetsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_dir = screenshot_assets.SCREENSHOT_DIR
        screenshot_assets.SCREENSHOT_DIR = Path(self.tmpdir.name)
        screenshot_assets.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        screenshot_assets.SCREENSHOT_DIR = self.original_dir
        self.tmpdir.cleanup()

    def test_asset_key_is_stable_for_same_url(self):
        url = "https://www.cgiar.org/path?a=1"
        self.assertEqual(
            screenshot_assets.screenshot_asset_key(url),
            screenshot_assets.screenshot_asset_key(url),
        )

    def test_asset_key_avoids_collisions_for_same_hostname(self):
        url_a = "https://www.cgiar.org/landing"
        url_b = "https://www.cgiar.org/news"

        key_a = screenshot_assets.screenshot_asset_key(url_a)
        key_b = screenshot_assets.screenshot_asset_key(url_b)

        self.assertNotEqual(key_a, key_b)
        self.assertTrue(key_a.startswith("www_cgiar_org_"))
        self.assertTrue(key_b.startswith("www_cgiar_org_"))

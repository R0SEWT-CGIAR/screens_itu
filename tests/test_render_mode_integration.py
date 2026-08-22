"""Costura entre la consola de tecnicos y la captura en vivo.

La consola normaliza cada link al guardarlo y la captura en vivo se decide por
`render_mode`. Si la normalizacion descarta esa clave, el link vuelve a
renderizarse como iframe sin que nadie lo note, asi que aqui se fija el
contrato de punta a punta.
"""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from quiosco import config_store, main
from quiosco.cast_manager import CastManager

LIVE_URL = "http://172.25.21.37:3456/"


def write_config(config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "chromecasts": [
                    {"id": "cc1", "name": "CC Uno", "host": "127.0.0.1", "port": 8009},
                ],
                "links": [
                    {"url": "https://www.cgiar.org/landing", "label": "Screenshot"},
                    {"url": "https://example.com/dashboard", "label": "Iframe"},
                    {
                        "url": LIVE_URL,
                        "label": "Suite en vivo",
                        "render_mode": "live_screenshot",
                    },
                ],
                "default_interval_seconds": 15,
            }
        ),
        encoding="utf-8",
    )


class RenderModeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        write_config(self.config_path)

    def test_load_preserves_the_live_screenshot_mode(self):
        cfg = config_store.load(self.config_path)

        self.assertEqual(cfg["links"][2]["render_mode"], "live_screenshot")

    def test_saving_does_not_drop_the_mode(self):
        cfg = config_store.load(self.config_path)
        config_store.save(
            self.config_path, cfg, backup_dir=Path(self.tmp.name) / "backups"
        )

        written = json.loads(self.config_path.read_text())
        self.assertEqual(written["links"][2]["render_mode"], "live_screenshot")

    def test_the_default_mode_is_not_written_to_the_config(self):
        """Igual que optional/direct: el config.json que el tecnico lee por SSH
        solo lleva lo que se aparta del comportamiento normal."""
        link = config_store.normalize_link({"url": "https://a.example/"})

        self.assertNotIn("render_mode", link)

    def test_an_unknown_mode_is_rejected_with_a_readable_message(self):
        with self.assertRaises(config_store.ConfigError) as ctx:
            config_store.normalize_link(
                {"url": "https://a.example/", "render_mode": "video"}
            )

        self.assertIn("video", str(ctx.exception))
        self.assertIn("live_screenshot", str(ctx.exception))


class RenderModeConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        write_config(self.config_path)

        self.original_manager = main.manager
        main.manager = CastManager(
            config_path=str(self.config_path), proxy_base="http://testserver"
        )
        self.client = TestClient(main.app)
        self.addCleanup(lambda: setattr(main, "manager", self.original_manager))

    @property
    def live_link_id(self) -> str:
        return next(
            link["id"] for link in main.manager.links if link["url"] == LIVE_URL
        )

    def written_links(self) -> list[dict]:
        return json.loads(self.config_path.read_text())["links"]

    def test_editing_another_field_keeps_the_live_mode(self):
        res = self.client.patch(
            f"/api/links/{self.live_link_id}", json={"label": "Suite renombrada"}
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["link"]["render_mode"], "live_screenshot")
        self.assertEqual(self.written_links()[2]["render_mode"], "live_screenshot")

    def test_a_link_can_be_switched_to_live_capture(self):
        iframe_id = main.manager.links[1]["id"]

        res = self.client.patch(
            f"/api/links/{iframe_id}", json={"render_mode": "live_screenshot"}
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["link"]["render_mode"], "live_screenshot")

    def test_a_link_can_be_switched_back_to_iframe(self):
        res = self.client.patch(
            f"/api/links/{self.live_link_id}", json={"render_mode": "iframe"}
        )

        self.assertEqual(res.status_code, 200)
        self.assertNotIn("render_mode", res.json()["link"])
        self.assertNotIn("render_mode", self.written_links()[2])

    def test_an_unknown_mode_is_rejected_by_the_api(self):
        res = self.client.patch(
            f"/api/links/{self.live_link_id}", json={"render_mode": "video"}
        )

        self.assertEqual(res.status_code, 400)
        self.assertIn("video", res.json()["detail"])

    def test_a_live_link_has_nothing_to_recapture(self):
        """El PNG se republica solo cada pocos segundos."""
        res = self.client.post(f"/api/links/{self.live_link_id}/recapture")

        self.assertEqual(res.status_code, 400)


class CaptureTargetTests(unittest.TestCase):
    """Reparto de links entre el ciclo de GIF y la captura en vivo."""

    def setUp(self):
        self.links = [
            {"url": "https://www.cgiar.org/landing", "label": "GIF"},
            {"url": "https://example.com/dashboard", "label": "Iframe"},
            {"url": "https://172.25.0.22/public/mapshow.htm?id=1", "label": "PRTG"},
            {"url": LIVE_URL, "label": "En vivo", "render_mode": "live_screenshot"},
        ]

    def test_a_live_link_is_not_captured_as_gif(self):
        urls, _ = main.gif_capture_targets(self.links, 1920, 1080)

        self.assertNotIn(LIVE_URL, urls)

    def test_the_gif_cycle_still_takes_screenshots_and_prtg(self):
        urls, _ = main.gif_capture_targets(self.links, 1920, 1080)

        self.assertEqual(
            urls,
            ["https://www.cgiar.org/landing", "https://172.25.0.22/public/mapshow.htm?id=1"],
        )

    def test_only_live_links_go_to_the_live_loop(self):
        urls, viewports = main.live_capture_targets(self.links, 1920, 1080)

        self.assertEqual(urls, [LIVE_URL])
        self.assertEqual(viewports[LIVE_URL], (1920, 1080))

    def test_zoom_sets_the_live_viewport(self):
        links = [{**self.links[3], "zoom": 0.5}]

        _, viewports = main.live_capture_targets(links, 1920, 1080)

        self.assertEqual(viewports[LIVE_URL], (3840, 2160))

    def test_a_disabled_link_is_captured_by_neither_loop(self):
        links = [{**link, "enabled": False} for link in self.links]

        gif_urls, _ = main.gif_capture_targets(links, 1920, 1080)
        live_urls, _ = main.live_capture_targets(links, 1920, 1080)

        self.assertEqual(gif_urls, [])
        self.assertEqual(live_urls, [])


if __name__ == "__main__":
    unittest.main()

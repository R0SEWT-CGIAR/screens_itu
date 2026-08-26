"""Endpoints de la consola de tecnicos, ejercitados por HTTP."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from quiosco import main
from quiosco.cast_manager import CastManager


def write_config(config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "chromecasts": [
                    {"id": "cc1", "name": "CC Uno", "host": "127.0.0.1", "port": 8009},
                    {"id": "cc2", "name": "CC Dos", "host": "127.0.0.2", "port": 8009},
                ],
                "links": [
                    {"url": "https://www.cgiar.org/landing", "label": "Screenshot"},
                    {"url": "https://example.com/dashboard", "label": "Iframe"},
                    {"url": "https://172.25.0.22/public/mapshow.htm?id=1", "label": "PRTG"},
                ],
                "default_interval_seconds": 15,
            }
        ),
        encoding="utf-8",
    )


class ConsoleApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        write_config(self.config_path)

        self.original_manager = main.manager
        self.original_queue = main.recapture_queue
        main.manager = CastManager(
            config_path=str(self.config_path), proxy_base="http://testserver"
        )
        # Sin context manager el lifespan no corre: no se conecta a Chromecasts
        # reales ni se lanza Playwright.
        self.client = TestClient(main.app)

        def restore():
            main.manager = self.original_manager
            main.recapture_queue = self.original_queue

        self.addCleanup(restore)

    @property
    def link_ids(self) -> list[str]:
        return [link["id"] for link in main.manager.links]

    def written_config(self) -> dict:
        return json.loads(self.config_path.read_text())


class LinkEndpointTests(ConsoleApiTests):
    def test_list_links_returns_ids_and_revision(self):
        res = self.client.get("/api/links")

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(len(body["links"]), 3)
        self.assertEqual(body["config_revision"], 1)
        self.assertTrue(all("id" in link for link in body["links"]))

    def test_create_link_persists_it(self):
        res = self.client.post(
            "/api/links",
            json={"url": "https://nuevo.example/", "label": "Nuevo", "zoom": 0.75},
        )

        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(body["link"]["label"], "Nuevo")
        self.assertEqual(body["config_revision"], 2)
        self.assertEqual(self.written_config()["links"][-1]["label"], "Nuevo")

    def test_create_link_with_a_bad_url_returns_400_with_a_readable_message(self):
        res = self.client.post("/api/links", json={"url": "javascript:alert(1)"})

        self.assertEqual(res.status_code, 400)
        self.assertIn("http://", res.json()["detail"])
        self.assertEqual(len(main.manager.links), 3)

    def test_create_link_with_an_out_of_range_zoom_returns_400(self):
        res = self.client.post(
            "/api/links", json={"url": "https://nuevo.example/", "zoom": 50}
        )

        self.assertEqual(res.status_code, 400)
        self.assertIn("zoom", res.json()["detail"])

    def test_patch_link_updates_only_the_fields_sent(self):
        link_id = self.link_ids[1]
        res = self.client.patch(f"/api/links/{link_id}", json={"label": "Renombrado"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["link"]["label"], "Renombrado")
        self.assertEqual(res.json()["link"]["url"], "https://example.com/dashboard")

    def test_patch_link_can_disable_it(self):
        link_id = self.link_ids[1]
        res = self.client.patch(f"/api/links/{link_id}", json={"enabled": False})

        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["link"]["enabled"])
        self.assertNotIn(link_id, [l["id"] for l in main.manager.links_for("cc1")])

    def test_patch_with_an_empty_body_is_rejected(self):
        res = self.client.patch(f"/api/links/{self.link_ids[0]}", json={})

        self.assertEqual(res.status_code, 400)

    def test_patch_unknown_link_returns_400(self):
        res = self.client.patch("/api/links/fantasma", json={"label": "X"})

        self.assertEqual(res.status_code, 400)
        self.assertIn("fantasma", res.json()["detail"])

    def test_delete_link_removes_it(self):
        link_id = self.link_ids[0]
        res = self.client.delete(f"/api/links/{link_id}")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(main.manager.links), 2)
        self.assertEqual(len(self.written_config()["links"]), 2)

    def test_reorder_links_applies_the_new_order(self):
        ids = self.link_ids
        res = self.client.put("/api/links/order", json={"link_ids": [ids[2], ids[1], ids[0]]})

        self.assertEqual(res.status_code, 200)
        self.assertEqual([l["id"] for l in res.json()["links"]], [ids[2], ids[1], ids[0]])

    def test_reorder_with_a_missing_id_returns_400(self):
        res = self.client.put("/api/links/order", json={"link_ids": self.link_ids[:2]})

        self.assertEqual(res.status_code, 400)
        self.assertEqual([l["id"] for l in main.manager.links], self.link_ids)


class PlaylistEndpointTests(ConsoleApiTests):
    def test_set_playlist_limits_what_a_screen_shows(self):
        ids = self.link_ids
        res = self.client.put(
            "/api/chromecasts/cc1/playlist", json={"link_ids": [ids[2], ids[0]]}
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["playlist"], [ids[2], ids[0]])
        self.assertEqual([l["id"] for l in main.manager.links_for("cc1")], [ids[2], ids[0]])
        # La otra pantalla no se toca.
        self.assertEqual([l["id"] for l in main.manager.links_for("cc2")], ids)

    def test_null_playlist_restores_all_links(self):
        self.client.put("/api/chromecasts/cc1/playlist", json={"link_ids": [self.link_ids[0]]})
        res = self.client.put("/api/chromecasts/cc1/playlist", json={"link_ids": None})

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["playlist"])
        self.assertEqual(len(main.manager.links_for("cc1")), 3)

    def test_playlist_for_an_unknown_screen_returns_400(self):
        res = self.client.put("/api/chromecasts/cc9/playlist", json={"link_ids": []})

        self.assertEqual(res.status_code, 400)


class IntervalEndpointTests(ConsoleApiTests):
    def test_interval_is_persisted(self):
        res = self.client.put("/api/config/interval", json={"seconds": 40})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["interval_seconds"], 40)
        self.assertEqual(self.written_config()["default_interval_seconds"], 40)

    def test_interval_below_the_minimum_returns_400(self):
        res = self.client.put("/api/config/interval", json={"seconds": 1})

        self.assertEqual(res.status_code, 400)
        self.assertEqual(main.manager.interval, 15)


class RecoveryEndpointTests(ConsoleApiTests):
    def test_skip_advances_the_current_link(self):
        main.manager.states["cc1"].current_index = 0
        res = self.client.post("/api/chromecasts/cc1/skip", json={"step": 1})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["current_label"], "Iframe")

    def test_skip_backwards_goes_to_the_previous_link(self):
        main.manager.states["cc1"].current_index = 0
        res = self.client.post("/api/chromecasts/cc1/skip", json={"step": -1})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["current_label"], "PRTG")

    def test_skip_on_an_empty_screen_returns_409(self):
        self.client.put("/api/chromecasts/cc1/playlist", json={"link_ids": []})
        res = self.client.post("/api/chromecasts/cc1/skip", json={"step": 1})

        self.assertEqual(res.status_code, 409)

    def test_skip_on_an_unknown_screen_returns_404(self):
        res = self.client.post("/api/chromecasts/cc9/skip", json={"step": 1})

        self.assertEqual(res.status_code, 404)

    def test_relaunch_reports_the_failure_when_the_display_cannot_load(self):
        # Sin Chromecast conectado, _relaunch_display no logra lanzar.
        res = self.client.post("/api/chromecasts/cc1/relaunch")

        self.assertEqual(res.status_code, 502)

    def test_relaunch_succeeds_when_the_display_loads(self):
        with patch.object(main.manager, "launch_display") as launch:
            def mark_launched(cc_id):
                main.manager.states[cc_id].display_launched = True
                return True

            launch.side_effect = mark_launched
            res = self.client.post("/api/chromecasts/cc1/relaunch")

        self.assertEqual(res.status_code, 200)
        launch.assert_called_once_with("cc1")

    def test_relaunch_on_an_unknown_screen_returns_404(self):
        res = self.client.post("/api/chromecasts/cc9/relaunch")

        self.assertEqual(res.status_code, 404)


class RecaptureEndpointTests(ConsoleApiTests):
    def test_recapture_queues_a_screenshot_link(self):
        main.recapture_queue = asyncio.Queue()
        screenshot_id = self.link_ids[0]

        res = self.client.post(f"/api/links/{screenshot_id}/recapture")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(main.recapture_queue.get_nowait(), "https://www.cgiar.org/landing")

    def test_recapture_queues_an_internal_prtg_link(self):
        """Las internas renderizan por iframe pero su GIF alimenta el fallback."""
        main.recapture_queue = asyncio.Queue()

        res = self.client.post(f"/api/links/{self.link_ids[2]}/recapture")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(main.recapture_queue.qsize(), 1)

    def test_recapture_of_an_iframe_only_link_is_rejected(self):
        main.recapture_queue = asyncio.Queue()

        res = self.client.post(f"/api/links/{self.link_ids[1]}/recapture")

        self.assertEqual(res.status_code, 400)
        self.assertIn("iframe", res.json()["detail"])

    def test_recapture_of_an_unknown_link_returns_404(self):
        main.recapture_queue = asyncio.Queue()

        res = self.client.post("/api/links/fantasma/recapture")

        self.assertEqual(res.status_code, 404)

    def test_recapture_before_the_capture_task_exists_returns_503(self):
        main.recapture_queue = None

        res = self.client.post(f"/api/links/{self.link_ids[0]}/recapture")

        self.assertEqual(res.status_code, 503)


class StatusEndpointTests(ConsoleApiTests):
    def test_status_carries_a_diagnosis_per_screen(self):
        res = self.client.get("/api/status")

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["config_revision"], 1)
        for cc in body["chromecasts"]:
            self.assertIn("health", cc)
            self.assertIn(cc["health"]["level"], {"ok", "atencion", "error"})
            self.assertTrue(cc["health"]["summary"])

    def test_current_endpoint_exposes_link_id_and_revision(self):
        res = self.client.get("/api/current/cc1")

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["link_id"], self.link_ids[0])
        self.assertEqual(body["config_revision"], 1)

    def test_display_page_reloads_itself_when_the_revision_changes(self):
        before = self.client.get("/cast/display?cc_id=cc1").text
        self.assertIn("var configRevision = 1;", before)

        self.client.post("/api/links", json={"url": "https://nuevo.example/", "label": "N"})
        after = self.client.get("/cast/display?cc_id=cc1").text

        self.assertIn("var configRevision = 2;", after)
        self.assertIn("window.location.reload()", after)
        self.assertEqual(self.client.get("/api/current/cc1").json()["config_revision"], 2)

    def test_display_page_only_renders_the_links_of_that_screen(self):
        ids = self.link_ids
        self.client.put("/api/chromecasts/cc1/playlist", json={"link_ids": [ids[1]]})

        html = self.client.get("/cast/display?cc_id=cc1").text

        self.assertIn(f'id="frame-{ids[1]}"', html)
        self.assertNotIn(f'id="frame-{ids[0]}"', html)
        # cc2 sigue con todos.
        html_cc2 = self.client.get("/cast/display?cc_id=cc2").text
        self.assertIn(f'id="frame-{ids[0]}"', html_cc2)

    # --- Preview visual de la consola ---

    def test_status_links_carry_how_the_display_page_renders_them(self):
        body = self.client.get("/api/status").json()
        by_label = {link["label"]: link for link in body["links"]}

        # cgiar.org no se deja embeber: la display page usa el GIF capturado.
        self.assertEqual(by_label["Screenshot"]["preview_mode"], "screenshot")
        self.assertIn("/static/screenshots/", by_label["Screenshot"]["preview_src"])
        # Las proxyables se previsualizan con el mismo src que el Chromecast.
        self.assertEqual(by_label["Iframe"]["preview_mode"], "iframe")
        self.assertTrue(by_label["Iframe"]["preview_src"].startswith("/p/"))
        self.assertEqual(by_label["PRTG"]["preview_src"], "/proxy/public/mapshow.htm?id=1")

    def test_capturable_links_still_offer_a_live_preview_when_proxyable(self):
        """PRTG se muestra como captura pero se deja embeber: el zoom se ve moverse."""
        prtg_id = self.link_ids[2]
        self.client.patch(f"/api/links/{prtg_id}", json={"render_mode": "live_screenshot"})

        by_label = {l["label"]: l for l in self.client.get("/api/status").json()["links"]}

        self.assertEqual(by_label["PRTG"]["preview_mode"], "screenshot")
        self.assertEqual(by_label["PRTG"]["live_preview_src"], "/proxy/public/mapshow.htm?id=1")
        # cgiar.org no se deja proxear: ahi el asset es lo unico que hay.
        self.assertIsNone(by_label["Screenshot"].get("live_preview_src"))

    def test_status_does_not_contaminate_the_links_that_go_to_config_json(self):
        self.client.get("/api/status")

        for link in main.manager.links:
            self.assertNotIn("preview_src", link)
            self.assertNotIn("preview_mode", link)

    def test_status_exposes_screen_resolution_for_the_preview(self):
        body = self.client.get("/api/status").json()

        for cc in body["chromecasts"]:
            self.assertEqual(len(cc["resolution"]), 2)
            self.assertIn("seconds_on_current", cc)

    def test_console_mirror_does_not_fake_the_watchdog_heartbeat(self):
        state = main.manager.states["cc1"]
        state.last_heartbeat_monotonic = None

        self.client.get("/api/current/cc1?preview=1")
        self.assertIsNone(
            state.last_heartbeat_monotonic,
            "el espejo de la consola no puede hacer pasar por viva una pantalla muerta",
        )

        # El poll real de la display page si cuenta.
        self.client.get("/api/current/cc1")
        self.assertIsNotNone(state.last_heartbeat_monotonic)

    def test_display_page_polls_as_preview_only_when_asked(self):
        mirror = self.client.get("/cast/display?cc_id=cc1&preview=1").text
        self.assertIn('var currentQuery = "?preview=1";', mirror)
        self.assertIn("window.parent.postMessage", mirror)

        real = self.client.get("/cast/display?cc_id=cc1").text
        self.assertIn('var currentQuery = "";', real)


if __name__ == "__main__":
    unittest.main()

"""Playlists por pantalla y mutaciones de config desde la consola de tecnicos."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quiosco import config_store
from quiosco.cast_manager import CastManager


def write_config(config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "chromecasts": [
                    {
                        "id": "cc1",
                        "name": "CC Uno",
                        "host": "127.0.0.1",
                        "port": 8009,
                        "uuid": "00000000-0000-0000-0000-000000000001",
                    },
                    {
                        "id": "cc2",
                        "name": "CC Dos",
                        "host": "127.0.0.2",
                        "port": 8009,
                        "uuid": "00000000-0000-0000-0000-000000000002",
                    },
                ],
                "links": [
                    {"url": "https://a.example/", "label": "A"},
                    {"url": "https://b.example/", "label": "B"},
                    {"url": "https://c.example/", "label": "C"},
                ],
                "default_interval_seconds": 15,
                "proxy_auto_subnet": "172.25.",
            }
        ),
        encoding="utf-8",
    )


class ConsoleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        write_config(self.config_path)
        self.manager = CastManager(config_path=str(self.config_path))

    @property
    def link_ids(self) -> list[str]:
        return [link["id"] for link in self.manager.links]

    def written_config(self) -> dict:
        return json.loads(self.config_path.read_text())

    def labels_for(self, cc_id: str) -> list[str]:
        return [link["label"] for link in self.manager.links_for(cc_id)]


class PlaylistResolutionTests(ConsoleTestCase):
    def test_without_playlist_every_screen_rotates_all_links(self):
        self.assertEqual(self.labels_for("cc1"), ["A", "B", "C"])
        self.assertEqual(self.labels_for("cc2"), ["A", "B", "C"])

    def test_playlist_gives_each_screen_its_own_selection_and_order(self):
        ids = self.link_ids
        self.manager.set_playlist("cc1", [ids[2], ids[0]])
        self.manager.set_playlist("cc2", [ids[1]])

        self.assertEqual(self.labels_for("cc1"), ["C", "A"])
        self.assertEqual(self.labels_for("cc2"), ["B"])

    def test_disabled_link_disappears_from_every_screen(self):
        self.manager.set_link_enabled(self.link_ids[1], False)

        self.assertEqual(self.labels_for("cc1"), ["A", "C"])
        self.assertEqual(self.labels_for("cc2"), ["A", "C"])

    def test_disabled_link_is_skipped_even_inside_a_playlist(self):
        ids = self.link_ids
        self.manager.set_playlist("cc1", ids)
        self.manager.set_link_enabled(ids[0], False)

        self.assertEqual(self.labels_for("cc1"), ["B", "C"])

    def test_clearing_a_playlist_restores_all_links(self):
        self.manager.set_playlist("cc1", [self.link_ids[0]])
        self.assertEqual(self.labels_for("cc1"), ["A"])

        self.manager.set_playlist("cc1", None)
        self.assertEqual(self.labels_for("cc1"), ["A", "B", "C"])
        self.assertNotIn("playlist", self.written_config()["chromecasts"][0])

    def test_playlist_for_unknown_screen_is_rejected(self):
        with self.assertRaises(config_store.ConfigError):
            self.manager.set_playlist("cc9", [])

    def test_current_index_is_relative_to_each_screen_playlist(self):
        ids = self.link_ids
        self.manager.set_playlist("cc1", [ids[2]])
        cc1, cc2 = self.manager.states["cc1"], self.manager.states["cc2"]

        cc1.current_index = 0
        cc2.current_index = 0

        self.assertEqual(self.manager._current_link(cc1)["label"], "C")
        self.assertEqual(self.manager._current_link(cc2)["label"], "A")


class LinkMutationTests(ConsoleTestCase):
    def test_add_link_persists_and_bumps_revision(self):
        before = self.manager.config_revision
        added = self.manager.add_link({"url": "https://d.example/", "label": "D", "zoom": 0.8})

        self.assertEqual(self.manager.config_revision, before + 1)
        self.assertEqual(self.labels_for("cc1"), ["A", "B", "C", "D"])
        written = self.written_config()["links"][-1]
        self.assertEqual(written["label"], "D")
        self.assertEqual(written["zoom"], 0.8)
        self.assertEqual(written["id"], added["id"])

    def test_add_link_rejects_a_bad_url_without_touching_the_file(self):
        before = self.config_path.read_text()
        with self.assertRaises(config_store.ConfigError):
            self.manager.add_link({"url": "javascript:alert(1)", "label": "Malo"})
        self.assertEqual(self.config_path.read_text(), before)
        self.assertEqual(self.manager.config_revision, 1)

    def test_add_link_ignores_a_client_supplied_id(self):
        added = self.manager.add_link(
            {"id": self.link_ids[0], "url": "https://d.example/", "label": "D"}
        )
        self.assertNotEqual(added["id"], self.link_ids[0])
        self.assertEqual(len({link["id"] for link in self.manager.links}), 4)

    def test_update_link_changes_label_and_zoom(self):
        link_id = self.link_ids[1]
        self.manager.update_link(link_id, {"label": "B renombrado", "zoom": 0.5})

        written = next(l for l in self.written_config()["links"] if l["id"] == link_id)
        self.assertEqual(written["label"], "B renombrado")
        self.assertEqual(written["zoom"], 0.5)

    def test_update_link_can_turn_optional_and_direct_off(self):
        link_id = self.link_ids[0]
        self.manager.update_link(link_id, {"optional": True, "direct": True})
        self.assertTrue(self.manager.links[0]["optional"])

        self.manager.update_link(link_id, {"optional": False, "direct": False})
        self.assertNotIn("optional", self.manager.links[0])
        self.assertNotIn("direct", self.manager.links[0])

    def test_update_link_keeps_the_id_and_the_position(self):
        link_id = self.link_ids[1]
        self.manager.update_link(link_id, {"label": "Sigue segundo"})

        self.assertEqual(self.link_ids[1], link_id)
        self.assertEqual(self.manager.links[1]["label"], "Sigue segundo")

    def test_update_unknown_link_is_rejected(self):
        with self.assertRaises(config_store.ConfigError):
            self.manager.update_link("fantasma", {"label": "X"})

    def test_delete_link_also_removes_it_from_playlists(self):
        ids = self.link_ids
        self.manager.set_playlist("cc1", [ids[0], ids[1]])
        self.manager.delete_link(ids[0])

        self.assertEqual(self.labels_for("cc1"), ["B"])
        self.assertEqual(self.written_config()["chromecasts"][0]["playlist"], [ids[1]])

    def test_delete_unknown_link_is_rejected(self):
        with self.assertRaises(config_store.ConfigError):
            self.manager.delete_link("fantasma")

    def test_reorder_links_rewrites_the_order(self):
        ids = self.link_ids
        self.manager.reorder_links([ids[2], ids[0], ids[1]])

        self.assertEqual([l["label"] for l in self.manager.links], ["C", "A", "B"])
        self.assertEqual([l["id"] for l in self.written_config()["links"]], [ids[2], ids[0], ids[1]])

    def test_reorder_rejects_an_incomplete_list(self):
        with self.assertRaises(config_store.ConfigError):
            self.manager.reorder_links(self.link_ids[:2])

    def test_reorder_rejects_a_duplicated_id(self):
        ids = self.link_ids
        with self.assertRaises(config_store.ConfigError):
            self.manager.reorder_links([ids[0], ids[0], ids[1]])

    def test_ids_survive_a_reorder_so_playlists_stay_valid(self):
        ids = self.link_ids
        self.manager.set_playlist("cc2", [ids[0]])
        self.manager.reorder_links([ids[2], ids[1], ids[0]])

        self.assertEqual(self.labels_for("cc2"), ["A"])


class IntervalPersistenceTests(ConsoleTestCase):
    def test_set_interval_is_written_to_config(self):
        self.manager.set_interval(45)

        self.assertEqual(self.manager.interval, 45)
        self.assertEqual(self.written_config()["default_interval_seconds"], 45)

    def test_set_interval_survives_a_restart(self):
        self.manager.set_interval(90)
        reloaded = CastManager(config_path=str(self.config_path))

        self.assertEqual(reloaded.interval, 90)

    def test_set_interval_rejects_below_the_minimum(self):
        with self.assertRaises(config_store.ConfigError):
            self.manager.set_interval(2)
        self.assertEqual(self.manager.interval, 15)

    def test_set_interval_without_persist_stays_in_memory(self):
        self.manager.set_interval(60, persist=False)

        self.assertEqual(self.manager.interval, 60)
        self.assertEqual(self.written_config()["default_interval_seconds"], 15)


class RuntimeStateSeparationTests(ConsoleTestCase):
    def test_a_rediscovered_ip_is_never_written_into_config(self):
        """CLAUDE.md: las IPs descubiertas van a runtime-state, no a config.json."""
        state = self.manager.states["cc1"]
        state.host = "10.0.0.55"
        state.port = 9009

        self.manager.set_interval(30)

        written = self.written_config()["chromecasts"][0]
        self.assertEqual(written["host"], "127.0.0.1")
        self.assertEqual(written["port"], 8009)

    def test_unmanaged_config_keys_are_preserved_on_save(self):
        self.manager.add_link({"url": "https://d.example/", "label": "D"})

        self.assertEqual(self.written_config()["proxy_auto_subnet"], "172.25.")


class IndexPreservationTests(ConsoleTestCase):
    def test_editing_another_link_does_not_move_the_screen(self):
        state = self.manager.states["cc1"]
        state.current_index = 1  # mostrando B

        self.manager.update_link(self.link_ids[2], {"label": "C renombrado"})

        self.assertEqual(self.manager._current_link(state)["label"], "B")

    def test_reordering_follows_the_link_the_screen_was_showing(self):
        ids = self.link_ids
        state = self.manager.states["cc1"]
        state.current_index = 1  # mostrando B

        self.manager.reorder_links([ids[1], ids[2], ids[0]])

        self.assertEqual(state.current_index, 0)
        self.assertEqual(self.manager._current_link(state)["label"], "B")

    def test_deleting_the_visible_link_keeps_the_index_in_range(self):
        state = self.manager.states["cc1"]
        state.current_index = 2  # mostrando C

        self.manager.delete_link(self.link_ids[2])

        self.assertLess(state.current_index, len(self.manager.links_for("cc1")))
        self.assertIsNotNone(self.manager._current_link(state))

    def test_emptying_a_playlist_leaves_the_screen_without_a_link(self):
        state = self.manager.states["cc1"]
        state.current_index = 2

        self.manager.set_playlist("cc1", [])

        self.assertEqual(state.current_index, 0)
        self.assertIsNone(self.manager._current_link(state))
        self.assertIsNone(state.current_url)


class AdvanceAndSkipTests(ConsoleTestCase):
    def test_advance_wraps_forward(self):
        state = self.manager.states["cc1"]
        state.current_index = 2
        self.manager._advance_index(state)

        self.assertEqual(state.current_index, 0)

    def test_advance_backwards_wraps_to_the_end(self):
        state = self.manager.states["cc1"]
        state.current_index = 0
        self.manager._advance_index(state, -1)

        self.assertEqual(state.current_index, 2)

    def test_advance_on_an_empty_screen_does_not_divide_by_zero(self):
        self.manager.set_playlist("cc1", [])
        state = self.manager.states["cc1"]
        state.current_index = 5

        self.manager._advance_index(state)

        self.assertEqual(state.current_index, 0)

    def test_skip_moves_to_the_next_link(self):
        state = self.manager.states["cc1"]
        state.current_index = 0

        self.assertTrue(self.manager.skip("cc1"))
        self.assertEqual(state.current_label, "B")

    def test_skip_backwards_moves_to_the_previous_link(self):
        state = self.manager.states["cc1"]
        state.current_index = 0

        self.assertTrue(self.manager.skip("cc1", -1))
        self.assertEqual(state.current_label, "C")

    def test_skip_on_an_empty_screen_reports_failure(self):
        self.manager.set_playlist("cc1", [])

        self.assertFalse(self.manager.skip("cc1"))

    def test_skip_recasts_the_media_when_in_fallback_mode(self):
        state = self.manager.states["cc1"]
        state.fallback_active = True

        with patch.object(self.manager, "cast_fallback_media", return_value=True) as cast:
            self.manager.skip("cc1")

        cast.assert_called_once_with("cc1")


class StatusPayloadTests(ConsoleTestCase):
    def test_status_exposes_revision_and_per_screen_playlists(self):
        ids = self.link_ids
        self.manager.set_playlist("cc1", [ids[1]])
        status = self.manager.get_status()

        self.assertEqual(status["config_revision"], self.manager.config_revision)
        cc1 = next(cc for cc in status["chromecasts"] if cc["id"] == "cc1")
        cc2 = next(cc for cc in status["chromecasts"] if cc["id"] == "cc2")
        self.assertEqual(cc1["playlist"], [ids[1]])
        self.assertEqual(cc1["playlist_link_ids"], [ids[1]])
        self.assertIsNone(cc2["playlist"])
        self.assertEqual(cc2["playlist_link_ids"], ids)

    def test_status_links_carry_ids_and_enabled(self):
        status = self.manager.get_status()

        for link in status["links"]:
            self.assertIn("id", link)
            self.assertIn("enabled", link)


class RotationWithEmptyPlaylistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        write_config(self.config_path)
        # El loop duerme self.interval, asi que hay que bajar el minimo para no
        # esperar 15s reales; la validacion de produccion queda intacta.
        min_patch = patch.object(config_store, "MIN_INTERVAL_SECONDS", 0.001)
        min_patch.start()
        self.addCleanup(min_patch.stop)
        self.manager = CastManager(config_path=str(self.config_path))
        self.manager.interval = 0.01

    async def test_rotation_survives_a_screen_left_without_links(self):
        """Vaciar la playlist en caliente no debe matar la rotacion: al
        rehabilitar un link se reanuda sola, sin que el tecnico toque Iniciar."""
        import asyncio

        state = self.manager.states["cc1"]
        state.rotating = True

        with patch.object(self.manager, "_relaunch_display"):
            task = asyncio.create_task(self.manager._rotation_loop("cc1"))
            await asyncio.sleep(0.05)
            self.assertIsNotNone(state.current_label)

            self.manager.set_playlist("cc1", [])
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            self.assertIsNone(state.current_label)

            self.manager.set_playlist("cc1", None)
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            self.assertIsNotNone(state.current_label)

        state.rotating = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_rotation_is_not_started_on_a_screen_without_links(self):
        state = self.manager.states["cc1"]
        self.manager.set_playlist("cc1", [])
        state.rotating = True

        await self.manager._rotation_loop("cc1")

        self.assertFalse(state.rotating)


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quiosco import config_store


def sample_config() -> dict:
    return {
        "chromecasts": [
            {"id": "cc1", "name": "CC Uno", "host": "127.0.0.1", "port": 8009},
            {"id": "cc2", "name": "CC Dos", "host": "127.0.0.2", "port": 8009},
        ],
        "links": [
            {"url": "https://a.example/", "label": "A", "zoom": 1.0},
            {"url": "https://b.example/", "label": "B", "zoom": 0.6},
        ],
        "default_interval_seconds": 15,
    }


class LinkIdTests(unittest.TestCase):
    def test_id_is_derived_from_url_and_stable(self):
        self.assertEqual(
            config_store.link_id_for("https://a.example/"),
            config_store.link_id_for("https://a.example/"),
        )

    def test_different_urls_get_different_ids(self):
        self.assertNotEqual(
            config_store.link_id_for("https://a.example/"),
            config_store.link_id_for("https://b.example/"),
        )

    def test_ids_survive_reordering_and_deletion(self):
        links = config_store.normalize_links(sample_config()["links"])
        original = {link["label"]: link["id"] for link in links}

        reordered = config_store.normalize_links(list(reversed(sample_config()["links"])))
        after_delete = config_store.normalize_links(sample_config()["links"][1:])

        self.assertEqual({l["label"]: l["id"] for l in reordered}, original)
        self.assertEqual(after_delete[0]["id"], original["B"])

    def test_duplicate_urls_get_distinct_ids(self):
        links = config_store.normalize_links(
            [
                {"url": "https://a.example/", "label": "Primero"},
                {"url": "https://a.example/", "label": "Segundo"},
            ]
        )
        self.assertNotEqual(links[0]["id"], links[1]["id"])
        self.assertTrue(links[1]["id"].endswith("-2"))

    def test_explicit_id_is_preserved(self):
        links = config_store.normalize_links(
            [{"id": "manual", "url": "https://a.example/", "label": "A"}]
        )
        self.assertEqual(links[0]["id"], "manual")


class NormalizeTests(unittest.TestCase):
    def test_enabled_defaults_to_true(self):
        link = config_store.normalize_link({"url": "https://a.example/"})
        self.assertTrue(link["enabled"])

    def test_label_falls_back_to_url(self):
        link = config_store.normalize_link({"url": "https://a.example/"})
        self.assertEqual(link["label"], "https://a.example/")

    def test_optional_and_direct_omitted_when_false(self):
        link = config_store.normalize_link({"url": "https://a.example/", "label": "A"})
        self.assertNotIn("optional", link)
        self.assertNotIn("direct", link)

    def test_optional_and_direct_kept_when_true(self):
        link = config_store.normalize_link(
            {"url": "https://a.example/", "label": "A", "optional": True, "direct": True}
        )
        self.assertTrue(link["optional"])
        self.assertTrue(link["direct"])

    def test_rejects_non_http_scheme(self):
        with self.assertRaises(config_store.ConfigError):
            config_store.normalize_link({"url": "file:///etc/passwd"})

    def test_rejects_url_without_host(self):
        with self.assertRaises(config_store.ConfigError):
            config_store.normalize_link({"url": "https://"})

    def test_rejects_empty_url(self):
        with self.assertRaises(config_store.ConfigError):
            config_store.normalize_link({"url": "   "})

    def test_rejects_out_of_range_zoom(self):
        with self.assertRaises(config_store.ConfigError):
            config_store.normalize_link({"url": "https://a.example/", "zoom": 99})

    def test_rejects_non_numeric_zoom(self):
        with self.assertRaises(config_store.ConfigError):
            config_store.normalize_link({"url": "https://a.example/", "zoom": "grande"})

    def test_rejects_interval_below_minimum(self):
        cfg = sample_config()
        cfg["default_interval_seconds"] = 2
        with self.assertRaises(config_store.ConfigError):
            config_store.normalize(cfg)

    def test_absent_playlist_is_not_materialized(self):
        cfg = config_store.normalize(sample_config())
        self.assertNotIn("playlist", cfg["chromecasts"][0])

    def test_playlist_drops_unknown_and_duplicate_ids(self):
        cfg = sample_config()
        links = config_store.normalize_links(cfg["links"])
        cfg["chromecasts"][0]["playlist"] = [
            links[1]["id"],
            "no-existe",
            links[1]["id"],
            links[0]["id"],
        ]
        normalized = config_store.normalize(cfg)
        self.assertEqual(
            normalized["chromecasts"][0]["playlist"], [links[1]["id"], links[0]["id"]]
        )

    def test_playlist_must_be_a_list(self):
        cfg = sample_config()
        cfg["chromecasts"][0]["playlist"] = "todo"
        with self.assertRaises(config_store.ConfigError):
            config_store.normalize(cfg)


class ResolvePlaylistTests(unittest.TestCase):
    def setUp(self):
        self.links = config_store.normalize_links(
            [
                {"url": "https://a.example/", "label": "A"},
                {"url": "https://b.example/", "label": "B"},
                {"url": "https://c.example/", "label": "C", "enabled": False},
            ]
        )

    def test_none_playlist_returns_all_enabled_links(self):
        resolved = config_store.resolve_playlist(self.links, None)
        self.assertEqual([l["label"] for l in resolved], ["A", "B"])

    def test_playlist_controls_order(self):
        ids = [self.links[1]["id"], self.links[0]["id"]]
        resolved = config_store.resolve_playlist(self.links, ids)
        self.assertEqual([l["label"] for l in resolved], ["B", "A"])

    def test_disabled_link_is_excluded_even_if_in_playlist(self):
        ids = [l["id"] for l in self.links]
        resolved = config_store.resolve_playlist(self.links, ids)
        self.assertEqual([l["label"] for l in resolved], ["A", "B"])

    def test_unknown_id_in_playlist_is_ignored(self):
        resolved = config_store.resolve_playlist(self.links, ["fantasma", self.links[0]["id"]])
        self.assertEqual([l["label"] for l in resolved], ["A"])

    def test_empty_playlist_resolves_to_nothing(self):
        self.assertEqual(config_store.resolve_playlist(self.links, []), [])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(sample_config()))
        self.backup_dir = self.root / "data" / config_store.BACKUP_DIRNAME

    def test_save_writes_normalized_config_with_ids(self):
        cfg = config_store.load(self.config_path)
        cfg["links"][0]["label"] = "Renombrado"
        config_store.save(self.config_path, cfg, backup_dir=self.backup_dir)

        written = json.loads(self.config_path.read_text())
        self.assertEqual(written["links"][0]["label"], "Renombrado")
        self.assertTrue(all("id" in link for link in written["links"]))

    def test_save_keeps_a_backup_of_the_previous_content(self):
        before = self.config_path.read_text()
        cfg = config_store.load(self.config_path)
        cfg["links"] = cfg["links"][:1]
        config_store.save(
            self.config_path, cfg, backup_dir=self.backup_dir, timestamp="20260820-120000"
        )

        backup = self.backup_dir / "config-20260820-120000.json"
        self.assertEqual(backup.read_text(), before)
        self.assertEqual(len(json.loads(self.config_path.read_text())["links"]), 1)

    def test_backups_are_pruned_to_the_retention_limit(self):
        self.backup_dir.mkdir(parents=True)
        for i in range(config_store.BACKUP_RETENTION + 5):
            (self.backup_dir / f"config-2026010{i:02d}-000000.json").write_text("{}")
        cfg = config_store.load(self.config_path)
        config_store.save(
            self.config_path, cfg, backup_dir=self.backup_dir, timestamp="20260820-120000"
        )
        self.assertEqual(
            len(list(self.backup_dir.glob("config-*.json"))), config_store.BACKUP_RETENTION
        )

    def test_save_rejects_invalid_config_without_touching_the_file(self):
        before = self.config_path.read_text()
        cfg = config_store.load(self.config_path)
        cfg["links"].append({"url": "ftp://nope/"})
        with self.assertRaises(config_store.ConfigError):
            config_store.save(self.config_path, cfg, backup_dir=self.backup_dir)
        self.assertEqual(self.config_path.read_text(), before)

    def test_falls_back_to_in_place_write_when_replace_is_unavailable(self):
        """En produccion config.json es un bind-mount: os.replace da EBUSY."""
        cfg = config_store.load(self.config_path)
        cfg["links"][0]["label"] = "Via in place"
        original_inode = self.config_path.stat().st_ino

        with patch(
            "quiosco.config_store.os.replace",
            side_effect=OSError(16, "Device or resource busy"),
        ):
            config_store.save(self.config_path, cfg, backup_dir=self.backup_dir)

        written = json.loads(self.config_path.read_text())
        self.assertEqual(written["links"][0]["label"], "Via in place")
        self.assertEqual(self.config_path.stat().st_ino, original_inode)

    def test_no_temp_files_are_left_behind(self):
        cfg = config_store.load(self.config_path)
        with patch(
            "quiosco.config_store.os.replace",
            side_effect=OSError(16, "Device or resource busy"),
        ):
            config_store.save(self.config_path, cfg, backup_dir=self.backup_dir)
        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith(".config-")]
        self.assertEqual(leftovers, [])

    def test_backup_failure_does_not_block_the_change(self):
        cfg = config_store.load(self.config_path)
        cfg["links"][0]["label"] = "Sin backup"
        with patch.object(Path, "mkdir", side_effect=OSError("read-only fs")):
            config_store.save(self.config_path, cfg, backup_dir=self.backup_dir)
        self.assertEqual(
            json.loads(self.config_path.read_text())["links"][0]["label"], "Sin backup"
        )


if __name__ == "__main__":
    unittest.main()

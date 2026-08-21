"""Diagnostico legible por pantalla."""

import unittest

from quiosco import health
from quiosco.cast_manager import DISPLAY_HEARTBEAT_TIMEOUT_SECONDS


def screen(**overrides) -> dict:
    """Pantalla sana rotando, con los campos que expone get_status()."""
    base = {
        "id": "cc1",
        "name": "CC Uno",
        "connected": True,
        "rotating": True,
        "display_launched": True,
        "display_ready": True,
        "fallback_active": False,
        "dashcast_failures": 0,
        "heartbeat_age_seconds": 2.0,
        "last_error": None,
        "reconnect_attempts": 0,
        "playlist_link_ids": ["a", "b", "c"],
    }
    base.update(overrides)
    return base


class DiagnoseTests(unittest.TestCase):
    def test_healthy_rotation_reports_ok_with_the_link_count_and_interval(self):
        result = health.diagnose(screen(), 30)

        self.assertEqual(result["level"], health.LEVEL_OK)
        self.assertIn("3 links", result["summary"])
        self.assertIn("30s", result["summary"])

    def test_interval_with_no_decimals_is_shown_as_an_integer(self):
        result = health.diagnose(screen(), 30.0)

        self.assertIn("cada 30s", result["summary"])

    def test_single_link_is_not_pluralized(self):
        result = health.diagnose(screen(playlist_link_ids=["a"]), 15)

        self.assertIn("1 link ", result["summary"])

    def test_disconnected_screen_is_an_error_and_surfaces_the_cause(self):
        result = health.diagnose(
            screen(connected=False, last_error="Socket desconectado"), 15
        )

        self.assertEqual(result["level"], health.LEVEL_ERROR)
        self.assertIn("Sin conexion", result["summary"])
        self.assertEqual(result["cause"], "Socket desconectado")
        self.assertTrue(result["action"])

    def test_disconnected_without_an_error_still_explains_something(self):
        result = health.diagnose(screen(connected=False, last_error=None), 15)

        self.assertTrue(result["cause"])

    def test_fallback_mode_explains_that_gifs_are_being_cast(self):
        result = health.diagnose(screen(fallback_active=True, dashcast_failures=3), 15)

        self.assertEqual(result["level"], health.LEVEL_ERROR)
        self.assertIn("degradado", result["summary"])
        self.assertIn("3 chequeos", result["cause"])
        self.assertIn("PROXY_BASE", result["action"])

    def test_stale_heartbeat_points_at_the_stuck_dashcast_logo(self):
        result = health.diagnose(
            screen(heartbeat_age_seconds=DISPLAY_HEARTBEAT_TIMEOUT_SECONDS + 30), 15
        )

        self.assertEqual(result["level"], health.LEVEL_ERROR)
        self.assertIn("no carga", result["summary"])
        self.assertIn("90s", result["cause"])
        self.assertIn("PROXY_BASE", result["action"])

    def test_missing_heartbeat_is_reported_as_no_beat_at_all(self):
        result = health.diagnose(screen(heartbeat_age_seconds=None), 15)

        self.assertEqual(result["level"], health.LEVEL_ERROR)
        self.assertIn("ningun latido", result["cause"])

    def test_a_screen_that_never_launched_a_display_is_not_blamed_on_heartbeat(self):
        result = health.diagnose(
            screen(display_launched=False, rotating=False, heartbeat_age_seconds=None), 15
        )

        self.assertEqual(result["level"], health.LEVEL_WARN)
        self.assertIn("rotacion detenida", result["summary"])

    def test_screen_without_links_says_so(self):
        result = health.diagnose(screen(playlist_link_ids=[]), 15)

        self.assertEqual(result["level"], health.LEVEL_WARN)
        self.assertIn("sin links", result["summary"])
        self.assertIn("playlist", result["action"])

    def test_stopped_rotation_is_a_warning_not_an_error(self):
        result = health.diagnose(screen(rotating=False, display_launched=False), 15)

        self.assertEqual(result["level"], health.LEVEL_WARN)
        self.assertIn("3 links", result["action"])

    def test_disconnection_outranks_every_other_symptom(self):
        result = health.diagnose(
            screen(connected=False, fallback_active=True, playlist_link_ids=[]), 15
        )

        self.assertIn("Sin conexion", result["summary"])

    def test_fallback_outranks_a_stale_heartbeat(self):
        result = health.diagnose(
            screen(fallback_active=True, heartbeat_age_seconds=None), 15
        )

        self.assertIn("degradado", result["summary"])

    def test_every_diagnosis_has_the_four_expected_keys(self):
        cases = [
            screen(),
            screen(connected=False),
            screen(fallback_active=True),
            screen(heartbeat_age_seconds=None),
            screen(playlist_link_ids=[]),
            screen(rotating=False, display_launched=False),
        ]
        for case in cases:
            with self.subTest(case=case):
                result = health.diagnose(case, 15)
                self.assertEqual(set(result), {"level", "summary", "cause", "action"})


if __name__ == "__main__":
    unittest.main()

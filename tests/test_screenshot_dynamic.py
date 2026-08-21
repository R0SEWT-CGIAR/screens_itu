"""Re-siembra dinamica del ciclo de captura y recaptura on-demand."""

import asyncio
import unittest
from unittest.mock import patch

from quiosco import screenshot


class ScreenshotLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.captured: list[tuple[str, int, int]] = []

        async def fake_take_gif(url, output_path, **kwargs):
            self.captured.append((url, kwargs.get("viewport_width"), kwargs.get("viewport_height")))
            return True

        patcher = patch.object(screenshot, "take_gif", side_effect=fake_take_gif)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _run_briefly(self, coro_factory, seconds=0.08):
        task = asyncio.create_task(coro_factory())
        await asyncio.sleep(seconds)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return task

    async def test_static_url_list_is_captured(self):
        await self._run_briefly(
            lambda: screenshot.screenshot_loop(
                ["https://a.example/"], interval_seconds=10, gif_duration_seconds=0
            )
        )

        self.assertIn("https://a.example/", [c[0] for c in self.captured])

    async def test_link_source_is_re_resolved_every_cycle(self):
        """Un link agregado desde la consola debe entrar sin reiniciar el servicio."""
        targets = {"urls": ["https://a.example/"], "viewports": {}}

        def link_source():
            return targets["urls"], targets["viewports"]

        task = asyncio.create_task(
            screenshot.screenshot_loop(
                [], interval_seconds=0.01, gif_duration_seconds=0, link_source=link_source
            )
        )
        await asyncio.sleep(0.05)
        self.assertEqual({c[0] for c in self.captured}, {"https://a.example/"})

        targets["urls"] = ["https://a.example/", "https://nuevo.example/"]
        await asyncio.sleep(0.05)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIn("https://nuevo.example/", [c[0] for c in self.captured])

    async def test_link_source_viewports_are_applied(self):
        def link_source():
            return ["https://a.example/"], {"https://a.example/": (2560, 1440)}

        await self._run_briefly(
            lambda: screenshot.screenshot_loop(
                [], interval_seconds=10, gif_duration_seconds=0, link_source=link_source
            )
        )

        self.assertEqual(self.captured[0], ("https://a.example/", 2560, 1440))

    async def test_a_url_dropped_from_the_source_stops_being_captured(self):
        targets = {"urls": ["https://a.example/", "https://b.example/"]}

        def link_source():
            return targets["urls"], {}

        task = asyncio.create_task(
            screenshot.screenshot_loop(
                [], interval_seconds=0.01, gif_duration_seconds=0, link_source=link_source
            )
        )
        await asyncio.sleep(0.04)
        targets["urls"] = ["https://a.example/"]
        self.captured.clear()
        await asyncio.sleep(0.05)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn("https://b.example/", [c[0] for c in self.captured])


class RecaptureQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.captured: list[str] = []

        async def fake_take_gif(url, output_path, **kwargs):
            self.captured.append(url)
            return True

        patcher = patch.object(screenshot, "take_gif", side_effect=fake_take_gif)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_queued_url_is_captured_without_waiting_the_interval(self):
        queue: asyncio.Queue[str] = asyncio.Queue()
        task = asyncio.create_task(
            screenshot.screenshot_loop(
                [], interval_seconds=600, gif_duration_seconds=0, recapture_queue=queue
            )
        )
        await asyncio.sleep(0.02)
        self.assertEqual(self.captured, [])

        queue.put_nowait("https://recapturar.example/")
        await asyncio.sleep(0.05)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.captured, ["https://recapturar.example/"])

    async def test_several_queued_requests_are_all_served(self):
        queue: asyncio.Queue[str] = asyncio.Queue()
        task = asyncio.create_task(
            screenshot.screenshot_loop(
                [], interval_seconds=600, gif_duration_seconds=0, recapture_queue=queue
            )
        )
        await asyncio.sleep(0.02)
        queue.put_nowait("https://uno.example/")
        queue.put_nowait("https://dos.example/")
        await asyncio.sleep(0.06)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.captured, ["https://uno.example/", "https://dos.example/"])

    async def test_the_interval_still_elapses_when_nothing_is_queued(self):
        queue: asyncio.Queue[str] = asyncio.Queue()
        task = asyncio.create_task(
            screenshot.screenshot_loop(
                ["https://a.example/"],
                interval_seconds=0.02,
                gif_duration_seconds=0,
                recapture_queue=queue,
            )
        )
        await asyncio.sleep(0.09)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertGreater(len(self.captured), 1)


if __name__ == "__main__":
    unittest.main()

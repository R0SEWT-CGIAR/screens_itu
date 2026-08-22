import asyncio
import tempfile
import tomllib
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from quiosco import screenshot


def png_bytes(size=(2, 2)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


class FakePage:
    def __init__(self, *, goto_error=None):
        self.goto_error = goto_error
        self.goto_calls = 0
        self.screenshot_calls = 0

    async def goto(self, *args, **kwargs):
        self.goto_calls += 1
        if self.goto_error:
            raise self.goto_error

    async def screenshot(self, *args, **kwargs):
        self.screenshot_calls += 1
        return png_bytes()

    async def wait_for_timeout(self, *args, **kwargs):
        return None


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, contexts=None):
        self.contexts = list(contexts or [])
        self.new_context_calls = []
        self.closed = False

    async def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        return self.contexts.pop(0)

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.launch_count = 0

    async def launch(self):
        self.launch_count += 1
        return self.browser


class FakePlaywrightContextManager:
    def __init__(self, chromium):
        self.playwright = SimpleNamespace(chromium=chromium)
        self.exited = False

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


class ScreenshotCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_writes_gif_and_closes_context(self):
        context = FakeContext(FakePage())
        browser = FakeBrowser([context])

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "capture.gif"
            with (
                patch.object(screenshot, "accept_cookie_banner", new=AsyncMock(return_value=False)),
                patch.object(screenshot.asyncio, "sleep", new=AsyncMock()),
            ):
                captured = await screenshot._take_gif_with_browser(
                    browser,
                    "https://example.com",
                    output_path,
                    duration_seconds=1,
                    viewport_width=2,
                    viewport_height=2,
                    output_width=2,
                    output_height=2,
                )

            self.assertTrue(captured)
            self.assertTrue(output_path.exists())
            self.assertTrue(context.closed)
            self.assertEqual(
                browser.new_context_calls,
                [{"viewport": {"width": 2, "height": 2}, "ignore_https_errors": True}],
            )

    async def test_capture_closes_context_when_navigation_fails(self):
        context = FakeContext(FakePage(goto_error=RuntimeError("navigation failed")))
        browser = FakeBrowser([context])

        captured = await screenshot._take_gif_with_browser(
            browser,
            "https://example.com",
            Path("unused.gif"),
        )

        self.assertFalse(captured)
        self.assertTrue(context.closed)

    async def test_take_gif_closes_browser_when_capture_raises(self):
        browser = FakeBrowser()
        chromium = FakeChromium(browser)
        playwright_context = FakePlaywrightContextManager(chromium)

        with (
            patch.object(screenshot, "async_playwright", return_value=playwright_context),
            patch.object(
                screenshot,
                "_take_gif_with_browser",
                new=AsyncMock(side_effect=RuntimeError("capture failed")),
            ),
        ):
            captured = await screenshot.take_gif(
                "https://example.com",
                Path("unused.gif"),
            )

        self.assertFalse(captured)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright_context.exited)

    async def test_loop_reuses_browser_for_all_urls_and_closes_on_cancellation(self):
        browser = FakeBrowser()
        chromium = FakeChromium(browser)
        playwright_context = FakePlaywrightContextManager(chromium)
        capture = AsyncMock(return_value=True)

        with (
            patch.object(screenshot, "async_playwright", return_value=playwright_context),
            patch.object(screenshot, "_take_gif_with_browser", new=capture),
            patch.object(
                screenshot.asyncio,
                "sleep",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await screenshot.screenshot_loop(
                    ["https://a.example", "https://b.example"],
                    interval_seconds=1,
                    gif_duration_seconds=1,
                )

        self.assertEqual(chromium.launch_count, 1)
        self.assertEqual(capture.await_count, 2)
        self.assertIs(capture.await_args_list[0].args[0], browser)
        self.assertIs(capture.await_args_list[1].args[0], browser)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright_context.exited)

    async def test_live_loop_keeps_page_open_and_publishes_resized_png(self):
        page = FakePage()
        context = FakeContext(page)
        browser = FakeBrowser([context])
        chromium = FakeChromium(browser)
        playwright_context = FakePlaywrightContextManager(chromium)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "live.png"
            with (
                patch.object(screenshot, "async_playwright", return_value=playwright_context),
                patch.object(
                    screenshot,
                    "accept_cookie_banner",
                    new=AsyncMock(return_value=False),
                ),
                patch.object(
                    screenshot,
                    "live_screenshot_asset_path",
                    return_value=output_path,
                ),
                patch.object(
                    screenshot.asyncio,
                    "sleep",
                    new=AsyncMock(side_effect=asyncio.CancelledError),
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await screenshot.live_screenshot_loop(
                        ["https://agents.example"],
                        interval_seconds=2,
                        viewport_map={"https://agents.example": (2, 2)},
                        output_size=(4, 3),
                    )

            self.assertTrue(output_path.exists())
            with Image.open(output_path) as captured:
                self.assertEqual(captured.format, "PNG")
                self.assertEqual(captured.size, (4, 3))

        self.assertEqual(page.goto_calls, 1)
        self.assertEqual(page.screenshot_calls, 1)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright_context.exited)

    async def test_live_browser_loop_closes_context_when_navigation_fails(self):
        context = FakeContext(FakePage(goto_error=RuntimeError("live navigation failed")))
        browser = FakeBrowser([context])

        with self.assertRaisesRegex(RuntimeError, "live navigation failed"):
            await screenshot._live_screenshot_browser_loop(
                browser,
                ["https://agents.example"],
                interval_seconds=2,
                viewport_map={},
                output_size=(1280, 720),
            )

        self.assertTrue(context.closed)


class DockerComposeInitTests(unittest.TestCase):
    def test_all_compose_variants_enable_init_reaper(self):
        repository_root = Path(__file__).resolve().parents[1]

        for filename in (
            "docker-compose.yml",
            "docker-compose.windows.yml",
            "docker-compose.exodia.yml",
        ):
            with self.subTest(filename=filename):
                contents = (repository_root / filename).read_text(encoding="utf-8")
                self.assertIn("    init: true\n", contents)

    def test_all_compose_variants_define_healthcheck_and_log_rotation(self):
        repository_root = Path(__file__).resolve().parents[1]

        for filename in (
            "docker-compose.yml",
            "docker-compose.windows.yml",
            "docker-compose.exodia.yml",
        ):
            with self.subTest(filename=filename):
                contents = (repository_root / filename).read_text(encoding="utf-8")
                self.assertIn("    healthcheck:\n", contents)
                self.assertIn("        max-size: \"10m\"\n", contents)
                self.assertIn("        max-file: \"5\"\n", contents)

    def test_exodia_compose_contains_resource_guardrails(self):
        repository_root = Path(__file__).resolve().parents[1]
        contents = (repository_root / "docker-compose.exodia.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("    pids_limit: 512\n", contents)
        self.assertIn("    mem_limit: 2g\n", contents)
        self.assertIn('    cpus: "4.0"\n', contents)

    def test_published_compose_variants_pin_project_version(self):
        repository_root = Path(__file__).resolve().parents[1]
        project = tomllib.loads(
            (repository_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        expected_image = f"    image: cipotato/quiosco:{project['project']['version']}\n"

        for filename in ("docker-compose.windows.yml", "docker-compose.exodia.yml"):
            with self.subTest(filename=filename):
                contents = (repository_root / filename).read_text(encoding="utf-8")
                self.assertIn(expected_image, contents)

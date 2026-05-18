"""Periodic animated GIF capture of unproxyable URLs using Playwright + Pillow."""

import asyncio
import logging
import re
from io import BytesIO
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

from screenshot_assets import screenshot_asset_path

logger = logging.getLogger(__name__)

FRAME_INTERVAL = 2  # seconds between frames
COOKIE_ACCEPT_PATTERNS = (
    r"^(accept all cookies|accept all|allow all|agree and continue|i accept|i agree)$",
    r"^(aceptar todas las cookies|aceptar todo|permitir todo|estoy de acuerdo)$",
    r"^(accept|aceptar|agree)$",
)


async def accept_cookie_banner(page) -> bool:
    """Best-effort dismissal for cookie banners before screenshots."""
    for pattern in COOKIE_ACCEPT_PATTERNS:
        text_match = re.compile(pattern, re.IGNORECASE)
        candidates = (
            page.get_by_role("button", name=text_match),
            page.get_by_role("link", name=text_match),
            page.locator("button, a").filter(has_text=text_match),
        )
        for locator in candidates:
            try:
                if await locator.count() == 0:
                    continue
                await locator.first.click(timeout=1500)
                await page.wait_for_timeout(500)
                logger.info("Cookie banner accepted with pattern: %s", pattern)
                return True
            except Exception:
                continue
    return False


async def take_gif(
    url: str,
    output_path: Path,
    duration_seconds: float = 60,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    output_width: int = 1920,
    output_height: int = 1080,
) -> bool:
    """Capture screenshots over *duration_seconds* and save as animated GIF."""
    num_frames = max(1, int(duration_seconds / FRAME_INTERVAL))
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": viewport_width, "height": viewport_height})
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await accept_cookie_banner(page)

            target_size = (output_width, output_height)
            frames: list[Image.Image] = []
            for _ in range(num_frames):
                raw = await page.screenshot(full_page=False)
                img = Image.open(BytesIO(raw)).convert("RGB")
                if img.size != target_size:
                    img = img.resize(target_size, Image.LANCZOS)
                frames.append(img)
                await asyncio.sleep(FRAME_INTERVAL)

            await browser.close()

        if not frames:
            return False

        # frame_duration in ms for each frame in the GIF
        frame_duration_ms = int(FRAME_INTERVAL * 1000)
        frames[0].save(
            str(output_path),
            save_all=True,
            append_images=frames[1:],
            duration=frame_duration_ms,
            loop=0,
        )
        logger.info("GIF saved (%d frames, %dx%d -> %dx%d): %s -> %s",
                     len(frames), viewport_width, viewport_height,
                     output_width, output_height, url, output_path)
        return True
    except Exception as e:
        logger.error("GIF capture failed for %s: %s", url, e)
        return False


async def screenshot_loop(
    urls: list[str],
    interval_seconds: float = 300,
    gif_duration_seconds: float = 60,
    viewport_map: dict[str, tuple[int, int]] | None = None,
    output_size: tuple[int, int] = (1920, 1080),
):
    """Periodically capture animated GIFs of the given URLs."""
    logger.info(
        "GIF capture loop started for %d URLs, interval=%ds, gif_duration=%ds",
        len(urls),
        interval_seconds,
        gif_duration_seconds,
    )
    viewports = viewport_map or {}
    while True:
        for url in urls:
            vw, vh = viewports.get(url, output_size)
            await take_gif(
                url, screenshot_asset_path(url),
                duration_seconds=gif_duration_seconds,
                viewport_width=vw, viewport_height=vh,
                output_width=output_size[0], output_height=output_size[1],
            )
        await asyncio.sleep(interval_seconds)


def start_screenshot_task(
    urls: list[str],
    interval_seconds: float = 300,
    gif_duration_seconds: float = 60,
    viewport_map: dict[str, tuple[int, int]] | None = None,
    output_size: tuple[int, int] = (1920, 1080),
) -> asyncio.Task:
    """Start the GIF capture background task. Call from within a running event loop."""
    return asyncio.create_task(
        screenshot_loop(urls, interval_seconds, gif_duration_seconds, viewport_map, output_size)
    )

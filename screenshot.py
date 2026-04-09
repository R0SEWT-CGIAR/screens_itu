"""Periodic animated GIF capture of unproxyable URLs using Playwright + Pillow."""

import asyncio
import logging
from io import BytesIO
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

from screenshot_assets import screenshot_asset_path

logger = logging.getLogger(__name__)

FRAME_INTERVAL = 2  # seconds between frames


async def take_gif(
    url: str,
    output_path: Path,
    duration_seconds: float = 60,
    width: int = 1920,
    height: int = 1080,
) -> bool:
    """Capture screenshots over *duration_seconds* and save as animated GIF."""
    num_frames = max(1, int(duration_seconds / FRAME_INTERVAL))
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": height})
            await page.goto(url, wait_until="networkidle", timeout=30000)

            frames: list[Image.Image] = []
            for _ in range(num_frames):
                raw = await page.screenshot(full_page=False)
                frames.append(Image.open(BytesIO(raw)).convert("RGB"))
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
        logger.info("GIF saved (%d frames): %s -> %s", len(frames), url, output_path)
        return True
    except Exception as e:
        logger.error("GIF capture failed for %s: %s", url, e)
        return False


async def screenshot_loop(
    urls: list[str],
    interval_seconds: float = 300,
    gif_duration_seconds: float = 60,
):
    """Periodically capture animated GIFs of the given URLs."""
    logger.info(
        "GIF capture loop started for %d URLs, interval=%ds, gif_duration=%ds",
        len(urls),
        interval_seconds,
        gif_duration_seconds,
    )
    while True:
        for url in urls:
            await take_gif(url, screenshot_asset_path(url), duration_seconds=gif_duration_seconds)
        await asyncio.sleep(interval_seconds)


def start_screenshot_task(
    urls: list[str],
    interval_seconds: float = 300,
    gif_duration_seconds: float = 60,
) -> asyncio.Task:
    """Start the GIF capture background task. Call from within a running event loop."""
    return asyncio.create_task(
        screenshot_loop(urls, interval_seconds, gif_duration_seconds)
    )

"""Periodic screenshots of unproxyable URLs using Playwright."""

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = Path("static/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


async def take_screenshot(url: str, output_path: Path, width: int = 1920, height: int = 1080) -> bool:
    """Take a screenshot of a URL and save it as PNG."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": height})
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.screenshot(path=str(output_path), full_page=False)
            await browser.close()
        logger.info("Screenshot saved: %s → %s", url, output_path)
        return True
    except Exception as e:
        logger.error("Screenshot failed for %s: %s", url, e)
        return False


def _hostname_key(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.replace(".", "_")


async def screenshot_loop(urls: list[str], interval_seconds: float = 300):
    """Periodically take screenshots of the given URLs."""
    logger.info("Screenshot loop started for %d URLs, interval=%ds", len(urls), interval_seconds)
    while True:
        for url in urls:
            key = _hostname_key(url)
            output = SCREENSHOT_DIR / f"{key}.png"
            await take_screenshot(url, output)
        await asyncio.sleep(interval_seconds)


def start_screenshot_task(urls: list[str], interval_seconds: float = 300) -> asyncio.Task:
    """Start the screenshot background task. Call from within a running event loop."""
    return asyncio.create_task(screenshot_loop(urls, interval_seconds))

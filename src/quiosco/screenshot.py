"""GIF and near-real-time PNG capture using Playwright + Pillow."""

import asyncio
import logging
import os
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image
from playwright.async_api import async_playwright

from .screenshot_assets import live_screenshot_asset_path, screenshot_asset_path

logger = logging.getLogger(__name__)

FRAME_INTERVAL = 2  # seconds between frames
LIVE_PAGE_SETTLE_MILLISECONDS = 2000
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
    """Capture one GIF using a short-lived Playwright browser."""
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                return await _take_gif_with_browser(
                    browser,
                    url,
                    output_path,
                    duration_seconds=duration_seconds,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    output_width=output_width,
                    output_height=output_height,
                )
            finally:
                await _close_resource(browser, "browser")
    except Exception as exc:
        logger.error("GIF capture failed for %s: %s", url, exc)
        return False


async def _close_resource(resource, label: str) -> None:
    """Best-effort close for Playwright resources without hiding cancellation."""
    try:
        await resource.close()
    except Exception as exc:
        logger.warning("Could not close Playwright %s: %s", label, exc)


async def _take_gif_with_browser(
    browser,
    url: str,
    output_path: Path,
    duration_seconds: float = 60,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    output_width: int = 1920,
    output_height: int = 1080,
) -> bool:
    """Capture one GIF while reusing a browser owned by the caller."""
    num_frames = max(1, int(duration_seconds / FRAME_INTERVAL))
    context = None
    try:
        # Un context per URL aisla cookies y cierra todas sus paginas al terminar.
        # ignore_https_errors: PRTG interno usa certificado invalido (ver ADR-002).
        context = await browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await accept_cookie_banner(page)

        target_size = (output_width, output_height)
        frames: list[Image.Image] = []
        for _ in range(num_frames):
            raw = await page.screenshot(full_page=False)
            img = Image.open(BytesIO(raw)).convert("RGB")
            if img.size != target_size:
                img = img.resize(target_size, Image.Resampling.LANCZOS)
            frames.append(img)
            await asyncio.sleep(FRAME_INTERVAL)

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
    except Exception as exc:
        logger.error("GIF capture failed for %s: %s", url, exc)
        return False
    finally:
        if context is not None:
            await _close_resource(context, "browser context")


async def screenshot_loop(
    urls: list[str],
    interval_seconds: float = 300,
    gif_duration_seconds: float = 60,
    viewport_map: dict[str, tuple[int, int]] | None = None,
    output_size: tuple[int, int] = (1920, 1080),
    link_source: Callable[[], tuple[list[str], dict[str, tuple[int, int]]]] | None = None,
    recapture_queue: "asyncio.Queue[str] | None" = None,
):
    """Periodically capture animated GIFs of the given URLs.

    Con *link_source* la lista se re-resuelve en cada ciclo, para que un link
    agregado desde la consola obtenga su GIF sin reiniciar el servicio. Con
    *recapture_queue* un tecnico puede pedir la recaptura de una URL sin esperar
    el ciclo completo.
    """
    logger.info(
        "GIF capture loop started for %d URLs, interval=%ds, gif_duration=%ds",
        len(urls),
        interval_seconds,
        gif_duration_seconds,
    )
    viewports = viewport_map or {}

    async def capture(url: str, sizes: dict[str, tuple[int, int]]) -> None:
        """Recaptura suelta: abre su propio browser de vida corta y lo cierra."""
        vw, vh = sizes.get(url, output_size)
        await take_gif(
            url, screenshot_asset_path(url),
            duration_seconds=gif_duration_seconds,
            viewport_width=vw, viewport_height=vh,
            output_width=output_size[0], output_height=output_size[1],
        )

    while True:
        if link_source is not None:
            urls, viewports = link_source()
        # El task arranca siempre, por si el tecnico agrega desde la consola un
        # link que necesite GIF. Sin objetivos no se lanza Chromium: hacerlo cada
        # ciclo para nada es el mismo churn de procesos que se busca evitar.
        try:
            if urls:
                # Un solo browser por ciclo: lanzar uno por GIF dejaba procesos
                # Chromium zombis acumulandose en el contenedor.
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch()
                    try:
                        for url in urls:
                            vw, vh = viewports.get(url, output_size)
                            await _take_gif_with_browser(
                                browser,
                                url,
                                screenshot_asset_path(url),
                                duration_seconds=gif_duration_seconds,
                                viewport_width=vw,
                                viewport_height=vh,
                                output_width=output_size[0],
                                output_height=output_size[1],
                            )
                    finally:
                        await _close_resource(browser, "browser")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("GIF capture cycle failed: %s", exc)
        await _sleep_serving_recaptures(
            interval_seconds, recapture_queue, capture, viewports
        )


def _save_png_atomically(
    raw: bytes,
    output_path: Path,
    output_size: tuple[int, int],
) -> None:
    """Resize a browser screenshot and atomically publish it as PNG."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    try:
        with Image.open(BytesIO(raw)) as source:
            image = source.convert("RGB")
        if image.size != output_size:
            image = image.resize(output_size, Image.Resampling.LANCZOS)
        image.save(temporary_path, format="PNG")
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


async def _live_screenshot_browser_loop(
    browser,
    urls: list[str],
    interval_seconds: float,
    viewport_map: dict[str, tuple[int, int]],
    output_size: tuple[int, int],
) -> None:
    """Keep one browser page per URL open and publish fresh frames."""
    sessions = []
    try:
        for url in urls:
            viewport_width, viewport_height = viewport_map.get(url, output_size)
            context = await browser.new_context(
                viewport={"width": viewport_width, "height": viewport_height},
                ignore_https_errors=True,
            )
            try:
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await accept_cookie_banner(page)
                await page.wait_for_timeout(LIVE_PAGE_SETTLE_MILLISECONDS)
            except BaseException:
                await _close_resource(context, "live browser context")
                raise
            sessions.append((url, context, page))

        while True:
            cycle_started = asyncio.get_running_loop().time()
            for url, _context, page in sessions:
                raw = await page.screenshot(full_page=False)
                output_path = live_screenshot_asset_path(url)
                await asyncio.to_thread(
                    _save_png_atomically,
                    raw,
                    output_path,
                    output_size,
                )

            elapsed = asyncio.get_running_loop().time() - cycle_started
            await asyncio.sleep(max(0.05, interval_seconds - elapsed))
    finally:
        for _url, context, _page in sessions:
            await _close_resource(context, "live browser context")


async def live_screenshot_loop(
    urls: list[str],
    interval_seconds: float = FRAME_INTERVAL,
    viewport_map: dict[str, tuple[int, int]] | None = None,
    output_size: tuple[int, int] = (1920, 1080),
) -> None:
    """Publish PNG frames while preserving each page's live browser session."""
    logger.info(
        "Live PNG capture loop started for %d URLs, interval=%gs",
        len(urls),
        interval_seconds,
    )
    viewports = viewport_map or {}
    while True:
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    await _live_screenshot_browser_loop(
                        browser,
                        urls,
                        interval_seconds,
                        viewports,
                        output_size,
                    )
                finally:
                    await _close_resource(browser, "live browser")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Live PNG capture session failed: %s", exc)
        await asyncio.sleep(max(1.0, interval_seconds))


async def _sleep_serving_recaptures(interval_seconds, queue, capture, viewports) -> None:
    """Espera el intervalo, atendiendo pedidos de recaptura mientras tanto."""
    deadline = time.monotonic() + interval_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if queue is None:
            await asyncio.sleep(remaining)
            return
        try:
            url = await asyncio.wait_for(queue.get(), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError):
            return
        logger.info("Recaptura pedida desde la consola: %s", url)
        await capture(url, viewports)


def start_screenshot_task(
    urls: list[str],
    interval_seconds: float = 300,
    gif_duration_seconds: float = 60,
    viewport_map: dict[str, tuple[int, int]] | None = None,
    output_size: tuple[int, int] = (1920, 1080),
    link_source: Callable[[], tuple[list[str], dict[str, tuple[int, int]]]] | None = None,
    recapture_queue: "asyncio.Queue[str] | None" = None,
) -> asyncio.Task:
    """Start the GIF capture background task. Call from within a running event loop."""
    return asyncio.create_task(
        screenshot_loop(
            urls, interval_seconds, gif_duration_seconds, viewport_map, output_size,
            link_source=link_source, recapture_queue=recapture_queue,
        )
    )


def start_live_screenshot_task(
    urls: list[str],
    interval_seconds: float = FRAME_INTERVAL,
    viewport_map: dict[str, tuple[int, int]] | None = None,
    output_size: tuple[int, int] = (1920, 1080),
) -> asyncio.Task:
    """Start the persistent live PNG capture task."""
    return asyncio.create_task(
        live_screenshot_loop(urls, interval_seconds, viewport_map, output_size)
    )

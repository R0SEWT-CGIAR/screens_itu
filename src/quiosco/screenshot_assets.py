import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

SCREENSHOT_DIR = Path("static/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_EXTENSIONS = {"gif", "png"}


def sanitize_hostname(hostname: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9]+", "_", hostname).strip("_")
    return sanitized or "unknown"


def screenshot_asset_key(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or parsed.netloc or "unknown"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"{sanitize_hostname(hostname)}_{digest}"


def screenshot_asset_path_for_key(asset_key: str, extension: str = "gif") -> Path:
    if extension not in SCREENSHOT_EXTENSIONS:
        raise ValueError(f"Unsupported screenshot extension: {extension}")
    return SCREENSHOT_DIR / f"{asset_key}.{extension}"


def screenshot_asset_path(url: str, extension: str = "gif") -> Path:
    return screenshot_asset_path_for_key(screenshot_asset_key(url), extension)


def live_screenshot_asset_path(url: str) -> Path:
    return screenshot_asset_path(url, "png")


def screenshot_asset_revision(asset_key: str | None, extension: str = "gif") -> int | None:
    if not asset_key:
        return None

    try:
        return screenshot_asset_path_for_key(asset_key, extension).stat().st_mtime_ns
    except FileNotFoundError:
        return None

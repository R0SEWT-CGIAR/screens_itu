import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

SCREENSHOT_DIR = Path("static/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_hostname(hostname: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9]+", "_", hostname).strip("_")
    return sanitized or "unknown"


def screenshot_asset_key(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or parsed.netloc or "unknown"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"{sanitize_hostname(hostname)}_{digest}"


def screenshot_asset_path_for_key(asset_key: str) -> Path:
    return SCREENSHOT_DIR / f"{asset_key}.gif"


def screenshot_asset_path(url: str) -> Path:
    return screenshot_asset_path_for_key(screenshot_asset_key(url))


def screenshot_asset_revision(asset_key: str | None) -> int | None:
    if not asset_key:
        return None

    try:
        return screenshot_asset_path_for_key(asset_key).stat().st_mtime_ns
    except FileNotFoundError:
        return None

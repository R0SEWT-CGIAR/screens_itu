"""Lightweight runtime resource monitoring for the long-lived kiosk service."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 300.0
THREAD_WARNING_THRESHOLD = 100
CGROUP_PID_WARNING_THRESHOLD = 250
CGROUP_MEMORY_WARNING_MB = 1024.0


@dataclass(frozen=True)
class RuntimeSample:
    process_threads: int | None
    process_rss_mb: float | None
    cgroup_pids: int | None
    cgroup_memory_mb: float | None
    process_count: int
    zombie_count: int
    chromium_process_count: int

    def warning_reasons(self) -> list[str]:
        """Return resource conditions that need operational attention."""
        reasons: list[str] = []
        if (
            self.process_threads is not None
            and self.process_threads >= THREAD_WARNING_THRESHOLD
        ):
            reasons.append("threads")
        if (
            self.cgroup_pids is not None
            and self.cgroup_pids >= CGROUP_PID_WARNING_THRESHOLD
        ):
            reasons.append("cgroup_pids")
        if (
            self.cgroup_memory_mb is not None
            and self.cgroup_memory_mb >= CGROUP_MEMORY_WARNING_MB
        ):
            reasons.append("cgroup_memory")
        if self.zombie_count:
            reasons.append("zombies")
        return reasons


def _read_status_value(status_path: Path, key: str) -> int | None:
    try:
        contents = status_path.read_text(encoding="utf-8")
    except OSError:
        return None

    prefix = f"{key}:"
    for line in contents.splitlines():
        if line.startswith(prefix):
            fields = line[len(prefix) :].strip().split()
            try:
                return int(fields[0])
            except (IndexError, ValueError):
                return None
    return None


def _read_first_int(paths: tuple[Path, ...]) -> int | None:
    for path in paths:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
    return None


def _process_state(stat_contents: str) -> str | None:
    """Extract the process state while allowing spaces in the command name."""
    command_end = stat_contents.rfind(")")
    if command_end < 0:
        return None
    fields = stat_contents[command_end + 1 :].strip().split()
    return fields[0] if fields else None


def collect_runtime_sample(
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> RuntimeSample:
    """Collect Linux process and cgroup counters without external dependencies."""
    process_threads = _read_status_value(proc_root / "self" / "status", "Threads")
    rss_kb = _read_status_value(proc_root / "self" / "status", "VmRSS")

    cgroup_pids = _read_first_int(
        (cgroup_root / "pids.current", cgroup_root / "pids" / "pids.current")
    )
    cgroup_memory_bytes = _read_first_int(
        (
            cgroup_root / "memory.current",
            cgroup_root / "memory" / "memory.usage_in_bytes",
        )
    )

    process_count = 0
    zombie_count = 0
    chromium_process_count = 0
    try:
        process_paths = tuple(path for path in proc_root.iterdir() if path.name.isdigit())
    except OSError:
        process_paths = ()

    for process_path in process_paths:
        try:
            state = _process_state(
                (process_path / "stat").read_text(encoding="utf-8")
            )
        except OSError:
            continue
        if state is None:
            continue

        process_count += 1
        if state == "Z":
            zombie_count += 1
        try:
            command = (process_path / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            command = ""
        if command.startswith(("chrome", "chromium")):
            chromium_process_count += 1

    return RuntimeSample(
        process_threads=process_threads,
        process_rss_mb=(rss_kb / 1024 if rss_kb is not None else None),
        cgroup_pids=cgroup_pids,
        cgroup_memory_mb=(
            cgroup_memory_bytes / (1024 * 1024)
            if cgroup_memory_bytes is not None
            else None
        ),
        process_count=process_count,
        zombie_count=zombie_count,
        chromium_process_count=chromium_process_count,
    )


def _format_optional_number(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def log_runtime_sample(sample: RuntimeSample) -> None:
    """Log one stable, machine-searchable resource snapshot."""
    warning_reasons = sample.warning_reasons()
    log_level = logging.WARNING if warning_reasons else logging.INFO
    logger.log(
        log_level,
        (
            "runtime_resources status=%s threads=%s process_rss_mb=%s "
            "cgroup_pids=%s cgroup_memory_mb=%s processes=%d zombies=%d "
            "chromium_processes=%d warnings=%s"
        ),
        "warning" if warning_reasons else "ok",
        _format_optional_number(sample.process_threads),
        _format_optional_number(sample.process_rss_mb),
        _format_optional_number(sample.cgroup_pids),
        _format_optional_number(sample.cgroup_memory_mb),
        sample.process_count,
        sample.zombie_count,
        sample.chromium_process_count,
        ",".join(warning_reasons) if warning_reasons else "none",
    )


async def runtime_monitor_loop(interval_seconds: float = MONITOR_INTERVAL_SECONDS) -> None:
    """Log runtime resources periodically without stopping the application on errors."""
    logger.info("Runtime resource monitor started (interval=%ss)", interval_seconds)
    while True:
        try:
            log_runtime_sample(collect_runtime_sample())
        except Exception:
            logger.exception("Runtime resource sample failed")
        await asyncio.sleep(interval_seconds)


def start_runtime_monitor_task(
    interval_seconds: float = MONITOR_INTERVAL_SECONDS,
) -> asyncio.Task:
    """Start the runtime monitor from an active event loop."""
    return asyncio.create_task(runtime_monitor_loop(interval_seconds))

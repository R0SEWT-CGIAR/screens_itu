import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quiosco import runtime_monitor


class RuntimeSampleTests(unittest.TestCase):
    def test_collects_process_and_cgroup_resources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            proc_root = root / "proc"
            cgroup_root = root / "cgroup"
            (proc_root / "self").mkdir(parents=True)
            cgroup_root.mkdir()
            (proc_root / "self" / "status").write_text(
                "Name:\tuvicorn\nVmRSS:\t204800 kB\nThreads:\t123\n",
                encoding="utf-8",
            )
            (cgroup_root / "pids.current").write_text("456\n", encoding="utf-8")
            (cgroup_root / "memory.current").write_text(
                str(1536 * 1024 * 1024), encoding="utf-8"
            )
            self._write_process(proc_root, 10, "uvicorn", "S")
            self._write_process(proc_root, 11, "chrome-headless", "R")
            self._write_process(proc_root, 12, "chrome-headless", "Z")

            sample = runtime_monitor.collect_runtime_sample(proc_root, cgroup_root)

        self.assertEqual(sample.process_threads, 123)
        self.assertEqual(sample.process_rss_mb, 200.0)
        self.assertEqual(sample.cgroup_pids, 456)
        self.assertEqual(sample.cgroup_memory_mb, 1536.0)
        self.assertEqual(sample.process_count, 3)
        self.assertEqual(sample.zombie_count, 1)
        self.assertEqual(sample.chromium_process_count, 2)
        self.assertEqual(
            sample.warning_reasons(),
            ["threads", "cgroup_pids", "cgroup_memory", "zombies"],
        )

    def test_missing_proc_and_cgroup_files_return_unknown_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample = runtime_monitor.collect_runtime_sample(
                root / "missing-proc", root / "missing-cgroup"
            )

        self.assertIsNone(sample.process_threads)
        self.assertIsNone(sample.process_rss_mb)
        self.assertIsNone(sample.cgroup_pids)
        self.assertIsNone(sample.cgroup_memory_mb)
        self.assertEqual(sample.process_count, 0)
        self.assertEqual(sample.zombie_count, 0)
        self.assertEqual(sample.chromium_process_count, 0)
        self.assertEqual(sample.warning_reasons(), [])

    def test_logs_warning_with_machine_searchable_fields(self):
        sample = runtime_monitor.RuntimeSample(
            process_threads=100,
            process_rss_mb=200.25,
            cgroup_pids=80,
            cgroup_memory_mb=300.5,
            process_count=10,
            zombie_count=0,
            chromium_process_count=7,
        )

        with self.assertLogs(runtime_monitor.logger, level=logging.WARNING) as captured:
            runtime_monitor.log_runtime_sample(sample)

        message = captured.output[0]
        self.assertIn("runtime_resources status=warning", message)
        self.assertIn("threads=100", message)
        self.assertIn("warnings=threads", message)

    def test_logs_healthy_sample_at_info_level(self):
        sample = runtime_monitor.RuntimeSample(
            process_threads=11,
            process_rss_mb=100.0,
            cgroup_pids=86,
            cgroup_memory_mb=350.0,
            process_count=10,
            zombie_count=0,
            chromium_process_count=7,
        )

        with self.assertLogs(runtime_monitor.logger, level=logging.INFO) as captured:
            runtime_monitor.log_runtime_sample(sample)

        self.assertIn("runtime_resources status=ok", captured.output[0])
        self.assertIn("warnings=none", captured.output[0])

    @staticmethod
    def _write_process(proc_root: Path, pid: int, command: str, state: str) -> None:
        process_path = proc_root / str(pid)
        process_path.mkdir()
        (process_path / "stat").write_text(
            f"{pid} ({command}) {state} 0 0 0 0\n", encoding="utf-8"
        )
        (process_path / "comm").write_text(f"{command}\n", encoding="utf-8")


class RuntimeMonitorLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_logs_collection_errors_and_remains_cancellable(self):
        with (
            patch.object(
                runtime_monitor,
                "collect_runtime_sample",
                side_effect=RuntimeError("proc unavailable"),
            ),
            patch.object(
                runtime_monitor.asyncio,
                "sleep",
                side_effect=asyncio.CancelledError,
            ),
            self.assertLogs(runtime_monitor.logger, level=logging.ERROR) as captured,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await runtime_monitor.runtime_monitor_loop(interval_seconds=1)

        self.assertIn("Runtime resource sample failed", captured.output[0])

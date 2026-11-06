"""End-to-end test: real filesystem events through to the SQLite rows and reports.

Uses the real watchdog observer on a temporary directory. No global input hooks
and no registry changes are involved.
"""

from __future__ import annotations

import queue
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from config import AppConfig
from database import DatabaseWriter
from event_processor import EventProcessor
from events import WorkAction
from report import ReportGenerator, write_reports
from watchdog_handler import FileSystemMonitor

TIMEOUT_SECONDS = 10.0


def wait_until(predicate, timeout: float = TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class FileMonitoringIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = AppConfig(
            app_dir=self.root,
            watch_dirs=(self.root,),
            exclude_dirs=(),
            exclude_patterns=("*.tmp",),
            target_extensions=frozenset({".py"}),
            db_path=self.root / "work_log.db",
            debounce_seconds=0.3,
            database_flush_seconds=0.1,
        )
        self.queue: queue.Queue = queue.Queue()
        self.writer = DatabaseWriter(self.config.db_path, batch_size=10, flush_seconds=0.1)
        self.writer.start_and_wait()
        self.processor = EventProcessor(self.config, self.queue, self.writer.submit)
        self.processor.start()
        self.monitor = FileSystemMonitor(self.queue, self.config.watch_dirs)
        self.monitor.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.monitor.stop()
        self.processor.stop()
        self.writer.stop()

    def _actions(self) -> list[str]:
        return [record.action for record in self.writer.fetch_day(date.today())]

    def test_create_and_delete_are_persisted(self) -> None:
        target = self.root / "notes.py"
        target.write_text("print('hello')\n", encoding="utf-8")
        self.assertTrue(
            wait_until(lambda: WorkAction.CREATE_FILE.value in self._actions()),
            f"未捕获新建事件: {self._actions()}",
        )

        target.unlink()
        self.assertTrue(
            wait_until(lambda: WorkAction.DELETE_FILE.value in self._actions()),
            f"未捕获删除事件: {self._actions()}",
        )

    def test_excluded_pattern_produces_no_rows(self) -> None:
        (self.root / "scratch.tmp").write_text("x", encoding="utf-8")
        # Give the observer time to deliver an event if it were going to.
        time.sleep(1.0)
        self.assertEqual([], self._actions())

    def test_report_is_generated_from_real_rows(self) -> None:
        (self.root / "deliverable.py").write_text("x" * 32, encoding="utf-8")
        self.assertTrue(wait_until(lambda: WorkAction.CREATE_FILE.value in self._actions()))

        reports = ReportGenerator(self.writer.fetch_day).generate(date.today())
        self.assertIsNotNone(reports)
        assert reports is not None
        self.assertIn("deliverable.py", reports.detailed)
        summary_path, detailed_path = write_reports(self.root / "LogFile", date.today(), reports)
        self.assertTrue(summary_path.is_file())
        self.assertTrue(detailed_path.is_file())


if __name__ == "__main__":
    unittest.main()

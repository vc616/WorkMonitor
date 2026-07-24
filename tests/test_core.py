from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from browser import PageTracker
from database import DatabaseWriter
from events import WorkAction
from models import LogEvent
from report import ReportGenerator


class CoreTests(unittest.TestCase):
    def test_writer_batches_and_reporter_reads_through_same_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = DatabaseWriter(Path(directory) / "work_log.db", batch_size=2, flush_seconds=0.05)
            writer.start_and_wait()
            try:
                now = datetime.now().replace(microsecond=0)
                writer.submit(LogEvent(now, "新建文件", "a.py", r"C:\p\a.py", 10, "p"))
                writer.submit(LogEvent(now, WorkAction.SAVE_FILE.value, "a.py", r"C:\p\a.py", 20, "p"))
                writer.submit(LogEvent(now, WorkAction.BROWSE_PAGE.value, "Docs", "", 4, "chrome.exe"))
                logs = writer.fetch_day(now.date())
                self.assertEqual(3, len(logs))
                reports = ReportGenerator(writer.fetch_day).generate(now.date())
                self.assertIsNotNone(reports)
                self.assertIn("代码文件：1 个", reports.summary)
                self.assertIn("Docs", reports.detailed)
            finally:
                writer.stop()

    def test_page_tracker_flushes_once_when_page_changes(self) -> None:
        tracker = PageTracker(minimum_seconds=3)
        timestamp = datetime(2026, 1, 1, 9, 0)
        self.assertIsNone(tracker.update("chrome.exe", "Docs", 0.0, timestamp))
        self.assertIsNone(tracker.update("chrome.exe", "Docs", 2.0, timestamp))
        event = tracker.update("chrome.exe", "Mail", 5.0, timestamp)
        self.assertIsNotNone(event)
        self.assertEqual(5, event.file_size)
        self.assertEqual(WorkAction.BROWSE_PAGE.value, event.action)
        self.assertIsNone(tracker.flush(5.0, timestamp))


if __name__ == "__main__":
    unittest.main()

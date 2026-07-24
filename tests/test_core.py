from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from browser import ApplicationTracker, PageTracker
from database import DatabaseWriter
from events import WorkAction
from gui import dates_between
from models import GeneratedReports, LogEvent
from report import ReportGenerator, write_reports


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
                writer.submit(LogEvent(now, WorkAction.USE_APPLICATION.value, "微信", "", 12, "wechat.exe"))
                logs = writer.fetch_day(now.date())
                self.assertEqual(4, len(logs))
                reports = ReportGenerator(writer.fetch_day).generate(now.date())
                self.assertIsNotNone(reports)
                self.assertIn("代码文件：1 个", reports.summary)
                self.assertIn("Docs", reports.detailed)
                self.assertIn("微信（0 分钟 12 秒）", reports.summary)
                self.assertIn("wechat.exe", reports.detailed)
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

    def test_application_tracker_records_only_configured_foreground_time(self) -> None:
        tracker = ApplicationTracker({"WeChat.exe": "微信", "WXWork.exe": "企业微信"}, minimum_seconds=3)
        timestamp = datetime(2026, 1, 1, 9, 0)
        self.assertIsNone(tracker.update("wechat.exe", 0.0, timestamp))
        self.assertIsNone(tracker.update("wechat.exe", 2.0, timestamp))
        event = tracker.update("notepad.exe", 5.0, timestamp)
        self.assertIsNotNone(event)
        self.assertEqual(WorkAction.USE_APPLICATION.value, event.action)
        self.assertEqual("微信", event.file_name)
        self.assertEqual("wechat.exe", event.project_dir)
        self.assertEqual("", event.file_path)
        self.assertEqual(5, event.file_size)

    def test_month_availability_and_drag_range_skip_empty_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = DatabaseWriter(Path(directory) / "work_log.db", batch_size=10, flush_seconds=0.05)
            writer.start_and_wait()
            try:
                writer.submit(
                    LogEvent(datetime(2026, 7, 3, 9, 0), "新建文件", "a.py", r"C:\p\a.py", 10, "p")
                )
                writer.submit(
                    LogEvent(datetime(2026, 7, 7, 9, 0), "新建文件", "b.py", r"C:\p\b.py", 10, "p")
                )
                self.assertEqual(
                    {date(2026, 7, 3), date(2026, 7, 7)},
                    writer.fetch_available_days(2026, 7),
                )
            finally:
                writer.stop()
        available = {date(2026, 7, 3), date(2026, 7, 7)}
        self.assertEqual(available, dates_between(date(2026, 7, 3), date(2026, 7, 7), available))

    def test_daily_reports_are_written_to_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "LogFile"
            paths = write_reports(
                output_dir,
                date(2026, 7, 24),
                GeneratedReports("# summary\n", "# detailed\n"),
            )
            self.assertTrue(output_dir.is_dir())
            self.assertTrue(all(path.parent == output_dir for path in paths))
            self.assertEqual("# summary\n", paths[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

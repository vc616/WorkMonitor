"""Regression tests for the correctness, stability and privacy fixes.

Deterministic clocks and in-memory fixtures are used wherever possible so no
test depends on real timing, a Tcl/Tk runtime, global input hooks or the
Windows registry.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from browser import BROWSERS, ApplicationTracker, PageTracker
from config import AppConfig, load_config
from database import (
    CREATE_TABLE_SQL,
    DatabaseWriter,
    _QueryRequest,
    parse_timestamp,
)
from event_processor import EventProcessor
from events import FileEventType, WorkAction
from input_activity import InputActivityCounter
from main import MonitorApplication
from models import FileEvent, LogEvent, WorkLogRecord
from report import ReportGenerator
from watchdog_handler import QueueingEventHandler

WATCH_ROOT = Path(r"D:\code")


def make_processor(
    root: Path,
    *,
    exclude_patterns: tuple[str, ...] = (),
    target_extensions: frozenset[str] = frozenset({".py", ".docx", ".md"}),
    debounce_seconds: float = 1.0,
    watch_dirs: tuple[Path, ...] | None = None,
) -> tuple[EventProcessor, list[LogEvent]]:
    logs: list[LogEvent] = []
    config = AppConfig(
        app_dir=root,
        watch_dirs=watch_dirs or (root,),
        exclude_dirs=(),
        exclude_patterns=exclude_patterns,
        target_extensions=target_extensions,
        db_path=root / "work_log.db",
        debounce_seconds=debounce_seconds,
    )
    return EventProcessor(config, queue.Queue(), logs.append), logs


class TimeAttributionTests(unittest.TestCase):
    def test_page_session_uses_start_time_instead_of_end_time(self) -> None:
        tracker = PageTracker(minimum_seconds=3)
        start = datetime(2026, 1, 1, 23, 58)
        tracker.update("chrome.exe", "Docs", 0.0, start)
        event = tracker.update("notepad.exe", None, 60.0, datetime(2026, 1, 1, 23, 59))
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(start, event.timestamp)
        self.assertEqual(60, event.file_size)

    def test_cross_midnight_session_is_split_between_days(self) -> None:
        tracker = PageTracker(minimum_seconds=0)
        start = datetime(2026, 1, 1, 23, 58)
        tracker.update("chrome.exe", "Docs", 0.0, start)
        last = tracker.update("notepad.exe", None, 420.0, datetime(2026, 1, 2, 0, 5))
        earlier = tracker.drain_events()
        self.assertEqual(1, len(earlier))
        self.assertIsNotNone(last)
        assert last is not None
        self.assertEqual(date(2026, 1, 1), earlier[0].timestamp.date())
        self.assertEqual(date(2026, 1, 2), last.timestamp.date())
        self.assertEqual(420, earlier[0].file_size + last.file_size)

    def test_application_session_crosses_midnight(self) -> None:
        tracker = ApplicationTracker({"wechat.exe": "微信"}, minimum_seconds=0)
        tracker.update("wechat.exe", 0.0, datetime(2026, 1, 1, 23, 59))
        last = tracker.update("notepad.exe", 120.0, datetime(2026, 1, 2, 0, 1))
        earlier = tracker.drain_events()
        self.assertEqual(1, len(earlier))
        self.assertIsNotNone(last)
        self.assertEqual(date(2026, 1, 1), earlier[0].timestamp.date())
        self.assertEqual(date(2026, 1, 2), last.timestamp.date())  # type: ignore[union-attr]


class BrowserTitleTests(unittest.TestCase):
    def test_new_tab_with_browser_suffix_is_ignored(self) -> None:
        moment = datetime(2026, 1, 1, 9, 0)
        for title in (
            "新标签页 - Google Chrome",
            "新建标签页 - Microsoft Edge",
            "New Tab - Google Chrome",
            "新分頁 - Google Chrome",
            "about:blank - Google Chrome",
        ):
            with self.subTest(title=title):
                tracker = PageTracker(minimum_seconds=0)
                tracker.update("chrome.exe", title, 0.0, moment)
                self.assertIsNone(tracker.flush(5.0, moment))

    def test_real_page_mentioning_new_tab_is_still_recorded(self) -> None:
        tracker = PageTracker(minimum_seconds=0)
        moment = datetime(2026, 1, 1, 9, 0)
        tracker.update("chrome.exe", "How to open a new tab - Google Chrome", 0.0, moment)
        event = tracker.flush(5.0, moment)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertIn("How to open a new tab", event.file_name)


class ReportAggregationTests(unittest.TestCase):
    def test_folder_operations_are_not_counted_as_file_operations(self) -> None:
        moment = datetime(2026, 1, 1, 9, 0)
        logs = [
            WorkLogRecord(moment, WorkAction.CREATE_FILE.value, "a.py", r"D:\p\a.py", 10, "p"),
            WorkLogRecord(moment, WorkAction.DELETE_FOLDER.value, "old", r"D:\p\old", 0, "p"),
        ]
        reports = ReportGenerator(lambda _day: logs).generate(date(2026, 1, 1))
        assert reports is not None
        self.assertIn("操作记录：1 条", reports.summary)
        self.assertIn("涉及文件：1 个", reports.summary)
        self.assertIn("文件夹操作：1 条", reports.summary)
        self.assertNotIn("📄 old", reports.detailed)
        self.assertIn("📁 文件夹操作", reports.detailed)

    def test_same_filename_in_different_folders_stays_separate(self) -> None:
        logs = [
            WorkLogRecord(datetime(2026, 1, 1, 9, 0), WorkAction.CREATE_FILE.value, "a.py", r"C:\one\a.py", 10, "p"),
            WorkLogRecord(datetime(2026, 1, 1, 10, 0), WorkAction.SAVE_FILE.value, "a.py", r"D:\two\a.py", 20, "p"),
        ]
        reports = ReportGenerator(lambda _day: logs).generate(date(2026, 1, 1))
        assert reports is not None
        self.assertIn("涉及文件：2 个", reports.summary)
        self.assertEqual(2, reports.detailed.count("📄 a.py"))


class EventProcessorFixTests(unittest.TestCase):
    def test_created_then_modified_produces_a_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            target = root / "demo.py"
            target.write_text("a", encoding="utf-8")
            now = datetime(2026, 1, 1, 9, 0)
            with patch.object(processor, "_process_opening_file", return_value=None):
                processor._process(FileEvent(FileEventType.CREATED, target, now))
                target.write_text("abcdef", encoding="utf-8")
                processor._process(FileEvent(FileEventType.MODIFIED, target, now))
            self.assertIn(WorkAction.SAVE_FILE.value, [event.action for event in logs])

    def test_repeated_modified_within_debounce_is_recorded_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            processor._monotonic = lambda: 100.0
            target = root / "demo.py"
            target.write_text("a", encoding="utf-8")
            now = datetime(2026, 1, 1, 9, 0)
            with (
                patch.object(processor, "_snapshot", side_effect=[(1, 1), (3, 2), (6, 3)]),
                patch.object(processor, "_process_opening_file", return_value=None),
            ):
                processor._process(FileEvent(FileEventType.CREATED, target, now))
                processor._process(FileEvent(FileEventType.MODIFIED, target, now))
                processor._process(FileEvent(FileEventType.MODIFIED, target, now))
            self.assertEqual(
                [WorkAction.CREATE_FILE.value, WorkAction.SAVE_FILE.value],
                [event.action for event in logs],
            )

    def test_clock_rollback_does_not_suppress_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            processor, _ = make_processor(Path(directory))
            processor._current_arrival = 100.0
            self.assertFalse(processor._is_recent_duplicate("k", "action"))
            processor._current_arrival = 99.0
            self.assertFalse(processor._is_recent_duplicate("k", "action"))

    def test_tilde_file_is_not_remapped_as_a_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            target = root / "~notes.md"
            target.write_text("x", encoding="utf-8")
            processor._process(FileEvent(FileEventType.CREATED, target, datetime(2026, 1, 1, 9, 0)))
            self.assertEqual(1, len(logs))
            self.assertEqual(WorkAction.CREATE_FILE.value, logs[0].action)
            self.assertEqual(str(target), logs[0].file_path)

    def test_destructive_events_flush_even_without_a_silent_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            clock = [0.0]
            processor._monotonic = lambda: clock[0]
            for index in range(12):
                clock[0] = index * 0.5
                processor._process(
                    FileEvent(FileEventType.DELETED, root / f"f{index}.py", datetime(2026, 1, 1, 9, 0))
                )
                processor._flush_pending_destructive_events()
            self.assertTrue(any(event.action == WorkAction.DELETE_FILE.value for event in logs))

    def test_stale_directory_operation_stops_swallowing_child_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            clock = [0.0]
            processor._monotonic = lambda: clock[0]
            folder = root / "project"
            now = datetime(2026, 1, 1, 9, 0)
            processor._process(FileEvent(FileEventType.DELETED, folder, now, is_directory=True))
            clock[0] = 10.0
            processor._flush_pending_destructive_events()
            rebuilt = folder / "new.py"
            rebuilt.parent.mkdir(parents=True, exist_ok=True)
            rebuilt.write_text("x", encoding="utf-8")
            processor._process(FileEvent(FileEventType.CREATED, rebuilt, now))
            self.assertTrue(any(event.action == WorkAction.CREATE_FILE.value for event in logs))

    def test_file_size_never_sleeps_on_the_event_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "a.py"
            target.write_text("x" * 32, encoding="utf-8")
            with patch("event_processor.time.sleep", side_effect=AssertionError("must not sleep")):
                self.assertEqual(32, EventProcessor._file_size(target, wait_for_stable=True))

    def test_anchored_pattern_only_matches_the_watch_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            processor, _ = make_processor(
                Path(directory), exclude_patterns=("/secret/",), watch_dirs=(WATCH_ROOT,)
            )
            self.assertTrue(processor._is_excluded(WATCH_ROOT / "secret" / "a.py"))
            self.assertFalse(processor._is_excluded(WATCH_ROOT / "sub" / "secret" / "a.py"))


class DatabaseFixTests(unittest.TestCase):
    def test_stop_drains_events_queued_behind_the_stop_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "w.db"
            writer = DatabaseWriter(db_path, batch_size=1000, flush_seconds=30, queue_max_size=1000)
            writer.start_and_wait()
            for index in range(20):
                writer.submit(
                    LogEvent(
                        datetime(2026, 1, 1, 9, 0),
                        WorkAction.CREATE_FILE.value,
                        f"a{index}.py",
                        f"/p/a{index}.py",
                        1,
                        "p",
                    )
                )
            writer.stop()
            connection = sqlite3.connect(db_path)
            try:
                count = connection.execute("SELECT COUNT(*) FROM work_log").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(20, count)

    def test_parse_timestamp_accepts_iso_and_legacy_formats(self) -> None:
        expected = datetime(2026, 1, 1, 9, 0)
        self.assertEqual(expected, parse_timestamp("2026-01-01 09:00:00"))
        self.assertEqual(expected, parse_timestamp("2026-01-01T09:00:00"))
        self.assertEqual(expected, parse_timestamp(expected))

    def test_query_skips_rows_with_unparseable_timestamps(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(CREATE_TABLE_SQL)
            connection.execute(
                "INSERT INTO work_log (timestamp, action, file_name, file_path, file_size, project_dir)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-01-01 09:00:00 plus noise", "新建文件", "bad.py", "/p/bad.py", 1, "p"),
            )
            connection.execute(
                "INSERT INTO work_log (timestamp, action, file_name, file_path, file_size, project_dir)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-01-01 09:00:00", "新建文件", "good.py", "/p/good.py", 1, "p"),
            )
            request = _QueryRequest(day=date(2026, 1, 1))
            DatabaseWriter._execute_query(connection, request)
            self.assertIsNone(request.error)
            self.assertEqual(["good.py"], [row.file_name for row in request.result or []])
        finally:
            connection.close()


class PrivacyAndConfigTests(unittest.TestCase):
    def test_webview2_is_not_treated_as_a_browser(self) -> None:
        self.assertNotIn("msedgewebview2.exe", BROWSERS)
        tracker = PageTracker(minimum_seconds=0)
        moment = datetime(2026, 1, 1, 9, 0)
        tracker.update("msedgewebview2.exe", "内嵌页面标题", 0.0, moment)
        self.assertIsNone(tracker.flush(5.0, moment))

    def test_new_config_fields_are_validated_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "watch_dirs": [str(root)],
                        "target_extensions": [".py"],
                        "db_path": "w.db",
                        "queue_max_size": 500,
                        "known_editors": ["Notepad.exe", "code.exe"],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(500, config.queue_max_size)
            self.assertIn("notepad", config.known_editors)
            self.assertIn("code", config.known_editors)

    def test_input_snapshot_only_submits_increments(self) -> None:
        events: list[LogEvent] = []
        counter = InputActivityCounter(events.append)
        moment = datetime(2026, 1, 1, 9, 5)
        counter.record_mouse_click(moment)
        counter.snapshot()
        counter.snapshot()
        counter.record_mouse_click(moment)
        counter.snapshot()
        counter.flush()
        self.assertEqual(2, len(events))
        self.assertEqual([1, 1], [event.file_size for event in events])

    def test_historical_report_does_not_touch_live_input_counter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                app_dir=root,
                watch_dirs=(root,),
                exclude_dirs=(),
                exclude_patterns=(),
                target_extensions=frozenset({".py"}),
                db_path=root / "w.db",
            )
            application = MonitorApplication(config)
            with (
                patch.object(application._input_activity, "snapshot") as snapshot,
                patch.object(application._reporter, "generate", return_value=None),
            ):
                application.generate_report(date(2020, 1, 1))
            snapshot.assert_not_called()

    def test_full_event_queue_does_not_raise_in_a_watchdog_callback(self) -> None:
        events: queue.Queue[FileEvent] = queue.Queue(maxsize=1)
        handler = QueueingEventHandler(events)
        event = FileEvent(FileEventType.CREATED, Path("a.py"), datetime(2026, 1, 1, 9, 0))
        events.put_nowait(event)
        handler._put(event)
        self.assertEqual(1, handler._dropped)


if __name__ == "__main__":
    unittest.main()

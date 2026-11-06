from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import database
from browser import PageTracker
from config import AppConfig, ConfigError, load_config
from event_processor import EventProcessor
from events import FileEventType, WorkAction
from models import FileEvent, LogEvent
from parsing import dates_between, parse_extensions, parse_tracked_apps
from report import ReportGenerator
from utils import configure_logging, format_duration

WATCH_ROOT = Path(r"D:\code")


def make_processor(
    root: Path,
    *,
    exclude_patterns: tuple[str, ...] = (),
    exclude_dirs: tuple[Path, ...] = (),
    target_extensions: frozenset[str] = frozenset({".py", ".docx"}),
    debounce_seconds: float = 1.0,
    watch_dirs: tuple[Path, ...] | None = None,
) -> tuple[EventProcessor, list[LogEvent]]:
    logs: list[LogEvent] = []
    config = AppConfig(
        app_dir=root,
        watch_dirs=watch_dirs or (root,),
        exclude_dirs=exclude_dirs,
        exclude_patterns=exclude_patterns,
        target_extensions=target_extensions,
        db_path=root / "work_log.db",
        debounce_seconds=debounce_seconds,
    )
    return EventProcessor(config, queue.Queue(), logs.append), logs


class ConfigValidationTests(unittest.TestCase):
    """A scalar where a list is expected used to be iterated character by character."""

    def _write(self, directory: str, payload: dict) -> Path:
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_scalar_watch_dirs_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, {"watch_dirs": r"D:\work", "target_extensions": [".py"], "db_path": "x.db"})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_scalar_target_extensions_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, {"watch_dirs": [directory], "target_extensions": ".py", "db_path": "x.db"})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_non_string_entries_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, {"watch_dirs": [directory, 42], "target_extensions": [".py"], "db_path": "x.db"})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_empty_db_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, {"watch_dirs": [directory], "target_extensions": [".py"], "db_path": "   "})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_relative_paths_resolve_against_the_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "portable"
            config_dir.mkdir()
            path = self._write(str(config_dir), {"watch_dirs": ["data"], "target_extensions": [".py"], "db_path": "logs.db"})
            loaded = load_config(path)
            self.assertEqual(config_dir.resolve(), loaded.app_dir.resolve())
            self.assertEqual(config_dir / "data", loaded.watch_dirs[0])
            self.assertEqual(config_dir / "logs.db", loaded.db_path)

    def test_exclude_patterns_keep_order_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                directory,
                {
                    "watch_dirs": [directory],
                    "target_extensions": [".py"],
                    "db_path": "x.db",
                    "exclude_patterns": ["# 注释", "*.tmp", "!keep/*.tmp"],
                },
            )
            loaded = load_config(path)
            self.assertEqual(("# 注释", "*.tmp", "!keep/*.tmp"), loaded.exclude_patterns)


class ExcludeRuleTests(unittest.TestCase):
    """Directory rules used to miss the directory itself and `build/` style rules."""

    def test_directory_pattern_excludes_the_directory_itself(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            processor, logs = make_processor(
                Path(directory),
                exclude_patterns=("**/node_modules/",),
                watch_dirs=(WATCH_ROOT,),
            )
            self.assertTrue(processor._is_excluded(WATCH_ROOT / "node_modules"))
            processor._process(
                FileEvent(FileEventType.DELETED, WATCH_ROOT / "node_modules", datetime(2026, 1, 1), is_directory=True)
            )
            processor._flush_pending_destructive_events(force=True)
            self.assertEqual([], logs)

    def test_directory_pattern_excludes_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            processor, _ = make_processor(
                Path(directory),
                exclude_patterns=("**/__pycache__/",),
                watch_dirs=(WATCH_ROOT,),
            )
            self.assertTrue(processor._is_excluded(WATCH_ROOT / "pkg" / "__pycache__" / "mod.pyc"))
            self.assertFalse(processor._is_excluded(WATCH_ROOT / "pkg" / "mod.py"))

    def test_plain_directory_name_matches_at_any_depth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            processor, _ = make_processor(Path(directory), exclude_patterns=("build/",), watch_dirs=(WATCH_ROOT,))
            self.assertTrue(processor._is_excluded(WATCH_ROOT / "build" / "out.py"))
            self.assertTrue(processor._is_excluded(WATCH_ROOT / "a" / "build" / "out.py"))
            self.assertFalse(processor._is_excluded(WATCH_ROOT / "build.py"))

    def test_wildcard_pattern_matches_basename_at_any_depth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            processor, _ = make_processor(Path(directory), exclude_patterns=("*.tmp",), watch_dirs=(WATCH_ROOT,))
            self.assertTrue(processor._is_excluded(WATCH_ROOT / "a" / "b" / "x.tmp"))
            self.assertFalse(processor._is_excluded(WATCH_ROOT / "a" / "b" / "x.py"))

    def test_negation_reincludes_a_previously_excluded_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            processor, _ = make_processor(
                Path(directory),
                exclude_patterns=("*.log", "!D:/keep/*.log"),
                watch_dirs=(WATCH_ROOT,),
            )
            self.assertTrue(processor._is_excluded(Path(r"D:\other\a.log")))
            self.assertFalse(processor._is_excluded(Path(r"D:\keep\a.log")))

    def test_absolute_pattern_only_matches_its_own_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            processor, _ = make_processor(
                Path(directory),
                exclude_patterns=(r"D:\temp\*",),
                watch_dirs=(WATCH_ROOT,),
            )
            self.assertTrue(processor._is_excluded(Path(r"D:\temp\a.py")))
            self.assertFalse(processor._is_excluded(Path(r"D:\temp2\a.py")))

    def test_exclude_dir_still_wins_over_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, _ = make_processor(
                root,
                exclude_dirs=(root / "vendor",),
                exclude_patterns=("!*",),
            )
            self.assertTrue(processor._is_excluded(root / "vendor" / "a.py"))


class AtomicSaveTests(unittest.TestCase):
    """Editors save by replacing the file, which arrived as delete + create."""

    def test_delete_then_recreate_becomes_a_single_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            target = root / "report.py"
            target.write_text("print(1)\n", encoding="utf-8")
            now = datetime(2026, 1, 1, 9, 0)

            processor._process(FileEvent(FileEventType.DELETED, target, now))
            processor._process(FileEvent(FileEventType.CREATED, target, now))
            processor._flush_pending_destructive_events(force=True)

            self.assertEqual(1, len(logs))
            self.assertEqual(WorkAction.SAVE_FILE.value, logs[0].action)
            self.assertEqual(str(target), logs[0].file_path)

    def test_delete_of_a_different_path_is_still_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            removed = root / "old.py"
            created = root / "new.py"
            created.write_text("x\n", encoding="utf-8")
            now = datetime(2026, 1, 1, 9, 0)

            processor._process(FileEvent(FileEventType.DELETED, removed, now))
            processor._process(FileEvent(FileEventType.CREATED, created, now))
            processor._flush_pending_destructive_events(force=True)

            self.assertEqual(
                [WorkAction.CREATE_FILE.value, WorkAction.DELETE_FILE.value],
                [event.action for event in logs],
            )


class LockFileTests(unittest.TestCase):
    def test_lock_file_opens_and_closes_the_real_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            document = root / "合同.docx"
            document.write_text("x" * 10, encoding="utf-8")
            lock = root / "~$合同.docx"
            now = datetime(2026, 1, 1, 9, 0)

            processor._process(FileEvent(FileEventType.CREATED, lock, now))
            self.assertEqual(1, len(logs))
            self.assertEqual(WorkAction.OPEN_FILE.value, logs[0].action)
            self.assertEqual(str(document), logs[0].file_path)
            self.assertEqual(10, logs[0].file_size)

            processor._process(FileEvent(FileEventType.DELETED, lock, now))
            processor._flush_expired_closes(now + timedelta(seconds=5))
            self.assertEqual(2, len(logs))
            self.assertEqual(WorkAction.CLOSE_FILE.value, logs[1].action)

    def test_save_after_close_within_debounce_is_reported_as_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            document = root / "预算.docx"
            document.write_text("x", encoding="utf-8")
            lock = root / "~$预算.docx"
            now = datetime(2026, 1, 1, 9, 0)

            processor._process(FileEvent(FileEventType.CREATED, lock, now))
            processor._process(FileEvent(FileEventType.DELETED, lock, now))
            processor._process(FileEvent(FileEventType.CREATED, lock, now + timedelta(seconds=0.3)))

            self.assertEqual(
                [WorkAction.OPEN_FILE.value, WorkAction.SAVE_FILE.value],
                [event.action for event in logs],
            )

    def test_modified_file_with_open_state_is_a_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = make_processor(root)
            document = root / "报表.docx"
            document.write_text("a", encoding="utf-8")
            lock = root / "~$报表.docx"
            now = datetime(2026, 1, 1, 9, 0)

            processor._process(FileEvent(FileEventType.CREATED, lock, now))
            processor._process(FileEvent(FileEventType.MODIFIED, document, now))
            document.write_text("abcdef", encoding="utf-8")
            processor._process(FileEvent(FileEventType.MODIFIED, document, now + timedelta(seconds=2)))

            self.assertEqual(WorkAction.SAVE_FILE.value, logs[-1].action)
            self.assertEqual(6, logs[-1].file_size)


class DatabaseResilienceTests(unittest.TestCase):
    def test_failed_month_query_is_reported_without_killing_the_writer(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            request = database._MonthDaysRequest(year=2026, month=7)
            # No table exists yet: the query fails, and the error path must not
            # raise (it used to touch request.day, which a month request lacks).
            with self.assertLogs("database", level="ERROR"):
                database.DatabaseWriter._execute_query(connection, request)
        finally:
            connection.close()
        self.assertIsNotNone(request.error)
        self.assertTrue(request.completed.is_set())

    def test_submit_after_stop_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = database.DatabaseWriter(Path(directory) / "w.db", flush_seconds=0.05)
            writer.start_and_wait()
            writer.stop()
            with self.assertRaises(RuntimeError):
                writer.submit(LogEvent(datetime.now(), "新建文件", "a.py", r"C:\a.py", 1, "p"))

    def test_stop_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = database.DatabaseWriter(Path(directory) / "w.db", flush_seconds=0.05)
            writer.start_and_wait()
            writer.stop()
            writer.stop()


class ReportBoundaryTests(unittest.TestCase):
    def test_empty_day_returns_none(self) -> None:
        self.assertIsNone(ReportGenerator(lambda _day: []).generate(date(2026, 1, 1)))

    def test_day_with_only_foreground_data(self) -> None:
        moment = datetime(2026, 1, 1, 9, 0)
        logs = [
            LogEvent(moment, WorkAction.BROWSE_PAGE.value, "Docs", "", 30, "chrome.exe"),
            LogEvent(moment, WorkAction.USE_APPLICATION.value, "微信", "", 60, "weixin.exe"),
        ]
        reports = ReportGenerator(lambda _day: logs).generate(date(2026, 1, 1))
        self.assertIsNotNone(reports)
        assert reports is not None
        self.assertIn("浏览记录：1 条", reports.summary)
        self.assertIn("前台记录：1 条", reports.summary)
        self.assertIn("Docs", reports.detailed)
        self.assertIn("微信", reports.detailed)

    def test_modified_count_ignores_office_lock_files(self) -> None:
        moment = datetime(2026, 1, 1, 9, 0)
        logs = [LogEvent(moment, WorkAction.SAVE_FILE.value, "~$草稿.docx", r"D:\p\~$草稿.docx", 1, "p")]
        reports = ReportGenerator(lambda _day: logs).generate(date(2026, 1, 1))
        assert reports is not None
        self.assertIn("修改文件：0 个", reports.summary)

    def test_category_counts_created_files_not_only_saves(self) -> None:
        """A day of new files used to report zero in every category."""
        moment = datetime(2026, 1, 1, 9, 0)
        logs = [
            LogEvent(moment, WorkAction.CREATE_FILE.value, "tool.py", r"D:\p\tool.py", 10, "p"),
            LogEvent(moment, WorkAction.CREATE_FILE.value, "预算.xlsx", r"D:\p\预算.xlsx", 10, "p"),
            LogEvent(moment, WorkAction.DELETE_FOLDER.value, "old", r"D:\p\old", 0, "p"),
        ]
        reports = ReportGenerator(lambda _day: logs).generate(date(2026, 1, 1))
        assert reports is not None
        self.assertIn("代码文件：1 个", reports.summary)
        self.assertIn("表格文件：1 个", reports.summary)
        self.assertIn("新建文件：2 个", reports.summary)
        self.assertIn("修改文件：0 个", reports.summary)


class BrowserTrackingTests(unittest.TestCase):
    def test_real_pages_mentioning_new_tab_are_tracked(self) -> None:
        tracker = PageTracker(minimum_seconds=0)
        moment = datetime(2026, 1, 1, 9, 0)
        tracker.update("chrome.exe", "How to open a new tab - Google", 0.0, moment)
        event = tracker.flush(5.0, moment)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertIn("How to open a new tab", event.file_name)

    def test_new_tab_page_is_ignored(self) -> None:
        tracker = PageTracker(minimum_seconds=0)
        moment = datetime(2026, 1, 1, 9, 0)
        tracker.update("chrome.exe", "新建标签页", 0.0, moment)
        self.assertIsNone(tracker.flush(5.0, moment))

    def test_untracked_browser_is_ignored(self) -> None:
        tracker = PageTracker(minimum_seconds=0)
        moment = datetime(2026, 1, 1, 9, 0)
        tracker.update("unknown.exe", "Some Page", 0.0, moment)
        self.assertIsNone(tracker.flush(5.0, moment))


class LoggingSetupTests(unittest.TestCase):
    def tearDown(self) -> None:
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.addHandler(logging.NullHandler())

    def test_no_console_handler_when_stderr_is_missing(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        with (
            patch("utils.resolve_log_dir", return_value=Path(directory)),
            patch("utils.sys.stderr", None),
        ):
            log_dir = configure_logging()
        self.assertEqual(Path(directory), log_dir)
        handlers = logging.getLogger().handlers
        self.assertEqual(1, len(handlers))
        self.assertIsInstance(handlers[0], logging.handlers.RotatingFileHandler)
        self.assertTrue((Path(directory) / "app.log").is_file())
        # Release the file handle so Windows can delete the directory.
        self.tearDown()

    def test_format_duration_never_goes_negative(self) -> None:
        self.assertEqual((0, 0, 0), format_duration(-5))
        self.assertEqual((1, 1, 1), format_duration(3661))


class ParsingHelperTests(unittest.TestCase):
    def test_dates_between_swaps_reversed_input(self) -> None:
        available = {date(2026, 7, 3), date(2026, 7, 5)}
        self.assertEqual(available, dates_between(date(2026, 7, 5), date(2026, 7, 3), available))

    def test_parse_extensions_deduplicates(self) -> None:
        self.assertEqual([".py", ".md"], parse_extensions("PY .py md"))

    def test_parse_tracked_apps_rejects_broken_lines(self) -> None:
        with self.assertRaises(ValueError):
            parse_tracked_apps("WeChat.exe 微信")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import queue
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from browser import ApplicationTracker, PageTracker
from database import DatabaseWriter
from event_processor import EventProcessor
from events import FileEventType, WorkAction
from config import AppConfig, ensure_user_config, load_config, save_config_updates
from gui import dates_between, parse_extensions, parse_tracked_apps
from input_activity import InputActivityCounter
from models import FileEvent, GeneratedReports, LogEvent
from report import ReportGenerator, write_reports
from startup import build_startup_command
from watchdog_handler import QueueingEventHandler


class CoreTests(unittest.TestCase):
    @staticmethod
    def _file_processor(
        root: Path,
        exclude_dirs: tuple[Path, ...] = (),
    ) -> tuple[EventProcessor, list[LogEvent]]:
        logs: list[LogEvent] = []
        config = AppConfig(
            app_dir=root,
            watch_dirs=(root,),
            exclude_dirs=exclude_dirs,
            exclude_patterns=(),
            target_extensions=frozenset({".py"}),
            db_path=root / "work_log.db",
        )
        return EventProcessor(config, queue.Queue(), logs.append), logs

    def test_watchdog_handler_keeps_directory_delete_and_move_events(self) -> None:
        event_queue: queue.Queue[FileEvent] = queue.Queue()
        handler = QueueingEventHandler(event_queue)

        handler.on_deleted(SimpleNamespace(src_path=r"D:\code", is_directory=True))
        handler.on_moved(
            SimpleNamespace(
                src_path=r"D:\code",
                dest_path=r"D:\archive\code",
                is_directory=True,
            )
        )

        deleted = event_queue.get_nowait()
        moved = event_queue.get_nowait()
        self.assertTrue(deleted.is_directory)
        self.assertEqual(FileEventType.DELETED, deleted.event_type)
        self.assertTrue(moved.is_directory)
        self.assertEqual(Path(r"D:\archive\code"), moved.destination)

    def test_directory_delete_collapses_descendant_file_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = self._file_processor(root)
            project = root / "large_project"
            now = datetime(2026, 7, 24, 14, 0)

            for index in range(100):
                processor._process(
                    FileEvent(FileEventType.DELETED, project / f"module_{index}.py", now)
                )
            processor._process(
                FileEvent(
                    FileEventType.DELETED,
                    project / "package" / "nested",
                    now,
                    is_directory=True,
                )
            )
            processor._process(
                FileEvent(
                    FileEventType.DELETED,
                    project / "package",
                    now,
                    is_directory=True,
                )
            )
            processor._process(
                FileEvent(FileEventType.DELETED, project, now, is_directory=True)
            )
            # Some backends can deliver a few child events after the directory event.
            processor._process(FileEvent(FileEventType.DELETED, project / "late.py", now))
            processor._flush_pending_destructive_events(force=True)

            self.assertEqual(1, len(logs))
            self.assertEqual(WorkAction.DELETE_FOLDER.value, logs[0].action)
            self.assertEqual("large_project", logs[0].file_name)
            self.assertEqual(str(project), logs[0].file_path)

    def test_directory_move_collapses_descendant_file_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = self._file_processor(root)
            source = root / "large_project"
            destination = root / "archive" / "large_project"
            now = datetime(2026, 7, 24, 14, 0)

            for index in range(100):
                processor._process(
                    FileEvent(
                        FileEventType.MOVED,
                        source / f"module_{index}.py",
                        now,
                        destination / f"module_{index}.py",
                    )
                )
            processor._process(
                FileEvent(
                    FileEventType.MOVED,
                    source / "package",
                    now,
                    destination / "package",
                    is_directory=True,
                )
            )
            processor._process(
                FileEvent(
                    FileEventType.MOVED,
                    source,
                    now,
                    destination,
                    is_directory=True,
                )
            )
            processor._process(FileEvent(FileEventType.CREATED, destination / "late.py", now))
            processor._flush_pending_destructive_events(force=True)

            self.assertEqual(1, len(logs))
            self.assertEqual(WorkAction.MOVE_FOLDER.value, logs[0].action)
            self.assertIn(str(source), logs[0].file_path)
            self.assertIn(str(destination), logs[0].file_path)

    def test_standalone_file_delete_is_still_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor, logs = self._file_processor(root)
            file_path = root / "single.py"
            processor._process(
                FileEvent(FileEventType.DELETED, file_path, datetime(2026, 7, 24, 14, 0))
            )
            processor._flush_pending_destructive_events(force=True)

            self.assertEqual(1, len(logs))
            self.assertEqual(WorkAction.DELETE_FILE.value, logs[0].action)
            self.assertEqual(str(file_path), logs[0].file_path)

    def test_move_to_recycle_bin_is_recorded_as_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recycle_bin = root / "$RECYCLE.BIN"
            processor, logs = self._file_processor(root, (recycle_bin,))
            project = root / "deleted_project"
            file_path = root / "deleted_file.py"
            now = datetime(2026, 7, 24, 14, 0)

            processor._process(
                FileEvent(
                    FileEventType.MOVED,
                    project,
                    now,
                    recycle_bin / "S-1-5-21" / "$RPROJECT",
                    is_directory=True,
                )
            )
            processor._process(
                FileEvent(
                    FileEventType.MOVED,
                    file_path,
                    now,
                    recycle_bin / "S-1-5-21" / "$RFILE.py",
                )
            )
            processor._flush_pending_destructive_events(force=True)

            self.assertEqual(2, len(logs))
            self.assertEqual(
                {
                    str(project): WorkAction.DELETE_FOLDER.value,
                    str(file_path): WorkAction.DELETE_FILE.value,
                },
                {event.file_path: event.action for event in logs},
            )

    def test_default_config_is_released_once_without_overwriting_user_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template_path = root / "default_config.json"
            config_path = root / "app" / "config.json"
            template_path.write_text('{"value": "default"}\n', encoding="utf-8")

            result = ensure_user_config(config_path, template_path)
            self.assertEqual(config_path, result)
            self.assertEqual('{"value": "default"}\n', config_path.read_text(encoding="utf-8"))

            config_path.write_text('{"value": "custom"}\n', encoding="utf-8")
            ensure_user_config(config_path, template_path)
            self.assertEqual('{"value": "custom"}\n', config_path.read_text(encoding="utf-8"))

    def test_default_load_releases_bundled_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template_path = root / "default_config.json"
            template_path.write_text(
                json.dumps(
                    {
                        "watch_dirs": [directory],
                        "target_extensions": [".py"],
                        "db_path": "work_log.db",
                    }
                ),
                encoding="utf-8",
            )
            app_dir = root / "app"
            with (
                patch("config.get_app_dir", return_value=app_dir),
                patch("config.get_resource_path", return_value=template_path),
            ):
                loaded = load_config()

            self.assertEqual(app_dir, loaded.app_dir)
            self.assertTrue((app_dir / "config.json").is_file())

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
                writer.submit(LogEvent(now, WorkAction.MOUSE_CLICK.value, "鼠标点击", "", 7, "input"))
                writer.submit(LogEvent(now, WorkAction.KEYSTROKE.value, "键盘敲击", "", 19, "input"))
                logs = writer.fetch_day(now.date())
                self.assertEqual(6, len(logs))
                reports = ReportGenerator(writer.fetch_day).generate(now.date())
                self.assertIsNotNone(reports)
                self.assertIn("代码文件：1 个", reports.summary)
                self.assertIn("Docs", reports.detailed)
                self.assertIn("微信（0 分钟 12 秒）", reports.summary)
                self.assertIn("wechat.exe", reports.detailed)
                self.assertIn("鼠标点击：7 次", reports.summary)
                self.assertIn("键盘敲击 19 次", reports.detailed)
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

    def test_settings_parsing_and_atomic_config_save(self) -> None:
        self.assertEqual([".py", ".md", ".docx"], parse_extensions("py, .md; DOCX py"))
        self.assertEqual(
            {"WeChat.exe": "微信", "WXWork.exe": "企业微信"},
            parse_tracked_apps("WeChat.exe = 微信\n# comment\nWXWork.exe=企业微信"),
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "watch_dirs": [directory],
                        "target_extensions": [".py"],
                        "db_path": "work_log.db",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = save_config_updates({"database_batch_size": 25}, config_path)
            self.assertEqual(25, result.database_batch_size)
            self.assertEqual(25, json.loads(config_path.read_text(encoding="utf-8"))["database_batch_size"])

    def test_startup_command_uses_background_python_launcher(self) -> None:
        command = build_startup_command(Path(__file__).resolve().parents[1])
        self.assertIn("main.py", command)
        self.assertTrue("pythonw.exe" in command.lower() or "python.exe" in command.lower())

    def test_input_counter_rolls_over_without_storing_input_details(self) -> None:
        events: list[LogEvent] = []
        counter = InputActivityCounter(events.append)
        counter.record_mouse_click(datetime(2026, 7, 24, 9, 5))
        counter.record_mouse_click(datetime(2026, 7, 24, 9, 30))
        counter.record_keystroke(datetime(2026, 7, 24, 9, 45))
        counter.record_keystroke(datetime(2026, 7, 24, 10, 1))
        self.assertEqual(2, len(events))
        self.assertEqual(
            [(WorkAction.MOUSE_CLICK.value, 2), (WorkAction.KEYSTROKE.value, 1)],
            [(event.action, event.file_size) for event in events],
        )
        self.assertTrue(all(event.file_path == "" for event in events))
        counter.flush()
        self.assertEqual(3, len(events))
        self.assertEqual(1, events[-1].file_size)


if __name__ == "__main__":
    unittest.main()

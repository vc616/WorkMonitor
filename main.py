from __future__ import annotations

import logging
import queue
import sys
import threading
from datetime import date

from browser import BrowserMonitor
from config import AppConfig, load_config
from database import DatabaseWriter
from event_processor import EventProcessor
from input_activity import InputActivityMonitor
from models import FileEvent, GeneratedReports, LogEvent
from report import ReportGenerator
from utils import configure_logging, configure_tcl_environment
from watchdog_handler import FileSystemMonitor

LOGGER = logging.getLogger(__name__)

DROP_WARNING_INTERVAL = 50


class MonitorApplication:
    def __init__(self, config: AppConfig):
        self.config = config
        self._file_queue: queue.Queue[FileEvent] = queue.Queue(maxsize=config.queue_max_size)
        self._database = DatabaseWriter(
            config.db_path,
            batch_size=config.database_batch_size,
            flush_seconds=config.database_flush_seconds,
            queue_max_size=config.queue_max_size,
        )
        self._processor = EventProcessor(config, self._file_queue, self._submit_log)
        self._browser = BrowserMonitor(
            self._submit_log,
            poll_seconds=config.browser_poll_seconds,
            minimum_seconds=config.browser_minimum_seconds,
            tracked_apps=dict(config.tracked_apps),
            application_minimum_seconds=config.application_minimum_seconds,
        )
        self._filesystem = FileSystemMonitor(self._file_queue, config.watch_dirs)
        self._input_activity = InputActivityMonitor(self._submit_log)
        self._reporter = ReportGenerator(self._database.fetch_day)
        self._shutdown_lock = threading.Lock()
        self._dropped_logs = 0
        self._dropped_lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._database.start_and_wait()
        try:
            self._processor.start()
            self._filesystem.start()
            self._browser.start()
            if self.config.input_activity_enabled:
                self._input_activity.start()
        except BaseException:
            LOGGER.exception("监控启动失败，正在清理")
            self.shutdown()
            raise
        self._started = True
        LOGGER.info("工作监控 V2 已启动")

    def generate_report(self, day: date) -> GeneratedReports | None:
        # Only today's report needs the live input bucket. Snapshotting here
        # submits increments only, so repeated generation stays idempotent and
        # historical reports never touch real-time counters.
        if day == date.today():
            self._input_activity.snapshot()
        return self._reporter.generate(day)

    def available_days(self, year: int, month: int) -> set[date]:
        return self._database.fetch_available_days(year, month)

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if not self._started and not self._database.is_alive():
                return
            LOGGER.info("正在停止工作监控")
            steps = (
                ("文件监控", self._filesystem.stop),
                ("输入统计", self._input_activity.stop),
                ("前台监控", self._browser.stop),
                ("事件处理", self._processor.stop),
                ("数据库", self._database.stop),
            )
            # One slow component must not prevent the database from flushing.
            for name, stop in steps:
                try:
                    stop()
                except Exception:
                    LOGGER.exception("停止 %s 失败", name)
            self._started = False
            LOGGER.info("工作监控已停止")

    def _submit_log(self, event: LogEvent) -> None:
        try:
            self._database.submit(event)
        except RuntimeError:
            with self._dropped_lock:
                self._dropped_logs += 1
                if self._dropped_logs % DROP_WARNING_INTERVAL == 1:
                    LOGGER.warning(
                        "数据库已停止，累计丢弃 %d 条日志（最近: %s）",
                        self._dropped_logs,
                        event.action,
                    )


def run_app() -> None:
    configure_tcl_environment()
    # The same writable directory hosts app.log and the generated reports.
    log_dir = configure_logging()
    config = load_config()
    application = MonitorApplication(config)
    application.start()
    try:
        # Imported lazily so a broken Tcl/Tk install is logged instead of
        # crashing before logging exists.
        from gui import MonitorGui
    except ImportError as exc:
        application.shutdown()
        raise RuntimeError(
            "无法加载图形界面，请确认 Python 安装包含 Tcl/Tk（tkinter）"
        ) from exc
    try:
        gui = MonitorGui(
            log_dir,
            application.generate_report,
            application.shutdown,
            application.available_days,
            config.app_dir / "config.json",
        )
        gui.run()
    except KeyboardInterrupt:
        application.shutdown()
    except BaseException:
        application.shutdown()
        raise


if __name__ == "__main__":
    try:
        run_app()
    except BaseException:
        LOGGER.exception("应用启动失败")
        # A windowed build has no console. Leave a trace even when logging
        # itself failed to initialise (for example on a read-only install).
        if getattr(sys, "frozen", False):
            try:
                import tempfile
                import traceback
                from pathlib import Path

                emergency = Path(tempfile.gettempdir()) / "WorkMonitorV2" / "startup_error.log"
                emergency.parent.mkdir(parents=True, exist_ok=True)
                emergency.write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass
        logging.shutdown()
        if not getattr(sys, "frozen", False):
            raise

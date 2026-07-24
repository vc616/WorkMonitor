from __future__ import annotations

import logging
import queue
import threading
from datetime import date

from browser import BrowserMonitor
from config import AppConfig, load_config
from database import DatabaseWriter
from event_processor import EventProcessor
from gui import MonitorGui
from models import FileEvent, GeneratedReports, LogEvent
from report import ReportGenerator
from utils import configure_logging
from watchdog_handler import FileSystemMonitor

LOGGER = logging.getLogger(__name__)


class MonitorApplication:
    def __init__(self, config: AppConfig):
        self.config = config
        self._file_queue: queue.Queue[FileEvent] = queue.Queue()
        self._database = DatabaseWriter(
            config.db_path,
            batch_size=config.database_batch_size,
            flush_seconds=config.database_flush_seconds,
        )
        self._processor = EventProcessor(config, self._file_queue, self._submit_log)
        self._browser = BrowserMonitor(
            self._submit_log,
            poll_seconds=config.browser_poll_seconds,
            minimum_seconds=config.browser_minimum_seconds,
        )
        self._filesystem = FileSystemMonitor(self._file_queue, config.watch_dirs)
        self._reporter = ReportGenerator(self._database.fetch_day)
        self._shutdown_lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._database.start_and_wait()
        try:
            self._processor.start()
            self._filesystem.start()
            self._browser.start()
        except BaseException:
            LOGGER.exception("监控启动失败，正在清理")
            self.shutdown()
            raise
        self._started = True
        LOGGER.info("工作监控 V2 已启动")

    def generate_report(self, day: date) -> GeneratedReports | None:
        return self._reporter.generate(day)

    def available_days(self, year: int, month: int) -> set[date]:
        return self._database.fetch_available_days(year, month)

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if not self._started and not self._database.is_alive():
                return
            LOGGER.info("正在停止工作监控")
            self._filesystem.stop()
            self._browser.stop()
            if self._processor.is_alive():
                self._processor.stop()
            if self._database.is_alive():
                self._database.stop()
            self._started = False
            LOGGER.info("工作监控已停止")

    def _submit_log(self, event: LogEvent) -> None:
        self._database.submit(event)


def run_app() -> None:
    configure_logging()
    config = load_config()
    application = MonitorApplication(config)
    application.start()
    try:
        gui = MonitorGui(
            config.app_dir,
            application.generate_report,
            application.shutdown,
            application.available_days,
        )
        gui.run()
    except KeyboardInterrupt:
        application.shutdown()
    except BaseException:
        application.shutdown()
        raise


if __name__ == "__main__":
    run_app()

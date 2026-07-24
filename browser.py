from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

try:
    import psutil
except ImportError:  # pragma: no cover - optional at import time for report tooling
    psutil = None  # type: ignore[assignment]

from events import WorkAction
from models import LogEvent

LOGGER = logging.getLogger(__name__)

BROWSERS = frozenset({"msedge.exe", "chrome.exe", "firefox.exe", "360se.exe", "brave.exe"})
NEW_TAB_TITLES = ("new tab", "新标签页", "新分頁", "新しいタブ")


def get_active_window_info() -> tuple[str | None, str | None]:
    try:
        import win32gui
        import win32process

        window = win32gui.GetForegroundWindow()
        if not window:
            return None, None
        title = win32gui.GetWindowText(window)
        _, process_id = win32process.GetWindowThreadProcessId(window)
        if psutil is None:
            return None, title
        process_name = psutil.Process(process_id).name().lower()
        return process_name, title
    except Exception:
        LOGGER.debug("读取前台窗口失败", exc_info=True)
        return None, None


@dataclass(slots=True)
class PageTracker:
    minimum_seconds: float
    browser: str | None = None
    title: str | None = None
    accumulated: float = 0.0
    last_check: float | None = None

    def update(
        self,
        browser: str | None,
        title: str | None,
        monotonic_now: float,
        timestamp: datetime,
    ) -> LogEvent | None:
        active = browser in BROWSERS and bool(title) and not self._is_new_tab(title or "")
        page = (browser, title) if active else (None, None)
        current = (self.browser, self.title)

        if page == current and active:
            self._accumulate(monotonic_now)
            return None

        event = self.flush(monotonic_now, timestamp)
        if active:
            self.browser = browser
            self.title = title
            self.last_check = monotonic_now
        return event

    def flush(self, monotonic_now: float, timestamp: datetime) -> LogEvent | None:
        self._accumulate(monotonic_now)
        event: LogEvent | None = None
        if self.browser and self.title and self.accumulated >= self.minimum_seconds:
            event = LogEvent(
                timestamp=timestamp,
                action=WorkAction.BROWSE_PAGE.value,
                file_name=self.title,
                file_path="",
                file_size=round(self.accumulated),
                project_dir=self.browser,
            )
        self.browser = None
        self.title = None
        self.accumulated = 0.0
        self.last_check = None
        return event

    def _accumulate(self, monotonic_now: float) -> None:
        if self.last_check is not None:
            self.accumulated += max(0.0, monotonic_now - self.last_check)
            self.last_check = monotonic_now

    @staticmethod
    def _is_new_tab(title: str) -> bool:
        lowered = title.lower()
        return any(marker in lowered for marker in NEW_TAB_TITLES)


@dataclass(slots=True)
class ApplicationTracker:
    tracked_apps: dict[str, str]
    minimum_seconds: float
    process_name: str | None = None
    application_name: str | None = None
    accumulated: float = 0.0
    last_check: float | None = None

    def __post_init__(self) -> None:
        self.tracked_apps = {process.lower(): name for process, name in self.tracked_apps.items()}

    def update(
        self,
        process_name: str | None,
        monotonic_now: float,
        timestamp: datetime,
    ) -> LogEvent | None:
        normalized = process_name.lower() if process_name else None
        application = self.tracked_apps.get(normalized or "")
        active_process = normalized if application else None

        if active_process == self.process_name and application is not None:
            self._accumulate(monotonic_now)
            return None

        event = self.flush(monotonic_now, timestamp)
        if application is not None:
            self.process_name = active_process
            self.application_name = application
            self.last_check = monotonic_now
        return event

    def flush(self, monotonic_now: float, timestamp: datetime) -> LogEvent | None:
        self._accumulate(monotonic_now)
        event: LogEvent | None = None
        if self.process_name and self.application_name and self.accumulated >= self.minimum_seconds:
            event = LogEvent(
                timestamp=timestamp,
                action=WorkAction.USE_APPLICATION.value,
                file_name=self.application_name,
                file_path="",
                file_size=round(self.accumulated),
                project_dir=self.process_name,
            )
        self.process_name = None
        self.application_name = None
        self.accumulated = 0.0
        self.last_check = None
        return event

    def _accumulate(self, monotonic_now: float) -> None:
        if self.last_check is not None:
            self.accumulated += max(0.0, monotonic_now - self.last_check)
            self.last_check = monotonic_now


class BrowserMonitor(threading.Thread):
    def __init__(
        self,
        submit_log: Callable[[LogEvent], None],
        poll_seconds: float = 1.0,
        minimum_seconds: float = 3.0,
        tracked_apps: dict[str, str] | None = None,
        application_minimum_seconds: float = 3.0,
        active_window_provider: Callable[[], tuple[str | None, str | None]] = get_active_window_info,
    ):
        super().__init__(name="foreground-monitor", daemon=True)
        self._submit_log = submit_log
        self._poll_seconds = poll_seconds
        self._active_window_provider = active_window_provider
        self._stop_event = threading.Event()
        self._tracker = PageTracker(minimum_seconds=minimum_seconds)
        self._application_tracker = ApplicationTracker(
            tracked_apps=tracked_apps or {},
            minimum_seconds=application_minimum_seconds,
        )

    def stop(self, timeout: float = 3.0) -> None:
        if not self.is_alive():
            return
        self._stop_event.set()
        self.join(timeout)
        if self.is_alive():
            LOGGER.warning("前台窗口监控未能在 %.1f 秒内停止", timeout)

    def run(self) -> None:
        LOGGER.info("前台窗口监控已启动，跟踪 %d 个应用进程", len(self._application_tracker.tracked_apps))
        while not self._stop_event.is_set():
            try:
                browser, title = self._active_window_provider()
            except Exception:
                LOGGER.exception("浏览器窗口采集失败")
                browser, title = None, None
            monotonic_now = time.monotonic()
            timestamp = datetime.now()
            page_event = self._tracker.update(browser, title, monotonic_now, timestamp)
            application_event = self._application_tracker.update(browser, monotonic_now, timestamp)
            for event in (page_event, application_event):
                if event is not None:
                    self._emit(event)
            self._stop_event.wait(self._poll_seconds)

        monotonic_now = time.monotonic()
        timestamp = datetime.now()
        for event in (
            self._tracker.flush(monotonic_now, timestamp),
            self._application_tracker.flush(monotonic_now, timestamp),
        ):
            if event is not None:
                self._emit(event)
        LOGGER.info("前台窗口监控已停止")

    def _emit(self, event: LogEvent) -> None:
        self._submit_log(event)
        if event.action == WorkAction.USE_APPLICATION.value:
            LOGGER.info("应用 | %s | 进程: %s | 前台: %d 秒", event.file_name, event.project_dir, event.file_size)
        else:
            LOGGER.info(
                "网页 | %s | 浏览器: %s | 停留: %d 秒",
                event.file_name,
                event.project_dir,
                event.file_size,
            )

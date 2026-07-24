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


class BrowserMonitor(threading.Thread):
    def __init__(
        self,
        submit_log: Callable[[LogEvent], None],
        poll_seconds: float = 1.0,
        minimum_seconds: float = 3.0,
        active_window_provider: Callable[[], tuple[str | None, str | None]] = get_active_window_info,
    ):
        super().__init__(name="browser-monitor", daemon=True)
        self._submit_log = submit_log
        self._poll_seconds = poll_seconds
        self._active_window_provider = active_window_provider
        self._stop_event = threading.Event()
        self._tracker = PageTracker(minimum_seconds=minimum_seconds)

    def stop(self, timeout: float = 3.0) -> None:
        if not self.is_alive():
            return
        self._stop_event.set()
        self.join(timeout)
        if self.is_alive():
            LOGGER.warning("浏览器监控未能在 %.1f 秒内停止", timeout)

    def run(self) -> None:
        LOGGER.info("浏览器监控已启动")
        while not self._stop_event.is_set():
            try:
                browser, title = self._active_window_provider()
            except Exception:
                LOGGER.exception("浏览器窗口采集失败")
                browser, title = None, None
            event = self._tracker.update(browser, title, time.monotonic(), datetime.now())
            if event is not None:
                self._emit(event)
            self._stop_event.wait(self._poll_seconds)

        event = self._tracker.flush(time.monotonic(), datetime.now())
        if event is not None:
            self._emit(event)
        LOGGER.info("浏览器监控已停止")

    def _emit(self, event: LogEvent) -> None:
        self._submit_log(event)
        LOGGER.info(
            "网页 | %s | 浏览器: %s | 停留: %d 秒",
            event.file_name,
            event.project_dir,
            event.file_size,
        )

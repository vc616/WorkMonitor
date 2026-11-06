from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, time as datetime_time, timedelta

try:
    import psutil
except ImportError:  # pragma: no cover - optional at import time for report tooling
    psutil = None  # type: ignore[assignment]

from events import WorkAction
from models import LogEvent

LOGGER = logging.getLogger(__name__)

BROWSERS = frozenset(
    {
        "msedge.exe",
        "chrome.exe",
        "firefox.exe",
        "brave.exe",
        "opera.exe",
        "vivaldi.exe",
        "arc.exe",
        "360se.exe",
        "360chrome.exe",
        "qqbrowser.exe",
        "sogouexplorer.exe",
        "2345explorer.exe",
        "liebao.exe",
    }
)
NEW_TAB_TITLES = frozenset(
    {
        "new tab",
        "newtab",
        "about:blank",
        "about:newtab",
        "新标签页",
        "新建标签页",
        "新分頁",
        "新增分頁",
        "新しいタブ",
        "새 탭",
    }
)


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


class SessionTracker:
    """Shared duration accounting for a foreground session.

    Elapsed time always comes from ``time.monotonic()``. Wall-clock values are
    used only to attribute the elapsed interval to the calendar day it belongs
    to, so a session crossing midnight is stored as one event per day, each
    stamped with its own start time instead of the session's end time.
    """

    def __init__(self, minimum_seconds: float):
        self.minimum_seconds = minimum_seconds
        self._key: tuple | None = None
        self._started_at: datetime | None = None
        self._last_check: float | None = None
        self._day_seconds: dict[date, float] = {}
        self._pending: list[LogEvent] = []

    def drain_events(self) -> list[LogEvent]:
        """Return events settled earlier than the one returned by ``flush``."""
        events = self._pending
        self._pending = []
        return events

    def _advance(
        self,
        key: tuple | None,
        active: bool,
        monotonic_now: float,
        timestamp: datetime,
    ) -> LogEvent | None:
        if key == self._key and active and self._key is not None:
            self._accumulate(monotonic_now, timestamp)
            return None
        event = self.flush(monotonic_now, timestamp)
        if active:
            self._key = key
            self._started_at = timestamp
            self._last_check = monotonic_now
            self._day_seconds = {}
        return event

    def flush(self, monotonic_now: float, timestamp: datetime) -> LogEvent | None:
        self._accumulate(monotonic_now, timestamp)
        events: list[LogEvent] = []
        started_at = self._started_at or timestamp
        if self._key is not None and sum(self._day_seconds.values()) >= self.minimum_seconds:
            for day in sorted(self._day_seconds):
                seconds = int(round(self._day_seconds[day]))
                if seconds <= 0:
                    continue
                event_time = (
                    started_at
                    if day == started_at.date()
                    else datetime.combine(day, datetime_time.min)
                )
                events.append(self._make_event(event_time, seconds))
        self._key = None
        self._started_at = None
        self._last_check = None
        self._day_seconds = {}
        # Keep ``flush`` returning a single event for existing callers; split
        # sessions expose the remaining days through ``drain_events``.
        self._pending.extend(events[:-1])
        return events[-1] if events else None

    def _accumulate(self, monotonic_now: float, timestamp: datetime) -> None:
        if self._last_check is None:
            self._last_check = monotonic_now
            return
        elapsed = max(0.0, monotonic_now - self._last_check)
        self._last_check = monotonic_now
        if elapsed <= 0 or self._key is None:
            return
        segment_start = timestamp - timedelta(seconds=elapsed)
        if segment_start.date() == timestamp.date():
            self._day_seconds[timestamp.date()] = self._day_seconds.get(timestamp.date(), 0.0) + elapsed
            return
        boundary = datetime.combine(timestamp.date(), datetime_time.min)
        before = max(0.0, (boundary - segment_start).total_seconds())
        after = max(0.0, (timestamp - boundary).total_seconds())
        if before:
            self._day_seconds[segment_start.date()] = self._day_seconds.get(segment_start.date(), 0.0) + before
        if after:
            self._day_seconds[timestamp.date()] = self._day_seconds.get(timestamp.date(), 0.0) + after

    def _make_event(self, event_time: datetime, seconds: int) -> LogEvent:
        raise NotImplementedError


class PageTracker(SessionTracker):
    def update(
        self,
        browser: str | None,
        title: str | None,
        monotonic_now: float,
        timestamp: datetime,
    ) -> LogEvent | None:
        cleaned_title = title.strip() if title else ""
        active = browser in BROWSERS and bool(cleaned_title) and not self._is_new_tab(cleaned_title)
        key = (browser, cleaned_title) if active else None
        return self._advance(key, active, monotonic_now, timestamp)

    def _make_event(self, event_time: datetime, seconds: int) -> LogEvent:
        browser, title = self._key  # type: ignore[misc]
        return LogEvent(
            timestamp=event_time,
            action=WorkAction.BROWSE_PAGE.value,
            file_name=title,
            file_path="",
            file_size=seconds,
            project_dir=browser,
        )

    @staticmethod
    def _is_new_tab(title: str) -> bool:
        normalized = " ".join(title.split()).lower()
        return normalized in NEW_TAB_TITLES


class ApplicationTracker(SessionTracker):
    def __init__(self, tracked_apps: dict[str, str], minimum_seconds: float):
        super().__init__(minimum_seconds)
        self.tracked_apps = {process.lower(): name for process, name in tracked_apps.items()}

    def update(
        self,
        process_name: str | None,
        monotonic_now: float,
        timestamp: datetime,
    ) -> LogEvent | None:
        normalized = process_name.lower() if process_name else None
        application = self.tracked_apps.get(normalized or "")
        active = application is not None
        key = (normalized, application) if active else None
        return self._advance(key, active, monotonic_now, timestamp)

    def _make_event(self, event_time: datetime, seconds: int) -> LogEvent:
        process_name, application_name = self._key  # type: ignore[misc]
        return LogEvent(
            timestamp=event_time,
            action=WorkAction.USE_APPLICATION.value,
            file_name=application_name,
            file_path="",
            file_size=seconds,
            project_dir=process_name,
        )


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
            self._settle(page_event, application_event)
            self._stop_event.wait(self._poll_seconds)

        monotonic_now = time.monotonic()
        timestamp = datetime.now()
        self._settle(
            self._tracker.flush(monotonic_now, timestamp),
            self._application_tracker.flush(monotonic_now, timestamp),
        )
        LOGGER.info("前台窗口监控已停止")

    def _settle(self, *events: LogEvent | None) -> None:
        for tracker in (self._tracker, self._application_tracker):
            for event in tracker.drain_events():
                self._emit(event)
        for event in events:
            if event is not None:
                self._emit(event)

    def _emit(self, event: LogEvent) -> None:
        self._submit_log(event)
        # Titles and application names are sensitive; keep them out of app.log.
        LOGGER.debug("前台会话已记录 | %s | %d 秒", event.action, event.file_size)

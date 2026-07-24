from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from events import WorkAction
from models import LogEvent

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class HourBucket:
    hour_start: datetime
    first_event: datetime
    mouse_clicks: int = 0
    keystrokes: int = 0


class InputActivityCounter:
    def __init__(self, submit_log: Callable[[LogEvent], None]):
        self._submit_log = submit_log
        self._lock = threading.Lock()
        self._bucket: HourBucket | None = None

    def record_mouse_click(self, occurred_at: datetime | None = None) -> None:
        self._record("mouse", occurred_at or datetime.now())

    def record_keystroke(self, occurred_at: datetime | None = None) -> None:
        self._record("keyboard", occurred_at or datetime.now())

    def flush(self) -> None:
        with self._lock:
            bucket = self._bucket
            self._bucket = None
        if bucket is not None:
            self._emit(bucket)

    def _record(self, activity: str, occurred_at: datetime) -> None:
        hour_start = occurred_at.replace(minute=0, second=0, microsecond=0)
        completed: HourBucket | None = None
        with self._lock:
            if self._bucket is None:
                self._bucket = HourBucket(hour_start=hour_start, first_event=occurred_at)
            elif self._bucket.hour_start != hour_start:
                completed = self._bucket
                self._bucket = HourBucket(hour_start=hour_start, first_event=occurred_at)
            if activity == "mouse":
                self._bucket.mouse_clicks += 1
            else:
                self._bucket.keystrokes += 1
        if completed is not None:
            self._emit(completed)

    def _emit(self, bucket: HourBucket) -> None:
        for action, label, count in (
            (WorkAction.MOUSE_CLICK.value, "鼠标点击", bucket.mouse_clicks),
            (WorkAction.KEYSTROKE.value, "键盘敲击", bucket.keystrokes),
        ):
            if count == 0:
                continue
            self._submit_log(
                LogEvent(
                    timestamp=bucket.first_event,
                    action=action,
                    file_name=label,
                    file_path="",
                    file_size=count,
                    project_dir="input",
                )
            )


class InputActivityMonitor:
    def __init__(self, submit_log: Callable[[LogEvent], None]):
        self._counter = InputActivityCounter(submit_log)
        self._keyboard_listener = None
        self._mouse_listener = None
        self._started = False

    def start(self) -> bool:
        try:
            from pynput import keyboard, mouse

            self._keyboard_listener = keyboard.Listener(on_press=self._on_key_press)
            self._mouse_listener = mouse.Listener(on_click=self._on_mouse_click)
            self._keyboard_listener.start()
            self._mouse_listener.start()
            self._started = True
            LOGGER.info("键盘与鼠标计数已启动（仅统计次数）")
            return True
        except Exception:
            LOGGER.warning("键盘与鼠标计数启动失败，其他监控不受影响", exc_info=True)
            self.stop()
            return False

    def snapshot(self) -> None:
        if self._started:
            self._counter.flush()

    def stop(self) -> None:
        for listener in (self._keyboard_listener, self._mouse_listener):
            if listener is not None:
                try:
                    listener.stop()
                    listener.join(timeout=2)
                except Exception:
                    LOGGER.debug("停止输入监听器失败", exc_info=True)
        if self._started:
            self._counter.flush()
        self._started = False
        self._keyboard_listener = None
        self._mouse_listener = None

    def _on_key_press(self, _key: object) -> None:
        self._counter.record_keystroke()

    def _on_mouse_click(self, _x: int, _y: int, _button: object, pressed: bool) -> None:
        if pressed:
            self._counter.record_mouse_click()

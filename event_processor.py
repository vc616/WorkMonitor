from __future__ import annotations

import fnmatch
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - optional at import time for report tooling
    psutil = None  # type: ignore[assignment]

from config import AppConfig
from events import FileEventType, WorkAction
from models import DebounceState, FileEvent, FileState, LogEvent

LOGGER = logging.getLogger(__name__)

KNOWN_EDITORS = frozenset(
    {
        "acad",
        "acadlt",
        "excel",
        "winword",
        "wps",
        "et",
        "wpp",
        "foxitpdfeditor",
        "foxitreader",
        "acrord32",
        "acrord64",
        "code",
        "pycharm64",
        "notepad++",
    }
)


class EventProcessor(threading.Thread):
    """Owns all mutable file state and converts raw events to log events."""

    def __init__(
        self,
        config: AppConfig,
        event_queue: queue.Queue[FileEvent],
        submit_log: Callable[[LogEvent], None],
    ):
        super().__init__(name="event-processor", daemon=True)
        self._config = config
        self._event_queue = event_queue
        self._submit_log = submit_log
        self._stop_event = threading.Event()
        self._file_states: dict[str, FileState] = {}
        self._debounce: dict[str, DebounceState] = {}
        self._snapshots: dict[str, tuple[int, int]] = {}
        self._pending_close: dict[str, datetime] = {}
        self._paths: dict[str, Path] = {}
        self._exclude_dirs = tuple(self._key(path) for path in config.exclude_dirs)
        self._watch_dirs = tuple(self._key(path) for path in config.watch_dirs)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        self.join(timeout)
        if self.is_alive():
            raise TimeoutError("EventProcessor 未能按时停止")

    def run(self) -> None:
        LOGGER.info("文件事件处理器已启动")
        while not self._stop_event.is_set() or not self._event_queue.empty():
            try:
                event = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                self._flush_expired_closes(datetime.now())
                continue
            try:
                self._process(event)
            except Exception:
                LOGGER.exception("处理文件事件失败: %s %s", event.event_type, event.path)
            finally:
                self._event_queue.task_done()
            self._flush_expired_closes(datetime.now())

        self._flush_expired_closes(datetime.max)
        LOGGER.info("文件事件处理器已停止")

    def _process(self, event: FileEvent) -> None:
        if event.event_type is FileEventType.MOVED:
            self._handle_moved(event)
            return
        if self._is_excluded(event.path):
            return

        filename = event.path.name.lower()
        if self._is_lock_file(event.path):
            self._handle_lock_event(event)
            return
        if not self._is_target(event.path) or ".sv$" in filename or ".tmp" in filename:
            return

        if event.event_type is FileEventType.MODIFIED:
            self._handle_modified(event)
        elif event.event_type is FileEventType.CREATED:
            self._emit(WorkAction.CREATE_FILE, event.path, self._file_size(event.path), event.occurred_at)
        elif event.event_type is FileEventType.DELETED:
            self._handle_deleted(event)

    def _handle_lock_event(self, event: FileEvent) -> None:
        real_path = self._real_path_from_lock(event.path)
        if real_path is None or not self._is_target(real_path):
            return
        key = self._key(real_path)
        self._paths[key] = real_path

        if event.event_type is FileEventType.CREATED:
            if self._is_duplicate(key, WorkAction.OPEN_FILE.value, event.occurred_at):
                return
            close_time = self._pending_close.pop(key, None)
            size = self._file_size(real_path)
            self._file_states[key] = FileState(event.occurred_at, size, size)
            if close_time and (event.occurred_at - close_time).total_seconds() <= self._config.debounce_seconds:
                self._emit(WorkAction.SAVE_FILE, real_path, size, event.occurred_at)
            else:
                self._emit(WorkAction.OPEN_FILE, real_path, size, event.occurred_at)
            return

        if event.event_type is FileEventType.DELETED:
            if self._is_duplicate(key, WorkAction.CLOSE_FILE.value, event.occurred_at):
                return
            self._pending_close[key] = event.occurred_at

    def _handle_modified(self, event: FileEvent) -> None:
        key = self._key(event.path)
        snapshot = self._snapshot(event.path)
        if snapshot is None:
            return
        previous = self._snapshots.get(key)
        self._snapshots[key] = snapshot
        if previous is None and key not in self._file_states:
            return
        if previous == snapshot:
            return

        size = self._file_size(event.path, wait_for_stable=True)
        state = self._file_states.get(key)
        if state is not None:
            state.last_size = size
            state.modified += 1
            self._emit(WorkAction.SAVE_FILE, event.path, size, event.occurred_at)
            return

        if self._process_opening_file(event.path) is not None:
            self._file_states[key] = FileState(event.occurred_at, size, size)
            self._paths[key] = event.path
            self._emit(WorkAction.OPEN_FILE, event.path, size, event.occurred_at)

    def _handle_deleted(self, event: FileEvent) -> None:
        key = self._key(event.path)
        self._pending_close.pop(key, None)
        self._snapshots.pop(key, None)
        if self._file_states.pop(key, None) is not None:
            self._emit(WorkAction.CLOSE_FILE, event.path, 0, event.occurred_at)
        elif not event.path.exists():
            self._emit(WorkAction.DELETE_FILE, event.path, 0, event.occurred_at)

    def _handle_moved(self, event: FileEvent) -> None:
        destination = event.destination
        if destination is None or self._is_excluded(event.path) or self._is_excluded(destination):
            return
        old_name = event.path.name.lower()
        new_name = destination.name.lower()
        if any(marker in old_name or marker in new_name for marker in (".tmp", ".sv$")):
            return
        if old_name.startswith("~") or new_name.startswith("~"):
            return
        if self._is_target(event.path) and self._is_target(destination):
            LOGGER.info("重命名文件 | %s -> %s", event.path.name, destination.name)
            self._handle_deleted(
                FileEvent(FileEventType.DELETED, event.path, event.occurred_at)
            )
            self._process(FileEvent(FileEventType.CREATED, destination, event.occurred_at))

    def _flush_expired_closes(self, now: datetime) -> None:
        expired = [
            key
            for key, close_time in self._pending_close.items()
            if (now - close_time).total_seconds() > self._config.debounce_seconds
        ]
        for key in expired:
            close_time = self._pending_close.pop(key)
            path = self._paths.get(key, Path(key))
            self._file_states.pop(key, None)
            self._emit(WorkAction.CLOSE_FILE, path, self._file_size(path), close_time)

    def _emit(self, action: WorkAction, path: Path, size: int, timestamp: datetime) -> None:
        event = LogEvent(
            timestamp=timestamp,
            action=action.value,
            file_name=path.name,
            file_path=str(path),
            file_size=size,
            project_dir=path.parent.name,
        )
        self._submit_log(event)
        LOGGER.info("日志 | %s | %s | %s | %d bytes", action.value, path.name, path.parent.name, size)

    def _is_duplicate(self, key: str, action: str, now: datetime) -> bool:
        previous = self._debounce.get(key)
        self._debounce[key] = DebounceState(now, action)
        return bool(
            previous
            and previous.last_action == action
            and (now - previous.last_event_time).total_seconds() < self._config.debounce_seconds
        )

    def _is_target(self, path: Path) -> bool:
        return path.suffix.lower() in self._config.target_extensions

    @staticmethod
    def _is_lock_file(path: Path) -> bool:
        return path.name.startswith("~") or path.suffix.lower() in {".dwl", ".dwl2"}

    def _real_path_from_lock(self, path: Path) -> Path | None:
        name = path.name
        lowered = name.lower()
        if lowered.startswith("~$"):
            return path.with_name(name[2:])
        if name.startswith("~"):
            candidate = path.with_name(name[1:])
            if self._is_target(candidate):
                return candidate
        if path.suffix.lower() in {".dwl", ".dwl2"}:
            return path.with_suffix(".dwg")
        return None

    def _is_excluded(self, path: Path) -> bool:
        normalized = self._key(path)
        excluded = any(
            normalized == directory or normalized.startswith(directory + os.sep)
            for directory in self._exclude_dirs
        )
        slash_path = normalized.replace(os.sep, "/")
        candidates = [slash_path, Path(normalized).name]
        for watch_dir in self._watch_dirs:
            watch_path = watch_dir.replace(os.sep, "/")
            if slash_path.startswith(watch_path + "/"):
                candidates.append(slash_path[len(watch_path) + 1 :])

        for raw_pattern in self._config.exclude_patterns:
            pattern = raw_pattern.strip()
            if not pattern or pattern.startswith("#"):
                continue
            include = pattern.startswith("!")
            if include:
                pattern = pattern[1:].strip()
            pattern = os.path.normcase(pattern.replace("\\", "/")).rstrip("/")
            matched = any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates)
            if raw_pattern.rstrip().endswith("/"):
                matched = matched or any(
                    candidate == pattern or candidate.startswith(pattern + "/") for candidate in candidates
                )
                if pattern.startswith("**/"):
                    directory_pattern = pattern[3:]
                    matched = matched or any(
                        fnmatch.fnmatch(part, directory_pattern)
                        for candidate in candidates
                        for part in candidate.split("/")[:-1]
                    )
            if matched:
                excluded = not include
        return excluded

    @staticmethod
    def _snapshot(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
            return stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _file_size(path: Path, wait_for_stable: bool = False) -> int:
        last_size = -1
        stable_count = 0
        deadline = time.monotonic() + (2.0 if wait_for_stable else 0.0)
        while True:
            try:
                current_size = path.stat().st_size
            except OSError:
                return max(0, last_size)
            stable_count = stable_count + 1 if current_size == last_size else 0
            last_size = current_size
            if not wait_for_stable or stable_count >= 2 or time.monotonic() >= deadline:
                return current_size
            time.sleep(0.1)

    @staticmethod
    def _process_opening_file(path: Path) -> str | None:
        if psutil is None:
            return None
        target = os.path.normcase(os.path.abspath(path))
        for process in psutil.process_iter(["name"]):
            try:
                name = (process.info["name"] or "").lower()
                stem = Path(name).stem
                if stem not in KNOWN_EDITORS:
                    continue
                if any(os.path.normcase(os.path.abspath(item.path)) == target for item in process.open_files()):
                    return name
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
                continue
        return None

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

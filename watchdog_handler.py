from __future__ import annotations

import logging
import queue
from datetime import datetime
from pathlib import Path

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
    from watchdog.observers import Observer
    _WATCHDOG_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised on minimal installations
    FileSystemEvent = object  # type: ignore[assignment,misc]
    FileSystemMovedEvent = object  # type: ignore[assignment,misc]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]
    _WATCHDOG_ERROR = exc

from events import FileEventType
from models import FileEvent

LOGGER = logging.getLogger(__name__)


class QueueingEventHandler(FileSystemEventHandler):
    """Converts Watchdog callbacks to immutable queue messages only."""

    def __init__(self, event_queue: queue.Queue[FileEvent]):
        super().__init__()
        self._event_queue = event_queue

    def on_created(self, event: FileSystemEvent) -> None:
        self._enqueue(FileEventType.CREATED, event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._enqueue(FileEventType.MODIFIED, event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._enqueue(FileEventType.DELETED, event)

    def on_moved(self, event: FileSystemMovedEvent) -> None:
        self._event_queue.put_nowait(
            FileEvent(
                event_type=FileEventType.MOVED,
                path=Path(event.src_path),
                destination=Path(event.dest_path),
                occurred_at=datetime.now(),
                is_directory=event.is_directory,
            )
        )

    def _enqueue(self, event_type: FileEventType, event: FileSystemEvent) -> None:
        if event.is_directory and event_type is not FileEventType.DELETED:
            return
        self._event_queue.put_nowait(
            FileEvent(
                event_type=event_type,
                path=Path(event.src_path),
                occurred_at=datetime.now(),
                is_directory=event.is_directory,
            )
        )


class FileSystemMonitor:
    def __init__(self, event_queue: queue.Queue[FileEvent], watch_dirs: tuple[Path, ...]):
        if _WATCHDOG_ERROR is not None:
            raise RuntimeError("文件监控需要 watchdog，请先安装 requirements.txt") from _WATCHDOG_ERROR
        self._handler = QueueingEventHandler(event_queue)
        self._watch_dirs = watch_dirs
        self._observer = Observer()
        self._started = False

    def start(self) -> None:
        scheduled = 0
        for directory in self._watch_dirs:
            if not directory.is_dir():
                LOGGER.warning("监控目录不存在，已跳过: %s", directory)
                continue
            try:
                self._observer.schedule(self._handler, str(directory), recursive=True)
                scheduled += 1
            except OSError:
                LOGGER.exception("无法监控目录: %s", directory)
        if scheduled == 0:
            LOGGER.warning("没有可用的监控目录，文件监控不会产生事件")
        self._observer.start()
        self._started = True
        LOGGER.info("文件监控已启动，共监控 %d 个目录", scheduled)

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        self._observer.stop()
        self._observer.join(timeout)
        if self._observer.is_alive():
            LOGGER.warning("Watchdog 未能在 %.1f 秒内停止", timeout)
        self._started = False

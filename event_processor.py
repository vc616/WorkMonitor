from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - optional at import time for report tooling
    psutil = None  # type: ignore[assignment]

from config import AppConfig
from events import FileEventType, WorkAction
from models import FileEvent, FileState, LogEvent

LOGGER = logging.getLogger(__name__)

MAX_TRACKED_PATHS = 10_000


@dataclass(frozen=True, slots=True)
class ExcludeRule:
    """A compiled gitignore-style rule."""

    regex: re.Pattern[str]
    include: bool
    directory: bool
    basename_only: bool
    anchored: bool = False


class EventProcessor(threading.Thread):
    """Owns all mutable file state and converts raw events to log events.

    All elapsed-time decisions use ``time.monotonic()`` (the arrival clock).
    ``FileEvent.occurred_at`` is wall clock and is used only as the timestamp
    stored in the database, never for a duration comparison.
    """

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
        self._debounce: dict[str, tuple[float, str]] = {}
        self._snapshots: dict[str, tuple[int, int]] = {}
        self._pending_close: dict[str, tuple[datetime, float]] = {}
        self._paths: dict[str, Path] = {}
        self._pending_destructive_events: list[tuple[float, FileEvent]] = []
        self._directory_operations: list[tuple[float, FileEvent]] = []
        self._editor_cache: dict[str, tuple[float, str | None]] = {}
        self._state_touched: dict[str, float] = {}
        self._current_arrival = 0.0
        self._monotonic: Callable[[], float] = time.monotonic
        self._exclude_dirs = tuple(self._key(path) for path in config.exclude_dirs)
        self._watch_dirs = tuple(self._key(path) for path in config.watch_dirs)
        self._exclude_rules = self._compile_exclude_patterns(config.exclude_patterns)

    def stop(self, timeout: float = 10.0) -> None:
        if not self.is_alive():
            return
        self._stop_event.set()
        self.join(timeout)
        if self.is_alive():
            raise TimeoutError("EventProcessor 未能按时停止")

    def run(self) -> None:
        LOGGER.info("文件事件处理器已启动")
        last_prune = self._monotonic()
        while not self._stop_event.is_set() or not self._event_queue.empty():
            try:
                event = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                self._tick()
                continue
            try:
                self._process(event)
            except Exception:
                LOGGER.exception("处理文件事件失败: %s %s", event.event_type, event.path)
            finally:
                self._event_queue.task_done()
            self._tick()
            now = self._monotonic()
            if now - last_prune >= 5.0:
                self._prune_state()
                last_prune = now

        self._flush_pending_destructive_events(force=True)
        self._flush_expired_closes(force=True)
        LOGGER.info("文件事件处理器已停止")

    def _tick(self) -> None:
        """Advance all time-based state using a single monotonic clock."""
        now = self._monotonic()
        self._flush_pending_destructive_events()
        self._flush_expired_closes(monotonic_now=now)

    def _process(self, event: FileEvent) -> None:
        self._current_arrival = self._monotonic()
        if event.is_directory:
            self._handle_directory_event(event)
            return
        self._touch(self._key(event.path))
        if self._is_covered_by_directory_operation(event):
            return
        if event.event_type is FileEventType.MOVED:
            if self._merge_atomic_save(event):
                return
            self._defer_destructive_event(event)
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
            if self._merge_atomic_save(event):
                return
            size = self._file_size(event.path)
            self._emit(WorkAction.CREATE_FILE, event.path, size, event.occurred_at)
            self._remember_snapshot(event.path)
        elif event.event_type is FileEventType.DELETED:
            self._defer_destructive_event(event)

    def _merge_atomic_save(self, event: FileEvent) -> bool:
        """Collapse "delete then recreate the same path" into a single save.

        Editors save either by deleting + recreating the target, or by writing
        a temporary file and renaming it onto the target. Both must produce one
        "修改后保存" instead of a bogus delete plus a bogus create.
        """
        target = event.destination if event.event_type is FileEventType.MOVED else event.path
        if target is None or self._is_excluded(target) or not self._is_target(target):
            return False
        key = self._key(target)
        for index, (_, pending) in enumerate(self._pending_destructive_events):
            if pending.event_type is not FileEventType.DELETED or pending.is_directory:
                continue
            if self._key(pending.path) != key:
                continue
            self._pending_destructive_events.pop(index)
            self._record_save(target, event.occurred_at)
            return True
        if event.event_type is FileEventType.MOVED:
            source = event.path.name.lower()
            if source.endswith((".tmp", ".sv$")) or source.startswith("~"):
                self._record_save(target, event.occurred_at)
                return True
        return False

    def _record_save(self, path: Path, timestamp: datetime) -> None:
        key = self._key(path)
        size = self._file_size(path)
        state = self._file_states.get(key)
        if state is not None:
            state.last_size = size
            state.modified += 1
        self._emit(WorkAction.SAVE_FILE, path, size, timestamp)
        self._remember_snapshot(path)

    def _remember_snapshot(self, path: Path) -> None:
        snapshot = self._snapshot(path)
        if snapshot is not None:
            self._snapshots[self._key(path)] = snapshot

    def _defer_destructive_event(self, event: FileEvent) -> None:
        self._pending_destructive_events.append((self._monotonic(), event))

    def _flush_pending_destructive_events(self, force: bool = False) -> None:
        if not self._pending_destructive_events and not self._directory_operations:
            return
        now = self._monotonic()
        # Directory operations stop suppressing child events after two windows.
        self._directory_operations = [
            (arrival, operation)
            for arrival, operation in self._directory_operations
            if now - arrival <= self._config.debounce_seconds * 2
        ]
        if force:
            due = self._pending_destructive_events
            self._pending_destructive_events = []
        else:
            cutoff = now - self._config.debounce_seconds
            due = [(arrival, event) for arrival, event in self._pending_destructive_events if arrival <= cutoff]
            self._pending_destructive_events = [
                (arrival, event) for arrival, event in self._pending_destructive_events if arrival > cutoff
            ]
            if not due:
                return

        operations = self._top_level_directory_operations()
        pending = [
            event
            for _, event in due
            if not any(
                self._event_is_inside_directory_operation(event, operation)
                for operation in operations
            )
        ]
        for event in pending:
            if event.event_type is FileEventType.MOVED:
                self._handle_moved(event)
            else:
                self._handle_deleted(event)
        for operation in operations:
            self._emit_directory_operation(operation)
        self._directory_operations = []

    def _handle_directory_event(self, event: FileEvent) -> None:
        if event.event_type not in {FileEventType.DELETED, FileEventType.MOVED}:
            return
        destination = event.destination
        if self._is_excluded(event.path):
            return
        if (
            event.event_type is FileEventType.MOVED
            and destination is not None
            and self._is_recycle_bin(destination)
        ):
            event = FileEvent(
                FileEventType.DELETED,
                event.path,
                event.occurred_at,
                is_directory=True,
            )
            destination = None
        elif destination is not None and self._is_excluded(destination):
            return

        self._directory_operations.append((self._monotonic(), event))
        self._clear_directory_state(event.path)
        if destination is not None:
            self._clear_directory_state(destination)

    def _top_level_directory_operations(self) -> list[FileEvent]:
        indexed = sorted(
            enumerate(self._directory_operations),
            key=lambda item: len(Path(self._key(item[1][1].path)).parts),
        )
        roots: dict[FileEventType, set[str]] = {
            FileEventType.DELETED: set(),
            FileEventType.MOVED: set(),
        }
        selected: list[tuple[int, FileEvent]] = []
        for index, (_, operation) in indexed:
            key = self._key(operation.path)
            event_roots = roots[operation.event_type]
            if key in event_roots or any(
                key.startswith(root + os.sep) for root in event_roots
            ):
                continue
            event_roots.add(key)
            selected.append((index, operation))
        return [operation for _, operation in sorted(selected)]

    def _emit_directory_operation(self, event: FileEvent) -> None:
        destination = event.destination
        if event.event_type is FileEventType.DELETED:
            key = self._key(event.path)
            if not self._is_recent_duplicate(key, WorkAction.DELETE_FOLDER.value):
                self._emit(WorkAction.DELETE_FOLDER, event.path, 0, event.occurred_at)
            return
        if destination is None:
            return
        key = f"{self._key(event.path)}->{self._key(destination)}"
        if not self._is_recent_duplicate(key, WorkAction.MOVE_FOLDER.value):
            self._emit_directory_move(event.path, destination, event.occurred_at)

    def _is_covered_by_directory_operation(self, event: FileEvent) -> bool:
        return any(
            self._event_is_inside_directory_operation(event, operation)
            for _, operation in self._directory_operations
        )

    def _event_is_inside_directory_operation(
        self,
        event: FileEvent,
        operation: FileEvent,
    ) -> bool:
        roots = [operation.path]
        if operation.destination is not None:
            roots.append(operation.destination)
        paths = [event.path]
        if event.destination is not None:
            paths.append(event.destination)
        return any(self._is_descendant(path, root) for path in paths for root in roots)

    def _clear_directory_state(self, directory: Path) -> None:
        root = self._key(directory)
        for mapping in (
            self._file_states,
            self._debounce,
            self._snapshots,
            self._pending_close,
            self._paths,
            self._editor_cache,
            self._state_touched,
        ):
            for key in tuple(mapping):
                if key.startswith(root + os.sep):
                    mapping.pop(key, None)

    def _handle_lock_event(self, event: FileEvent) -> None:
        real_path = self._real_path_from_lock(event.path)
        if real_path is None or not self._is_target(real_path):
            return
        key = self._key(real_path)
        self._paths[key] = real_path
        self._touch(key)

        if event.event_type is FileEventType.CREATED:
            if self._is_recent_duplicate(key, WorkAction.OPEN_FILE.value):
                return
            close_entry = self._pending_close.pop(key, None)
            size = self._file_size(real_path)
            self._file_states[key] = FileState(event.occurred_at, size, size)
            self._remember_snapshot(real_path)
            if close_entry is not None and (self._current_arrival - close_entry[1]) < self._config.debounce_seconds:
                self._emit(WorkAction.SAVE_FILE, real_path, size, event.occurred_at)
            else:
                self._emit(WorkAction.OPEN_FILE, real_path, size, event.occurred_at)
            return

        if event.event_type is FileEventType.DELETED:
            if self._is_recent_duplicate(key, WorkAction.CLOSE_FILE.value):
                return
            self._pending_close[key] = (event.occurred_at, self._current_arrival)

    def _handle_modified(self, event: FileEvent) -> None:
        key = self._key(event.path)
        snapshot = self._snapshot(event.path)
        if snapshot is None:
            return
        previous = self._snapshots.get(key)
        self._snapshots[key] = snapshot
        if previous == snapshot:
            return
        # A single physical save can emit several MODIFIED callbacks; keep a
        # fixed debounce window instead of counting each intermediate write.
        if self._is_recent_duplicate(key, WorkAction.SAVE_FILE.value):
            return
        size = snapshot[0]
        state = self._file_states.get(key)
        if state is not None:
            state.last_size = size
            state.modified += 1
            self._emit(WorkAction.SAVE_FILE, event.path, size, event.occurred_at)
            return
        # Files opened by editors outside the known list still need a record.
        editor = self._opening_process(event.path)
        if editor is not None:
            self._file_states[key] = FileState(event.occurred_at, size, size)
            self._paths[key] = event.path
            self._emit(WorkAction.OPEN_FILE, event.path, size, event.occurred_at)
        else:
            self._emit(WorkAction.SAVE_FILE, event.path, size, event.occurred_at)

    def _handle_deleted(self, event: FileEvent) -> None:
        key = self._key(event.path)
        self._pending_close.pop(key, None)
        self._snapshots.pop(key, None)
        self._state_touched.pop(key, None)
        self._editor_cache.pop(key, None)
        if self._file_states.pop(key, None) is not None:
            self._emit(WorkAction.CLOSE_FILE, event.path, 0, event.occurred_at)
        elif not event.path.exists():
            self._emit(WorkAction.DELETE_FILE, event.path, 0, event.occurred_at)

    def _handle_moved(self, event: FileEvent) -> None:
        destination = event.destination
        if destination is None or self._is_excluded(event.path):
            return
        old_name = event.path.name.lower()
        new_name = destination.name.lower()
        if any(marker in old_name or marker in new_name for marker in (".tmp", ".sv$")):
            return
        if old_name.startswith("~") or new_name.startswith("~"):
            return
        if self._is_recycle_bin(destination):
            if self._is_target(event.path):
                self._handle_deleted(
                    FileEvent(FileEventType.DELETED, event.path, event.occurred_at)
                )
            return
        if self._is_excluded(destination):
            return
        if self._is_target(event.path) and self._is_target(destination):
            LOGGER.debug("重命名事件展开为删除与新建记录")
            self._handle_deleted(
                FileEvent(FileEventType.DELETED, event.path, event.occurred_at)
            )
            self._process(FileEvent(FileEventType.CREATED, destination, event.occurred_at))

    def _emit_directory_move(
        self,
        source: Path,
        destination: Path,
        timestamp: datetime,
    ) -> None:
        event = LogEvent(
            timestamp=timestamp,
            action=WorkAction.MOVE_FOLDER.value,
            file_name=f"{source.name} -> {destination.name}",
            file_path=f"{source} -> {destination}",
            file_size=0,
            project_dir=destination.parent.name,
        )
        self._submit_log(event)
        LOGGER.debug("日志 | %s | 文件夹移动已记录", event.action)

    def _flush_expired_closes(
        self,
        now: datetime | None = None,
        *,
        monotonic_now: float | None = None,
        force: bool = False,
    ) -> None:
        """Emit pending close events once their debounce window has elapsed.

        The production path passes ``monotonic_now``. Passing ``now`` keeps the
        deterministic wall-clock path available for unit tests and callers that
        construct events with explicit timestamps.
        """
        for key, (close_time, close_monotonic) in list(self._pending_close.items()):
            if force:
                expired = True
            elif monotonic_now is not None:
                expired = max(0.0, monotonic_now - close_monotonic) >= self._config.debounce_seconds
            elif now is not None:
                expired = max(0.0, (now - close_time).total_seconds()) >= self._config.debounce_seconds
            else:
                expired = max(0.0, self._monotonic() - close_monotonic) >= self._config.debounce_seconds
            if not expired:
                continue
            self._pending_close.pop(key, None)
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
        # File names and full paths are sensitive; never write them to app.log.
        LOGGER.debug("日志 | %s | 扩展名 %s | %d bytes", action.value, path.suffix.lower(), size)

    def _is_recent_duplicate(self, key: str, action: str) -> bool:
        """Fixed-window debounce; a duplicate does not extend the window."""
        now = self._current_arrival or self._monotonic()
        previous = self._debounce.get(key)
        if (
            previous
            and previous[1] == action
            and 0 <= now - previous[0] < self._config.debounce_seconds
        ):
            return True
        self._debounce[key] = (now, action)
        self._touch(key)
        return False

    def _touch(self, key: str) -> None:
        self._state_touched[key] = self._current_arrival or self._monotonic()

    def _prune_state(self) -> None:
        """Bound all per-path state so a long-running monitor cannot leak memory."""
        now = self._monotonic()
        ttl = max(60.0, self._config.debounce_seconds * 30)
        self._state_touched = {key: value for key, value in self._state_touched.items() if now - value <= ttl}
        for mapping in (self._debounce, self._snapshots, self._editor_cache):
            for key in tuple(mapping):
                if key not in self._state_touched:
                    mapping.pop(key, None)
        for mapping in (self._file_states, self._paths, self._pending_close, self._debounce, self._snapshots):
            if len(mapping) > MAX_TRACKED_PATHS:
                for key in list(mapping)[: len(mapping) - MAX_TRACKED_PATHS]:
                    mapping.pop(key, None)
        if len(self._pending_destructive_events) > MAX_TRACKED_PATHS:
            self._pending_destructive_events = self._pending_destructive_events[-MAX_TRACKED_PATHS:]

    def _is_target(self, path: Path) -> bool:
        return path.suffix.lower() in self._config.target_extensions

    @staticmethod
    def _is_lock_file(path: Path) -> bool:
        # Only Office/WPS "~$" files and AutoCAD .dwl/.dwl2 are lock files.
        # A regular file such as "~notes.md" must not be remapped.
        return path.name.startswith("~$") or path.suffix.lower() in {".dwl", ".dwl2"}

    def _real_path_from_lock(self, path: Path) -> Path | None:
        name = path.name
        if name.startswith("~$"):
            return path.with_name(name[2:])
        if path.suffix.lower() in {".dwl", ".dwl2"}:
            return path.with_suffix(".dwg")
        return None

    def _is_excluded(self, path: Path) -> bool:
        normalized = self._key(path)
        if any(
            normalized == directory or normalized.startswith(directory + os.sep)
            for directory in self._exclude_dirs
        ):
            return True
        if not self._exclude_rules:
            return False

        slash_path = normalized.replace(os.sep, "/")
        segments = tuple(part for part in slash_path.split("/") if part)
        if not segments:
            return False

        candidates = [segments]
        relative_candidates: list[tuple[str, ...]] = []
        for watch_dir in self._watch_dirs:
            watch_path = watch_dir.replace(os.sep, "/").rstrip("/")
            if slash_path == watch_path:
                relative_candidates.append(())
            elif slash_path.startswith(watch_path + "/"):
                relative = slash_path[len(watch_path) + 1 :]
                relative_candidates.append(tuple(part for part in relative.split("/") if part))
                candidates.append(relative_candidates[-1])

        excluded = False
        for rule in self._exclude_rules:
            pool = relative_candidates if rule.anchored else candidates
            if self._rule_matches(rule, pool, segments):
                excluded = not rule.include
        return excluded

    @staticmethod
    def _rule_matches(
        rule: ExcludeRule,
        candidates: list[tuple[str, ...]],
        segments: tuple[str, ...],
    ) -> bool:
        if rule.basename_only and not rule.directory and not rule.anchored:
            # `*.tmp` has no separator, so it matches the file name at any depth.
            return rule.regex.fullmatch(segments[-1]) is not None

        for candidate in candidates:
            if not candidate:
                continue
            if rule.regex.fullmatch("/".join(candidate)):
                return True
            if not rule.directory:
                continue
            # A directory rule also excludes everything below the matching directory.
            for depth in range(1, len(candidate)):
                if rule.regex.fullmatch("/".join(candidate[:depth])):
                    return True
            if rule.basename_only and not rule.anchored and any(
                rule.regex.fullmatch(part) for part in candidate[:-1]
            ):
                # `build/` matches a directory with that name at any depth.
                # The last segment may be a plain file, so it is not compared.
                # An anchored `/build/` must NOT match at arbitrary depth.
                return True
        return False

    @classmethod
    def _compile_exclude_patterns(cls, patterns: tuple[str, ...]) -> tuple[ExcludeRule, ...]:
        rules: list[ExcludeRule] = []
        for raw_pattern in patterns:
            pattern = raw_pattern.strip()
            if not pattern or pattern.startswith("#"):
                continue
            include = pattern.startswith("!")
            if include:
                pattern = pattern[1:].strip()
            if not pattern:
                continue
            directory = pattern.endswith("/")
            # A leading slash anchors the rule to the watch root instead of
            # matching a directory with the same name at any depth.
            anchored = pattern.startswith("/")
            pattern = pattern.replace("\\", "/").strip("/")
            if not pattern:
                continue
            pattern = pattern.lower()
            try:
                regex = re.compile(cls._glob_to_regex(pattern))
            except re.error:
                LOGGER.warning("忽略无效的排除规则: %s", raw_pattern)
                continue
            rules.append(
                ExcludeRule(
                    regex=regex,
                    include=include,
                    directory=directory,
                    basename_only="/" not in pattern,
                    anchored=anchored,
                )
            )
        return tuple(rules)

    @staticmethod
    def _glob_to_regex(pattern: str) -> str:
        parts: list[str] = []
        index = 0
        while index < len(pattern):
            if pattern.startswith("**/", index):
                parts.append("(?:[^/]+/)*")
                index += 3
            elif pattern.startswith("**", index):
                parts.append(".*")
                index += 2
            elif pattern[index] == "*":
                parts.append("[^/]*")
                index += 1
            elif pattern[index] == "?":
                parts.append("[^/]")
                index += 1
            else:
                parts.append(re.escape(pattern[index]))
                index += 1
        return "".join(parts)

    @staticmethod
    def _snapshot(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
            return stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _file_size(path: Path, wait_for_stable: bool = False) -> int:
        """Never block the single event thread; stability comes from snapshots."""
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _opening_process(self, path: Path) -> str | None:
        """Cached process lookup; at most one scan per path per TTL window."""
        key = self._key(path)
        now = self._monotonic()
        ttl = max(30.0, self._config.debounce_seconds * 10)
        cached = self._editor_cache.get(key)
        if cached is not None and now - cached[0] <= ttl:
            return cached[1]
        result = self._process_opening_file(path)
        self._editor_cache[key] = (now, result)
        return result

    def _process_opening_file(self, path: Path) -> str | None:
        if psutil is None:
            return None
        target = os.path.normcase(os.path.abspath(path))
        known = self._config.known_editors
        for process in psutil.process_iter(["name"]):
            try:
                name = (process.info["name"] or "").lower()
                if Path(name).stem not in known:
                    continue
                if any(os.path.normcase(os.path.abspath(item.path)) == target for item in process.open_files()):
                    return name
            except (psutil.Error, OSError, AttributeError, TypeError):
                continue
        return None

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

    @classmethod
    def _is_descendant(cls, path: Path, directory: Path) -> bool:
        return cls._key(path).startswith(cls._key(directory) + os.sep)

    @staticmethod
    def _is_recycle_bin(path: Path) -> bool:
        return any(part.casefold() == "$recycle.bin" for part in path.parts)

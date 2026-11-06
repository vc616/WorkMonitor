from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path

from models import LogEvent, WorkLogRecord

LOGGER = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS work_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME,
    action TEXT,
    file_name TEXT,
    file_path TEXT,
    file_size INTEGER,
    project_dir TEXT
)
"""
CREATE_TIMESTAMP_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_work_log_timestamp ON work_log(timestamp)"
)
CREATE_ACTION_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_work_log_action ON work_log(action)"
)
INSERT_LOG_SQL = """
INSERT INTO work_log (timestamp, action, file_name, file_path, file_size, project_dir)
VALUES (?, ?, ?, ?, ?, ?)
"""
SELECT_DAY_SQL = """
SELECT timestamp, action, file_name, file_path, file_size, project_dir
FROM work_log
WHERE timestamp >= ? AND timestamp < ?
ORDER BY timestamp ASC, id ASC
"""
SELECT_MONTH_DAYS_SQL = """
SELECT DISTINCT substr(timestamp, 1, 10) AS day
FROM work_log
WHERE timestamp >= ? AND timestamp < ?
ORDER BY day ASC
"""

MAX_WRITE_FAILURES = 5
DROP_WARNING_INTERVAL = 100


def parse_timestamp(value: object) -> datetime:
    """Accept both the canonical format and ISO variants written by older builds."""
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        pass
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


@dataclass(slots=True)
class _QueryRequest:
    day: date
    completed: threading.Event = field(default_factory=threading.Event)
    result: list[WorkLogRecord] | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _MonthDaysRequest:
    year: int
    month: int
    completed: threading.Event = field(default_factory=threading.Event)
    result: set[date] | None = None
    error: BaseException | None = None


class _StopRequest:
    pass


DatabaseCommand = LogEvent | _QueryRequest | _MonthDaysRequest | _StopRequest


class DatabaseWriter(threading.Thread):
    """Owns the application's only SQLite connection."""

    def __init__(
        self,
        db_path: Path,
        batch_size: int = 100,
        flush_seconds: float = 0.5,
        queue_max_size: int = 10_000,
    ):
        super().__init__(name="sqlite-writer", daemon=True)
        self._db_path = db_path
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._commands: queue.Queue[DatabaseCommand] = queue.Queue(maxsize=max(100, queue_max_size))
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopped = threading.Event()
        self._accepting = threading.Event()
        self._stop_requested = threading.Event()
        self._dropped = 0
        self._write_failures = 0

    def start_and_wait(self, timeout: float = 10.0) -> None:
        self.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("SQLite Writer 启动超时")
        if self._startup_error is not None:
            raise RuntimeError(f"SQLite 初始化失败: {self._startup_error}") from self._startup_error
        self._accepting.set()

    def submit(self, event: LogEvent) -> None:
        if not self._accepting.is_set() or self._stopped.is_set() or not self.is_alive():
            raise RuntimeError("SQLite Writer 已停止")
        try:
            self._commands.put_nowait(event)
        except queue.Full:
            self._dropped += 1
            if self._dropped % DROP_WARNING_INTERVAL == 1:
                LOGGER.warning("写入队列已满，累计丢弃 %d 条日志", self._dropped)

    def fetch_day(self, day: date, timeout: float = 30.0) -> list[WorkLogRecord]:
        if not self._accepting.is_set() or self._stopped.is_set() or not self.is_alive():
            raise RuntimeError("SQLite Writer 已停止")
        request = _QueryRequest(day=day)
        # Queries must never be dropped when the queue is under pressure.
        self._put_query(request, timeout)
        if not request.completed.wait(timeout):
            raise TimeoutError("查询工作日志超时")
        if request.error is not None:
            raise RuntimeError(f"查询工作日志失败: {request.error}") from request.error
        return request.result or []

    def fetch_available_days(self, year: int, month: int, timeout: float = 30.0) -> set[date]:
        """Return dates with at least one record without opening another connection."""
        if not self._accepting.is_set() or self._stopped.is_set() or not self.is_alive():
            raise RuntimeError("SQLite Writer 已停止")
        request = _MonthDaysRequest(year=year, month=month)
        self._put_query(request, timeout)
        if not request.completed.wait(timeout):
            raise TimeoutError("查询月份工作日期超时")
        if request.error is not None:
            raise RuntimeError(f"查询月份工作日期失败: {request.error}") from request.error
        return request.result or set()

    def _put_query(self, request: _QueryRequest | _MonthDaysRequest, timeout: float) -> None:
        try:
            self._commands.put(request, timeout=timeout)
        except queue.Full as exc:
            raise TimeoutError("写入队列持续繁忙，查询未能送达") from exc

    @property
    def dropped_events(self) -> int:
        return self._dropped

    def stop(self, timeout: float = 10.0) -> None:
        # Reject new submissions before the stop sentinel is enqueued.
        self._accepting.clear()
        if self._stopped.is_set():
            return
        if not self.is_alive():
            self._stopped.set()
            return
        # A full queue must not prevent shutdown: the writer also polls this
        # flag, so the sentinel is only an optimisation for the common case.
        self._stop_requested.set()
        try:
            self._commands.put_nowait(_StopRequest())
        except queue.Full:
            LOGGER.warning("写入队列繁忙，改用停止标志通知写入线程")
        self.join(timeout)
        if self.is_alive():
            raise TimeoutError("SQLite Writer 未能按时停止")

    def run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._db_path, timeout=30)
            self._configure(connection)
        except BaseException as exc:
            self._startup_error = exc
            LOGGER.exception("SQLite Writer 启动失败")
            self._ready.set()
            self._stopped.set()
            return

        self._ready.set()
        pending: list[LogEvent] = []
        last_flush = time.monotonic()
        try:
            while True:
                remaining = max(0.0, self._flush_seconds - (time.monotonic() - last_flush))
                if self._stop_requested.is_set():
                    command = _StopRequest()
                else:
                    try:
                        command = self._commands.get(timeout=remaining)
                    except queue.Empty:
                        self._safe_flush(connection, pending)
                        last_flush = time.monotonic()
                        continue

                if isinstance(command, LogEvent):
                    pending.append(command)
                    if len(pending) >= self._batch_size:
                        self._safe_flush(connection, pending)
                        last_flush = time.monotonic()
                    continue

                self._safe_flush(connection, pending)
                last_flush = time.monotonic()
                if isinstance(command, (_QueryRequest, _MonthDaysRequest)):
                    self._execute_query(connection, command)
                    continue
                self._drain_before_stop(connection, pending)
                break
        except BaseException:
            LOGGER.exception("SQLite Writer 意外终止")
        finally:
            if connection is not None:
                try:
                    if pending:
                        LOGGER.info("关闭前写入剩余 %d 条日志", len(pending))
                    self._flush(connection, pending)
                except BaseException:
                    LOGGER.exception("关闭数据库前写入剩余日志失败")
                finally:
                    connection.close()
            self._stopped.set()

    def _drain_before_stop(self, connection: sqlite3.Connection, pending: list[LogEvent]) -> None:
        """Events already queued behind the stop sentinel must still be stored."""
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if isinstance(command, LogEvent):
                pending.append(command)
            elif isinstance(command, (_QueryRequest, _MonthDaysRequest)):
                command.error = RuntimeError("SQLite Writer 已停止")
                command.completed.set()

    def _safe_flush(self, connection: sqlite3.Connection, pending: list[LogEvent]) -> None:
        """A transient write error must not kill the writer thread."""
        if not pending:
            return
        try:
            self._flush(connection, pending)
            self._write_failures = 0
        except Exception:
            self._write_failures += 1
            LOGGER.exception("批量写入失败（第 %d 次），保留 %d 条日志待重试", self._write_failures, len(pending))
            if self._write_failures >= MAX_WRITE_FAILURES:
                LOGGER.error("连续写入失败，丢弃 %d 条日志", len(pending))
                pending.clear()
                self._write_failures = 0

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA cache_size=-20000")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute(CREATE_TABLE_SQL)
        connection.execute(CREATE_TIMESTAMP_INDEX_SQL)
        connection.execute(CREATE_ACTION_INDEX_SQL)
        connection.commit()

    @staticmethod
    def _flush(connection: sqlite3.Connection, pending: list[LogEvent]) -> None:
        if not pending:
            return
        rows = [
            (
                item.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                item.action,
                item.file_name,
                item.file_path,
                item.file_size,
                item.project_dir,
            )
            for item in pending
        ]
        try:
            connection.executemany(INSERT_LOG_SQL, rows)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        LOGGER.debug("批量写入 %d 条日志", len(rows))
        pending.clear()

    @staticmethod
    def _execute_query(
        connection: sqlite3.Connection,
        request: _QueryRequest | _MonthDaysRequest,
    ) -> None:
        label = (
            f"{request.year:04d}-{request.month:02d}"
            if isinstance(request, _MonthDaysRequest)
            else request.day.isoformat()
        )
        try:
            if isinstance(request, _MonthDaysRequest):
                start = datetime(request.year, request.month, 1)
                end = datetime(
                    request.year + (request.month == 12),
                    1 if request.month == 12 else request.month + 1,
                    1,
                )
                rows = connection.execute(
                    SELECT_MONTH_DAYS_SQL,
                    (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
                ).fetchall()
                days: set[date] = set()
                for row in rows:
                    try:
                        days.add(date.fromisoformat(str(row[0])[:10]))
                    except (ValueError, TypeError):
                        LOGGER.warning("跳过日期格式异常的记录: %r", row[0])
                request.result = days
                return
            start = datetime.combine(request.day, datetime_time.min)
            end = start + timedelta(days=1)
            rows = connection.execute(
                SELECT_DAY_SQL,
                (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchall()
            records: list[WorkLogRecord] = []
            for row in rows:
                try:
                    timestamp = parse_timestamp(row[0])
                except (ValueError, TypeError):
                    LOGGER.warning("跳过时间戳格式异常的记录: %r", row[0])
                    continue
                records.append(
                    WorkLogRecord(
                        timestamp=timestamp,
                        action=row[1] or "",
                        file_name=row[2] or "",
                        file_path=row[3] or "",
                        file_size=int(row[4] or 0),
                        project_dir=row[5] or "",
                    )
                )
            request.result = records
        except BaseException as exc:
            request.error = exc
            LOGGER.exception("查询 %s 的工作日志失败", label)
        finally:
            request.completed.set()

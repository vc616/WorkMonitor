from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from events import FileEventType


@dataclass(frozen=True, slots=True)
class FileEvent:
    event_type: FileEventType
    path: Path
    occurred_at: datetime
    destination: Path | None = None


@dataclass(frozen=True, slots=True)
class LogEvent:
    timestamp: datetime
    action: str
    file_name: str
    file_path: str
    file_size: int
    project_dir: str


@dataclass(frozen=True, slots=True)
class WorkLogRecord:
    timestamp: datetime
    action: str
    file_name: str
    file_path: str
    file_size: int
    project_dir: str


@dataclass(slots=True)
class FileState:
    open_time: datetime
    initial_size: int
    last_size: int
    modified: int = 0


@dataclass(slots=True)
class DebounceState:
    last_event_time: datetime
    last_action: str


@dataclass(frozen=True, slots=True)
class GeneratedReports:
    summary: str
    detailed: str

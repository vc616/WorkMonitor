from __future__ import annotations

import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(*parts: str) -> Path:
    """Return a bundled resource path in source and PyInstaller builds."""
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_dir.joinpath(*parts)


def configure_tcl_environment() -> None:
    """Point Tcl/Tk at the runtime bundled inside the frozen executable.

    Called before tkinter is imported. In a source checkout the directory does
    not exist, so the interpreter's own Tcl installation is used unchanged.
    """
    tcl_root = get_resource_path("tcl")
    for folder, variable in (("tcl8.6", "TCL_LIBRARY"), ("tk8.6", "TK_LIBRARY")):
        candidate = tcl_root / folder
        if (candidate / "init.tcl").is_file():
            os.environ.setdefault(variable, str(candidate))


def _candidate_log_dirs() -> list[Path]:
    candidates = [get_app_dir() / "LogFile"]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "WorkMonitorV2" / "LogFile")
    candidates.append(Path(tempfile.gettempdir()) / "WorkMonitorV2" / "LogFile")
    return candidates


def resolve_log_dir() -> Path:
    """Pick a writable log directory so a read-only install path cannot block startup."""
    for candidate in _candidate_log_dirs():
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return Path(tempfile.gettempdir()) / "WorkMonitorV2" / "LogFile"


def configure_logging(verbose: bool = False) -> Path:
    """Set up logging and return the writable directory used for all output files."""
    log_dir = resolve_log_dir()
    log_file = log_dir / "app.log"
    # DEBUG records may contain file names and page titles; keep them out of
    # the persistent log unless the operator explicitly asks for verbose mode.
    level = logging.DEBUG if verbose else logging.INFO

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(file_handler)

    # A windowed PyInstaller build has no usable stderr; a StreamHandler would
    # fail on every record.
    if sys.stderr is not None:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", LOG_DATE_FORMAT))
        root_logger.addHandler(console_handler)

    return log_dir


def format_duration(seconds: int) -> tuple[int, int, int]:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return hours, minutes, seconds

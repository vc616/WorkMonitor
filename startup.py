from __future__ import annotations

import subprocess
import sys
from pathlib import Path

STARTUP_VALUE_NAME = "WorkMonitorV2"
STARTUP_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def build_startup_command(app_dir: Path) -> str:
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([str(Path(sys.executable).resolve())])
    interpreter = Path(sys.executable).resolve()
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.exists():
        interpreter = pythonw
    return subprocess.list2cmdline([str(interpreter), str((app_dir / "main.py").resolve())])


def read_startup_command() -> str | None:
    """Return the registered command, or None when the value does not exist."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
        return str(value)
    except (FileNotFoundError, OSError, ImportError):
        return None


def is_startup_enabled(app_dir: Path | None = None) -> bool:
    """Report enabled only when the registered command still points here.

    Passing ``app_dir`` detects a stale entry left behind after the program
    was moved or switched between the source and EXE variants.
    """
    command = read_startup_command()
    if command is None:
        return False
    if app_dir is None:
        return True
    return command == build_startup_command(app_dir)


def set_startup_enabled(enabled: bool, app_dir: Path) -> None:
    try:
        import winreg
    except ImportError as exc:
        raise RuntimeError("开机启动仅支持 Windows") from exc

    if enabled:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            STARTUP_REGISTRY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key,
                STARTUP_VALUE_NAME,
                0,
                winreg.REG_SZ,
                build_startup_command(app_dir),
            )
        return

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            STARTUP_REGISTRY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, STARTUP_VALUE_NAME)
    except FileNotFoundError:
        pass

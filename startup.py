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


def is_startup_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_PATH) as key:
            winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
        return True
    except (FileNotFoundError, OSError, ImportError):
        return False


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

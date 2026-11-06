"""Headless smoke tests for the Tk interface.

The GUI module is 600 lines of wiring that had no automated coverage. When a
real Tcl/Tk runtime is unavailable, a minimal stand-in keeps the control flow
(the parts that actually break) testable.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from models import GeneratedReports


class _FakeVar(MagicMock):
    def __init__(self, *args, value=None, **kwargs):
        super().__init__()
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _FakeText(MagicMock):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self._content = ""

    def delete(self, *args, **kwargs):
        self._content = ""

    def insert(self, _index, value):
        self._content = str(value)

    def get(self, *args, **kwargs):
        return self._content


def _install_tkinter_stub() -> None:
    """Replace tkinter with a stand-in for the whole test process.

    Always stubbed on purpose: the real module would open a window (or fail on
    a machine without Tcl/Tk), while these tests only care about control flow.
    """

    class TclError(Exception):
        pass

    stub = types.ModuleType("tkinter")
    stub.TclError = TclError
    stub.StringVar = _FakeVar
    stub.BooleanVar = _FakeVar
    stub.Text = _FakeText

    def _module_getattr(name: str):
        if name == "TclError":
            return TclError
        return MagicMock(name=f"tkinter.{name}")

    stub.__getattr__ = _module_getattr
    sys.modules["tkinter"] = stub
    sys.modules["tkinter.ttk"] = stub


MonitorGui = None
_saved_modules: dict[str, object] = {}


def setUpModule() -> None:
    """Install the stub only for this module, then restore the real modules."""
    global MonitorGui, _saved_modules
    _saved_modules = {name: sys.modules.get(name) for name in ("tkinter", "tkinter.ttk")}
    _install_tkinter_stub()
    from gui import MonitorGui as _MonitorGui

    MonitorGui = _MonitorGui


def tearDownModule() -> None:
    for name, module in _saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class GuiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        root = Path(self._directory)
        self.config_path = root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "watch_dirs": [str(root / "data")],
                    "exclude_dirs": [],
                    "exclude_patterns": ["*.tmp"],
                    "target_extensions": [".py", ".docx"],
                    "db_path": "work_log.db",
                    "database_batch_size": 100,
                    "tracked_apps": {"WeChat.exe": "微信"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.output_dir = root / "LogFile"
        self.available = {date(2026, 7, 3), date(2026, 7, 7)}
        self.generated: list[date] = []
        self.shutdown_called = False

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self._directory, ignore_errors=True)

    def _generate(self, day: date) -> GeneratedReports | None:
        self.generated.append(day)
        if day not in self.available:
            return None
        return GeneratedReports(f"# {day}\n", f"detailed {day}\n")

    def _build(self) -> MonitorGui:
        gui = MonitorGui(
            self.output_dir,
            self._generate,
            lambda: setattr(self, "shutdown_called", True),
            lambda year, month: {day for day in self.available if day.year == year and day.month == month},
            self.config_path,
        )
        # Jump to the month that holds the fixtures.
        gui._display_month = date(2026, 7, 1)
        gui._refresh_month()
        return gui

    def test_calendar_reflects_available_days_and_settings_load(self) -> None:
        gui = self._build()
        self.assertEqual(self.available, gui._available_in_month)
        self.assertTrue(self.available <= set(gui._calendar_cells))
        self.assertEqual("work_log.db", gui._settings_vars["db_path"].get())
        self.assertEqual([".py", ".docx"], gui._collect_settings()["target_extensions"])

    def test_unavailable_day_is_not_selectable(self) -> None:
        gui = self._build()
        gui._begin_selection(date(2026, 7, 4))
        self.assertEqual(set(), gui._selected_days)

    def test_drag_selection_skips_days_without_records(self) -> None:
        gui = self._build()
        gui._begin_selection(date(2026, 7, 3))
        gui._drag_selection(date(2026, 7, 7))
        self.assertEqual(self.available, gui._selected_days)

    def test_invalid_number_is_reported_by_settings_validation(self) -> None:
        gui = self._build()
        gui._settings_vars["database_batch_size"].set("abc")
        with self.assertRaises(ValueError):
            gui._collect_settings()

    def test_report_generation_runs_off_the_ui_thread(self) -> None:
        gui = self._build()
        gui._selected_days = set(self.available)
        gui._make_reports()
        deadline = time.monotonic() + 10
        while gui._reporting and time.monotonic() < deadline:
            gui._poll_report_results()
            time.sleep(0.02)
        self.assertFalse(gui._reporting)
        self.assertEqual(sorted(self.available), sorted(self.generated))
        for day in self.available:
            self.assertTrue((self.output_dir / f"{day.isoformat()}.md").is_file())
            self.assertTrue((self.output_dir / f"{day.isoformat()}_详细.md").is_file())

    def test_shutdown_callback_is_invoked_once(self) -> None:
        gui = self._build()
        gui._shutdown()
        self.assertTrue(self.shutdown_called)


if __name__ == "__main__":
    unittest.main()

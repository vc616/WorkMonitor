"""Pure helpers shared by the GUI and the tests.

Kept free of tkinter so the logic can be exercised in environments without a
Tcl/Tk runtime.
"""

from __future__ import annotations

import re
from datetime import date, timedelta


def dates_between(start: date, end: date, available: set[date]) -> set[date]:
    if end < start:
        start, end = end, start
    count = (end - start).days + 1
    return {start + timedelta(days=index) for index in range(count) if start + timedelta(days=index) in available}


def parse_extensions(value: str) -> list[str]:
    extensions: list[str] = []
    for item in re.split(r"[,;\s]+", value):
        item = item.strip().lower()
        if not item:
            continue
        extensions.append(item if item.startswith(".") else f".{item}")
    return list(dict.fromkeys(extensions))


def parse_tracked_apps(value: str) -> dict[str, str]:
    applications: dict[str, str] = {}
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"应用映射第 {line_number} 行缺少 =")
        process, display_name = (part.strip() for part in line.split("=", 1))
        if not process or not display_name:
            raise ValueError(f"应用映射第 {line_number} 行不完整")
        applications[process] = display_name
    return applications

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from models import GeneratedReports
from report import write_reports
from tray import TrayController

LOGGER = logging.getLogger(__name__)

WINDOW_BG = "#f5f7fb"
PANEL_BG = "#ffffff"
TEXT_COLOR = "#1d2939"
MUTED_COLOR = "#667085"
ACCENT_COLOR = "#2864d7"
ACCENT_HOVER = "#1d4fb4"
AVAILABLE_BG = "#ffffff"
AVAILABLE_HOVER = "#edf4ff"
EMPTY_BG = "#edf0f4"
EMPTY_TEXT = "#a5adb9"
SELECTED_BG = "#2864d7"
SELECTED_TEXT = "#ffffff"


def dates_between(start: date, end: date, available: set[date]) -> set[date]:
    """Return only recorded dates in an inclusive range for drag selection."""
    if end < start:
        start, end = end, start
    count = (end - start).days + 1
    return {start + timedelta(days=index) for index in range(count) if start + timedelta(days=index) in available}


class MonitorGui:
    def __init__(
        self,
        output_dir: Path,
        generate_report: Callable[[date], GeneratedReports | None],
        shutdown_callback: Callable[[], None],
        available_days: Callable[[int, int], set[date]],
    ):
        self._output_dir = output_dir
        self._generate_report = generate_report
        self._shutdown_callback = shutdown_callback
        self._available_days = available_days
        self._selected_days: set[date] = set()
        self._available_in_month: set[date] = set()
        self._calendar_cells: dict[date, tk.Label] = {}
        self._weekday_headers: list[tk.Widget] = []
        self._drag_anchor: date | None = None
        self._dragging = False
        self._hover_day: date | None = None
        today = date.today()
        self._display_month = date(today.year, today.month, 1)

        self.root = tk.Tk()
        self.root.title("工作监控 · 日报中心")
        self.root.geometry("760x650")
        self.root.minsize(680, 580)
        self.root.configure(bg=WINDOW_BG)
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        self._configure_style()
        self._build_layout()
        self._tray = TrayController(self.show, self.request_shutdown)
        self._refresh_month()

    def run(self) -> None:
        self._tray.start()
        self.root.mainloop()

    def show(self) -> None:
        self.root.after(0, self._show_now)

    def request_shutdown(self) -> None:
        self.root.after(0, self._shutdown)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background=WINDOW_BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("Title.TLabel", background=WINDOW_BG, foreground=TEXT_COLOR, font=("Segoe UI", 19, "bold"))
        style.configure("Subtitle.TLabel", background=WINDOW_BG, foreground=MUTED_COLOR, font=("Segoe UI", 10))
        style.configure("PanelTitle.TLabel", background=PANEL_BG, foreground=TEXT_COLOR, font=("Segoe UI", 11, "bold"))
        style.configure("Muted.TLabel", background=PANEL_BG, foreground=MUTED_COLOR, font=("Segoe UI", 9))
        style.configure("CalendarHeader.TLabel", background=PANEL_BG, foreground=MUTED_COLOR, font=("Segoe UI", 9, "bold"))
        style.configure("Primary.TButton", background=ACCENT_COLOR, foreground="white", padding=(16, 8), font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#b9c5d8")])
        style.configure("Secondary.TButton", background="#eef2f7", foreground=TEXT_COLOR, padding=(12, 8), font=("Segoe UI", 10))
        style.map("Secondary.TButton", background=[("active", "#dfe6f0")])

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(28, 24, 28, 22))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 18))
        ttk.Label(header, text="工作日报中心", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="选择有记录的日期，可拖拽批量生成日报。灰色日期没有数据，不会被处理。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        panel = ttk.Frame(outer, style="Panel.TFrame", padding=(20, 18, 20, 18))
        panel.pack(fill="both", expand=True)

        month_bar = ttk.Frame(panel, style="Panel.TFrame")
        month_bar.pack(fill="x", pady=(0, 14))
        self._previous_button = ttk.Button(month_bar, text="‹", width=3, command=lambda: self._change_month(-1))
        self._previous_button.pack(side="left")
        self._month_title = ttk.Label(month_bar, text="", style="PanelTitle.TLabel", anchor="center")
        self._month_title.pack(side="left", fill="x", expand=True)
        self._next_button = ttk.Button(month_bar, text="›", width=3, command=lambda: self._change_month(1))
        self._next_button.pack(side="right")

        legend = ttk.Frame(panel, style="Panel.TFrame")
        legend.pack(fill="x", pady=(0, 12))
        self._legend_item(legend, AVAILABLE_BG, "有记录")
        self._legend_item(legend, EMPTY_BG, "无记录")
        self._legend_item(legend, SELECTED_BG, "已选择")

        self._calendar_frame = ttk.Frame(panel, style="Panel.TFrame")
        self._calendar_frame.pack(fill="both", expand=True)
        for column in range(7):
            self._calendar_frame.columnconfigure(column, weight=1, uniform="calendar")
        for row in range(7):
            self._calendar_frame.rowconfigure(row, weight=1, uniform="calendar")
        for column, weekday in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            header = ttk.Label(
                self._calendar_frame,
                text=weekday,
                style="CalendarHeader.TLabel",
                anchor="center",
            )
            header.grid(row=0, column=column, sticky="nsew", padx=3, pady=(0, 5))
            self._weekday_headers.append(header)

        separator = ttk.Separator(panel, orient="horizontal")
        separator.pack(fill="x", pady=(14, 12))
        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill="x")
        self._selection_label = ttk.Label(footer, text="未选择日期", style="Muted.TLabel")
        self._selection_label.pack(side="left", anchor="center")
        ttk.Button(footer, text="清空选择", style="Secondary.TButton", command=self._clear_selection).pack(side="right")
        self._generate_button = ttk.Button(
            footer,
            text="生成选中日期",
            style="Primary.TButton",
            command=self._make_reports,
            state="disabled",
        )
        self._generate_button.pack(side="right", padx=(0, 8))
        self._status_label = ttk.Label(outer, text="正在读取月份记录…", style="Subtitle.TLabel")
        self._status_label.pack(anchor="w", pady=(12, 0))

    @staticmethod
    def _legend_item(parent: ttk.Frame, color: str, text: str) -> None:
        item = ttk.Frame(parent, style="Panel.TFrame")
        item.pack(side="left", padx=(0, 16))
        swatch = tk.Label(item, width=2, height=1, bg=color, relief="solid", bd=1, highlightthickness=0)
        swatch.pack(side="left", padx=(0, 5))
        ttk.Label(item, text=text, style="Muted.TLabel").pack(side="left")

    def _refresh_month(self) -> None:
        self._month_title.configure(text=f"{self._display_month.year} 年 {self._display_month.month:02d} 月")
        try:
            self._available_in_month = set(
                self._available_days(self._display_month.year, self._display_month.month)
            )
        except Exception:
            LOGGER.exception("读取月份记录失败")
            self._available_in_month = set()
            self._status_label.configure(text="读取月份记录失败，请稍后重试。")
        visible_days = self._visible_days()
        self._selected_days.difference_update(
            day for day in visible_days if day.month == self._display_month.month and day not in self._available_in_month
        )
        for child in self._calendar_frame.grid_slaves():
            if int(child.grid_info().get("row", 0)) > 0:
                child.destroy()
        self._calendar_cells.clear()
        for index, day in enumerate(visible_days):
            row, column = divmod(index, 7)
            if day.month != self._display_month.month:
                tk.Label(self._calendar_frame, text="", bg=PANEL_BG).grid(
                    row=row + 1, column=column, sticky="nsew", padx=3, pady=3
                )
                continue
            cell = tk.Label(
                self._calendar_frame,
                text=str(day.day),
                font=("Segoe UI", 11, "bold"),
                relief="solid",
                bd=1,
                highlightthickness=0,
                cursor="hand2" if day in self._available_in_month else "arrow",
            )
            cell.grid(row=row + 1, column=column, sticky="nsew", padx=3, pady=3, ipady=8)
            cell.bind("<Button-1>", lambda event, selected_day=day: self._begin_selection(selected_day))
            cell.bind("<B1-Motion>", lambda event, selected_day=day: self._drag_selection(selected_day))
            cell.bind("<ButtonRelease-1>", self._finish_selection)
            cell.bind("<Enter>", lambda event, selected_day=day: self._hover(selected_day))
            cell.bind("<Leave>", lambda event, selected_day=day: self._leave(selected_day))
            self._calendar_cells[day] = cell
            self._paint_cell(day)
        self._update_selection_ui()
        if self._available_in_month:
            self._status_label.configure(text=f"本月有记录 {len(self._available_in_month)} 天 · 可拖拽选择多个日期")
        else:
            self._status_label.configure(text="本月没有工作记录 · 灰色日期不可选择")

    def _visible_days(self) -> list[date]:
        first = self._display_month
        start = first - timedelta(days=first.weekday())
        return [start + timedelta(days=index) for index in range(42)]

    def _change_month(self, offset: int) -> None:
        month_index = self._display_month.year * 12 + self._display_month.month - 1 + offset
        year, month = divmod(month_index, 12)
        self._display_month = date(year, month + 1, 1)
        self._hover_day = None
        self._refresh_month()

    def _begin_selection(self, day: date) -> None:
        if day not in self._available_in_month:
            self._status_label.configure(text=f"{day.isoformat()} 没有记录，未加入选择。")
            return
        self._drag_anchor = day
        self._dragging = True
        self._selected_days = {day}
        self._paint_all_cells()
        self._update_selection_ui()

    def _drag_selection(self, day: date) -> None:
        if not self._dragging or self._drag_anchor is None:
            return
        self._selected_days = dates_between(self._drag_anchor, day, self._available_in_month)
        self._paint_all_cells()
        self._update_selection_ui()

    def _finish_selection(self, _event: tk.Event) -> None:
        self._dragging = False

    def _hover(self, day: date) -> None:
        self._hover_day = day
        if not self._dragging:
            self._paint_cell(day)

    def _leave(self, day: date) -> None:
        if self._hover_day == day:
            self._hover_day = None
            self._paint_cell(day)

    def _paint_all_cells(self) -> None:
        for day in self._calendar_cells:
            self._paint_cell(day)

    def _paint_cell(self, day: date) -> None:
        cell = self._calendar_cells.get(day)
        if cell is None:
            return
        if day in self._selected_days:
            cell.configure(bg=SELECTED_BG, fg=SELECTED_TEXT, relief="solid", bd=1)
        elif day not in self._available_in_month:
            cell.configure(bg=EMPTY_BG, fg=EMPTY_TEXT, relief="flat", bd=0)
        elif day == self._hover_day:
            cell.configure(bg=AVAILABLE_HOVER, fg=TEXT_COLOR, relief="solid", bd=1)
        else:
            cell.configure(bg=AVAILABLE_BG, fg=TEXT_COLOR, relief="solid", bd=1)

    def _clear_selection(self) -> None:
        self._selected_days.clear()
        self._drag_anchor = None
        self._paint_all_cells()
        self._update_selection_ui()

    def _update_selection_ui(self) -> None:
        count = len(self._selected_days)
        if count == 0:
            self._selection_label.configure(text="未选择日期")
            self._generate_button.configure(state="disabled")
        else:
            self._selection_label.configure(text=f"已选择 {count} 天 · 灰色日期会自动跳过")
            self._generate_button.configure(state="normal")

    def _make_reports(self) -> None:
        selected_days = sorted(self._selected_days)
        if not selected_days:
            return
        output_paths: list[Path] = []
        skipped = 0
        try:
            self._generate_button.configure(state="disabled")
            for day in selected_days:
                reports = self._generate_report(day)
                if reports is None:
                    skipped += 1
                    continue
                output_paths.extend(write_reports(self._output_dir, day, reports))
        except Exception:
            LOGGER.exception("批量生成日报失败")
            messagebox.showerror("生成失败", "日报生成失败，请查看日志。", parent=self.root)
        else:
            if not output_paths:
                messagebox.showinfo("无数据", "选中的日期没有可生成的记录。", parent=self.root)
            else:
                self._status_label.configure(text=f"已生成 {len(output_paths) // 2} 天日报")
                detail = "\n".join(str(path) for path in output_paths[:8])
                if len(output_paths) > 8:
                    detail += "\n…"
                suffix = f"\n跳过无数据日期：{skipped} 天" if skipped else ""
                messagebox.showinfo("生成完成", f"已生成 {len(output_paths) // 2} 天日报。{suffix}\n\n{detail}", parent=self.root)
        finally:
            self._update_selection_ui()

    def _show_now(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _shutdown(self) -> None:
        self._tray.stop()
        try:
            self._shutdown_callback()
        finally:
            self.root.destroy()

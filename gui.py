from __future__ import annotations

import logging
import re
import tkinter as tk
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from config import DEFAULT_TRACKED_APPS, ConfigError, read_config_data, save_config_updates
from models import GeneratedReports
from report import write_reports
from startup import is_startup_enabled, set_startup_enabled
from tray import TrayController

LOGGER = logging.getLogger(__name__)

WINDOW_BG = "#f3f5f7"
PANEL_BG = "#ffffff"
TEXT_COLOR = "#20262e"
MUTED_COLOR = "#69727d"
ACCENT_COLOR = "#167663"
ACCENT_HOVER = "#105f50"
TAB_IDLE_BG = "#e5e9ed"
TAB_HOVER_BG = "#d8e3e0"
AVAILABLE_BG = "#ffffff"
AVAILABLE_HOVER = "#e8f4f1"
EMPTY_BG = "#edf0f2"
EMPTY_TEXT = "#a2a9b1"
SELECTED_BG = ACCENT_COLOR
SELECTED_TEXT = "#ffffff"


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


class MonitorGui:
    def __init__(
        self,
        output_dir: Path,
        generate_report: Callable[[date], GeneratedReports | None],
        shutdown_callback: Callable[[], None],
        available_days: Callable[[int, int], set[date]],
        config_path: Path,
    ):
        self._output_dir = output_dir
        self._generate_report = generate_report
        self._shutdown_callback = shutdown_callback
        self._available_days = available_days
        self._config_path = config_path
        self._selected_days: set[date] = set()
        self._available_in_month: set[date] = set()
        self._calendar_cells: dict[date, tk.Label] = {}
        self._drag_anchor: date | None = None
        self._dragging = False
        self._hover_day: date | None = None
        self._settings_vars: dict[str, tk.StringVar] = {}
        self._settings_texts: dict[str, tk.Text] = {}
        today = date.today()
        self._display_month = date(today.year, today.month, 1)

        self.root = tk.Tk()
        self._startup_var = tk.BooleanVar(master=self.root, value=False)
        self._input_activity_var = tk.BooleanVar(master=self.root, value=True)
        self.root.title("工作监控 · 日报中心")
        self.root.geometry("900x760")
        self.root.minsize(780, 660)
        self.root.configure(bg=WINDOW_BG)
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        self._configure_style()
        self._build_layout()
        self._tray = TrayController(self.show, self.request_shutdown)
        self._refresh_month()
        self._load_settings()
        self.root.withdraw()

    def run(self) -> None:
        if not self._tray.start():
            self._show_now()
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
        style.configure("Title.TLabel", background=WINDOW_BG, foreground=TEXT_COLOR, font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Subtitle.TLabel", background=WINDOW_BG, foreground=MUTED_COLOR, font=("Microsoft YaHei UI", 9))
        style.configure("PanelTitle.TLabel", background=PANEL_BG, foreground=TEXT_COLOR, font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Muted.TLabel", background=PANEL_BG, foreground=MUTED_COLOR, font=("Microsoft YaHei UI", 9))
        style.configure("Field.TLabel", background=PANEL_BG, foreground=TEXT_COLOR, font=("Microsoft YaHei UI", 9))
        style.configure("CalendarHeader.TLabel", background=PANEL_BG, foreground=MUTED_COLOR, font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Primary.TButton", background=ACCENT_COLOR, foreground="white", padding=(18, 9), font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Primary.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#b9c5d8")])
        style.configure("Secondary.TButton", background="#e9edf0", foreground=TEXT_COLOR, padding=(14, 9), font=("Microsoft YaHei UI", 9))
        style.map("Secondary.TButton", background=[("active", "#dce2e6")])
        style.configure("Settings.TLabelframe", background=PANEL_BG, relief="flat")
        style.configure("Settings.TLabelframe.Label", background=PANEL_BG, foreground=TEXT_COLOR, font=("Microsoft YaHei UI", 11, "bold"))

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(30, 24, 30, 24))
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 18))
        title_area = ttk.Frame(header, style="App.TFrame")
        title_area.pack(side="left")
        ttk.Label(title_area, text="工作监控", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_area, text="日报、文件活动与前台应用统计", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))
        status_area = tk.Frame(header, bg=WINDOW_BG)
        status_area.pack(side="right", anchor="n", pady=(7, 0))
        tk.Label(status_area, text="●", bg=WINDOW_BG, fg="#28a077", font=("Segoe UI", 10)).pack(side="left", padx=(0, 6))
        tk.Label(
            status_area,
            text="后台监控中",
            bg=WINDOW_BG,
            fg=MUTED_COLOR,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left")

        tab_bar = tk.Frame(outer, bg=WINDOW_BG)
        tab_bar.pack(fill="x", pady=(0, 10))
        self._tab_buttons: dict[str, tk.Button] = {}
        for key, title in (("calendar", "日报日历"), ("settings", "参数设置")):
            button = tk.Button(
                tab_bar,
                text=title,
                command=lambda selected=key: self._show_page(selected),
                relief="flat",
                bd=0,
                padx=24,
                pady=10,
                cursor="hand2",
                font=("Microsoft YaHei UI", 10),
                highlightthickness=0,
            )
            button.pack(side="left", padx=(0, 8))
            self._tab_buttons[key] = button

        page_host = ttk.Frame(outer, style="Panel.TFrame")
        page_host.pack(fill="both", expand=True)
        page_host.rowconfigure(0, weight=1)
        page_host.columnconfigure(0, weight=1)
        calendar_page = ttk.Frame(page_host, style="Panel.TFrame", padding=(22, 20, 22, 18))
        settings_page = ttk.Frame(page_host, style="Panel.TFrame")
        calendar_page.grid(row=0, column=0, sticky="nsew")
        settings_page.grid(row=0, column=0, sticky="nsew")
        self._pages = {"calendar": calendar_page, "settings": settings_page}
        self._build_calendar_page(calendar_page)
        self._build_settings_page(settings_page)
        self._show_page("calendar")

    def _show_page(self, page_name: str) -> None:
        self._pages[page_name].tkraise()
        for name, button in self._tab_buttons.items():
            selected = name == page_name
            button.configure(
                bg=ACCENT_COLOR if selected else TAB_IDLE_BG,
                fg="white" if selected else MUTED_COLOR,
                activebackground=ACCENT_HOVER if selected else TAB_HOVER_BG,
                activeforeground="white" if selected else TEXT_COLOR,
                font=("Microsoft YaHei UI", 10, "bold" if selected else "normal"),
            )

    def _build_calendar_page(self, panel: ttk.Frame) -> None:
        month_bar = ttk.Frame(panel, style="Panel.TFrame")
        month_bar.pack(fill="x", pady=(0, 14))
        ttk.Button(month_bar, text="‹", width=3, command=lambda: self._change_month(-1)).pack(side="left")
        self._month_title = ttk.Label(month_bar, text="", style="PanelTitle.TLabel", anchor="center")
        self._month_title.pack(side="left", fill="x", expand=True)
        ttk.Button(month_bar, text="›", width=3, command=lambda: self._change_month(1)).pack(side="right")

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
            ttk.Label(
                self._calendar_frame,
                text=weekday,
                style="CalendarHeader.TLabel",
                anchor="center",
            ).grid(row=0, column=column, sticky="nsew", padx=3, pady=(0, 5))

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=(14, 12))
        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill="x")
        self._selection_label = ttk.Label(footer, text="未选择日期", style="Muted.TLabel")
        self._selection_label.pack(side="left")
        ttk.Button(footer, text="清空选择", style="Secondary.TButton", command=self._clear_selection).pack(side="right")
        self._generate_button = ttk.Button(
            footer,
            text="生成选中日期",
            style="Primary.TButton",
            command=self._make_reports,
            state="disabled",
        )
        self._generate_button.pack(side="right", padx=(0, 8))
        self._status_label = ttk.Label(panel, text="正在读取月份记录…", style="Muted.TLabel")
        self._status_label.pack(anchor="w", pady=(12, 0))

    def _build_settings_page(self, page: ttk.Frame) -> None:
        canvas = tk.Canvas(page, bg=PANEL_BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas, style="Panel.TFrame", padding=(20, 16, 20, 18))
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", self._settings_mousewheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))
        self._settings_canvas = canvas

        file_group = self._settings_group(content, "文件监控")
        self._add_text_field(file_group, "监控目录", "watch_dirs", 3)
        self._add_text_field(file_group, "排除目录", "exclude_dirs", 3)
        self._add_text_field(file_group, "排除规则", "exclude_patterns", 4)
        self._add_entry_field(file_group, "目标扩展名", "target_extensions")

        database_group = self._settings_group(content, "数据库与事件")
        self._add_entry_field(database_group, "数据库路径", "db_path")
        self._add_entry_field(database_group, "批量写入条数", "database_batch_size")
        self._add_entry_field(database_group, "批量刷新间隔（秒）", "database_flush_seconds")
        self._add_entry_field(database_group, "文件事件防抖（秒）", "debounce_seconds")
        self._add_entry_field(database_group, "Everything DLL（兼容项）", "everything_dll_path")

        foreground_group = self._settings_group(content, "浏览器与常用应用")
        self._add_entry_field(foreground_group, "前台检测间隔（秒）", "browser_poll_seconds")
        self._add_entry_field(foreground_group, "网页最短记录（秒）", "browser_minimum_seconds")
        self._add_entry_field(foreground_group, "应用最短记录（秒）", "application_minimum_seconds")
        self._add_text_field(foreground_group, "应用进程映射", "tracked_apps", 9)

        system_group = self._settings_group(content, "系统")
        ttk.Checkbutton(
            system_group,
            text="统计每小时鼠标点击和键盘敲击次数",
            variable=self._input_activity_var,
        ).pack(anchor="w", pady=(0, 8))
        ttk.Checkbutton(system_group, text="登录 Windows 后自动启动", variable=self._startup_var).pack(anchor="w")
        ttk.Label(
            system_group,
            text="启动后默认静默运行，可从系统托盘图标打开窗口。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(5, 0))

        save_bar = ttk.Frame(content, style="Panel.TFrame")
        save_bar.pack(fill="x", pady=(8, 0))
        self._settings_status = ttk.Label(save_bar, text="", style="Muted.TLabel")
        self._settings_status.pack(side="left")
        ttk.Button(save_bar, text="重新载入", style="Secondary.TButton", command=self._load_settings).pack(side="right")
        ttk.Button(save_bar, text="保存设置", style="Primary.TButton", command=self._save_settings).pack(side="right", padx=(0, 8))

    @staticmethod
    def _settings_group(parent: ttk.Frame, title: str) -> ttk.LabelFrame:
        group = ttk.LabelFrame(parent, text=title, style="Settings.TLabelframe", padding=(14, 12))
        group.pack(fill="x", pady=(0, 14))
        group.columnconfigure(1, weight=1)
        return group

    def _add_entry_field(self, parent: ttk.LabelFrame, label: str, key: str) -> None:
        row = parent.grid_size()[1]
        variable = tk.StringVar()
        self._settings_vars[key] = variable
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 14), pady=5)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=5)

    def _add_text_field(self, parent: ttk.LabelFrame, label: str, key: str, height: int) -> None:
        row = parent.grid_size()[1]
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 14), pady=5)
        text = tk.Text(
            parent,
            height=height,
            wrap="none",
            font=("Consolas", 9),
            relief="solid",
            bd=1,
            highlightthickness=0,
            undo=True,
        )
        text.grid(row=row, column=1, sticky="ew", pady=5)
        self._settings_texts[key] = text

    def _settings_mousewheel(self, event: tk.Event) -> None:
        self._settings_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _load_settings(self) -> None:
        try:
            _, data = read_config_data(self._config_path)
            text_values = {
                "watch_dirs": "\n".join(str(item) for item in data.get("watch_dirs", [])),
                "exclude_dirs": "\n".join(str(item) for item in data.get("exclude_dirs", [])),
                "exclude_patterns": "\n".join(str(item) for item in data.get("exclude_patterns", [])),
                "tracked_apps": "\n".join(
                    f"{process} = {display_name}"
                    for process, display_name in data.get("tracked_apps", DEFAULT_TRACKED_APPS).items()
                ),
            }
            for key, value in text_values.items():
                widget = self._settings_texts[key]
                widget.delete("1.0", "end")
                widget.insert("1.0", value)
            extensions = data.get("target_extensions", [])
            values = {
                "target_extensions": ", ".join(str(item) for item in extensions),
                "db_path": data.get("db_path", "work_log.db"),
                "database_batch_size": data.get("database_batch_size", 100),
                "database_flush_seconds": data.get("database_flush_seconds", 0.5),
                "debounce_seconds": data.get("debounce_seconds", 1.0),
                "everything_dll_path": data.get("everything_dll_path", ""),
                "browser_poll_seconds": data.get("browser_poll_seconds", 1.0),
                "browser_minimum_seconds": data.get("browser_minimum_seconds", 3.0),
                "application_minimum_seconds": data.get("application_minimum_seconds", 3.0),
            }
            for key, value in values.items():
                self._settings_vars[key].set(str(value))
            self._startup_var.set(is_startup_enabled())
            self._input_activity_var.set(bool(data.get("input_activity_enabled", True)))
            self._settings_status.configure(text="配置已载入")
        except Exception:
            LOGGER.exception("载入界面设置失败")
            self._settings_status.configure(text="配置载入失败")

    def _collect_settings(self) -> dict[str, Any]:
        def lines(key: str) -> list[str]:
            return [line.strip() for line in self._settings_texts[key].get("1.0", "end").splitlines() if line.strip()]

        def number(key: str, minimum: float, integer: bool = False) -> int | float:
            raw = self._settings_vars[key].get().strip()
            try:
                value = int(raw) if integer else float(raw)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是数字") from exc
            if value < minimum:
                raise ValueError(f"{key} 不能小于 {minimum}")
            return value

        watch_dirs = lines("watch_dirs")
        extensions = parse_extensions(self._settings_vars["target_extensions"].get())
        db_path = self._settings_vars["db_path"].get().strip()
        if not watch_dirs:
            raise ValueError("至少需要一个监控目录")
        if not extensions:
            raise ValueError("至少需要一个目标扩展名")
        if not db_path:
            raise ValueError("数据库路径不能为空")
        return {
            "watch_dirs": watch_dirs,
            "exclude_dirs": lines("exclude_dirs"),
            "exclude_patterns": lines("exclude_patterns"),
            "target_extensions": extensions,
            "db_path": db_path,
            "everything_dll_path": self._settings_vars["everything_dll_path"].get().strip(),
            "database_batch_size": number("database_batch_size", 1, integer=True),
            "database_flush_seconds": number("database_flush_seconds", 0.05),
            "debounce_seconds": number("debounce_seconds", 0.1),
            "browser_poll_seconds": number("browser_poll_seconds", 0.1),
            "browser_minimum_seconds": number("browser_minimum_seconds", 0.0),
            "application_minimum_seconds": number("application_minimum_seconds", 0.0),
            "tracked_apps": parse_tracked_apps(self._settings_texts["tracked_apps"].get("1.0", "end")),
            "input_activity_enabled": self._input_activity_var.get(),
        }

    def _save_settings(self) -> None:
        try:
            updates = self._collect_settings()
            save_config_updates(updates, self._config_path)
            set_startup_enabled(self._startup_var.get(), self._config_path.parent)
        except (ValueError, ConfigError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self.root)
            return
        except Exception:
            LOGGER.exception("保存设置失败")
            messagebox.showerror("保存失败", "无法保存设置，请查看运行日志。", parent=self.root)
            return
        self._settings_status.configure(text="已保存，监控参数重启后生效")
        messagebox.showinfo(
            "设置已保存",
            "参数设置已保存。监控参数将在重启程序后生效，开机启动设置已立即生效。",
            parent=self.root,
        )

    @staticmethod
    def _legend_item(parent: ttk.Frame, color: str, text: str) -> None:
        item = ttk.Frame(parent, style="Panel.TFrame")
        item.pack(side="left", padx=(0, 16))
        tk.Label(item, width=2, height=1, bg=color, relief="solid", bd=1, highlightthickness=0).pack(side="left", padx=(0, 5))
        ttk.Label(item, text=text, style="Muted.TLabel").pack(side="left")

    def _refresh_month(self) -> None:
        self._month_title.configure(text=f"{self._display_month.year} 年 {self._display_month.month:02d} 月")
        try:
            self._available_in_month = set(self._available_days(self._display_month.year, self._display_month.month))
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
                font=("Microsoft YaHei UI", 11, "bold"),
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
        start = self._display_month - timedelta(days=self._display_month.weekday())
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
        self._selection_label.configure(text="未选择日期" if count == 0 else f"已选择 {count} 天 · 灰色日期会自动跳过")
        self._generate_button.configure(state="disabled" if count == 0 else "normal")

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

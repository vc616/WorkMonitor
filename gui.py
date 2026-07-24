from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable
from datetime import date, datetime
from tkinter import messagebox
from pathlib import Path

from models import GeneratedReports
from report import write_reports
from tray import TrayController

LOGGER = logging.getLogger(__name__)


class MonitorGui:
    def __init__(
        self,
        output_dir: Path,
        generate_report: Callable[[date], GeneratedReports | None],
        shutdown_callback: Callable[[], None],
    ):
        self._output_dir = output_dir
        self._generate_report = generate_report
        self._shutdown_callback = shutdown_callback
        self.root = tk.Tk()
        self.root.title("工作监控")
        self.root.geometry("360x150")
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        tk.Label(self.root, text="日报日期（YYYY-MM-DD）：").pack(pady=(15, 3))
        self._date_var = tk.StringVar(value=date.today().isoformat())
        tk.Entry(self.root, textvariable=self._date_var, width=18).pack()
        tk.Button(self.root, text="总结日志", command=self._make_report, width=16).pack(pady=12)
        self._tray = TrayController(self.show, self.request_shutdown)

    def run(self) -> None:
        self._tray.start()
        self.root.mainloop()

    def show(self) -> None:
        self.root.after(0, self._show_now)

    def request_shutdown(self) -> None:
        self.root.after(0, self._shutdown)

    def _show_now(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _make_report(self) -> None:
        value = self._date_var.get().strip()
        try:
            day = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror("日期错误", "请输入 YYYY-MM-DD 格式的日期。", parent=self.root)
            return
        try:
            reports = self._generate_report(day)
            if reports is None:
                messagebox.showinfo("无数据", f"{value} 没有工作记录。", parent=self.root)
                return
            summary, detailed = write_reports(self._output_dir, day, reports)
            messagebox.showinfo(
                "完成",
                f"精简日报：\n{summary}\n\n完整日报：\n{detailed}",
                parent=self.root,
            )
        except Exception:
            LOGGER.exception("生成日报失败")
            messagebox.showerror("生成失败", "日报生成失败，请查看日志。", parent=self.root)

    def _shutdown(self) -> None:
        self._tray.stop()
        try:
            self._shutdown_callback()
        finally:
            self.root.destroy()

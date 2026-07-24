from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from utils import get_resource_path

LOGGER = logging.getLogger(__name__)


def create_tray_icon():
    from PIL import Image, ImageDraw

    icon_path = get_resource_path("assets", "work_monitor.ico")
    if icon_path.is_file():
        with Image.open(icon_path) as icon:
            return icon.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=13, fill="#167663")
    draw.ellipse((14, 14, 50, 50), outline="white", width=4)
    draw.line((32, 32, 32, 21), fill="white", width=4)
    draw.line((32, 32, 42, 37), fill="white", width=4)
    return image


class TrayController:
    def __init__(self, show_window: Callable[[], None], exit_app: Callable[[], None]):
        self._show_window = show_window
        self._exit_app = exit_app
        self._icon = None

    def start(self) -> bool:
        try:
            import pystray

            self._icon = pystray.Icon(
                "work_monitor",
                create_tray_icon(),
                "工作监控",
                menu=pystray.Menu(
                    pystray.MenuItem("打开窗口", lambda *_: self._show_window()),
                    pystray.MenuItem("退出程序", lambda *_: self._exit_app()),
                ),
            )
            threading.Thread(target=self._run_icon, name="tray", daemon=True).start()
            return True
        except Exception:
            LOGGER.exception("系统托盘启动失败，程序仍可通过窗口运行")
            return False

    def _run_icon(self) -> None:
        try:
            self._icon.run()
        except Exception:
            LOGGER.exception("系统托盘运行失败，正在显示主窗口")
            self._show_window()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()

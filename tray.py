from __future__ import annotations

import logging
import threading
from collections.abc import Callable

LOGGER = logging.getLogger(__name__)


def create_tray_icon():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (64, 64), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill="steelblue")
    draw.text((22, 21), "M", fill="white")
    return image


class TrayController:
    def __init__(self, show_window: Callable[[], None], exit_app: Callable[[], None]):
        self._show_window = show_window
        self._exit_app = exit_app
        self._icon = None

    def start(self) -> None:
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
            threading.Thread(target=self._icon.run, name="tray", daemon=True).start()
        except Exception:
            LOGGER.exception("系统托盘启动失败，程序仍可通过窗口运行")

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()

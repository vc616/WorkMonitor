from __future__ import annotations

from enum import StrEnum


class FileEventType(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"


class WorkAction(StrEnum):
    OPEN_FILE = "打开文件"
    CLOSE_FILE = "关闭文件"
    SAVE_FILE = "修改后保存"
    CREATE_FILE = "新建文件"
    DELETE_FILE = "删除文件"
    DELETE_FOLDER = "删除文件夹"
    MOVE_FOLDER = "移动文件夹"
    BROWSE_PAGE = "浏览网页"
    USE_APPLICATION = "使用应用"
    MOUSE_CLICK = "鼠标点击"
    KEYSTROKE = "键盘敲击"


MODIFIED_ACTIONS = frozenset(
    {
        WorkAction.SAVE_FILE.value,
        "修改/保存",
        "新建/修改",
        "修改文件",
    }
)

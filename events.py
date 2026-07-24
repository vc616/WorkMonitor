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
    BROWSE_PAGE = "浏览网页"
    USE_APPLICATION = "使用应用"


MODIFIED_ACTIONS = frozenset(
    {
        WorkAction.SAVE_FILE.value,
        "修改/保存",
        "新建/修改",
        "修改文件",
    }
)

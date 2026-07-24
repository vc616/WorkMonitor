from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils import get_app_dir

DEFAULT_TRACKED_APPS = {
    "wechat.exe": "微信",
    "weixin.exe": "微信",
    "wxwork.exe": "企业微信",
    "wecom.exe": "企业微信",
    "dingtalk.exe": "钉钉",
    "feishu.exe": "飞书",
    "lark.exe": "飞书",
    "qq.exe": "QQ",
    "tim.exe": "TIM",
    "teams.exe": "Microsoft Teams",
    "ms-teams.exe": "Microsoft Teams",
    "slack.exe": "Slack",
    "telegram.exe": "Telegram",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AppConfig:
    app_dir: Path
    watch_dirs: tuple[Path, ...]
    exclude_dirs: tuple[Path, ...]
    exclude_patterns: tuple[str, ...]
    target_extensions: frozenset[str]
    db_path: Path
    debounce_seconds: float = 1.0
    browser_poll_seconds: float = 1.0
    browser_minimum_seconds: float = 3.0
    database_batch_size: int = 100
    database_flush_seconds: float = 0.5
    tracked_apps: tuple[tuple[str, str], ...] = tuple(DEFAULT_TRACKED_APPS.items())
    application_minimum_seconds: float = 3.0


def _resolve_path(value: str, app_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else app_dir / path


def load_config(path: Path | None = None) -> AppConfig:
    app_dir = get_app_dir()
    config_path = path or app_dir / "config.json"
    try:
        with config_path.open("r", encoding="utf-8") as file:
            raw: dict[str, Any] = json.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {config_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {config_path}: {exc}") from exc

    required = ("watch_dirs", "target_extensions", "db_path")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ConfigError(f"配置缺少字段: {', '.join(missing)}")

    extensions = frozenset(
        extension.lower() if str(extension).startswith(".") else f".{str(extension).lower()}"
        for extension in raw["target_extensions"]
    )
    if not extensions:
        raise ConfigError("target_extensions 不能为空")

    try:
        batch_size = max(1, int(raw.get("database_batch_size", 100)))
        flush_seconds = max(0.05, float(raw.get("database_flush_seconds", 0.5)))
        debounce_seconds = max(0.1, float(raw.get("debounce_seconds", 1.0)))
        poll_seconds = max(0.1, float(raw.get("browser_poll_seconds", 1.0)))
        minimum_seconds = max(0.0, float(raw.get("browser_minimum_seconds", 3.0)))
        application_minimum_seconds = max(0.0, float(raw.get("application_minimum_seconds", 3.0)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置中的数值无效: {exc}") from exc

    tracked_raw = raw.get("tracked_apps", DEFAULT_TRACKED_APPS)
    if not isinstance(tracked_raw, dict):
        raise ConfigError("tracked_apps 必须是进程名到应用名称的对象")
    tracked_apps = tuple(
        (str(process).strip().lower(), str(name).strip())
        for process, name in tracked_raw.items()
        if str(process).strip() and str(name).strip()
    )

    return AppConfig(
        app_dir=app_dir,
        watch_dirs=tuple(_resolve_path(str(item), app_dir) for item in raw["watch_dirs"]),
        exclude_dirs=tuple(_resolve_path(str(item), app_dir) for item in raw.get("exclude_dirs", [])),
        exclude_patterns=tuple(str(item) for item in raw.get("exclude_patterns", [])),
        target_extensions=extensions,
        db_path=_resolve_path(str(raw["db_path"]), app_dir),
        debounce_seconds=debounce_seconds,
        browser_poll_seconds=poll_seconds,
        browser_minimum_seconds=minimum_seconds,
        database_batch_size=batch_size,
        database_flush_seconds=flush_seconds,
        tracked_apps=tracked_apps,
        application_minimum_seconds=application_minimum_seconds,
    )

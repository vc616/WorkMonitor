# WorkMonitor V2

这是基于 `monitor_daemon.py` 重构的工作监控程序。程序保留旧版 `work_log` SQLite 表结构和现有 `config.json`，入口改为 `main.py`。

## 架构

Watchdog 回调只负责将 `FileEvent` 放入队列，`EventProcessor` 独占文件状态并生成统一的 `LogEvent`。浏览器监控使用同一个 `PageTracker`，所有日志写入和日报查询都由 `DatabaseWriter` 在线程内完成。SQLite 使用 WAL、索引和批量提交，应用中不会再创建多个连接或使用 `DB_LOCK`。

## 运行

```text
python -m pip install -r requirements.txt
python main.py
```

`config.json` 中的 `watch_dirs`、`exclude_dirs`、`exclude_patterns`、`target_extensions`、`db_path` 与旧版兼容。SQLite 数据库继续使用根目录的 `work_log.db`。可选性能配置为 `database_batch_size`、`database_flush_seconds`、`debounce_seconds`、`browser_poll_seconds` 和 `browser_minimum_seconds`。`everything_dll_path` 会被兼容读取但 V2 不再加载 Everything DLL。

日报仍通过托盘菜单打开窗口后生成，输出到 `LogFile` 目录中的 `YYYY-MM-DD.md` 和 `YYYY-MM-DD_详细.md`；目录不存在时会自动创建。

## 日历日报

窗口按月显示工作记录。白色日期表示数据库中有记录，灰色日期表示无记录且不可单独选择。按住鼠标从一个有记录日期拖到另一个日期，可以批量选择范围；范围中的灰色日期会自动跳过。点击“生成选中日期”后，每个选中日期分别生成精简日报和详细日报。

## 常用应用统计

程序复用前台窗口轮询，记录微信、企业微信、钉钉、飞书、QQ、Teams 等常用应用的前台激活时长。默认只保存应用名称、进程名、结束时间和累计秒数，不保存聊天内容或窗口标题。可在 `config.json` 的 `tracked_apps` 中增删进程，并通过 `application_minimum_seconds` 调整最短记录时长。

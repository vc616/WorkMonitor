# WorkMonitor V2

这是基于 `monitor_daemon.py` 重构的工作监控程序。程序保留旧版 `work_log` SQLite 表结构和现有 `config.json`，入口改为 `main.py`。

## 架构

Watchdog 回调只负责将 `FileEvent` 放入队列，`EventProcessor` 独占文件状态并生成统一的 `LogEvent`。浏览器监控使用同一个 `PageTracker`，所有日志写入和日报查询都由 `DatabaseWriter` 在线程内完成。SQLite 使用 WAL、索引和批量提交，应用中不会再创建多个连接或使用 `DB_LOCK`。

## 运行

```text
python -m pip install -r requirements.txt
python main.py
```

`config.json` 中的 `watch_dirs`、`exclude_dirs`、`exclude_patterns`、`target_extensions`、`db_path` 与旧版兼容。可选性能配置为 `database_batch_size`、`database_flush_seconds`、`debounce_seconds`、`browser_poll_seconds` 和 `browser_minimum_seconds`。`everything_dll_path` 会被兼容读取但 V2 不再加载 Everything DLL。

日报仍通过托盘菜单打开窗口后生成，输出到程序目录的 `YYYY-MM-DD.md` 和 `YYYY-MM-DD_详细.md`。

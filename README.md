# WorkMonitor V2

WorkMonitor V2 是一个面向 Windows 的本地工作活动记录工具。它在后台记录文件操作、浏览器页面停留、常用应用前台时长，以及每小时键盘和鼠标活动，并按日期生成 Markdown 工作日报。

所有数据默认保存在本机，不依赖在线服务。

## 功能

- 监控指定目录中的文件创建、删除、打开、关闭和保存事件
- 识别 AutoCAD、Office、WPS 等软件产生的锁文件和临时文件
- 统计浏览器页面标题及前台停留时间
- 统计微信、企业微信、钉钉、飞书、QQ、Teams 等应用的前台时长
- 按小时汇总鼠标点击次数和键盘敲击次数
- 使用月历查看有记录的日期，支持拖拽批量选择
- 为每个日期生成精简日报和详细日报
- 在界面中编辑监控参数并保存到 `config.json`
- 支持当前用户开机启动和静默托盘运行

## 系统要求

- Windows 10 或 Windows 11
- Python 3.11 及以上版本
- Python 安装需包含 Tcl/Tk
- 运行账户需要有目标目录的读取权限

## 安装

在 PowerShell 中执行：

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

启动程序：

```powershell
.\venv\Scripts\python.exe main.py
```

程序启动后默认不显示主窗口，只在系统托盘保留图标。通过托盘菜单可以打开窗口或退出程序。

`monitor_daemon.py` 是旧入口的兼容启动器，现有快捷方式仍可继续使用。

## 使用界面

### 日报日历

- 白色日期表示数据库中存在记录
- 灰色日期没有记录，不可单独选择
- 按住鼠标拖拽可以批量选择日期
- 拖拽范围中的灰色日期会自动跳过
- 点击“生成选中日期”后，每天分别生成两份 Markdown 日报

生成文件保存在 `LogFile`：

```text
LogFile/
├── YYYY-MM-DD.md
└── YYYY-MM-DD_详细.md
```

### 参数设置

设置页可以修改：

- 监控目录和排除目录
- 排除规则和目标文件扩展名
- SQLite 数据库路径及批量写入参数
- 文件事件防抖时间
- 浏览器与常用应用的最短记录时间
- 常用应用进程映射
- 键盘和鼠标次数统计开关
- Windows 开机启动开关

设置使用临时文件校验后原子写入 `config.json`。监控参数需要重启程序后生效；开机启动设置保存后立即生效。

## 配置参考

路径相对值以程序目录为基准。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `watch_dirs` | `D:\` | 递归监控的目录列表 |
| `exclude_dirs` | 见 `config.json` | 完全排除的目录列表 |
| `exclude_patterns` | 见 `config.json` | Gitignore 风格的排除规则，支持 `!` 重新包含 |
| `target_extensions` | Office、CAD、代码等 | 需要记录的文件扩展名 |
| `db_path` | `work_log.db` | SQLite 数据库路径 |
| `database_batch_size` | `100` | 单次批量写入的最大日志数 |
| `database_flush_seconds` | `0.5` | 未满批次时的刷新间隔 |
| `debounce_seconds` | `1.0` | 文件事件防抖及关闭/重开合并窗口 |
| `browser_poll_seconds` | `1.0` | 前台窗口检测间隔 |
| `browser_minimum_seconds` | `3.0` | 网页停留的最短记录时间 |
| `application_minimum_seconds` | `3.0` | 常用应用前台时长的最短记录时间 |
| `input_activity_enabled` | `true` | 是否统计每小时键盘和鼠标次数 |
| `tracked_apps` | 见 `config.json` | 进程名到应用显示名称的映射 |
| `everything_dll_path` | `Everything64.dll` | 旧版兼容字段，V2 不再加载该 DLL |

常用应用映射示例：

```json
{
  "WeChat.exe": "微信",
  "WXWork.exe": "企业微信",
  "DingTalk.exe": "钉钉"
}
```

进程名可以在 Windows 任务管理器的“详细信息”页查看。

## 数据文件

```text
WorkMonitorV2/
├── config.json              # 用户配置
├── work_log.db              # SQLite 工作记录
├── LogFile/
│   ├── app.log              # 程序运行日志
│   ├── YYYY-MM-DD.md        # 精简日报
│   └── YYYY-MM-DD_详细.md    # 详细日报
└── ...
```

数据库使用 WAL、索引和批量提交。运行期间可能临时出现 `work_log.db-wal` 和 `work_log.db-shm`，程序正常关闭后由 SQLite 处理。

## 数据与隐私

WorkMonitor V2 不上传数据，但会在本地保存以下内容：

- 文件监控：文件名、完整路径、大小、操作类型和时间
- 网页浏览：浏览器进程、页面标题和停留秒数
- 常用应用：应用名称、进程名和前台秒数，不保存窗口标题或聊天内容
- 输入活动：每小时鼠标点击和键盘敲击总数，不保存具体按键、按钮、坐标或输入内容

`work_log.db` 未加密。若记录中可能包含敏感文件路径或页面标题，请按本机敏感数据管理，并限制项目目录的访问权限。

## 架构

```text
Watchdog ──> FileEvent Queue ──> EventProcessor ──┐
                                                 │
Foreground Monitor ──> Browser/App LogEvent ─────┼──> Log Queue ──> SQLite Writer
                                                 │
Input Hooks ──> Hourly Count LogEvent ───────────┘
```

- Watchdog 回调只负责投递事件，不执行数据库或文件稳定性检查
- `EventProcessor` 独占文件状态、快照、防抖和待关闭状态
- 浏览器与常用应用共用一次前台窗口读取
- 键盘和鼠标使用事件监听，不进行高频轮询
- `DatabaseWriter` 独占唯一 SQLite 连接，负责批量写入和日报查询

## 主要模块

| 文件 | 职责 |
| --- | --- |
| `main.py` | 应用启动、停止和组件编排 |
| `config.py` | 配置读取、验证和原子保存 |
| `database.py` | SQLite Writer、SQL、索引和查询 |
| `watchdog_handler.py` | 文件系统事件入队 |
| `event_processor.py` | 文件事件处理、CAD/Office 识别和防抖 |
| `browser.py` | 浏览器页面和常用应用前台统计 |
| `input_activity.py` | 每小时键盘和鼠标次数统计 |
| `report.py` | 精简日报和详细日报生成 |
| `gui.py` | 日历、批量选择和参数设置界面 |
| `startup.py` | Windows 当前用户开机启动 |
| `tray.py` | 系统托盘图标和菜单 |

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试不会启动真实全局输入钩子，也不会修改 Windows 开机启动项。

## 常见问题

### 保存设置后没有立即变化

除开机启动外，监控参数会在下一次启动时载入。请从托盘菜单退出程序后重新启动。

### 没有系统托盘图标

确认已安装 `Pillow` 和 `pystray`。托盘启动失败时程序会回退显示主窗口，并将异常写入 `LogFile/app.log`。

### 没有键盘或鼠标统计

确认 `input_activity_enabled` 已启用，并安装了 `pynput`。如果目标程序以管理员权限运行，监控程序可能也需要使用相同权限级别运行。

### 出现 `Can't find a usable init.tcl`

当前 Python 安装缺少 Tcl/Tk。请通过 Python 官方安装程序补装 Tcl/Tk，之后重新创建虚拟环境。

### 日历日期是灰色

灰色表示该日期在 `work_log.db` 中没有任何记录。它不会阻止拖拽范围选择，生成日报时会自动跳过。

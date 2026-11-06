# WorkMonitor V2

WorkMonitor V2 是一个面向 Windows 的本地工作活动记录工具。它在后台记录文件操作、浏览器页面停留、常用应用前台时长，以及每小时键盘和鼠标活动，并按日期生成 Markdown 工作日报。

所有数据默认保存在本机，不依赖在线服务。

## 功能

- 监控指定目录中的文件创建、删除、打开、关闭和保存事件
- 删除或移动文件夹时折叠其子文件事件，只保留一条文件夹操作记录
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

### 使用 EXE

直接运行 `WorkMonitorV2.exe`。程序会静默启动并显示系统托盘图标；首次运行时，会在 EXE 同目录释放可编辑的 `config.json`。后续启动始终读取该文件，升级或替换 EXE 不会覆盖已有配置。

建议将 EXE 放在当前用户有写入权限的独立目录中，因为配置、数据库和 `LogFile` 都保存在程序目录。

### 构建 EXE

项目使用 PyInstaller 生成 Windows 单文件、无控制台窗口的 EXE：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1
```

构建结果为 `dist\WorkMonitorV2.exe`。`assets\default_config.json`、程序图标，以及从当前 Python 安装复制的 Tcl/Tk 运行时都会打包进 EXE 内部，不需要随 EXE 单独分发；程序启动时自动把 `TCL_LIBRARY` / `TK_LIBRARY` 指向内置目录，因此目标机器无需安装 Python。

构建脚本会先切换到项目目录再调用 PyInstaller，并显式指定 `--distpath` / `--workpath`，从任意目录执行都不会把产物写到别处。图标只在缺失时重新生成，不会覆盖已提交的资产。EXE 未启用 UPX 压缩（装有全局键鼠钩子的加壳程序容易被杀软误报），也未做代码签名，首次运行可能触发 SmartScreen 提示。

若窗口化 EXE 在启动阶段异常退出且 `app.log` 未能创建，可在 `%TEMP%\WorkMonitorV2\startup_error.log` 查看紧急错误记录。

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
- SQLite 数据库路径、批量写入参数与事件队列上限
- 文件事件防抖时间
- 浏览器与常用应用的最短记录时间
- 常用应用进程映射、可识别编辑器进程
- 键盘和鼠标次数统计开关
- Windows 开机启动开关

设置使用临时文件校验后原子写入 `config.json`。监控参数需要重启程序后生效；开机启动设置保存后立即生效。

## 配置参考

路径相对值以**配置文件所在目录**为基准（通常就是程序目录）；只有显式指定外部配置文件时两者才会不同。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `watch_dirs` | `D:\` | 递归监控的目录列表 |
| `exclude_dirs` | 见 `config.json` | 完全排除的目录列表，优先级高于所有排除规则 |
| `exclude_patterns` | 见 `config.json` | Gitignore 风格的排除规则，支持 `!` 重新包含 |
| `target_extensions` | Office、CAD、代码等 | 需要记录的文件扩展名 |
| `db_path` | `work_log.db` | SQLite 数据库路径 |
| `database_batch_size` | `100` | 单次批量写入的最大日志数 |
| `database_flush_seconds` | `0.5` | 未满批次时的刷新间隔 |
| `queue_max_size` | `10000` | 文件事件队列与写入队列的上限，满时丢弃最早未处理事件并告警 |
| `debounce_seconds` | `1.0` | 文件事件防抖及关闭/重开合并窗口 |
| `browser_poll_seconds` | `1.0` | 前台窗口检测间隔 |
| `browser_minimum_seconds` | `3.0` | 网页停留的最短记录时间 |
| `application_minimum_seconds` | `3.0` | 常用应用前台时长的最短记录时间 |
| `input_activity_enabled` | `true` | 是否统计每小时键盘和鼠标次数 |
| `tracked_apps` | 见 `config.json` | 进程名到应用显示名称的映射 |
| `known_editors` | 见 `config.json` | 可识别的编辑器进程名，用于补充「打开文件」事件 |

`exclude_patterns` 的 `!` 重新包含**只对 `exclude_patterns` 自身生效**，无法覆盖 `exclude_dirs`。规则支持 `*`、`?`、`**`、结尾 `/` 和以 `/` 开头锚定到监控根目录；不支持字符类 `[abc]` 与 `\#` 转义，也不读取目录中的 `.gitignore` 文件。

旧版字段 `everything_dll_path` 已从 V2 中移除，读取时会被忽略，可安全删除。

`known_editors` 中的进程名可以不带扩展名；程序按进程名主干匹配。不在列表中的编辑器仍能产生「修改后保存」记录，只是不会额外推断「打开文件」。

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

`LogFile/app.log` 按 5 MB 轮转，最多保留 3 份历史文件。若程序目录不可写（例如放在 `Program Files` 下），日志会自动改写到 `%LOCALAPPDATA%\WorkMonitorV2\LogFile`，不会阻止程序启动。

## 数据与隐私

WorkMonitor V2 不上传数据，但会在本地保存以下内容：

- 文件监控：文件名、完整路径、大小、操作类型和时间
- 网页浏览：浏览器进程、页面标题和停留秒数
- 常用应用：应用名称、进程名和前台秒数，不保存窗口标题或聊天内容
- 输入活动：每小时鼠标点击和键盘敲击总数，不保存具体按键、按钮、坐标或输入内容

`work_log.db` **未加密**，生成的 Markdown 日报同样是明文。若记录中可能包含敏感文件路径或页面标题，请按本机敏感数据管理，并限制项目目录的访问权限。

关于日志与运行行为：

- `LogFile/app.log` 默认只写 INFO 及以上级别，文件事件与前台会话的敏感信息（页面标题、文件名、完整路径）只出现在 DEBUG 记录中，**默认不落盘**。显式以 verbose 模式运行时 DEBUG 会写入日志，请勿在共享环境开启。
- `msedgewebview2.exe` 不在浏览器白名单中，避免把 Teams / Office / 微信等内嵌 WebView2 的窗口标题当成网页记录。
- **点击窗口右上角的关闭按钮只会隐藏到托盘，所有监控继续运行**；首次关闭会弹出提示。只有托盘菜单的「退出程序」才会停止采集（包括全局键鼠钩子）。
- 程序静默开机自启时同样会在后台采集。请在不需要时从设置页关闭开机启动，并从托盘退出。
- 仓库中的 `dist/`、`smoke_test/`、`LogFile/` 与根目录 `work_log.db` 可能含有**真实运行结果**（明文路径与页面标题）。它们已被 `.gitignore` 忽略，但**请勿整体复制项目目录用于分发**；发布时只复制 EXE 与 `assets/default_config.json`。

程序目录不可写时，日志和**日报**都会自动改写到 `%LOCALAPPDATA%\WorkMonitorV2\LogFile`（最后回退到系统临时目录）；但 `config.json` 与数据库不会回退，仍需要可写的程序目录或显式的 `db_path`。

## 架构

```text
Watchdog ──> FileEvent Queue ──> EventProcessor ──┐
                                                 │
Foreground Monitor ──> Browser/App LogEvent ─────┼──> Log Queue ──> SQLite Writer
                                                 │
Input Hooks ──> Hourly Count LogEvent ───────────┘
```

- Watchdog 回调只负责投递事件，不执行数据库或文件稳定性检查；队列满时丢弃并周期性告警
- `EventProcessor` 独占文件状态、快照、防抖和待关闭状态；状态带 TTL 与容量上限，事件线程内不做阻塞等待
- 所有时间差判断统一使用 `time.monotonic()`，事件自带的 `occurred_at` 只用于落库时间戳；前台会话按天拆分归属
- 浏览器与常用应用共用一次前台窗口读取
- 键盘和鼠标使用事件监听，不进行高频轮询；生成当天日报时只提交增量，重复生成不会重复计数
- `DatabaseWriter` 独占唯一 SQLite 连接，负责批量写入和日报查询；停止前会排空队列中的剩余事件

## 主要模块

| 文件 | 职责 |
| --- | --- |
| `main.py` | 应用启动、停止和组件编排 |
| `config.py` | 配置读取、验证和原子保存 |
| `database.py` | SQLite Writer、SQL、索引和查询 |
| `watchdog_handler.py` | 文件系统事件入队 |
| `event_processor.py` | 文件事件处理、排除规则、CAD/Office 识别和防抖 |
| `browser.py` | 浏览器页面和常用应用前台统计 |
| `input_activity.py` | 每小时键盘和鼠标次数统计 |
| `report.py` | 精简日报和详细日报生成 |
| `parsing.py` | 设置项解析与日期区间计算（不依赖 tkinter） |
| `gui.py` | 日历、批量选择和参数设置界面 |
| `startup.py` | Windows 当前用户开机启动 |
| `tray.py` | 系统托盘图标和菜单 |

## 事件判定规则

- **排除规则**按 gitignore 风格匹配，支持 `*`、`?`、`**`、结尾 `/` 表示目录、以 `/` 开头锚定到监控根目录，以及 `!` 重新包含。目录规则同时排除该目录自身和其下所有文件。
- **原子保存合并**：编辑器的「删除旧文件 + 写入新文件」和「写临时文件 + 重命名覆盖」都会合并为一条“修改后保存”，不会产生虚假的删除与新建。
- **修改防抖**：一次物理保存可能触发多次 `modified`，程序用文件大小与修改时间快照配合固定窗口防抖，只记录一次；即使编辑器不在 `known_editors` 中，也会至少留下一条保存记录。
- **文件夹折叠**：删除或移动文件夹时，其子文件事件会被折叠成一条文件夹操作记录。目录操作只在两个防抖窗口内抑制子事件，删除后重建同名目录不会被永久漏记。
- **锁文件识别**：仅 Office/WPS 的 `~$` 文件和 AutoCAD 的 `.dwl`/`.dwl2` 会被映射回真实文档；普通以 `~` 开头的文件按普通文件处理。
- **跨天归属**：网页与应用的前台会话按会话开始时刻落库，跨午夜的会话会拆成两条分别归属各自日期的记录。

## 测试

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖配置校验、排除规则、锁文件与原子保存、数据库写入与查询、日报生成、界面装配，以及真实文件系统的端到端流程；另有专门的回归用例覆盖单调时钟、跨天会话归属、文件夹操作不计入文件统计、队列背压、停止时排空、报告幂等等修复点。测试不会启动真实全局输入钩子，也不会修改 Windows 开机启动项；界面测试使用替身，不打开窗口，也不需要 Tcl/Tk。

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

### 程序目录只读导致无法保存

把 EXE 移到当前用户可写的目录，或让配置里的 `db_path` 指向可写位置。日志和日报会自动回退到 `%LOCALAPPDATA%`，但配置和数据库不会。

### 关掉窗口后程序还在记录吗

会。关闭按钮只把窗口隐藏到托盘，文件监控、前台统计和键鼠计数继续运行。需要从托盘图标菜单选择「退出程序」才会完全停止；不需要开机自启时请先在设置页关闭该开关。

### 日报没有生成在程序目录的 LogFile

说明程序目录不可写，日志与日报已一起改写到 `%LOCALAPPDATA%\WorkMonitorV2\LogFile`（或系统临时目录）。生成完成弹窗会列出实际文件路径。

### 开机启动开关显示未开启，但我以前设置过

设置页会校验注册表中的启动命令是否仍指向当前程序目录。移动过目录、或从源码版切换到 EXE 版后，旧命令会失效并显示为未开启；重新勾选并保存一次即可修复。

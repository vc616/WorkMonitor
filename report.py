from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from pathlib import Path
from collections.abc import Callable

from events import MODIFIED_ACTIONS, WorkAction
from models import GeneratedReports, WorkLogRecord
from utils import format_duration

LOGGER = logging.getLogger(__name__)

CATEGORIES = {
    "表格文件": frozenset({".xls", ".xlsx"}),
    "文档文件": frozenset({".doc", ".docx", ".pdf"}),
    "图纸文件": frozenset({".dwg", ".dxf"}),
    "代码文件": frozenset({".py", ".md"}),
}


class ReportGenerator:
    def __init__(self, fetch_logs: Callable[[date], list[WorkLogRecord]]):
        self._fetch_logs = fetch_logs

    def generate(self, day: date) -> GeneratedReports | None:
        logs = self._fetch_logs(day)
        if not logs:
            return None

        file_logs: list[WorkLogRecord] = []
        browser_logs: list[WorkLogRecord] = []
        application_logs: list[WorkLogRecord] = []
        modified_files: set[str] = set()
        category_files: dict[str, set[str]] = {name: set() for name in CATEGORIES}
        projects: dict[str, dict[str, list[WorkLogRecord]]] = defaultdict(lambda: defaultdict(list))
        page_totals: dict[str, int] = defaultdict(int)
        page_visits: dict[tuple[str, str], list[WorkLogRecord]] = defaultdict(list)
        application_totals: dict[str, int] = defaultdict(int)
        application_visits: dict[tuple[str, str], list[WorkLogRecord]] = defaultdict(list)
        input_by_hour: dict[int, dict[str, int]] = defaultdict(lambda: {"mouse": 0, "keyboard": 0})

        # All report aggregates are built in one pass over the ordered rows.
        for log in logs:
            if log.action == WorkAction.BROWSE_PAGE.value:
                browser_logs.append(log)
                page_totals[log.file_name] += log.file_size
                page_visits[(log.project_dir, log.file_name)].append(log)
                continue
            if log.action == WorkAction.USE_APPLICATION.value:
                application_logs.append(log)
                application_totals[log.file_name] += log.file_size
                application_visits[(log.project_dir, log.file_name)].append(log)
                continue
            if log.action == WorkAction.MOUSE_CLICK.value:
                input_by_hour[log.timestamp.hour]["mouse"] += log.file_size
                continue
            if log.action == WorkAction.KEYSTROKE.value:
                input_by_hour[log.timestamp.hour]["keyboard"] += log.file_size
                continue
            file_logs.append(log)
            projects[log.project_dir][log.file_name].append(log)
            if log.action in MODIFIED_ACTIONS and not Path(log.file_name).name.lower().startswith("~$"):
                modified_files.add(log.file_name)
                extension = Path(log.file_name).suffix.lower()
                for category, extensions in CATEGORIES.items():
                    if extension in extensions:
                        category_files[category].add(log.file_name)

        first = logs[0].timestamp
        last = logs[-1].timestamp
        work_hours, work_minutes, work_seconds = format_duration(int((last - first).total_seconds()))
        browser_hours, browser_minutes, browser_seconds = format_duration(
            sum(log.file_size for log in browser_logs)
        )
        application_hours, application_minutes, application_seconds = format_duration(
            sum(log.file_size for log in application_logs)
        )

        summary_lines = [
            f"# 工作日报（{day.isoformat()}）",
            "",
            "## 工作概况",
            "",
            f"- 工作时段：{first:%H:%M:%S} - {last:%H:%M:%S}",
            f"- 工作时长：{work_hours} 小时 {work_minutes} 分钟 {work_seconds} 秒",
            f"- 操作记录：{len(file_logs)} 条",
            f"- 涉及项目：{len({log.project_dir for log in file_logs})} 个",
            f"- 涉及文件：{len({log.file_path for log in file_logs})} 个",
            "",
            "## 文件工作",
            "",
            *[f"- {category}：{len(category_files[category])} 个" for category in CATEGORIES],
            f"- 修改文件：{len(modified_files)} 个",
            "- 修改文件列表：",
            *[f"    - {file_name}" for file_name in sorted(modified_files)],
            "",
            "## 网页浏览",
            "",
            f"- 浏览记录：{len(browser_logs)} 条",
            f"- 累计时长：{browser_hours} 小时 {browser_minutes} 分钟 {browser_seconds} 秒",
            "- 主要页面（按累计时间排序）：",
            *[
                f"    - {title}（{duration} 秒）"
                for title, duration in sorted(page_totals.items(), key=lambda item: item[1], reverse=True)[:10]
            ],
            "",
            "## 应用使用",
            "",
            f"- 前台记录：{len(application_logs)} 条",
            f"- 累计时长：{application_hours} 小时 {application_minutes} 分钟 {application_seconds} 秒",
            "- 应用时长（按累计时间排序）：",
            *[
                f"    - {application_name}（{duration // 60} 分钟 {duration % 60} 秒）"
                for application_name, duration in sorted(
                    application_totals.items(), key=lambda item: item[1], reverse=True
                )
            ],
            "",
            "## 输入活动",
            "",
            f"- 鼠标点击：{sum(item['mouse'] for item in input_by_hour.values())} 次",
            f"- 键盘敲击：{sum(item['keyboard'] for item in input_by_hour.values())} 次",
            "- 每小时统计：",
            *[
                f"    - {hour:02d}:00–{hour:02d}:59：鼠标 {counts['mouse']} 次，键盘 {counts['keyboard']} 次"
                for hour, counts in sorted(input_by_hour.items())
            ],
        ]

        detailed_lines = ["=" * 50, f"📅 【工作复盘日报】 日期: {day.isoformat()}", "=" * 50]
        if browser_logs:
            detailed_lines.extend(["", "🌐 网页浏览记录", "-" * 40])
            for (browser, title), visits in page_visits.items():
                total = sum(item.file_size for item in visits)
                detailed_lines.extend(
                    [
                        f"  📄 {title}",
                        f"     ├── 浏览器: {browser}",
                        f"     ├── 首次浏览: {visits[0].timestamp:%H:%M:%S}",
                        f"     ├── 最后浏览: {visits[-1].timestamp:%H:%M:%S}",
                        f"     ├── 浏览次数: {len(visits)} 次",
                        f"     └── 累计时长: {total // 60}分钟 {total % 60}秒",
                    ]
                )

        if application_logs:
            detailed_lines.extend(["", "💻 常用应用前台记录", "-" * 40])
            for (process_name, application_name), visits in application_visits.items():
                total = sum(item.file_size for item in visits)
                detailed_lines.extend(
                    [
                        f"  ◼ {application_name}",
                        f"     ├── 进程: {process_name}",
                        f"     ├── 首次记录: {visits[0].timestamp:%H:%M:%S}",
                        f"     ├── 最后记录: {visits[-1].timestamp:%H:%M:%S}",
                        f"     ├── 前台次数: {len(visits)} 次",
                        f"     └── 累计时长: {total // 60}分钟 {total % 60}秒",
                    ]
                )

        if input_by_hour:
            detailed_lines.extend(["", "⌨ 输入活动（按小时）", "-" * 40])
            detailed_lines.extend(
                f"  {hour:02d}:00–{hour:02d}:59 | 鼠标点击 {counts['mouse']} 次 | 键盘敲击 {counts['keyboard']} 次"
                for hour, counts in sorted(input_by_hour.items())
            )

        for project, files in projects.items():
            detailed_lines.extend(["", f"📂 项目/目录: 【{project}】", "-" * 40])
            for file_name, events in files.items():
                duration = int((events[-1].timestamp - events[0].timestamp).total_seconds() // 60)
                saves = sum(item.action in MODIFIED_ACTIONS for item in events)
                size = f"{events[-1].file_size / (1024 * 1024):.2f} MB" if events[-1].file_size else "未知大小"
                detailed_lines.extend(
                    [
                        f"  📄 {file_name}",
                        f"     ├── 开始时间: {events[0].timestamp:%H:%M:%S} ({events[0].action})",
                        f"     ├── 最后操作: {events[-1].timestamp:%H:%M:%S} ({events[-1].action})",
                        f"     ├── 停留时长: {duration}分钟 | 共保存 {saves} 次",
                        f"     └── 文件大小: {size}",
                    ]
                )
        return GeneratedReports("\n".join(summary_lines) + "\n", "\n".join(detailed_lines) + "\n")


def write_reports(output_dir: Path, day: date, reports: GeneratedReports) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"{day.isoformat()}.md"
    detailed_path = output_dir / f"{day.isoformat()}_详细.md"
    summary_path.write_text(reports.summary, encoding="utf-8")
    detailed_path.write_text(reports.detailed, encoding="utf-8")
    LOGGER.info("日报已生成: %s, %s", summary_path, detailed_path)
    return summary_path, detailed_path

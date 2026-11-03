#!/usr/bin/env python3
r"""Analyze check_pipeline.json and group errors by task id.

Usage:
  python check/analyze_pipeline_report.py D:\path\to\check_pipeline.json
  python check/analyze_pipeline_report.py D:\path\to\root --output summary.md
  python check/analyze_pipeline_report.py -d D:\path\to\parent -r report

The script reads one report file or recursively finds check_pipeline.json files
under a directory. It groups remaining issues by check name and reason, listing
the affected task ids and session files for each error.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


REPORT_NAME = "check_pipeline.json"
DEFAULT_MARKDOWN_NAME = "pipeline_report_summary.md"
TURN_REASON_RE = re.compile(r"^main user 对话轮次为 \d+，不满足 > 4$")

def find_report_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(path)
    return sorted(path.rglob(REPORT_NAME))


def find_report_paths_from_parent(parent_dir: Path, child_path: str) -> list[Path]:
    if not parent_dir.exists():
        raise FileNotFoundError(parent_dir)
    if not parent_dir.is_dir():
        raise NotADirectoryError(parent_dir)

    child = Path(child_path)
    if child.name == REPORT_NAME or child.suffix.lower() == ".json":
        direct_report = parent_dir / child
        reports = [direct_report] if direct_report.is_file() else []
        reports.extend(path for path in parent_dir.glob(f"*/{child_path}") if path.is_file())
        return sorted(dict.fromkeys(reports))

    direct_report = parent_dir / child / REPORT_NAME
    reports = [direct_report] if direct_report.is_file() else []
    reports.extend(path for path in parent_dir.glob(f"*/{child_path}/{REPORT_NAME}") if path.is_file())
    return sorted(dict.fromkeys(reports))


def should_ignore(check_name: str, reason: str, check_title: str = "") -> bool:
    if check_name == "basic_acceptance" and TURN_REASON_RE.match(reason):
        return True
    return False


def group_reason(check_name: str, reason: str) -> str:
    return reason


def as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def report_task_id(report: dict[str, Any], report_path: Path | None = None) -> str:
    source_root = as_text(report.get("source_root"))
    if source_root:
        return Path(source_root).name
    if report_path is not None:
        return report_path.parent.parent.name
    return ""


def get_task_id(item: dict[str, Any], report: dict[str, Any]) -> str:
    task_id = report_task_id(report)
    if task_id:
        return task_id
    return as_text(item.get("产物id") or item.get("project"))


def get_session_path(item: dict[str, Any], report: dict[str, Any]) -> str:
    session_id = as_text(item.get("文件id") or item.get("session"))
    if not session_id:
        return ""
    session_path = Path(session_id)
    if session_path.is_absolute():
        return str(session_path)
    source_root = as_text(report.get("source_root"))
    return str(Path(source_root) / session_path) if source_root else session_id


def iter_issue_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = result.get("tasks")
    if isinstance(tasks, list):
        return [item for item in tasks if isinstance(item, dict)]

    child_report = result.get("report")
    if isinstance(child_report, dict):
        child_tasks = child_report.get("tasks")
        if isinstance(child_tasks, list):
            return [item for item in child_tasks if isinstance(item, dict)]

    issue_locations = result.get("issue_locations")
    if isinstance(issue_locations, list):
        return [item for item in issue_locations if isinstance(item, dict)]

    return []


def make_empty_task_report(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    source_root = as_text(report.get("source_root"))
    task_id = report_task_id(report, path)
    return {
        "task_id": task_id,
        "source_root": source_root,
        "report_path": str(path),
        "raw_issues": 0,
        "ignored_issues": 0,
        "kept_issues": 0,
        "status": "成功",
        "errors": [],
    }


def add_task_error(task_reports: dict[str, dict[str, Any]], task_id: str, location: dict[str, Any], error: dict[str, Any]) -> None:
    if task_id not in task_reports:
        task_reports[task_id] = {
            "task_id": task_id,
            "source_root": "",
            "report_path": "",
            "raw_issues": 0,
            "ignored_issues": 0,
            "kept_issues": 0,
            "status": "异常",
            "errors": [],
        }
    task_report = task_reports[task_id]
    task_report["status"] = "异常"
    task_report["errors"].append(error)


def analyze_report(path: Path) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8-sig") as fh:
        report = json.load(fh)

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    counters = defaultdict(int)
    task_reports: dict[str, dict[str, Any]] = {}
    default_task_report = make_empty_task_report(path, report)
    if default_task_report["task_id"]:
        task_reports[default_task_report["task_id"]] = default_task_report
    results = report.get("results", [])
    if not isinstance(results, list):
        return [], {"reports": 1, "raw_issues": 0, "ignored_issues": 0, "kept_issues": 0}, list(task_reports.values())

    for result in results:
        if not isinstance(result, dict):
            continue
        check_name = as_text(result.get("name"))
        check_title = as_text(result.get("title"))
        for item in iter_issue_items(result):
            reason = as_text(item.get("原因") or item.get("reason"))
            if not reason:
                continue

            counters["raw_issues"] += 1
            task_id = get_task_id(item, report)
            if task_id not in task_reports:
                source_root = as_text(report.get("source_root"))
                task_reports[task_id] = {
                    "task_id": task_id,
                    "source_root": source_root,
                    "report_path": str(path),
                    "raw_issues": 0,
                    "ignored_issues": 0,
                    "kept_issues": 0,
                    "status": "成功",
                    "errors": [],
                }
            task_reports[task_id]["raw_issues"] += 1
            if should_ignore(check_name, reason, check_title):
                counters["ignored_issues"] += 1
                task_reports[task_id]["ignored_issues"] += 1
                continue

            counters["kept_issues"] += 1
            task_reports[task_id]["kept_issues"] += 1
            grouped_reason = group_reason(check_name, reason)
            session_id = as_text(item.get("文件id") or item.get("session"))
            trajectory_path = get_session_path(item, report)
            line = as_text(item.get("行号") or item.get("line"))
            key = (check_name, check_title, grouped_reason)
            if key not in grouped:
                grouped[key] = {
                    "check_name": check_name,
                    "check_title": check_title,
                    "reason": grouped_reason,
                    "count": 0,
                    "task_ids": [],
                    "locations": [],
                }
            entry = grouped[key]
            entry["count"] += 1
            if task_id and task_id not in entry["task_ids"]:
                entry["task_ids"].append(task_id)
            entry["locations"].append(
                {
                    "task_id": task_id,
                    "session": session_id,
                    "trajectory_path": trajectory_path,
                    "line": line,
                }
            )
            add_task_error(
                task_reports,
                task_id,
                entry["locations"][-1],
                {
                    "check_name": check_name,
                    "check_title": check_title,
                    "reason": reason,
                    "session": session_id,
                    "trajectory_path": trajectory_path,
                    "line": line,
                },
            )

    rows = sorted(
        grouped.values(),
        key=lambda item: (-int(item["count"]), item["check_name"], item["reason"]),
    )
    counters["reports"] += 1
    return rows, dict(counters), sorted(task_reports.values(), key=lambda item: item["task_id"])


def merge_rows(all_rows: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    index = {(row["check_name"], row["check_title"], row["reason"]): row for row in all_rows}
    for row in rows:
        key = (row["check_name"], row["check_title"], row["reason"])
        if key not in index:
            copied = dict(row)
            copied["task_ids"] = list(row["task_ids"])
            copied["locations"] = list(row["locations"])
            all_rows.append(copied)
            index[key] = copied
            continue
        target = index[key]
        target["count"] += row["count"]
        for task_id in row["task_ids"]:
            if task_id not in target["task_ids"]:
                target["task_ids"].append(task_id)
        target["locations"].extend(row["locations"])


def merge_task_reports(all_task_reports: dict[str, dict[str, Any]], task_reports: list[dict[str, Any]]) -> None:
    for task_report in task_reports:
        task_id = task_report["task_id"]
        if task_id not in all_task_reports:
            copied = dict(task_report)
            copied["errors"] = list(task_report.get("errors", []))
            all_task_reports[task_id] = copied
            continue
        target = all_task_reports[task_id]
        target["raw_issues"] += task_report.get("raw_issues", 0)
        target["ignored_issues"] += task_report.get("ignored_issues", 0)
        target["kept_issues"] += task_report.get("kept_issues", 0)
        target["errors"].extend(task_report.get("errors", []))
        if task_report.get("source_root"):
            target["source_root"] = task_report["source_root"]
        if task_report.get("report_path"):
            target["report_path"] = task_report["report_path"]
        target["status"] = "异常" if target["kept_issues"] else "成功"


def write_json(output_path: Path, payload: dict[str, Any]) -> None:
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def write_csv(output_path: Path, rows: list[dict[str, Any]]) -> None:
    with output_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["check_name", "check_title", "reason", "count", "task_ids", "locations"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "check_name": row["check_name"],
                    "check_title": row["check_title"],
                    "reason": row["reason"],
                    "count": row["count"],
                    "task_ids": ",".join(row["task_ids"]),
                    "locations": json.dumps(row["locations"], ensure_ascii=False),
                }
            )


def format_location(location: dict[str, Any]) -> str:
    parts = []
    if location.get("task_id"):
        parts.append(as_text(location["task_id"]))
    if location.get("trajectory_path"):
        parts.append(f"轨迹={location['trajectory_path']}")
    elif location.get("session"):
        parts.append(f"轨迹={location['session']}")
    if location.get("line"):
        parts.append(f"行号={location['line']}")
    return "; ".join(parts)


def markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        escaped = [as_text(value).replace("|", "\\|").replace("\n", "<br>") for value in row]
        lines.append("| " + " | ".join(escaped) + " |")
    return lines


def write_markdown(output_path: Path, payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    rows = payload["errors"]
    task_reports = payload.get("tasks", [])
    generated_at = as_text(payload.get("generated_at"))
    success_tasks = [task for task in task_reports if task.get("status") == "成功"]
    failed_tasks = [task for task in task_reports if task.get("status") != "成功"]
    lines = [
        "# Pipeline 检查汇总报告",
        "",
        "## 汇总",
        "",
        f"- 报告数量: {summary.get('reports', 0)}",
        f"- 原始问题数: {summary.get('raw_issues', 0)}",
        f"- 已忽略问题数: {summary.get('ignored_issues', 0)}",
        f"- 保留问题数: {summary.get('kept_issues', 0)}",
        f"- 成功 Task 数: {len(success_tasks)}",
        f"- 异常 Task 数: {len(failed_tasks)}",
    ]
    if generated_at:
        lines.append(f"- 生成时间: {generated_at}")
    lines.extend(["", "## 异常 Task", ""])

    if failed_tasks:
        lines.extend(markdown_table(["Task ID", "保留问题", "报告路径"], [[task["task_id"], task["kept_issues"], task["report_path"]] for task in failed_tasks]))
    else:
        lines.append("无异常 Task。")

    lines.extend(["", "## 异常 Task 详情", ""])

    if not failed_tasks:
        lines.append("无异常详情。")
    for task in failed_tasks:
        lines.extend([f"### {task['task_id']}", ""])
        lines.extend(markdown_table(["检查项", "原因", "轨迹文件", "行号"], [[error["check_title"] or error["check_name"], error["reason"], error.get("trajectory_path") or error["session"], error["line"]] for error in task.get("errors", [])]))
        lines.append("")

    lines.extend(["", "## 成功 Task", ""])

    if success_tasks:
        lines.extend(markdown_table(["Task ID", "原始问题", "已忽略", "保留问题", "报告路径"], [[task["task_id"], task["raw_issues"], task["ignored_issues"], task["kept_issues"], task["report_path"]] for task in success_tasks]))
    else:
        lines.append("无成功 Task。")

    lines.extend(["", "## 问题类型汇总", ""])

    if not rows:
        lines.append("没有剩余问题。")
    for index, row in enumerate(rows, start=1):
        lines.extend(
            [
                f"### {index}. {row['check_name']}（{row['count']}）",
                "",
                f"- 检查项: {row['check_title']}",
                f"- 原因: {row['reason']}",
                f"- 任务 ID: {', '.join(row['task_ids'])}",
                "- 位置:",
            ]
        )
        for location in row["locations"]:
            lines.append(f"  - {format_location(location)}")
        lines.append("")

    with output_path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")


def print_summary(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def infer_output_format(output_path: Path, explicit_format: str | None) -> str:
    if explicit_format:
        return explicit_format
    suffix = output_path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        return "json"
    return "md"


def prepare_output_path(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        if output_path.is_dir():
            raise IsADirectoryError(output_path)
        output_path.unlink()


def default_output_path(args: argparse.Namespace) -> Path:
    if args.parent_dir:
        return Path(args.parent_dir) / DEFAULT_MARKDOWN_NAME
    if args.path:
        input_path = Path(args.path)
        return input_path.parent / DEFAULT_MARKDOWN_NAME if input_path.is_file() else input_path / DEFAULT_MARKDOWN_NAME
    return Path(DEFAULT_MARKDOWN_NAME)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze check_pipeline.json errors by task id.")
    parser.add_argument("path", nargs="?", help="check_pipeline.json file, or a directory containing reports")
    parser.add_argument("-d", "--parent-dir", help="父目录，通常是包含多个 TASK 子目录的目录")
    parser.add_argument(
        "-r",
        "--report-subdir",
        default="report",
        help="报告子目录或相对报告文件路径；配合 -d 使用，默认: report",
    )
    parser.add_argument("-o", "--output", help=f"write summary to this path; default: {DEFAULT_MARKDOWN_NAME}")
    parser.add_argument(
        "--format",
        choices=("md", "json", "csv"),
        default=None,
        help="output format; inferred from --output suffix when omitted, default: md",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.parent_dir:
            report_paths = find_report_paths_from_parent(Path(args.parent_dir), args.report_subdir)
        elif args.path:
            report_paths = find_report_paths(Path(args.path))
        else:
            print("请传入 path，或使用 -d 指定父目录", file=sys.stderr)
            return 2
    except FileNotFoundError as exc:
        print(f"路径不存在: {exc}", file=sys.stderr)
        return 2
    except NotADirectoryError as exc:
        print(f"不是目录: {exc}", file=sys.stderr)
        return 2

    if not report_paths:
        print("未找到 check_pipeline.json", file=sys.stderr)
        return 1

    all_rows: list[dict[str, Any]] = []
    all_task_reports: dict[str, dict[str, Any]] = {}
    totals = defaultdict(int)
    for report_path in report_paths:
        rows, counters, task_reports = analyze_report(report_path)
        merge_rows(all_rows, rows)
        merge_task_reports(all_task_reports, task_reports)
        for key, value in counters.items():
            totals[key] += value

    all_rows.sort(key=lambda item: (-int(item["count"]), item["check_name"], item["reason"]))
    tasks = sorted(all_task_reports.values(), key=lambda item: (item.get("status") == "成功", item.get("task_id", "")))
    payload = {
        "summary": dict(totals),
        "tasks": tasks,
        "errors": all_rows,
    }

    output_path = Path(args.output) if args.output else default_output_path(args)
    output_format = infer_output_format(output_path, args.format)
    try:
        prepare_output_path(output_path)
    except IsADirectoryError as exc:
        print(f"输出路径是目录，不能覆盖: {exc}", file=sys.stderr)
        return 2

    if output_format == "csv":
        write_csv(output_path, all_rows)
    elif output_format == "json":
        write_json(output_path, payload)
    else:
        write_markdown(output_path, payload)

    print(f"已生成汇总文档: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

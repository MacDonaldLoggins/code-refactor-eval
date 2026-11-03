#!/usr/bin/env python3
r"""Run check_pipeline.py and then generate a Markdown summary report.

Usage:
  python run_pipeline_with_summary.py -d D:\path\to\tasks
  python run_pipeline_with_summary.py -d D:\path\to\tasks --workers 4
  python run_pipeline_with_summary.py -d D:\path\to\tasks --summary-output D:\path\to\summary.md
  python C:\Users\21402\Desktop\code\claude_code_session\批量清洗检查-脚本\run_pipeline_with_summary.py 
  "C:\Users\21402\Desktop\code\test-tasks" -path sessions -o "C:\Users\21402\Desktop\code\test-tasks"
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType


SCRIPT_DIR = Path(__file__).resolve().parent


def resolve_repo_root(script_dir: Path) -> Path:
    for base_dir in (script_dir, *script_dir.parents):
        if (base_dir / "check_pipeline.py").is_file():
            return base_dir
        if (base_dir / "check" / "check_pipeline.py").is_file():
            return base_dir
    return script_dir


ROOT_DIR = resolve_repo_root(SCRIPT_DIR)
CHECK_DIR = ROOT_DIR if (ROOT_DIR / "check_pipeline.py").is_file() else ROOT_DIR / "check"
CHECK_PIPELINE_PATH = CHECK_DIR / "check_pipeline.py"
for module_dir in (ROOT_DIR, CHECK_DIR, SCRIPT_DIR):
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))


def load_module_from_path(module_name: str, module_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

from analyze_pipeline_report import (  # noqa: E402
    DEFAULT_MARKDOWN_NAME,
    analyze_report,
    infer_output_format,
    merge_rows,
    merge_task_reports,
    write_csv,
    write_json,
    write_markdown,
)

check_pipeline_module = load_module_from_path("check_pipeline_impl", CHECK_PIPELINE_PATH)
check_pipeline = check_pipeline_module.check_pipeline
parse_name_set = check_pipeline_module.parse_name_set


def build_summary(find_path: Path, report_subdir: str, output_path: Path, output_format: str | None) -> None:
    report_name = "check_pipeline.json"
    report_paths = sorted(
        path / report_subdir / report_name
        for path in find_path.iterdir()
        if path.is_dir() and path.name.startswith("TASK-") and (path / report_subdir / report_name).is_file()
    )
    for candidate in (
        find_path / report_name,
        find_path / report_subdir / report_name,
    ):
        if candidate.is_file():
            report_paths.insert(0, candidate)
    report_paths = sorted(dict.fromkeys(report_paths))
    if not report_paths:
        raise FileNotFoundError(f"未找到 {report_name}: {find_path}")

    all_rows: list[dict] = []
    all_task_reports: dict[str, dict] = {}
    totals: dict[str, int] = {}
    for report_path in report_paths:
        rows, counters, task_reports = analyze_report(report_path)
        merge_rows(all_rows, rows)
        merge_task_reports(all_task_reports, task_reports)
        for key, value in counters.items():
            totals[key] = totals.get(key, 0) + value

    all_rows.sort(key=lambda item: (-int(item["count"]), item["check_name"], item["reason"]))
    tasks = sorted(all_task_reports.values(), key=lambda item: (item.get("status") == "成功", item.get("task_id", "")))
    payload = {
        "summary": totals,
        "tasks": tasks,
        "errors": all_rows,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    output_format = infer_output_format(output_path, output_format)
    if output_format == "csv":
        write_csv(output_path, all_rows)
    elif output_format == "json":
        write_json(output_path, payload)
    else:
        write_markdown(output_path, payload)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 pipeline 检查并生成汇总分析报告。")
    parser.add_argument("-r", "-d", "--parent-dir", dest="find_path", type=Path, required=True, help="父目录，通常是包含多个 TASK 子目录的目录。")
    parser.add_argument("-path", dest="session_json_path", type=Path, default=Path("sessions"), help="session 文件或目录相对路径，默认 sessions。")
    parser.add_argument("-o", dest="output_dir", type=Path, required=False, help="pipeline 报告输出目录。省略时默认使用 -r/-d。")
    parser.add_argument("--only", default="", help="只运行指定检查脚本，多个名称用英文逗号分隔。")
    parser.add_argument("--skip", default="", help="跳过指定检查脚本，多个名称用英文逗号分隔。")
    parser.add_argument("--workers", type=int, default=5, help="并行处理项目数量，默认 1。")
    parser.add_argument("--summary-output", type=Path, help=f"汇总分析报告输出路径，默认写入 -r/-d 下的 {DEFAULT_MARKDOWN_NAME}。")
    parser.add_argument("--summary-format", choices=("md", "json", "csv"), default=None, help="汇总分析报告格式，默认根据输出后缀推断。")
    parser.add_argument("--report-subdir", default="report", help="每个 TASK 下的 pipeline 报告目录，默认 report。")
    parser.add_argument("--no-price-error-move", action="store_true", help="只记录 calculate_price 异常，不移动 TASK。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1:
        print("--workers 必须大于等于 1。", file=sys.stderr)
        return 2

    find_path = args.find_path.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else None
    summary_output = (args.summary_output.expanduser().resolve() if args.summary_output else find_path / DEFAULT_MARKDOWN_NAME)

    start_time = time.perf_counter()
    check_pipeline(
        find_path,
        args.session_json_path,
        output_dir,
        only=parse_name_set(args.only),
        skip=parse_name_set(args.skip),
        workers=args.workers,
        move_price_errors=not args.no_price_error_move,
    )
    build_summary(output_dir or find_path, args.report_subdir, summary_output, args.summary_format)
    elapsed = time.perf_counter() - start_time
    print(f"已生成汇总分析报告: {summary_output}")
    print(f"[summary] 全部完成，总耗时 {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

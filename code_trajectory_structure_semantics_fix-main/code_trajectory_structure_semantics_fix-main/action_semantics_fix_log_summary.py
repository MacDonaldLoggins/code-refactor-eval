#!/usr/bin/env python3
"""Summarize failures from action_semantics_fix task logs."""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_ROOT = ROOT_DIR / "log"
DEFAULT_SUMMARY_PATH = DEFAULT_LOG_ROOT / "action_semantics_fix_log_summary.md"
LOG_FILENAME = "action_semantics_fix.log"
TIMESTAMP_DIR_RE = re.compile(r"^\d{8}_\d{6}$")
SUCCESS_STATUSES = {"true", "resumed", "completed", "ok", "success"}


@dataclass(frozen=True)
class LogEntry:
    line_no: int
    script: str
    status: str
    description: str


@dataclass(frozen=True)
class FailedRun:
    task: str
    run_id: str
    log_path: str
    failures: list[LogEntry]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize failed action_semantics_fix entries from log/."
    )
    parser.add_argument(
        "--log-root",
        type=Path,
        default=DEFAULT_LOG_ROOT,
        help=f"Log root directory. Default: {DEFAULT_LOG_ROOT}",
    )
    parser.add_argument(
        "--all-runs",
        action="store_true",
        help="Inspect every timestamped log instead of only the latest run per task.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of text.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_SUMMARY_PATH,
        help=f"Write a markdown summary to this path. Default: {DEFAULT_SUMMARY_PATH}",
    )
    return parser.parse_args(argv)


def discover_log_files(log_root: Path, all_runs: bool) -> list[Path]:
    if not log_root.is_dir():
        raise FileNotFoundError(f"log root not found: {log_root}")

    if all_runs:
        return sorted(
            path
            for path in log_root.rglob(LOG_FILENAME)
            if path.is_file()
        )

    latest_per_task: list[Path] = []
    for task_dir in sorted(path for path in log_root.iterdir() if path.is_dir()):
        runs = [
            path
            for path in task_dir.glob(f"*/{LOG_FILENAME}")
            if path.is_file() and TIMESTAMP_DIR_RE.match(path.parent.name)
        ]
        if not runs:
            continue
        latest = max(runs, key=lambda path: (path.parent.name, path.stat().st_mtime_ns))
        latest_per_task.append(latest)
    return latest_per_task


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def parse_log_file(path: Path) -> list[LogEntry]:
    entries: list[LogEntry] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 2:
            continue
        script = parts[0]
        status = parts[1].lower()
        description = parts[2] if len(parts) > 2 else ""
        if status not in SUCCESS_STATUSES:
            entries.append(
                LogEntry(
                    line_no=line_no,
                    script=script,
                    status=status,
                    description=description,
                )
            )
    return entries


def build_failures(log_files: list[Path]) -> list[FailedRun]:
    failures: list[FailedRun] = []
    for log_path in log_files:
        entries = parse_log_file(log_path)
        if not entries:
            continue
        failures.append(
            FailedRun(
                task=log_path.parent.parent.name,
                run_id=log_path.parent.name,
                log_path=str(log_path),
                failures=entries,
            )
        )
    return failures


def to_json_payload(log_root: Path, all_runs: bool, failures: list[FailedRun], scanned: int) -> dict[str, Any]:
    return {
        "log_root": str(log_root),
        "all_runs": all_runs,
        "scanned_log_files": scanned,
        "tasks_with_failures": len(failures),
        "failed_entries": sum(len(item.failures) for item in failures),
        "runs": [
            {
                "task": item.task,
                "run_id": item.run_id,
                "log_path": item.log_path,
                "failures": [asdict(entry) for entry in item.failures],
            }
            for item in failures
        ],
    }


def render_markdown_summary(
    log_root: Path,
    all_runs: bool,
    failures: list[FailedRun],
    scanned: int,
) -> str:
    failed_entries = sum(len(item.failures) for item in failures)
    lines = [
        "# action_semantics_fix log summary",
        "",
        f"- log root: `{log_root}`",
        f"- mode: `{'all-runs' if all_runs else 'latest-per-task'}`",
        f"- scanned log files: {scanned}",
        f"- task/run blocks with failures: {len(failures)}",
        f"- failed lines: {failed_entries}",
        "",
    ]
    if not failures:
        lines.append("No failures found.")
        lines.append("")
        return "\n".join(lines)

    for item in failures:
        lines.append(f"## {item.task} [{item.run_id}]")
        lines.append(f"- log: `{display_path(Path(item.log_path))}`")
        for entry in item.failures:
            detail = f"`{entry.script}` `{entry.status}`"
            if entry.description:
                detail += f" {entry.description}"
            detail += f" (line {entry.line_no})"
            lines.append(f"- {detail}")
        lines.append("")
    return "\n".join(lines)


def write_summary_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def print_text_summary(log_root: Path, all_runs: bool, failures: list[FailedRun], scanned: int) -> None:
    failed_entries = sum(len(item.failures) for item in failures)
    print(
        f"scanned {scanned} log file(s); "
        f"{len(failures)} task/run block(s) with failures; "
        f"{failed_entries} failed line(s)."
    )
    if not failures:
        print("no failures found.")
        return

    print()
    for item in failures:
        header = f"{item.task} [{item.run_id}]"
        if all_runs:
            header += " (all-runs)"
        print(header)
        print(f"  {display_path(Path(item.log_path))}")
        for entry in item.failures:
            detail = f"{entry.script} {entry.status}"
            if entry.description:
                detail += f" {entry.description}"
            print(f"  - {detail}")
        print()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_root = args.log_root.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    log_files = discover_log_files(log_root, args.all_runs)
    failures = build_failures(log_files)
    markdown = render_markdown_summary(log_root, args.all_runs, failures, len(log_files))
    write_summary_file(output_path, markdown)

    if args.json:
        payload = to_json_payload(log_root, args.all_runs, failures, len(log_files))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text_summary(log_root, args.all_runs, failures, len(log_files))
    print(f"summary written: {display_path(output_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

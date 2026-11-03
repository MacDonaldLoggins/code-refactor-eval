#!/usr/bin/env python3
"""Batch pipeline for Claude Code session format cleanup, repair, and CJK scan.

Flow:
  1. Clear old reports and restore original_sessions from fix_original_sessions when present.
  2. Check TASK/original_sessions JSON/JSONL format.
  3. Move format-failed TASK directories into format_failed_tasks.
  4. Repair only TASKs inside format_failed_tasks.
  5. Recheck format_failed_tasks, move fixed TASKs back, keep unresolved TASKs there.
  6. Run CJK scan for all format-passing TASKs and move CJK-hit TASKs into cjk_tasks.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "repair_scripts" else SCRIPT_DIR
for import_path in (SCRIPT_DIR, ROOT_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from check_session_format import check_cli_target
from clear_session_format import clear_task, finalize_repaired_task, restore_original_sessions
from cjk_session_format import check_cjk_task
from repair_session_format import repair_cli_target, write_no_repair_case_report


TASK_SOURCE_DIRS = {"original_sessions", "fix_original_sessions"}
SKIP_DIR_NAMES = {
    "original_sessions",
    "fix_original_sessions",
    "sessions",
    "report",
    ".tmp",
    ".backup",
    ".repair_backup",
}
FORMAT_FAILED_DIR_NAME = "format_failed_tasks"
CJK_TASKS_DIR_NAME = "cjk_tasks"
MANAGED_DIR_NAMES = {FORMAT_FAILED_DIR_NAME, CJK_TASKS_DIR_NAME}
FORMAT_SUCCESS_STATUSES = {"passed", "skipped"}
REPORT_PATH_KEYS = ("check_report", "repair_report", "case_report", "cjk_report")
DEFAULT_PIPELINE_REPORT_NAME = "session_format_check_repair_pipeline_report.md"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch clean, check, repair, recheck, and CJK-scan Claude Code TASKs")
    parser.add_argument("-r", "--root", action="append", required=True, type=Path, help="TASK root or a single TASK directory; repeatable")
    parser.add_argument("--check-only", action="store_true", help="Only clear, check, and move format failures; skip repair and CJK scan")
    parser.add_argument("--all-tasks", action="store_true", help="Recursively scan nested TASK directories when no direct TASK children are enough")
    parser.add_argument("--format-failure-dir", type=Path, help=f"Format failure directory. Relative paths resolve under each batch root. Default: {FORMAT_FAILED_DIR_NAME}")
    parser.add_argument("--cjk-dir", type=Path, help=f"CJK-hit directory. Relative paths resolve under each batch root. Default: {CJK_TASKS_DIR_NAME}")
    parser.add_argument("--report", default=DEFAULT_PIPELINE_REPORT_NAME, help="Summary report name written directly under ROOT")
    args = parser.parse_args(argv)
    args.roots = list(args.root or [])
    return args


def _has_session_source(path: Path) -> bool:
    return any((path / name).is_dir() for name in TASK_SOURCE_DIRS)


def _is_task_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("TASK-") and _has_session_source(path)


def _is_under_managed_dir(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part in MANAGED_DIR_NAMES for part in relative.parts[:-1])


def discover_session_tasks(roots: list[Path], all_tasks: bool = False) -> list[Path]:
    tasks: dict[str, Path] = {}
    for raw_root in roots:
        root = raw_root.expanduser().resolve()
        if not root.is_dir():
            continue
        if _is_task_dir(root):
            tasks[str(root)] = root
            continue

        direct = [path for path in sorted(root.iterdir()) if _is_task_dir(path)]
        for task in direct:
            tasks[str(task)] = task

        if all_tasks or not direct:
            for path in root.rglob("TASK-*"):
                if not _is_task_dir(path):
                    continue
                if _is_under_managed_dir(path, root):
                    continue
                if any(part in SKIP_DIR_NAMES for part in path.relative_to(root).parts[:-1]):
                    continue
                tasks[str(path)] = path
    return sorted(tasks.values(), key=lambda item: str(item))


def _batch_root_for_input(root: Path) -> Path:
    root = root.expanduser().resolve()
    return root.parent if _is_task_dir(root) else root


def _resolve_managed_dir(batch_root: Path, configured: Path | None, default_name: str) -> Path:
    if configured is None:
        return batch_root / default_name
    path = configured.expanduser()
    if not path.is_absolute():
        path = batch_root / path
    return path.resolve()


def _unique_task_target(directory: Path, task_name: str) -> Path:
    target = directory / task_name
    if not target.exists():
        return target
    index = 1
    while True:
        candidate = directory / f"{task_name}-{index}"
        if not candidate.exists():
            return candidate
        index += 1


def _move_task_dir(task_dir: Path, destination_dir: Path) -> Path:
    task_path = task_dir.resolve()
    destination = destination_dir.resolve()
    if task_path.parent == destination:
        return task_path
    try:
        destination.relative_to(task_path)
    except ValueError:
        pass
    else:
        raise ValueError(f"destination cannot be inside TASK directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    target = _unique_task_target(destination, task_path.name)
    return Path(shutil.move(str(task_path), str(target))).resolve()


def _rewrite_path_after_move(path_text: str, old_task: Path, new_task: Path) -> str:
    if not path_text:
        return ""
    path = Path(path_text)
    try:
        relative = path.relative_to(old_task)
    except ValueError:
        return path_text
    return str(new_task / relative)


def _update_task_path(row: dict[str, Any], new_task: Path) -> None:
    old_task = Path(row["task"])
    for key in REPORT_PATH_KEYS:
        row[key] = _rewrite_path_after_move(str(row.get(key) or ""), old_task, new_task)
    row["task"] = str(new_task)


def _new_row(task: Path, batch_root: Path) -> dict[str, Any]:
    started = time.time()
    return {
        "root": str(batch_root),
        "original_task": str(task),
        "original_parent": str(task.parent),
        "task": str(task),
        "name": task.name,
        "status": "discovered",
        "before_status": None,
        "before_checked": 0,
        "after_status": None,
        "after_checked": 0,
        "clear_status": None,
        "clear_removed_report": "",
        "clear_removed_original_sessions": "",
        "clear_restored_original_sessions": "",
        "repair_status": None,
        "repaired_files": 0,
        "repairable_lines": 0,
        "finalize_status": None,
        "finalize_archived_fix_original_sessions": "",
        "restore_status": None,
        "restore_removed_original_sessions": "",
        "restore_restored_original_sessions": "",
        "check_report": "",
        "repair_report": "",
        "case_report": "",
        "cjk_hits": 0,
        "cjk_manual_review": 0,
        "cjk_report": "",
        "moved_to_format_failed": "",
        "moved_back_from_format_failed": "",
        "moved_to_cjk": "",
        "elapsed_seconds": 0.0,
        "error": "",
        "_started": started,
    }


def _finish_row(row: dict[str, Any]) -> None:
    row["elapsed_seconds"] = round(time.time() - float(row.get("_started") or time.time()), 2)


def _check_task(row: dict[str, Any], phase: str) -> dict[str, Any]:
    task = Path(row["task"])
    result = check_cli_target(task, task)
    if phase == "before":
        row["before_status"] = result["status"]
        row["before_checked"] = result["checked"]
    else:
        row["after_status"] = result["status"]
        row["after_checked"] = result["checked"]
    if result["report"]:
        row["check_report"] = result["report"]
    return result


def _format_succeeded(row: dict[str, Any]) -> bool:
    if row.get("after_status") in FORMAT_SUCCESS_STATUSES:
        return True
    return not row.get("moved_to_format_failed") and row.get("before_status") in FORMAT_SUCCESS_STATUSES


def _clear_and_initial_check(rows: list[dict[str, Any]], format_failure_dirs: dict[str, Path]) -> None:
    for index, row in enumerate(rows, 1):
        task = Path(row["task"])
        print(f"[clear/check {index}/{len(rows)}] {task.name}")
        try:
            clear = clear_task(task, require_fixed=False)
            row.update(
                {
                    "clear_status": clear["status"],
                    "clear_removed_report": clear["removed_report"],
                    "clear_removed_original_sessions": clear["removed_original_sessions"],
                    "clear_restored_original_sessions": clear["restored_original_sessions"],
                }
            )
            before = _check_task(row, "before")
            if before["status"] == "failed":
                moved = _move_task_dir(task, format_failure_dirs[row["root"]])
                row["moved_to_format_failed"] = str(moved)
                row["status"] = "format_failed"
                _update_task_path(row, moved)
            else:
                row["status"] = "format_passed"
                if before["status"] == "passed":
                    row["case_report"] = str(write_no_repair_case_report(task, int(before["checked"]), before["status"]))
        except Exception as exc:
            row["status"] = "error"
            row["error"] = str(exc)
        _finish_row(row)
        print(f"  -> {row['status']} before={row['before_status']} moved={row['moved_to_format_failed']}")


def _include_existing_format_failed_tasks(rows: list[dict[str, Any]], batch_roots: list[Path], format_failure_dirs: dict[str, Path]) -> None:
    known = {str(Path(row["task"]).resolve()) for row in rows}
    for batch_root in batch_roots:
        failure_dir = format_failure_dirs[str(batch_root)]
        if not failure_dir.is_dir():
            continue
        for task in sorted(path for path in failure_dir.iterdir() if _is_task_dir(path)):
            key = str(task.resolve())
            if key in known:
                continue
            row = _new_row(task.resolve(), batch_root)
            row["status"] = "format_failed"
            row["moved_to_format_failed"] = str(task.resolve())
            row["original_parent"] = str(batch_root)
            rows.append(row)
            known.add(key)


def _restore_failed_repair(row: dict[str, Any]) -> None:
    restored = restore_original_sessions(row["task"])
    row["restore_status"] = restored["status"]
    row["restore_removed_original_sessions"] = restored["removed_original_sessions"]
    row["restore_restored_original_sessions"] = restored["restored_original_sessions"]


def _repair_and_recheck(rows: list[dict[str, Any]]) -> None:
    failed_rows = [row for row in rows if row["status"] == "format_failed"]
    for index, row in enumerate(failed_rows, 1):
        task = Path(row["task"])
        print(f"[repair/recheck {index}/{len(failed_rows)}] {task.name}")
        try:
            repair = repair_cli_target(task, task)
            row.update(
                {
                    "repair_status": repair["status"],
                    "repaired_files": repair["repaired_files"],
                    "repairable_lines": repair["repairable_lines"],
                    "repair_report": repair["report"],
                    "case_report": repair["case_report"],
                }
            )
            after = _check_task(row, "after")
            if after["status"] in FORMAT_SUCCESS_STATUSES:
                finalized = finalize_repaired_task(task)
                row["finalize_status"] = finalized["status"]
                row["finalize_archived_fix_original_sessions"] = finalized["archived_fix_original_sessions"]
                moved_back = _move_task_dir(task, Path(row["original_parent"]))
                row["moved_back_from_format_failed"] = str(moved_back)
                row["status"] = "repaired" if row["repairable_lines"] else "format_passed"
                _update_task_path(row, moved_back)
            else:
                _restore_failed_repair(row)
                row["status"] = "unresolved"
        except Exception as exc:
            try:
                _restore_failed_repair(row)
            except Exception as restore_exc:
                row["error"] = f"{exc}; restore failed: {restore_exc}"
            else:
                row["error"] = str(exc)
            row["status"] = "error"
        _finish_row(row)
        print(
            f"  -> {row['status']} repair={row['repair_status']} after={row['after_status']} "
            f"files={row['repaired_files']} lines={row['repairable_lines']}"
        )


def _run_cjk_scan(rows: list[dict[str, Any]], cjk_dirs: dict[str, Path]) -> None:
    candidates = [row for row in rows if _format_succeeded(row) and row["status"] != "error"]
    for index, row in enumerate(candidates, 1):
        task = Path(row["task"])
        print(f"[cjk {index}/{len(candidates)}] {task.name}")
        try:
            cjk = check_cjk_task(task, full_scan=True)
            row.update(
                {
                    "cjk_hits": cjk["hits"],
                    "cjk_manual_review": cjk["manual_review"],
                    "cjk_report": cjk["report"],
                }
            )
            if int(cjk["hits"]) > 0:
                moved = _move_task_dir(task, cjk_dirs[row["root"]])
                row["moved_to_cjk"] = str(moved)
                row["status"] = "cjk_review"
                _update_task_path(row, moved)
        except Exception as exc:
            row["status"] = "error"
            row["error"] = str(exc)
        _finish_row(row)
        print(f"  -> cjk_hits={row['cjk_hits']} moved={row['moved_to_cjk']}")


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _summary_json_path(root: Path, report_name: str) -> Path:
    path = Path(report_name)
    if path.suffix:
        return root / path.with_suffix(".json")
    return root / f"{report_name}.json"


def write_summary(root: Path, rows: list[dict[str, Any]], report_name: str, format_failure_dir: Path, cjk_dir: Path) -> tuple[Path, Path]:
    report_path = root / report_name
    json_path = _summary_json_path(root, report_name)

    public_rows = [_public_row(row) for row in rows]
    summary = {
        "total": len(rows),
        "format_passed_initial": sum(1 for row in rows if not row.get("moved_to_format_failed") and row.get("before_status") in FORMAT_SUCCESS_STATUSES),
        "format_failed_initial": sum(1 for row in rows if row.get("moved_to_format_failed")),
        "repaired": sum(1 for row in rows if row.get("moved_back_from_format_failed") and int(row.get("repairable_lines") or 0) > 0),
        "unresolved": sum(1 for row in rows if row["status"] == "unresolved"),
        "errors": sum(1 for row in rows if row["status"] == "error"),
        "cjk_review": sum(1 for row in rows if row["status"] == "cjk_review"),
        "repaired_files": sum(int(row.get("repaired_files") or 0) for row in rows),
        "repairable_lines": sum(int(row.get("repairable_lines") or 0) for row in rows),
        "cjk_hits": sum(int(row.get("cjk_hits") or 0) for row in rows),
        "cjk_manual_review": sum(int(row.get("cjk_manual_review") or 0) for row in rows),
        "format_failure_dir": str(format_failure_dir),
        "cjk_dir": str(cjk_dir),
    }

    lines = [
        "# Session Repair Pipeline Summary",
        "",
        "## Summary",
        "",
        f"- total: {summary['total']}",
        f"- format_passed_initial: {summary['format_passed_initial']}",
        f"- format_failed_initial: {summary['format_failed_initial']}",
        f"- repaired: {summary['repaired']}",
        f"- unresolved: {summary['unresolved']}",
        f"- errors: {summary['errors']}",
        f"- cjk_review: {summary['cjk_review']}",
        f"- repaired_files: {summary['repaired_files']}",
        f"- repairable_lines: {summary['repairable_lines']}",
        f"- cjk_hits: {summary['cjk_hits']}",
        f"- cjk_manual_review: {summary['cjk_manual_review']}",
        f"- format_failure_dir: `{summary['format_failure_dir']}`",
        f"- cjk_dir: `{summary['cjk_dir']}`",
        "",
        "## Task Status",
        "",
    ]
    for row in rows:
        lines.append(
            f"- {row['name']}: status={row['status']}, before={row['before_status']}, "
            f"after={row['after_status']}, repair={row['repair_status']}, "
            f"cjk_hits={row['cjk_hits']}, task=`{row['task']}`"
        )
        if row.get("error"):
            lines.append(f"  error: {row['error']}")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps({"summary": summary, "tasks": public_rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path, json_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    roots = [root.expanduser().resolve() for root in args.roots]

    batch_roots = sorted({_batch_root_for_input(root) for root in roots}, key=lambda item: str(item))
    format_failure_dirs = {
        str(root): _resolve_managed_dir(root, args.format_failure_dir, FORMAT_FAILED_DIR_NAME)
        for root in batch_roots
    }
    cjk_dirs = {
        str(root): _resolve_managed_dir(root, args.cjk_dir, CJK_TASKS_DIR_NAME)
        for root in batch_roots
    }

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        batch_root = _batch_root_for_input(root)
        for task in discover_session_tasks([root], all_tasks=args.all_tasks):
            key = str(task)
            if key in seen:
                continue
            seen.add(key)
            rows.append(_new_row(task, batch_root))

    print(f"Discovered {len(rows)} TASKs")
    _clear_and_initial_check(rows, format_failure_dirs)
    _include_existing_format_failed_tasks(rows, batch_roots, format_failure_dirs)

    if not args.check_only:
        _repair_and_recheck(rows)
        _run_cjk_scan(rows, cjk_dirs)

    for batch_root in batch_roots:
        root_rows = [row for row in rows if row["root"] == str(batch_root)]
        report_path, json_path = write_summary(
            batch_root,
            root_rows,
            args.report,
            format_failure_dirs[str(batch_root)],
            cjk_dirs[str(batch_root)],
        )
        print(f"REPORT {report_path}")
        print(f"SUMMARY {json_path}")

    return 1 if any(row["status"] in {"format_failed", "unresolved", "error"} for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())

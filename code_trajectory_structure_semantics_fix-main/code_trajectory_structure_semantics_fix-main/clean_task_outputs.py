#!/usr/bin/env python3
r"""Clean generated task outputs.

Deletes these paths under each TASK directory:
  .gitignore
  .tmp
  .backup
  convert-sessions
  clean_sessions
  original_sessions (backed up first)
  data_success
  data_unfixed
  clear
  report
  prompt.md
  align_response_report*.md
  session_ending_report*.md
  runtest_evidence_delete_report.json
  runtest_evidence_presweep_report.json
  runtest_summary.json

Usage:
  python clean_task_outputs.py -r D:\IdeaProject\Repo_Data_4\20260512\TASK-20260318-0DA1DD
  python clean_task_outputs.py -d D:\IdeaProject\Repo_Data_4\20260512
  python clean_task_outputs.py -d D:\IdeaProject\Repo_Data_4\20260512 --dry-run
  python clean_task_outputs.py -d D:\IdeaProject\Repo_Data_4\20260512 --workers 8
"""
from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


TARGET_NAMES = (
    ".gitignore",
    ".tmp",
    ".backup",
    "convert-sessions",
    "clean_sessions",
    "original_sessions",
    "data_success",
    "data_unfixed",
    "clear",
    "report",
    "prompt.md",
)
TARGET_FILE_PREFIXES = ("align_response_report", "session_ending_report")
TARGET_FILES = (
    "runtest_evidence_delete_report.json",
    "runtest_evidence_presweep_report.json",
    "runtest_summary.json",
)
BACKUP_ROOT = Path(__file__).resolve().parent / "原始轨迹备份"


def collect_task_dirs(single_dirs: list[Path], parent_dirs: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    task_dirs: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            print(f"[WARN] TASK 目录不存在，跳过: {resolved}")
            return
        if resolved in seen:
            return
        seen.add(resolved)
        task_dirs.append(resolved)

    for parent in parent_dirs:
        resolved_parent = parent.expanduser().resolve()
        if not resolved_parent.is_dir():
            print(f"[WARN] 父目录不存在，跳过: {resolved_parent}")
            continue
        for child in sorted(resolved_parent.iterdir()):
            if child.is_dir() and child.name.startswith("TASK-"):
                add(child)

    for single in single_dirs:
        add(single)

    return task_dirs


def backup_original_sessions(task_dir: Path, dry_run: bool) -> None:
    source = task_dir / "original_sessions"
    if not source.exists():
        return

    destination = BACKUP_ROOT / task_dir.name / "original_sessions"
    action = "DRY-RUN BACKUP" if dry_run else "BACKUP"
    print(f"[{action}] {source} -> {destination}")
    if dry_run:
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)


def remove_path(path: Path, dry_run: bool) -> bool:
    if not path.exists():
        return False

    action = "DRY-RUN" if dry_run else "DELETE"
    print(f"[{action}] {path}")
    if dry_run:
        return True

    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def clean_task_dir(task_dir: Path, dry_run: bool) -> int:
    deleted = 0
    for name in TARGET_NAMES:
        if name == "original_sessions":
            backup_original_sessions(task_dir, dry_run)
        if remove_path(task_dir / name, dry_run):
            deleted += 1
    for child in task_dir.iterdir():
        if child.is_file() and (
            child.name in TARGET_FILES
            or any(child.name.startswith(prefix) for prefix in TARGET_FILE_PREFIXES)
        ):
            if remove_path(child, dry_run):
                deleted += 1
    return deleted


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="删除 TASK 目录下生成的临时、转换和报告文件。")
    parser.add_argument("-r", "--root", dest="single_dirs", type=Path, action="append", default=[], help="单个 TASK 目录，可重复传入。")
    parser.add_argument("-d", "--dir", dest="parent_dirs", type=Path, action="append", default=[], help="父目录，批量扫描其下 TASK-* 子目录，可重复传入。")
    parser.add_argument("--dry-run", action="store_true", help="只打印将删除的路径，不实际删除。")
    parser.add_argument("--workers", type=int, default=1, help="并发清理 TASK 目录的线程数，默认 1。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.single_dirs and not args.parent_dirs:
        print("必须至少传入一个 -r 或 -d。", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("--workers 必须大于等于 1。", file=sys.stderr)
        return 2

    task_dirs = collect_task_dirs(args.single_dirs, args.parent_dirs)
    if not task_dirs:
        print("未找到可清理的 TASK 目录。")
        return 0

    total_deleted = 0
    if args.workers == 1:
        for task_dir in task_dirs:
            print(f"\n[TASK] {task_dir}")
            total_deleted += clean_task_dir(task_dir, args.dry_run)
    else:
        print(f"[INFO] 使用 {args.workers} 个线程并发清理 {len(task_dirs)} 个 TASK 目录。")
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(clean_task_dir, task_dir, args.dry_run): task_dir for task_dir in task_dirs}
            for future in as_completed(futures):
                task_dir = futures[future]
                try:
                    deleted = future.result()
                except Exception as exc:
                    print(f"[ERROR] 清理失败: {task_dir}: {exc}", file=sys.stderr)
                    continue
                total_deleted += deleted
                print(f"[TASK DONE] {task_dir}，处理 {deleted} 个路径。")

    verb = "将删除" if args.dry_run else "已删除"
    print(f"\n[DONE] 共处理 {len(task_dirs)} 个 TASK 目录，{verb} {total_deleted} 个路径。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""清理单个 TASK 的修复产物，并恢复原始 sessions。

执行逻辑：
  1. 删除 TASK/report。
  2. 删除 TASK/original_sessions。
  3. 将 TASK/fix_original_sessions 重命名为 TASK/original_sessions。

流水线调用时可允许没有 TASK/fix_original_sessions：这种情况下只删除
TASK/report，并保留现有 TASK/original_sessions 继续检查。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="清理 TASK 修复产物并恢复 original_sessions")
    parser.add_argument("-r", "--root", required=True, type=Path, help="要清理的 TASK 目录")
    parser.add_argument(
        "--finalize-repair",
        action="store_true",
        help="修复通过后归档 fix_original_sessions，不还原覆盖 original_sessions",
    )
    return parser.parse_args(argv)


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            return candidate
        index += 1


def clear_task(task_dir: str | Path, require_fixed: bool = True) -> dict[str, str]:
    task_path = Path(task_dir).expanduser().resolve()
    if not task_path.is_dir():
        raise NotADirectoryError(task_path)

    report_dir = task_path / "report"
    original_dir = task_path / "original_sessions"
    fixed_dir = task_path / "fix_original_sessions"

    removed_report = ""
    removed_original = ""
    restored_original = ""

    if report_dir.exists():
        if not report_dir.is_dir():
            raise NotADirectoryError(report_dir)
        shutil.rmtree(report_dir)
        removed_report = str(report_dir)

    if fixed_dir.exists() and not fixed_dir.is_dir():
        raise NotADirectoryError(fixed_dir)

    if not fixed_dir.is_dir():
        if require_fixed:
            raise FileNotFoundError(fixed_dir)
        if original_dir.exists() and not original_dir.is_dir():
            raise NotADirectoryError(original_dir)
        if not original_dir.is_dir():
            raise FileNotFoundError(original_dir)
        return {
            "task": str(task_path),
            "status": "report_removed",
            "removed_report": removed_report,
            "removed_original_sessions": "",
            "restored_original_sessions": "",
        }

    if original_dir.exists():
        if not original_dir.is_dir():
            raise NotADirectoryError(original_dir)
        shutil.rmtree(original_dir)
        removed_original = str(original_dir)
    fixed_dir.rename(original_dir)
    restored_original = str(original_dir)

    return {
        "task": str(task_path),
        "status": "restored_original_sessions",
        "removed_report": removed_report,
        "removed_original_sessions": removed_original,
        "restored_original_sessions": restored_original,
    }


def finalize_repaired_task(task_dir: str | Path) -> dict[str, str]:
    task_path = Path(task_dir).expanduser().resolve()
    if not task_path.is_dir():
        raise NotADirectoryError(task_path)

    original_dir = task_path / "original_sessions"
    fixed_dir = task_path / "fix_original_sessions"
    backup_root = task_path / ".repair_backup"

    if not original_dir.is_dir():
        raise FileNotFoundError(original_dir)
    if fixed_dir.exists() and not fixed_dir.is_dir():
        raise NotADirectoryError(fixed_dir)
    if not fixed_dir.is_dir():
        return {
            "task": str(task_path),
            "status": "no_fix_original_sessions",
            "archived_fix_original_sessions": "",
        }

    backup_root.mkdir(parents=True, exist_ok=True)
    target = _unique_path(backup_root / fixed_dir.name)
    shutil.move(str(fixed_dir), str(target))
    return {
        "task": str(task_path),
        "status": "archived_fix_original_sessions",
        "archived_fix_original_sessions": str(target),
    }


def restore_original_sessions(task_dir: str | Path) -> dict[str, str]:
    task_path = Path(task_dir).expanduser().resolve()
    if not task_path.is_dir():
        raise NotADirectoryError(task_path)

    original_dir = task_path / "original_sessions"
    fixed_dir = task_path / "fix_original_sessions"

    if fixed_dir.exists() and not fixed_dir.is_dir():
        raise NotADirectoryError(fixed_dir)
    if not fixed_dir.is_dir():
        return {
            "task": str(task_path),
            "status": "no_fix_original_sessions",
            "removed_original_sessions": "",
            "restored_original_sessions": "",
        }

    removed_original = ""
    if original_dir.exists():
        if not original_dir.is_dir():
            raise NotADirectoryError(original_dir)
        shutil.rmtree(original_dir)
        removed_original = str(original_dir)
    fixed_dir.rename(original_dir)
    return {
        "task": str(task_path),
        "status": "restored_original_sessions",
        "removed_original_sessions": removed_original,
        "restored_original_sessions": str(original_dir),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = finalize_repaired_task(args.root) if args.finalize_repair else clear_task(args.root)
    except OSError as exc:
        print(f"清理失败: {exc}", file=sys.stderr)
        return 1

    print(f"CLEARED: {result['task']}")
    if args.finalize_repair:
        print(f"ARCHIVED: {result['archived_fix_original_sessions']}")
    else:
        print(f"REMOVED: {result['removed_report']}")
        print(f"REMOVED: {result['removed_original_sessions']}")
        print(f"RESTORED: {result['restored_original_sessions']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

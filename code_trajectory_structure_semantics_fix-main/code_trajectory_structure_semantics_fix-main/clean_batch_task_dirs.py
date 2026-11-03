#!/usr/bin/env python3
r"""清空批次任务目录的内容（保留目录本身）。

默认删除当前仓库根目录（code_trajectory_structure_semantics_fix\）下
  batch_after_tasks/
  batch_before_tasks/
这两个文件夹中的**所有内容**（子目录和文件），但不删除文件夹本身。

Usage:
  python clean_batch_task_dirs.py                 # 清空默认两个目录的内容
  python clean_batch_task_dirs.py --dry-run       # 只打印将删除的内容，不实际删除
  python clean_batch_task_dirs.py --dirs batch_after_tasks success_task
  python clean_batch_task_dirs.py --root D:\other\repo
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DIRS = ("batch_after_tasks", "batch_before_tasks")
RETRY_DELAYS = (0.5, 1.0, 2.0)


def extended_path(path: Path) -> str:
    """Return the ``\\\\?\\`` extended-length form of an absolute Windows path.

    Bypasses the Win32 MAX_PATH (260 chars) limit without depending on the
    LongPathsEnabled registry setting.  On non-Windows hosts this is a no-op.
    """
    text = str(path)
    if sys.platform == "win32" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def _remove_readonly_onexc(func, path, _exc) -> None:
    """rmtree onexc callback: clear the read-only bit (e.g. .git objects) and retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def robust_rmtree(path: Path) -> None:
    shutil.rmtree(extended_path(path), onexc=_remove_readonly_onexc)


def robust_unlink(path: Path) -> None:
    os.chmod(extended_path(path), stat.S_IWRITE)
    os.unlink(extended_path(path))


def delete_with_retries(path: Path) -> None:
    """Delete one file/dir, retrying transient failures (locked files etc.)."""
    last_error: Exception | None = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            if path.is_symlink() or path.is_file():
                robust_unlink(path)
            else:
                robust_rmtree(path)
            return
        except Exception as exc:  # noqa: BLE001 - retried below / reported by caller
            last_error = exc
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
    raise last_error  # type: ignore[misc]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="删除批次任务目录中的所有内容（保留目录本身）。",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="仓库根目录，默认取脚本所在目录。",
    )
    parser.add_argument(
        "--dirs",
        nargs="+",
        default=list(DEFAULT_DIRS),
        metavar="NAME",
        help=f"要清空的目录名（相对 --root），默认: {' '.join(DEFAULT_DIRS)}",
    )
    parser.add_argument(
        "--include-gitkeep",
        action="store_true",
        help="同时删除 .gitkeep（默认保留，以便 git 继续跟踪空目录）。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将删除的内容，不实际删除。",
    )
    return parser.parse_args(argv)


def empty_directory(directory: Path, dry_run: bool, keep_gitkeep: bool = True) -> tuple[int, int]:
    """Delete every child of *directory*; return (removed, failed) counts."""
    removed = 0
    failed = 0
    for child in sorted(directory.iterdir()):
        if keep_gitkeep and child.name == ".gitkeep":
            print(f"[KEEP] {child}")
            continue
        action = "DRY-RUN" if dry_run else "DELETE"
        print(f"[{action}] {child}")
        if dry_run:
            removed += 1
            continue
        try:
            delete_with_retries(child)
            removed += 1
        except Exception as exc:  # noqa: BLE001 - keep cleaning the rest
            failed += 1
            print(f"[ERROR] 删除失败: {child}: {exc}", file=sys.stderr)
    return removed, failed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"根目录不存在: {root}", file=sys.stderr)
        return 2

    total_removed = 0
    total_failed = 0
    for name in args.dirs:
        target = (root / name).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            print(f"[WARN] 目录必须位于根目录内，跳过: {target}", file=sys.stderr)
            total_failed += 1
            continue
        if not target.is_dir():
            print(f"[WARN] 目录不存在，跳过: {target}")
            continue
        print(f"\n[DIR] {target}")
        removed, failed = empty_directory(target, args.dry_run, keep_gitkeep=not args.include_gitkeep)
        total_removed += removed
        total_failed += failed
        verb = "将删除" if args.dry_run else "已删除"
        print(f"[DIR DONE] {target}：{verb} {removed} 项，失败 {failed} 项。")

    verb = "将删除" if args.dry_run else "已删除"
    print(f"\n[DONE] 共{verb} {total_removed} 项，失败 {total_failed} 项。")
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
r"""删除最终 sessions 中的 SUBAGENT-003 孤儿 subagent 文件。

这个脚本的目标很明确：只处理 `pipeline_report_summary.md` 中标记为
SUBAGENT-003 的 subagent JSON 文件，也就是最终 `sessions` 目录里
没有任何父级 `_metadata.agent` 映射指向的孤儿 subagent。

默认干跑，不会删除任何文件；传入 --apply 后才会实际删除。

使用示例：
  python fix_subagent_003.py
  python fix_subagent_003.py --apply
  python fix_subagent_003.py --task TASK-20260421-3CFAB1
  python fix_subagent_003.py --apply --clean-dirs
  python fix_subagent_003.py --task TASK-20260421-3CFAB1 --apply --clean-dirs

实际路径示例：
  python "C:\Users\15837\Desktop\workspace\fix\claude_code_session\检查-修复脚本\fix_subagent_003.py" --summary "C:\Users\15837\Desktop\workspace\data\5-21-200-wq\pipeline_report_summary.md" -o "C:\Users\15837\Desktop\workspace\data\5-21-200-wq" --apply
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Set


TASK_RE = re.compile(r"TASK-\d{8}-[A-Z0-9]+")
DEFAULT_REPORT_NAME = "fix_subagent_003_report.json"
SUBAGENT_PATH_RE = re.compile(
    r"((?:[A-Za-z]:\\|/)[^|;\r\n]*?[\\/](?:sessions|data_success)[\\/][^|;\r\n]*?[\\/]subagents[\\/][^|;\r\n]*?agent-[^|;\r\n]*?\.json)"
)
SESSION_CONTAINER_NAMES = {"sessions", "data_success"}


def parse_summary_targets(summary_path: Path) -> Dict[str, Set[Path]]:
    """返回 summary 中列出的 SUBAGENT-003 文件，按 TASK 分组。"""
    text = summary_path.read_text(encoding="utf-8")
    targets: Dict[str, Set[Path]] = {}

    for line in text.splitlines():
        if "SUBAGENT-003" not in line:
            continue

        task_match = TASK_RE.search(line)
        path_match = SUBAGENT_PATH_RE.search(line)
        if not task_match or not path_match:
            continue

        task_id = task_match.group(0)
        subagent_path = Path(path_match.group(1).strip())
        targets.setdefault(task_id, set()).add(subagent_path)

    return targets


def filter_targets(
    targets: Dict[str, Set[Path]], task_ids: Iterable[str] | None
) -> Dict[str, Set[Path]]:
    if not task_ids:
        return targets
    wanted = set(task_ids)
    return {task_id: paths for task_id, paths in targets.items() if task_id in wanted}


def is_subagent_path(path: Path) -> bool:
    """判断路径是否指向 session 容器下的 subagents/agent-*.json 文件。"""
    normalized_parts = {part.lower() for part in path.parts}
    return (
        bool(SESSION_CONTAINER_NAMES & normalized_parts)
        and "subagents" in normalized_parts
        and path.name.startswith("agent-")
        and path.suffix == ".json"
    )


def sessions_root_for(path: Path) -> Path:
    """返回 path 所属的 session 容器根目录，兼容 Windows 和 POSIX 路径。"""
    for index, part in enumerate(path.parts):
        if part.lower() in SESSION_CONTAINER_NAMES:
            return Path(*path.parts[: index + 1])
    raise ValueError(f"路径不在 session 容器目录下: {path}")


def remove_empty_parents(path: Path, stop_dir: Path, *, apply: bool) -> int:
    """删除空的父目录，但不会删除 stop_dir 本身；干跑时只打印计划。"""
    removed = 0
    current = path.parent
    stop_dir = stop_dir.resolve()

    while current.exists() and current.resolve() != stop_dir:
        try:
            next(current.iterdir())
            break
        except StopIteration:
            print(f"{'EMPTY_DIR DELETE' if apply else 'EMPTY_DIR WOULD_DELETE'} {current}")
            if apply:
                current.rmdir()
            removed += 1
            current = current.parent

    return removed


def delete_targets(targets: Dict[str, Set[Path]], *, apply: bool, clean_dirs: bool) -> tuple[Dict[str, int], list[dict[str, Any]]]:
    stats = {
        "target_files": 0,
        "deleted_files": 0,
        "would_delete_files": 0,
        "missing_files": 0,
        "skipped_non_subagent": 0,
        "removed_empty_dirs": 0,
        "would_remove_empty_dirs": 0,
    }
    records: list[dict[str, Any]] = []

    for task_id in sorted(targets):
        for path in sorted(targets[task_id], key=str):
            stats["target_files"] += 1
            record: dict[str, Any] = {
                "产物id": task_id,
                "文件id": str(path),
                "原因": "SUBAGENT-003：最终 sessions 中没有父级 _metadata.agent 映射指向该 subagent，判定为孤儿 subagent",
                "操作结果": "pending",
                "apply": apply,
                "clean_dirs": clean_dirs,
                "removed_empty_dirs": 0,
            }

            if not is_subagent_path(path):
                stats["skipped_non_subagent"] += 1
                print(f"SKIP_NON_SUBAGENT {task_id} {path}")
                record["操作结果"] = "skipped_non_subagent"
                records.append(record)
                continue

            if not path.exists():
                stats["missing_files"] += 1
                print(f"MISSING {task_id} {path}")
                record["操作结果"] = "missing"
                records.append(record)
                continue

            if apply:
                print(f"DELETE {task_id} {path}")
                path.unlink()
                stats["deleted_files"] += 1
                record["操作结果"] = "deleted"
            else:
                print(f"WOULD_DELETE {task_id} {path}")
                stats["would_delete_files"] += 1
                record["操作结果"] = "would_delete"

            if clean_dirs:
                removed_empty_dirs = remove_empty_parents(
                    path, sessions_root_for(path), apply=apply
                )
                record["removed_empty_dirs"] = removed_empty_dirs
                if apply:
                    stats["removed_empty_dirs"] += removed_empty_dirs
                else:
                    stats["would_remove_empty_dirs"] += removed_empty_dirs
            records.append(record)

    return stats, records


def resolve_output_path(output: Path | None, root: Path) -> Path:
    if output is None:
        return root / DEFAULT_REPORT_NAME
    output = output.expanduser().resolve()
    if output.exists() and output.is_dir():
        return output / DEFAULT_REPORT_NAME
    if output.suffix.lower() != ".json":
        return output / DEFAULT_REPORT_NAME
    return output


def write_report(output_path: Path, *, summary_path: Path, apply: bool, clean_dirs: bool, stats: dict[str, int], records: list[dict[str, Any]]) -> None:
    payload = {
        "report_type": "fix_subagent_003_report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_path": str(summary_path),
        "apply": apply,
        "clean_dirs": clean_dirs,
        "summary": stats,
        "results": records,
        "tasks": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="包含 pipeline_report_summary.md 的目录，默认是脚本所在目录。",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="汇总报告路径，默认是 <root>/pipeline_report_summary.md。",
    )
    parser.add_argument(
        "--task",
        action="append",
        help="只处理指定 TASK ID，可重复传入多个。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际删除文件；默认干跑，只打印将删除的文件。",
    )
    parser.add_argument(
        "--clean-dirs",
        action="store_true",
        help="删除文件后，同时清理空目录；干跑时只打印将清理的目录。",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=f"JSON 修复报告输出路径或目录；默认写入 <root>/{DEFAULT_REPORT_NAME}。",
    )
    args = parser.parse_args()

    summary_path = args.summary or (args.root / "pipeline_report_summary.md")
    targets = filter_targets(parse_summary_targets(summary_path), args.task)
    output_path = resolve_output_path(args.output, args.root)

    print(f"mode={'APPLY' if args.apply else 'DRY_RUN'} summary={summary_path} tasks={len(targets)}")

    stats, records = delete_targets(targets, apply=args.apply, clean_dirs=args.clean_dirs)
    print("SUMMARY", " ".join(f"{key}={value}" for key, value in stats.items()))
    write_report(output_path, summary_path=summary_path, apply=args.apply, clean_dirs=args.clean_dirs, stats=stats, records=records)
    print(f"report: {output_path}")
    if not args.apply:
        print("dry run only; pass --apply to actually delete files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

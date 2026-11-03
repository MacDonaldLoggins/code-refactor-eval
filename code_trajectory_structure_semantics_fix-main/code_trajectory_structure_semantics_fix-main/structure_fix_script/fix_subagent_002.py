#!/usr/bin/env python3
r"""Remove _metadata.agent mappings whose subagent JSON file is missing.

This is a conservative repair for SUBAGENT-002:
  _metadata.agent points to a subagent_id, but the current session group does
  not contain the corresponding subagent JSON file.

By default this script is a dry-run. Use --apply to write changes.

Examples:
  python fix_subagent_002_remove_missing_metadata.py ^
    -r C:\path\to\TASK-12-提交给客户 --dry-run

  python fix_subagent_002_remove_missing_metadata.py ^
    -r C:\path\to\TASK-12-提交给客户 --apply
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUBAGENT_DIR_NAMES = {"subagent", "subagents"}
SKIP_DIR_NAMES = {"__pycache__", ".git", ".idea", "node_modules", "report", "tool-results"}


@dataclass
class RemovedMapping:
    message_index: int
    tool_call_id: str
    subagent_id: str


@dataclass
class FileResult:
    path: Path
    mappings_seen: int = 0
    removed: list[RemovedMapping] = field(default_factory=list)
    removed_empty_agent_maps: int = 0
    changed: bool = False
    error: str = ""


def canonical_agent_id(value: str) -> str:
    value = value.strip()
    return value[6:] if value.startswith("agent-") else value


def is_session_json(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() != ".json":
        return False
    if path.name.endswith(".meta.json"):
        return False
    if any(part in SKIP_DIR_NAMES for part in path.parts):
        return False
    if any(part in SUBAGENT_DIR_NAMES for part in path.parts):
        return False
    return True


def should_descend(path: Path) -> bool:
    if path.name in SKIP_DIR_NAMES:
        return False
    if path.name.startswith("."):
        return False
    return True


def iter_session_files(root: Path, session_path: Path) -> list[Path]:
    target = root / session_path
    if target.is_file():
        return [target] if is_session_json(target) else []

    if target.is_dir():
        files: list[Path] = []
        stack = [target]
        while stack:
            current = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda item: str(item).lower(), reverse=True)
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if should_descend(child):
                        stack.append(child)
                elif is_session_json(child):
                    files.append(child)
        return sorted(files, key=lambda item: str(item).lower())

    # Batch root: each direct child is a TASK directory.
    files = []
    try:
        task_dirs = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name)
    except OSError:
        return []
    for task_dir in task_dirs:
        files.extend(iter_session_files(task_dir, session_path))
    return sorted(files, key=lambda item: str(item).lower())


def load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON is not an object")
    return payload


def write_json_object(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def subagent_ids_for_session(session_path: Path) -> set[str]:
    """Return subagent ids visible to check_subagent for one main session."""
    ids: set[str] = set()
    session_group_dir = session_path.with_suffix("")
    if not session_group_dir.is_dir():
        return ids

    for dir_name in SUBAGENT_DIR_NAMES:
        subagent_dir = session_group_dir / dir_name
        if not subagent_dir.is_dir():
            continue
        for path in subagent_dir.iterdir():
            if not path.is_file() or path.suffix.lower() != ".json":
                continue
            if path.name.endswith(".meta.json"):
                continue
            ids.add(canonical_agent_id(path.stem))
    return ids


def remove_missing_mappings(payload: dict[str, Any], existing_subagent_ids: set[str]) -> FileResult:
    result = FileResult(path=Path())
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return result

    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            continue
        metadata = message.get("_metadata")
        agent_map = metadata.get("agent") if isinstance(metadata, dict) else None
        if not isinstance(agent_map, dict):
            continue

        for tool_call_id, subagent_id in list(agent_map.items()):
            if not isinstance(tool_call_id, str) or not isinstance(subagent_id, str):
                continue
            if not subagent_id.strip() or subagent_id.strip() == "main":
                continue

            result.mappings_seen += 1
            canonical_id = canonical_agent_id(subagent_id)
            if canonical_id in existing_subagent_ids:
                continue

            agent_map.pop(tool_call_id, None)
            result.changed = True
            result.removed.append(
                RemovedMapping(
                    message_index=index,
                    tool_call_id=tool_call_id,
                    subagent_id=subagent_id,
                )
            )

        if not agent_map:
            metadata.pop("agent", None)
            result.removed_empty_agent_maps += 1

    return result


def process_file(path: Path, *, dry_run: bool) -> FileResult:
    try:
        payload = load_json_object(path)
        existing_ids = subagent_ids_for_session(path)
        result = remove_missing_mappings(payload, existing_ids)
        result.path = path
        if result.changed and not dry_run:
            write_json_object(path, payload)
        return result
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return FileResult(path=path, error=str(exc))


def print_summary(results: list[FileResult], *, dry_run: bool) -> None:
    changed = [item for item in results if item.changed]
    errors = [item for item in results if item.error]
    removed_count = sum(len(item.removed) for item in results)
    empty_maps = sum(item.removed_empty_agent_maps for item in results)
    seen = sum(item.mappings_seen for item in results)

    print(f"Mode: {'DRY-RUN' if dry_run else 'WRITE'}")
    print(f"Scanned files: {len(results)}")
    print(f"Files with removable mappings: {len(changed)}")
    print(f"_metadata.agent mappings seen: {seen}")
    print(f"Removed missing mappings: {removed_count}")
    print(f"Removed empty _metadata.agent maps: {empty_maps}")
    print(f"Errors: {len(errors)}")

    for result in changed:
        print(f"\nCHANGED {result.path}")
        print(f"  removed={len(result.removed)} empty_agent_maps={result.removed_empty_agent_maps}")
        for item in result.removed[:20]:
            print(
                "  "
                f"message={item.message_index} "
                f"tool_call_id={item.tool_call_id} "
                f"subagent_id={item.subagent_id}"
            )
        if len(result.removed) > 20:
            print(f"  ... {len(result.removed) - 20} more")

    for result in errors:
        print(f"\nERROR {result.path}: {result.error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove SUBAGENT-002 mappings from main session _metadata.agent when the subagent file is missing."
    )
    parser.add_argument(
        "-r",
        "--root",
        type=Path,
        required=True,
        help="Batch root, TASK directory, or parent directory containing the session path.",
    )
    parser.add_argument(
        "-path",
        "--session-path",
        type=Path,
        default=Path("sessions"),
        help="Session file or directory path relative to each TASK. Default: sessions",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes to disk.")
    mode.add_argument("--dry-run", action="store_true", help="Preview changes without writing. Default.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    if not root.exists():
        print(f"Path does not exist: {root}")
        return 2

    dry_run = not args.apply
    files = iter_session_files(root, args.session_path)
    if not files:
        print(f"No applicable main session JSON files found under: {root} / {args.session_path}; nothing to fix.")
        return 0

    results = [
        process_file(path, dry_run=dry_run)
        for path in files
    ]
    print_summary(results, dry_run=dry_run)
    return 1 if any(item.error for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

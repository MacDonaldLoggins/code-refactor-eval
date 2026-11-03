#!/usr/bin/env python3
r"""Fix stale _metadata.agent mappings in session JSON files.

This script removes entries from a message's ``_metadata.agent`` map when the
map key is not present in that same message's ``tool_calls[].id`` or
``tool_call_id``. If the map becomes empty, the ``agent`` key is removed from
``_metadata``.

Usage:
  python fix_subagent_agent_map.py -r D:\path\to\pipeline_report_summary.md --dry-run
  python fix_subagent_agent_map.py D:\path\to\TASK-xxx --dry-run
  python fix_subagent_agent_map.py D:\path\to\20260512
  python fix_subagent_agent_map.py D:\path\to\session.json
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SESSION_DIR_NAMES = {"sessions", "subagent", "subagents"}
SUBAGENT_SOURCE_MAPPING_TITLE = "subagent 来源映射"
REPORT_TASK_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
REPORT_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
SKIP_DIR_NAMES = {
    ".git",
    ".idea",
    "__pycache__",
    "node_modules",
    "repo",
    "docs",
    "report",
    "tool-results",
}


@dataclass
class FileFixResult:
    path: Path
    changed: bool = False
    messages_seen: int = 0
    metadata_agent_maps_seen: int = 0
    removed_mappings: int = 0
    removed_empty_agent_maps: int = 0
    error: str = ""


@dataclass(frozen=True)
class ReportIssue:
    task_id: str
    session_path: str
    line_number: int
    reason: str


@dataclass(frozen=True)
class TargetEdit:
    path: Path
    line_number: int
    reason: str


def load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON is not an object")
    return payload


def write_json_object(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def message_tool_reference_ids(message: dict[str, Any]) -> set[str]:
    ids: set[str] = set()

    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id.strip():
        ids.add(tool_call_id.strip())

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            candidate = tool_call.get("id")
            if isinstance(candidate, str) and candidate.strip():
                ids.add(candidate.strip())

    return ids


def fix_agent_map_for_message(message: dict[str, Any]) -> tuple[int, bool]:
    metadata = message.get("_metadata")
    agent_map = metadata.get("agent") if isinstance(metadata, dict) else None
    if not isinstance(agent_map, dict):
        return 0, False

    reference_ids = message_tool_reference_ids(message)
    stale_keys = [
        key
        for key in list(agent_map.keys())
        if not isinstance(key, str) or key.strip() not in reference_ids
    ]
    if not stale_keys:
        return 0, False

    for key in stale_keys:
        agent_map.pop(key, None)
    removed_empty_map = False
    if not agent_map:
        metadata.pop("agent", None)
        removed_empty_map = True
    return len(stale_keys), removed_empty_map


def fix_document(payload: dict[str, Any], path: Path) -> FileFixResult:
    result = FileFixResult(path=path)
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return result

    for message in messages:
        if not isinstance(message, dict):
            continue
        result.messages_seen += 1

        metadata = message.get("_metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("agent"), dict):
            continue
        result.metadata_agent_maps_seen += 1

        removed_mappings, removed_empty_map = fix_agent_map_for_message(message)
        if removed_mappings:
            result.removed_mappings += removed_mappings
            result.changed = True
        if removed_empty_map:
            result.removed_empty_agent_maps += 1

    return result


def split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip().replace("\\|", "|").replace("<br>", "\n") for cell in stripped[1:-1].split("|")]


def parse_pipeline_report(report_path: Path) -> list[ReportIssue]:
    issues: list[ReportIssue] = []
    current_task = ""
    in_detail_section = False
    for line in report_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if line.strip() == "## 异常 Task 详情":
            in_detail_section = True
            continue
        if line.strip() == "## 成功 Task":
            in_detail_section = False
            continue
        if not in_detail_section:
            continue

        heading = REPORT_TASK_HEADING_RE.match(line.strip())
        if heading:
            current_task = heading.group(1).strip()
            continue

        cells = split_markdown_row(line)
        if len(cells) < 4 or cells[0] in {"检查项", "---"}:
            continue
        if cells[0] != SUBAGENT_SOURCE_MAPPING_TITLE:
            continue

        try:
            line_number = int(cells[3])
        except ValueError:
            continue
        if current_task and cells[2]:
            issues.append(
                ReportIssue(
                    task_id=current_task,
                    session_path=cells[2].replace("/", "\\"),
                    line_number=line_number,
                    reason=cells[1],
                )
            )
    return issues


def session_path_for_issue(report_path: Path, issue: ReportIssue) -> Path:
    return report_path.parent / issue.task_id / issue.session_path


def targets_from_report(report_path: Path) -> list[TargetEdit]:
    targets: dict[tuple[Path, int], TargetEdit] = {}
    for issue in parse_pipeline_report(report_path):
        session_path = session_path_for_issue(report_path, issue)
        if not session_path.is_file():
            continue
        key = (session_path, issue.line_number)
        targets.setdefault(
            key,
            TargetEdit(path=session_path, line_number=issue.line_number, reason=issue.reason),
        )
    return sorted(targets.values(), key=lambda item: (str(item.path).lower(), -item.line_number))


def top_level_element_lines(text: str) -> list[int]:
    lines: list[int] = []
    depth = 0
    line = 1
    in_string = False
    escape = False
    expecting_element = True

    for char in text:
        if char == "\n":
            line += 1

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            if depth == 1 and expecting_element:
                lines.append(line)
                expecting_element = False
            in_string = True
            continue

        if char in "[{":
            if depth == 1 and expecting_element:
                lines.append(line)
                expecting_element = False
            depth += 1
            continue

        if char in "]}":
            depth -= 1
            continue

        if depth == 1 and char == ",":
            expecting_element = True
            continue

        if depth == 1 and not char.isspace() and expecting_element:
            lines.append(line)
            expecting_element = False

    return lines


def messages_element_lines_from_text(text: str) -> list[int]:
    marker = '"messages"'
    start = text.find(marker)
    if start == -1:
        return []
    array_start = text.find("[", start + len(marker))
    if array_start == -1:
        return []
    line_offset = text.count("\n", 0, array_start)
    return [line_offset + line for line in top_level_element_lines(text[array_start:])]


def message_index_for_line(starts: list[int], line_number: int) -> int | None:
    if not starts:
        return None
    for index in range(len(starts) - 1, -1, -1):
        if starts[index] <= line_number:
            return index
    return None


def process_report_targets(report_path: Path, *, dry_run: bool) -> list[FileFixResult]:
    targets_by_path: dict[Path, list[TargetEdit]] = {}
    for target in targets_from_report(report_path):
        targets_by_path.setdefault(target.path, []).append(target)

    results: list[FileFixResult] = []
    for session_path in sorted(targets_by_path, key=lambda item: str(item).lower()):
        result = FileFixResult(path=session_path)
        try:
            text = session_path.read_text(encoding="utf-8-sig")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("top-level JSON is not an object")
            messages = payload.get("messages")
            if not isinstance(messages, list):
                raise ValueError("missing messages array")

            starts = messages_element_lines_from_text(text)
            result.messages_seen = len([message for message in messages if isinstance(message, dict)])

            for target in sorted(targets_by_path[session_path], key=lambda item: item.line_number, reverse=True):
                message_index = message_index_for_line(starts, target.line_number)
                if message_index is None or message_index >= len(messages):
                    result.error = f"cannot locate message for line {target.line_number}"
                    continue

                message = messages[message_index]
                if not isinstance(message, dict):
                    continue
                metadata = message.get("_metadata")
                if not isinstance(metadata, dict) or not isinstance(metadata.get("agent"), dict):
                    continue
                result.metadata_agent_maps_seen += 1

                removed_mappings, removed_empty_map = fix_agent_map_for_message(message)
                if removed_mappings:
                    result.removed_mappings += removed_mappings
                    result.changed = True
                if removed_empty_map:
                    result.removed_empty_agent_maps += 1

            if result.changed and not dry_run:
                write_json_object(session_path, payload)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            result.error = str(exc)
        results.append(result)

    return results


def should_descend(path: Path) -> bool:
    if path.name in SKIP_DIR_NAMES:
        return False
    return not path.name.startswith(".")


def is_session_json(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() != ".json":
        return False
    if path.name.endswith(".meta.json"):
        return False
    return any(part in SESSION_DIR_NAMES for part in path.parts)


def iter_json_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() == ".json" else []

    files: list[Path] = []
    stack = [root]
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


def process_file(path: Path, *, dry_run: bool) -> FileFixResult:
    try:
        payload = load_json_object(path)
        result = fix_document(payload, path)
        if result.changed and not dry_run:
            write_json_object(path, payload)
        return result
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return FileFixResult(path=path, error=str(exc))


def print_summary(results: list[FileFixResult], *, dry_run: bool) -> None:
    changed = [result for result in results if result.changed]
    errors = [result for result in results if result.error]
    removed_mappings = sum(result.removed_mappings for result in results)
    removed_empty_maps = sum(result.removed_empty_agent_maps for result in results)

    mode = "DRY-RUN" if dry_run else "WRITE"
    print(f"Mode: {mode}")
    print(f"Scanned files: {len(results)}")
    print(f"Changed files: {len(changed)}")
    print(f"Removed stale mappings: {removed_mappings}")
    print(f"Removed empty _metadata.agent maps: {removed_empty_maps}")
    print(f"Errors: {len(errors)}")

    for result in changed:
        print(
            "CHANGED "
            f"{result.path} "
            f"removed_mappings={result.removed_mappings} "
            f"removed_empty_agent_maps={result.removed_empty_agent_maps}"
        )

    for result in errors:
        print(f"ERROR {result.path}: {result.error}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="删除 session JSON 中 _metadata.agent 的无效 key；删空后移除 agent 字段。"
    )
    parser.add_argument("path", nargs="?", type=Path, help="session JSON 文件、TASK 目录或批量任务根目录。")
    parser.add_argument("-r", "--report", type=Path, help="指定 pipeline_report_summary.md 报告路径。")
    parser.add_argument("--dry-run", action="store_true", help="只报告将要修改的文件，不写回。")
    args = parser.parse_args(argv)
    if args.report is None and args.path is None:
        parser.error("需要指定 -r/--report 报告路径，或指定 session JSON/TASK/根目录路径。")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = (args.report or args.path).expanduser().resolve()
    if not root.exists():
        print(f"Path does not exist: {root}")
        return 2

    if args.report is not None:
        if not root.is_file():
            print(f"Report path is not a file: {root}")
            return 2
        results = process_report_targets(root, dry_run=args.dry_run)
        if not results:
            print(f"No {SUBAGENT_SOURCE_MAPPING_TITLE} issues found in: {root}")
            return 0
    else:
        files = iter_json_files(root)
        if not files:
            print(f"No session JSON files found under: {root}")
            return 1
        results = [process_file(path, dry_run=args.dry_run) for path in files]

    print_summary(results, dry_run=args.dry_run)
    return 1 if any(result.error for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

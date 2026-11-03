#!/usr/bin/env python3
"""Apply assistant scene-1 repairs directly to session files."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCRIPT_NAME = Path(__file__).stem
SESSION_DIR_NAME = "sessions"
REPORT_FILE_NAME = f"{SCRIPT_NAME}_report.json"
CONVERT_SESSION_DIR_NAME = "convert-sessions"
REQUEST_INTERRUPTED_MARKER = "Request interrupted by"
SKIPPED_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
CONTINUATION_MARKER = "This session is being continued from a previous conversation that ran out of context."
DEFAULT_ASSISTANT_TIMESTAMP = "2026-06-08T14:57:11.773Z"
DEFAULT_ASSISTANT_CONTENT = (
    "As the current conversation approaches the context window limit, the system will automatically trigger context window compression."
)


def _is_session_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() == ".json"
        and not path.name.startswith(".")
        and not path.name.endswith(".meta.json")
        and path.name != "agent_mappings.json"
        and not any(part in SKIPPED_DIR_NAMES for part in path.parts)
    )


def _message_lines(path: Path) -> list[int]:
    """Return source lines for top-level elements in the messages array."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    marker = '"messages"'
    marker_start = text.find(marker)
    if marker_start < 0:
        return []
    array_start = text.find("[", marker_start + len(marker))
    if array_start < 0:
        return []

    lines: list[int] = []
    line_offset = text.count("\n", 0, array_start)
    depth = 0
    line = 1
    in_string = False
    escaped = False
    expecting_element = True
    for char in text[array_start:]:
        if char == "\n":
            line += 1
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            if depth == 1 and expecting_element:
                lines.append(line + line_offset)
                expecting_element = False
            in_string = True
            continue
        if char in "[{":
            if depth == 1 and expecting_element:
                lines.append(line + line_offset)
                expecting_element = False
            depth += 1
        elif char in "]}":
            depth -= 1
        elif depth == 1 and char == ",":
            expecting_element = True
        elif depth == 1 and expecting_element and not char.isspace():
            lines.append(line + line_offset)
            expecting_element = False
    return lines


def _message_addition(message: dict[str, Any]) -> tuple[Any, str]:
    content = message.get("content", "")
    metadata = message.get("_metadata")
    timestamp = metadata.get("timestamp") if isinstance(metadata, dict) else None
    if not isinstance(timestamp, str):
        timestamp = message.get("timestamp", "")
    return content if content is not None else "", timestamp if isinstance(timestamp, str) else ""


def _content_contains(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, list):
        return any(_content_contains(item, needle) for item in value)
    if isinstance(value, dict):
        return any(_content_contains(item, needle) for item in value.values())
    return False


_SESSION_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _session_id_matches(session_id: str, candidate: Path) -> bool:
    candidate_id = candidate.stem
    if session_id == candidate_id:
        return True
    session_uuids = set(_SESSION_UUID_RE.findall(session_id))
    candidate_uuids = set(_SESSION_UUID_RE.findall(candidate_id))
    return bool(session_uuids and session_uuids.intersection(candidate_uuids))


def _matching_convert_sessions(task_dir: Path, session_id: str) -> list[Path]:
    convert_dir = task_dir / CONVERT_SESSION_DIR_NAME
    if not convert_dir.is_dir():
        return []
    return [path for path in _session_files(convert_dir) if _session_id_matches(session_id, path)]


def _has_interrupted_followup(path: Path, timestamp: str) -> bool:
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for index, message in enumerate(messages[:-1]):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        _, message_timestamp = _message_addition(message)
        if message_timestamp != timestamp:
            continue
        following = messages[index + 1]
        if (
            isinstance(following, dict)
            and following.get("role") == "user"
            and _content_contains(following.get("content"), REQUEST_INTERRUPTED_MARKER)
        ):
            return True
    return False


def _is_interrupted_tool_followup(
    task_dir: Path,
    session_path: Path,
    following: dict[str, Any] | None,
    timestamp: str,
) -> bool:
    if not isinstance(following, dict) or following.get("role") != "user" or not timestamp:
        return False
    return any(
        _has_interrupted_followup(candidate, timestamp)
        for candidate in _matching_convert_sessions(task_dir, session_path.stem)
    )

def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _task_dirs(paths: Iterable[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for path in paths:
        path = path.expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"TASK 目录不存在或不是目录: {path}")
        if (path / SESSION_DIR_NAME).exists() or path.name.startswith("TASK-"):
            found[str(path)] = path
            continue
        for child in path.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                if child.name.startswith("TASK-") or (child / SESSION_DIR_NAME).exists():
                    found[str(child)] = child
    return sorted(found.values(), key=lambda item: item.name.lower())


def _session_files(sessions_dir: Path) -> list[Path]:
    return [
        path
        for path in sorted(
            (path for path in sessions_dir.rglob("*.json") if _is_session_file(path)),
            key=lambda path: path.relative_to(sessions_dir).as_posix().lower(),
        )
    ] if sessions_dir.is_dir() else []


def _message_timestamp(message: dict[str, Any]) -> str:
    metadata = message.get("_metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("timestamp"), str):
        return metadata["timestamp"]
    timestamp = message.get("timestamp")
    return timestamp if isinstance(timestamp, str) else ""


def _is_continuation_user(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and CONTINUATION_MARKER in str(message.get("content", ""))
    )


def _assistant_node(content: str, timestamp: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "agent": "main",
        "reasoning_content": "",
        "content": content,
        "_metadata": {
            "timestamp": timestamp,
            "tokens": {
                "input": 4034,
                "output": 377,
                "reasoning": 0,
                "cache": {"read": 36293, "write": 507},
            },
        },
    }


def scence_1(
    source_task_dirs: Iterable[Path],
    *,
    target_task_dirs: Iterable[Path] | None = None,
    assistant_content: str = DEFAULT_ASSISTANT_CONTENT,
    assistant_timestamp: str = DEFAULT_ASSISTANT_TIMESTAMP,
) -> dict[str, Any]:
    """Modify only ``-i`` tasks; ``-r`` is used to resolve the TASK folder."""
    source_tasks = list(source_task_dirs)
    target_tasks = list(target_task_dirs) if target_task_dirs is not None else source_tasks
    target_records = [
        (task_dir, session_path)
        for task_dir in target_tasks
        for session_path in _session_files(task_dir / SESSION_DIR_NAME)
    ]
    result: dict[str, Any] = {
        "script": SCRIPT_NAME,
        "scene": "scence_1",
        "source_tasks_checked": len(source_tasks),
        "target_tasks_checked": len(target_tasks),
        "target_sessions_checked": len(target_records),
        "changed": 0,
        "matches": [],
        "skipped": [],
    }
    if not source_tasks:
        result["skipped"].append("-r 下未找到 TASK 目录")
        return result
    if not target_tasks:
        result["skipped"].append("-i 未找到目标 TASK 目录")
        return result
    for task_dir, session_path in target_records:
        payload = _load_json(session_path)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        inserts: list[tuple[int, str]] = []
        source_lines = _message_lines(session_path)
        inserted_issues: list[dict[str, Any]] = []
        ignored_issues: list[dict[str, Any]] = []
        ignored_user_indexes: set[int] = set()
        for index, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "tool":
                following = messages[index + 1] if index + 1 < len(messages) else None
                timestamp = _message_timestamp(message)
                if (
                    isinstance(following, dict)
                    and following.get("role") == "user"
                    and _is_interrupted_tool_followup(
                        task_dir,
                        session_path,
                        following,
                        timestamp,
                    )
                ):
                    ignored_user_indexes.add(index + 1)
                    ignored_issues.append(
                        {
                            "line": source_lines[index] if index < len(source_lines) else 1,
                            "content": message.get("content", ""),
                            "timestamp": timestamp,
                        }
                    )
                    continue
            if not _is_continuation_user(message):
                continue
            if index in ignored_user_indexes:
                continue
            previous = messages[index - 1] if index else None
            if isinstance(previous, dict) and previous.get("role") == "assistant":
                continue
            previous_timestamp = _message_timestamp(previous) if isinstance(previous, dict) else ""
            inserts.append((index, previous_timestamp))
            inserted_issues.append(
                {
                    "line": source_lines[index] if index < len(source_lines) else 1,
                    "previous_role": previous.get("role", "") if isinstance(previous, dict) else "",
                    "previous_timestamp": previous_timestamp,
                }
            )

        for index, _ in reversed(inserts):
            messages.insert(index, _assistant_node(assistant_content, assistant_timestamp))
        if inserts:
            session_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if not inserts and not ignored_issues:
            continue
        result["changed"] += len(inserts)
        result["matches"].append(
            {
                "task_id": task_dir.name,
                "path": str(session_path),
                "inserted_before_indexes": [index for index, _ in inserts],
                "previous_timestamps": sorted({timestamp for _, timestamp in inserts if timestamp}),
                "issues": inserted_issues,
                "ignored": ignored_issues,
            }
        )
    return result


def _build_structure_style_report(task_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    counted_issue_total = 0
    counted_session_total = 0
    for match in result.get("matches", []):
        sub: list[dict[str, Any]] = []
        for issue in match.get("issues", []):
            addition = {
                "content": DEFAULT_ASSISTANT_CONTENT,
                "timestamp": DEFAULT_ASSISTANT_TIMESTAMP,
            }
            if issue.get("previous_role"):
                addition["previous_role"] = issue["previous_role"]
            if issue.get("previous_timestamp"):
                addition["previous_timestamp"] = issue["previous_timestamp"]
            sub.append(
                {
                    "function": SCRIPT_NAME,
                    "action_type": "add",
                    "before_content": "",
                    "after_content": DEFAULT_ASSISTANT_CONTENT,
                    "problem": "continuation user节点上方缺少assistant节点",
                    "line": issue.get("line", 1),
                    "addition": addition,
                }
            )
            counted_issue_total += 1
        counted_session_total += 1 if sub else 0
        for ignored in match.get("ignored", []):
            sub.append(
                {
                    "function": "结构体检查",
                    "action_type": "Ignore",
                    "before_content": "",
                    "after_content": "",
                    "problem": "tool节点下方应为tool或assistant，实际为user",
                    "line": ignored.get("line", 1),
                    "addition": {
                        "content": ignored.get("content", ""),
                        "timestamp": ignored.get("timestamp", ""),
                        "reason": "structure.py 阻断：convert-sessions 对应 user 包含 Request interrupted by",
                    },
                }
            )
        if sub:
            sessions.append({"session_id": Path(match["path"]).stem, "sub": sub})

    return {
        "report_type": SCRIPT_NAME,
        "task_id": task_dir.name,
        "task_path": str(task_dir),
        "create": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total_trajectory_count": result.get("target_sessions_checked", 0),
            "hit_trajectory_count": counted_session_total if counted_issue_total else 0,
            "total_issues": counted_issue_total,
        },
        "sessions": sessions,
    }


def _resolve_target_task(root: Path, value: Path) -> Path:
    candidates = [value.expanduser()]
    if not value.expanduser().is_absolute():
        candidates.insert(0, root / value)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir():
            tasks = _task_dirs([resolved])
            if len(tasks) == 1:
                return tasks[0]
    raise FileNotFoundError(f"-i 目标 TASK 文件夹不存在或无法识别: {value}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="修改 assistant 场景节点")
    parser.add_argument("-r", "--root", type=Path, required=True, help="包含 TASK 子目录的根目录")
    parser.add_argument("-i", "--input", dest="input_task", type=Path, required=True, help="要修改的 TASK 文件夹")
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    source_tasks = _task_dirs([root])
    target_task = _resolve_target_task(root, args.input_task)
    result = scence_1(
        source_tasks,
        target_task_dirs=[target_task],
    )
    report_path = target_task / "report" / REPORT_FILE_NAME
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = _build_structure_style_report(target_task, result)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    report_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

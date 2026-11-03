#!/usr/bin/env python3
"""Validate message-node ordering in a TASK ``sessions`` directory.

The rule is intentionally small and follows the trajectory format used by this
repository:

* ``assistant`` must be preceded by ``user`` or ``tool``;
* ``tool`` must be followed by ``tool`` or ``assistant``;
* ``user`` must be preceded by ``system``, ``assistant`` or ``tool``.

When a task fails, ``<task>/report/structure.json`` is written. Passing tasks
do not get a report file.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


REPORT_NAME = "structure.json"
SESSION_DIR_NAME = "sessions"
CONVERT_SESSION_DIR_NAME = "convert-sessions"
REQUEST_INTERRUPTED_MARKER = "Request interrupted by"
SKIPPED_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
REQUIRED_ROLES = ("user", "assistant", "tool")
ASSISTANT_PREVIOUS_ROLES = {"user", "tool"}
TOOL_NEXT_ROLES = {"tool", "assistant"}
USER_PREVIOUS_ROLES = {"system", "assistant", "tool"}
ALLOWED_META_KEYS = {"session_meta", "total_token", "cwd"}
ALLOWED_SESSION_META_KEYS = {"source", "session_ids"}
ALLOWED_TOTAL_TOKEN_KEYS = {
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
}


@dataclass
class Issue:
    file: str
    line: int
    problem: str
    function: str = "结构体检查"
    role: str = ""
    message_index: int | None = None
    previous_role: str = ""
    next_role: str = ""
    content: Any = ""
    timestamp: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "function": self.function,
            "action_type": "check",
            "before_content": "",
            "after_content": "",
            "problem": self.problem,
            "line": self.line,
            "addition": {
                "content": self.content,
                "timestamp": self.timestamp,
            },
        }


@dataclass
class TaskCheck:
    task_dir: Path
    files_checked: int = 0
    issues: list[Issue] = field(default_factory=list)


def _is_session_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() == ".json"
        and not path.name.startswith(".")
        and not path.name.endswith(".meta.json")
        and path.name != "agent_mappings.json"
        and not any(part in SKIPPED_DIR_NAMES for part in path.parts)
    )


def _session_files(sessions_dir: Path) -> list[Path]:
    if not sessions_dir.is_dir():
        return []
    return sorted(
        (path for path in sessions_dir.rglob("*.json") if _is_session_file(path)),
        key=lambda path: path.relative_to(sessions_dir).as_posix().lower(),
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


def _load_messages(path: Path) -> list[dict[str, Any]]:
    payload = _load_payload(path)
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("缺少 messages 数组")
    return [message for message in messages if isinstance(message, dict)]


def _load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("顶层不是 JSON object")
    return payload


def _relative_file(task_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(task_dir).as_posix()
    except ValueError:
        return path.name


def _add_missing_role_issue(result: TaskCheck, file_name: str, roles: list[str], role: str) -> None:
    if role not in roles:
        result.issues.append(
            Issue(file_name, 1, f"缺少{role}节点", function="基础格式检查", role=role)
        )


def _meta_key_line(path: Path, key: str) -> int:
    """Find an approximate source line for a top-level meta key."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        meta_match = re.search(r'"meta"\s*:', text)
        search_start = meta_match.start() if meta_match else 0
        key_position = text.find(f'"{key}"', search_start)
        if key_position >= 0:
            return text.count("\n", 0, key_position) + 1
    except OSError:
        pass
    return 1


def _message_addition(message: dict[str, Any]) -> tuple[Any, str]:
    content = message.get("content", "")
    metadata = message.get("_metadata")
    timestamp = metadata.get("timestamp") if isinstance(metadata, dict) else None
    if not isinstance(timestamp, str):
        timestamp = message.get("timestamp", "")
    return content if content is not None else "", timestamp if isinstance(timestamp, str) else ""


def _content_contains(value: Any, needle: str) -> bool:
    """Search string, list, or object content without assuming one shape."""
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
    """Match converted files whose names may omit the session type segment."""
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
    return [
        path
        for path in _session_files(convert_dir)
        if _session_id_matches(session_id, path)
    ]


def _has_interrupted_followup(path: Path, timestamp: str) -> bool:
    try:
        payload = _load_payload(path)
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
    """Ignore a tool->user structure hit confirmed by convert-sessions."""
    if (
        not isinstance(following, dict)
        or following.get("role") != "user"
        or not timestamp
    ):
        return False
    session_id = session_path.stem
    return any(
        _has_interrupted_followup(candidate, timestamp)
        for candidate in _matching_convert_sessions(task_dir, session_id)
    )


def _meta_issues(task_dir: Path, path: Path, payload: dict[str, Any]) -> list[Issue]:
    file_name = _relative_file(task_dir, path)
    issues: list[Issue] = []
    meta = payload.get("meta")
    if meta is None:
        return [Issue(file_name, 1, "缺少meta节点", function="meta结构检查")]
    if not isinstance(meta, dict):
        return [Issue(file_name, 1, "meta节点必须是对象", function="meta结构检查")]

    for key in sorted(set(meta) - ALLOWED_META_KEYS):
        issues.append(Issue(file_name, _meta_key_line(path, str(key)), f"meta节点存在多余变量：{key}", function="meta结构检查"))

    nested_rules = (
        ("session_meta", ALLOWED_SESSION_META_KEYS),
        ("total_token", ALLOWED_TOTAL_TOKEN_KEYS),
    )
    for parent_key, allowed_keys in nested_rules:
        value = meta.get(parent_key)
        if not isinstance(value, dict):
            continue
        for key in sorted(set(value) - allowed_keys):
            issues.append(
                Issue(
                    file_name,
                    _meta_key_line(path, str(key)),
                    f"meta.{parent_key}节点存在多余变量：{key}",
                    function="meta结构检查",
                )
            )
    return issues


def _check_file(result: TaskCheck, path: Path) -> None:
    file_name = _relative_file(result.task_dir, path)
    try:
        payload = _load_payload(path)
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
        line = getattr(exc, "lineno", 1) or 1
        result.issues.append(
            Issue(file_name, int(line), f"轨迹文件无法解析：{exc}", function="基础格式检查")
        )
        return

    messages = payload.get("messages")
    if not isinstance(messages, list):
        result.issues.append(Issue(file_name, 1, "缺少 messages 数组", function="基础格式检查"))
        return

    roles = [message.get("role", "") if isinstance(message.get("role", ""), str) else "" for message in messages]
    for required_role in REQUIRED_ROLES:
        _add_missing_role_issue(result, file_name, roles, required_role)

    source_lines = _message_lines(path)
    for index, role in enumerate(roles):
        previous = roles[index - 1] if index else ""
        following = roles[index + 1] if index + 1 < len(roles) else ""
        line = source_lines[index] if index < len(source_lines) else 1
        message_number = index + 1
        content, timestamp = _message_addition(messages[index])

        if role == "assistant" and previous not in ASSISTANT_PREVIOUS_ROLES:
            result.issues.append(
                Issue(
                    file_name,
                    line,
                    f"assistant节点上方应为user或tool，实际为{previous or '空'}",
                    function="结构体检查",
                    role=role,
                    message_index=message_number,
                    previous_role=previous,
                    next_role=following,
                    content=content,
                    timestamp=timestamp,
                )
            )
        elif (
            role == "tool"
            and following not in TOOL_NEXT_ROLES
            and not _is_interrupted_tool_followup(
                result.task_dir,
                path,
                messages[index + 1] if index + 1 < len(messages) else None,
                timestamp,
            )
        ):
            result.issues.append(
                Issue(
                    file_name,
                    line,
                    f"tool节点下方应为tool或assistant，实际为{following or '空'}",
                    function="结构体检查",
                    role=role,
                    message_index=message_number,
                    previous_role=previous,
                    next_role=following,
                    content=content,
                    timestamp=timestamp,
                )
            )
        elif role == "user" and previous not in USER_PREVIOUS_ROLES:
            result.issues.append(
                Issue(
                    file_name,
                    line,
                    f"user节点上方应为system、assistant或tool，实际为{previous or '空'}",
                    function="结构体检查",
                    role=role,
                    message_index=message_number,
                    previous_role=previous,
                    next_role=following,
                    content=content,
                    timestamp=timestamp,
                )
            )


def meta_rule(task_path: Path | str) -> list[Issue]:
    """Return issues for extra top-level variables under each session's meta."""
    task_dir = Path(task_path).expanduser().resolve()
    if not task_dir.is_dir():
        raise FileNotFoundError(f"TASK 目录不存在或不是目录: {task_dir}")

    issues: list[Issue] = []
    for path in _session_files(task_dir / SESSION_DIR_NAME):
        try:
            payload = _load_payload(path)
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            # The structure rule reports malformed JSON separately.
            continue
        issues.extend(_meta_issues(task_dir, path, payload))
    return issues


def inner_rule(task_path: Path | str) -> list[Issue]:
    """Validate message metadata and metadata agent/tool-call references."""
    task_dir = Path(task_path).expanduser().resolve()
    if not task_dir.is_dir():
        raise FileNotFoundError(f"TASK 目录不存在或不是目录: {task_dir}")

    issues: list[Issue] = []
    for path in _session_files(task_dir / SESSION_DIR_NAME):
        try:
            payload = _load_payload(path)
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            continue

        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        file_name = _relative_file(task_dir, path)
        source_lines = _message_lines(path)
        tool_call_ids = {
            tool_call.get("id")
            for message in messages
            if isinstance(message, dict)
            for tool_call in (message.get("tool_calls") or [])
            if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str)
        }

        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            line = source_lines[index] if index < len(source_lines) else 1
            content, timestamp = _message_addition(message)
            metadata = message.get("_metadata")
            if not isinstance(metadata, dict):
                if metadata is not None:
                    issues.append(
                        Issue(
                            file_name,
                            line,
                            "_metadata节点必须是对象",
                            function="基础格式检查",
                            content=content,
                            timestamp=timestamp,
                        )
                    )
                continue

            if not isinstance(metadata.get("timestamp"), str) or not metadata.get("timestamp", "").strip():
                issues.append(
                    Issue(
                        file_name,
                        line,
                        "_metadata节点缺少timestamp",
                        function="基础格式检查",
                        content=content,
                        timestamp=timestamp,
                    )
                )

            agent_map = metadata.get("agent")
            if agent_map is None:
                continue
            if not isinstance(agent_map, dict):
                issues.append(
                    Issue(
                        file_name,
                        line,
                        "_metadata.agent必须是对象",
                        function="基础格式检查",
                        content=content,
                        timestamp=timestamp,
                    )
                )
                continue
            for tool_call_id in agent_map:
                if tool_call_id not in tool_call_ids:
                    issues.append(
                        Issue(
                            file_name,
                            line,
                            f"_metadata.agent中的工具id未在tool_calls.id中找到：{tool_call_id}",
                            function="基础格式检查",
                            content=content,
                            timestamp=timestamp,
                        )
                    )
    return issues


def role_tool_rule(task_path: Path | str) -> list[Issue]:
    """Validate required fields on messages whose role is ``tool``."""
    task_dir = Path(task_path).expanduser().resolve()
    if not task_dir.is_dir():
        raise FileNotFoundError(f"TASK 目录不存在或不是目录: {task_dir}")

    issues: list[Issue] = []
    required_fields = ("agent", "tool_call_id", "content", "_metadata")
    for path in _session_files(task_dir / SESSION_DIR_NAME):
        try:
            payload = _load_payload(path)
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            continue

        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        file_name = _relative_file(task_dir, path)
        source_lines = _message_lines(path)
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            line = source_lines[index] if index < len(source_lines) else 1
            content, timestamp = _message_addition(message)
            for field_name in required_fields:
                if field_name in message:
                    continue
                issues.append(
                    Issue(
                        file_name,
                        line,
                        f"role为tool的节点缺少{field_name}字段",
                        function="tool结构检查",
                        role="tool",
                        message_index=index + 1,
                        content=content,
                        timestamp=timestamp,
                    )
                )
    return issues


def role_assistant_rule(task_path: Path | str) -> list[Issue]:
    """Validate the two allowed field layouts on ``assistant`` messages."""
    task_dir = Path(task_path).expanduser().resolve()
    if not task_dir.is_dir():
        raise FileNotFoundError(f"TASK 目录不存在或不是目录: {task_dir}")

    issues: list[Issue] = []
    required_fields = ("agent", "reasoning_content", "content", "_metadata")
    for path in _session_files(task_dir / SESSION_DIR_NAME):
        try:
            payload = _load_payload(path)
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            continue

        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        file_name = _relative_file(task_dir, path)
        source_lines = _message_lines(path)
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            line = source_lines[index] if index < len(source_lines) else 1
            content, timestamp = _message_addition(message)
            for field_name in required_fields:
                if field_name in message:
                    continue
                issues.append(
                    Issue(
                        file_name,
                        line,
                        f"role为assistant的节点缺少{field_name}字段",
                        function="assistant结构检查",
                        role="assistant",
                        message_index=index + 1,
                        content=content,
                        timestamp=timestamp,
                    )
                )
    return issues


def _build_report(result: TaskCheck) -> dict[str, Any]:
    grouped: dict[str, list[Issue]] = {}
    for issue in result.issues:
        grouped.setdefault(issue.file, []).append(issue)

    sessions = [
        {
            "session_id": Path(file_name).stem,
            "sub": [issue.as_dict() for issue in issues],
        }
        for file_name, issues in sorted(grouped.items())
    ]
    return {
        "report_type": "structure_report",
        "task_id": result.task_dir.name,
        "task_path": str(result.task_dir),
        "create": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total_trajectory_count": result.files_checked,
            "hit_trajectory_count": len(sessions),
            "total_issues": len(result.issues),
        },
        "sessions": sessions,
    }


def structure_rule(task_path: Path | str) -> Path | None:
    """Check one TASK directory and write its failure report when needed."""
    task_dir = Path(task_path).expanduser().resolve()
    if not task_dir.is_dir():
        raise FileNotFoundError(f"TASK 目录不存在或不是目录: {task_dir}")

    result = TaskCheck(task_dir=task_dir)
    sessions_dir = task_dir / SESSION_DIR_NAME
    files = _session_files(sessions_dir)
    result.files_checked = len(files)
    if not sessions_dir.is_dir():
        result.issues.append(Issue(SESSION_DIR_NAME, 1, "缺少sessions文件夹", function="基础格式检查"))
    elif not files:
        result.issues.append(
            Issue(SESSION_DIR_NAME, 1, "sessions文件夹中缺少轨迹文件", function="基础格式检查")
        )
    else:
        for path in files:
            _check_file(result, path)
        result.issues.extend(meta_rule(task_dir))
        result.issues.extend(inner_rule(task_dir))
        result.issues.extend(role_tool_rule(task_dir))
        result.issues.extend(role_assistant_rule(task_dir))

    report_path = task_dir / "report" / REPORT_NAME
    if not result.issues:
        # A previous failed run may have left a stale report behind.
        if report_path.is_file():
            report_path.unlink()
        return None
    report_dir = task_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(_build_report(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


def _task_dirs(paths: Iterable[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for path in paths:
        path = path.expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"TASK 目录不存在或不是目录: {path}")
        if (path / SESSION_DIR_NAME).exists() or path.name.startswith("TASK-"):
            found[str(path)] = path
            continue
        children = [child for child in path.iterdir() if child.is_dir() and not child.name.startswith(".")]
        for child in children:
            if child.name.startswith("TASK-") or (child / SESSION_DIR_NAME).exists():
                found[str(child)] = child
    return sorted(found.values(), key=lambda item: item.name.lower())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 TASK sessions 轨迹节点结构")
    parser.add_argument("paths", nargs="+", type=Path, help="TASK 目录，或包含 TASK 子目录的根目录")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        task_dirs = _task_dirs(args.paths)
        if not task_dirs:
            raise FileNotFoundError("未找到 TASK 目录")
        failed = 0
        for task_dir in task_dirs:
            report_path = structure_rule(task_dir)
            if report_path:
                failed += 1
                print(f"不通过: {task_dir.name} -> {report_path}")
            else:
                print(f"通过: {task_dir.name}")
        return 1 if failed else 0
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"structure check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

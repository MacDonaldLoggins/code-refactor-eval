#!/usr/bin/env python3
"""检查 session/subagent 中的 skill/command 标签结构。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_NAME = "check_skill_commands.json"
REPORT_TYPE = "check_skill_commands_report"
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
SUBAGENT_DIR_NAMES = {"subagent", "subagents"}
COMMAND_TAGS = ("command-name", "command-message", "command-args")
COMMAND_MARKER_RE = re.compile(r"</?command-(?:name|message|args)>")
TAG_VALUE_RE = re.compile(r"<(?P<tag>command-name|command-message|command-args)>(?P<value>.*?)</(?P=tag)>", re.DOTALL)


@dataclass(frozen=True)
class Finding:
    file_path: str
    message_index: int | None
    reason: str
    content: str
    command_name: str = ""


@dataclass
class SkillCommandResult:
    project_dir: str
    session_path: str
    valid_json: bool = True
    message_count: int = 0
    command_message_count: int = 0
    command_count: int = 0
    command_names: Counter[str] = field(default_factory=Counter)
    findings: list[Finding] = field(default_factory=list)
    error: str = ""

    @property
    def all_pass(self) -> bool:
        return self.valid_json and not self.findings


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def text_preview(text: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def message_agent(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    agent = message.get("agent")
    if isinstance(agent, str) and agent.strip():
        return agent.strip()
    metadata = message.get("_metadata")
    if isinstance(metadata, dict):
        subagent_id = metadata.get("subagent_id")
        if isinstance(subagent_id, str) and subagent_id.strip():
            return subagent_id.strip()
    return "main"


def message_timestamp(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    timestamp = message.get("timestamp")
    if isinstance(timestamp, str):
        return timestamp
    metadata = message.get("_metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("timestamp"), str):
        return metadata["timestamp"]
    return ""


def message_tool_id(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str):
        return tool_call_id
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return ""
    ids = [
        tool_call.get("id")
        for tool_call in tool_calls
        if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str)
    ]
    return ",".join(ids)


def message_subagent(message: dict[str, Any] | None) -> str:
    agent = message_agent(message)
    return "" if agent in {"", "main"} else agent


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


def messages_element_lines(session_path: Path) -> list[int]:
    try:
        text = session_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    marker = '"messages"'
    start = text.find(marker)
    if start == -1:
        return []
    array_start = text.find("[", start + len(marker))
    if array_start == -1:
        return []
    line_offset = text.count("\n", 0, array_start)
    return [line_offset + line for line in top_level_element_lines(text[array_start:])]


def load_json_payload(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_messages(payload: Any, path: Path) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise ValueError(f"{path} 缺少 messages 数组")
        return [message for message in messages if isinstance(message, dict)]
    raise ValueError(f"{path} 顶层不是 JSON object")


def collect_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(collect_text_values(item))
        return parts
    if isinstance(value, dict):
        parts: list[str] = []
        for item in value.values():
            parts.extend(collect_text_values(item))
        return parts
    return []


def command_text_from_message(message: dict[str, Any]) -> str:
    return "\n".join(part for part in collect_text_values(message) if part)


def tag_values(text: str, tag: str) -> list[str]:
    return [match.group("value").strip() for match in TAG_VALUE_RE.finditer(text) if match.group("tag") == tag]


def has_command_marker(text: str) -> bool:
    return bool(COMMAND_MARKER_RE.search(text))


def check_command_text(text: str) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    command_names = tag_values(text, "command-name")
    command_messages = tag_values(text, "command-message")
    command_args = tag_values(text, "command-args")

    for tag in COMMAND_TAGS:
        open_count = len(re.findall(fr"<{tag}>", text))
        close_count = len(re.findall(fr"</{tag}>", text))
        if open_count != close_count:
            reasons.append(f"SKILLCMD-001 <{tag}> 起止标签数量不一致：open={open_count}, close={close_count}")

    if not command_names:
        reasons.append("SKILLCMD-002 出现 command 标签但缺少 <command-name>...</command-name>。")
    if len(command_names) > 1:
        reasons.append(f"SKILLCMD-003 同一条消息中出现多个 command-name：{len(command_names)} 个。")
    if len(command_args) > 1:
        reasons.append(f"SKILLCMD-004 同一条消息中出现多个 command-args：{len(command_args)} 个。")
    if len(command_messages) > 1:
        reasons.append(f"SKILLCMD-005 同一条消息中出现多个 command-message：{len(command_messages)} 个。")

    for command_name in command_names:
        if not command_name:
            reasons.append("SKILLCMD-006 command-name 为空。")
        elif not command_name.startswith("/"):
            reasons.append(f"SKILLCMD-007 command-name={command_name!r} 未以 / 开头。")

    if command_names and command_args:
        name_end = text.find("</command-name>")
        args_start = text.find("<command-args>")
        if args_start != -1 and name_end != -1 and args_start < name_end:
            reasons.append("SKILLCMD-008 command-args 出现在 command-name 闭合前。")

    return reasons, command_names


def should_scan_project_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and path.name not in SKIPPED_SESSION_DIR_NAMES


def is_candidate_session_file(path: Path) -> bool:
    return (
        path.is_file()
        and not path.name.startswith(".")
        and not path.name.endswith(".meta.json")
        and path.suffix.lower() in SESSION_FILE_SUFFIXES
        and not any(part in SKIPPED_SESSION_DIR_NAMES for part in path.parts)
    )


def iter_session_paths(project_dir: Path, session_path_arg: Path) -> list[Path]:
    target = project_dir / session_path_arg
    if target.is_file():
        return [target] if is_candidate_session_file(target) else []
    if target.is_dir():
        return sorted(
            (path for path in target.rglob("*") if is_candidate_session_file(path)),
            key=lambda path: str(path.relative_to(project_dir)),
        )
    return [target]


def has_session_target(project_dir: Path, session_path_arg: Path) -> bool:
    return (project_dir / session_path_arg).exists()


def check_one_file(project_dir: Path, session_path: Path) -> SkillCommandResult:
    result = SkillCommandResult(project_dir=project_dir.name, session_path=str(session_path))
    if not session_path.exists():
        result.valid_json = False
        result.error = "session file does not exist"
        result.findings.append(Finding(str(session_path), None, result.error, ""))
        return result

    try:
        messages = extract_messages(load_json_payload(session_path), session_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.valid_json = False
        result.error = str(exc)
        result.findings.append(Finding(str(session_path), None, result.error, ""))
        return result

    result.message_count = len(messages)
    for index, message in enumerate(messages, start=1):
        text = command_text_from_message(message)
        if not has_command_marker(text):
            continue
        result.command_message_count += 1
        reasons, command_names = check_command_text(text)
        for command_name in command_names:
            result.command_names[command_name] += 1
        result.command_count += len(command_names)
        for reason in reasons:
            result.findings.append(
                Finding(
                    file_path=str(session_path),
                    message_index=index,
                    reason=reason,
                    content=text_preview(text),
                    command_name=command_names[0] if command_names else "",
                )
            )
    return result


def file_id_for_session(source_root: Path, project_id: str, session_path: Path) -> str:
    for base in (source_root / project_id, source_root):
        try:
            return session_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return session_path.name


def project_id_for_report(source_root: Path, results: list[SkillCommandResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def message_at_index(session_path: Path, message_index: int | None) -> tuple[dict[str, Any] | None, int | None]:
    if message_index is None or message_index < 1:
        return None, None
    try:
        messages = extract_messages(load_json_payload(session_path), session_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None, None
    if message_index > len(messages):
        return None, None
    lines = messages_element_lines(session_path)
    line = lines[message_index - 1] if message_index - 1 < len(lines) else None
    return messages[message_index - 1], line


def build_json_report(source_root: Path, results: list[SkillCommandResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    tasks: list[dict[str, Any]] = []
    command_names = Counter[str]()
    for result in results:
        command_names.update(result.command_names)
        for finding in result.findings:
            session_path = Path(finding.file_path)
            message, line = message_at_index(session_path, finding.message_index)
            tasks.append(
                {
                    "产物id": result.project_dir,
                    "文件id": file_id_for_session(source_root, result.project_dir, session_path),
                    "原因": finding.reason,
                    "行号": line or 1,
                    "role": str(message.get("role", "")) if message else "",
                    "agent": message_agent(message),
                    "timestamp": message_timestamp(message),
                    "工具id": message_tool_id(message),
                    "subagent": message_subagent(message),
                    "content 原文": normalize_content(message.get("content")) if message else finding.content,
                    "command_name": finding.command_name,
                }
            )

    return {
        "report_type": REPORT_TYPE,
        "project_id": project_id_for_report(source_root, results),
        "source_root": str(source_root),
        "generated_at": generated_at_text(),
        "summary": {
            "files": total,
            "passed": passed,
            "failed": total - passed,
            "issues": len(tasks),
            "command_messages": sum(result.command_message_count for result in results),
            "commands": sum(result.command_count for result in results),
            "unique_command_names": len(command_names),
            "invalid_json_failed": sum(1 for result in results if not result.valid_json),
        },
        "command_names": [
            {"command_name": name, "出现次数": count}
            for name, count in command_names.most_common()
        ],
        "tasks": tasks,
    }


def check_skill_commands(find_path: Path | str, session_json_path: Path | str, output_dir: Path | str) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"scan path does not exist or is not a directory: {root_dir}")

    session_relative_path = Path(session_json_path)
    results: list[SkillCommandResult] = []
    root_session_paths = iter_session_paths(root_dir, session_relative_path)
    project_dirs = (
        [root_dir]
        if root_session_paths or has_session_target(root_dir, session_relative_path)
        else sorted((path for path in root_dir.iterdir() if should_scan_project_dir(path)), key=lambda path: path.name)
    )
    for project_dir in project_dirs:
        session_paths = iter_session_paths(project_dir, session_relative_path)
        if not session_paths:
            missing_session_path = project_dir / session_relative_path
            if missing_session_path.exists():
                continue
            session_paths = [missing_session_path]
        for session_path in session_paths:
            relative_name = session_path.name
            if session_path.exists():
                try:
                    relative_name = f"{project_dir.name}/{session_path.relative_to(project_dir).as_posix()}"
                except ValueError:
                    relative_name = f"{project_dir.name}/{session_path.name}"
            print(f"checking skill commands: {relative_name} ... ", end="", flush=True)
            result = check_one_file(project_dir, session_path)
            result.project_dir = project_dir.name
            print("pass" if result.all_pass else "fail")
            results.append(result)

    report_output_dir = Path(output_dir).expanduser().resolve()
    report_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_output_dir / REPORT_NAME
    report_path.write_text(
        json.dumps(build_json_report(root_dir, results), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    passed = sum(1 for result in results if result.all_pass)
    print(f"\nJSON report written: {report_path}")
    print(f"scanned files: {len(results)}")
    print(f"passed: {passed}")
    print(f"failed: {len(results) - passed}")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check skill command XML-like tags in session files.")
    parser.add_argument("-r", dest="find_path", type=Path, required=True, help="Root containing project subdirectories.")
    parser.add_argument("-path", dest="session_json_path", type=Path, required=True, help="Session file or session directory path relative to each project directory.")
    parser.add_argument("-o", dest="output_dir", type=Path, required=True, help=f"Report output directory. Generates {REPORT_NAME}.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        check_skill_commands(args.find_path, args.session_json_path, args.output_dir)
    except Exception as exc:  # noqa: BLE001 - CLI should print actionable failures.
        print(f"check_skill_commands failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

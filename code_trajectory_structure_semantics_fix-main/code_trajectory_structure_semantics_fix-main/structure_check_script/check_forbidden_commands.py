#!/usr/bin/env python3
"""检查清洗后 JSON 中是否仍残留 slash 命令。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


REPORT_NAME = "check_forbidden_commands.json"
REPORT_TYPE = "check_forbidden_commands_report"
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
FORBIDDEN_COMMANDS = (
    "/help",
    "/clear",
    "/compact",
    "/model",
    "/cost",
    "/stats",
    "/loop",
    "/simplify",
    "/security-review",
    "/init",
    "/schedule",
    "/batch",
    "/claude-api",
    "/update-config",
    "/fewer-permission-prompts",
    "/keybindings-help",
)
COMMAND_RE = re.compile(
    r"(?<![\w/.-])("
    + "|".join(re.escape(command) for command in sorted(FORBIDDEN_COMMANDS, key=len, reverse=True))
    + r")(?![\w.-])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    file_path: str
    message_index: int | None
    json_path: str
    token: str
    reason: str
    content: str


@dataclass
class ForbiddenCommandResult:
    project_dir: str
    session_path: str
    valid_json: bool = True
    string_values_checked: int = 0
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
        parts = [normalize_content(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def text_preview(text: str, limit: int = 700) -> str:
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


def extract_messages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, dict)]


def path_join(parent: str, child: str) -> str:
    return f"{parent}.{child}" if parent else child


def is_message_json_path(json_path: str) -> bool:
    return bool(re.fullmatch(r"messages\[\d+\]", json_path))


def should_skip_content_branch(value: dict[str, Any], json_path: str) -> bool:
    return (
        is_message_json_path(json_path)
        and str(value.get("role", "")).lower() == "system"
    )


def iter_strings(
    value: Any,
    *,
    json_path: str = "",
    message_index: int | None = None,
    in_content: bool = False,
) -> Iterator[tuple[str, str, int | None]]:
    if isinstance(value, str):
        if in_content:
            yield json_path, value, message_index
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            child_message_index = message_index
            if json_path == "messages" and isinstance(item, dict):
                child_message_index = index + 1
            yield from iter_strings(
                item,
                json_path=f"{json_path}[{index}]" if json_path else f"[{index}]",
                message_index=child_message_index,
                in_content=in_content,
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            child_json_path = path_join(json_path, key_text)
            if in_content:
                yield from iter_strings(
                    item,
                    json_path=child_json_path,
                    message_index=message_index,
                    in_content=True,
                )
                continue
            if key_text == "content":
                if should_skip_content_branch(value, json_path):
                    continue
                yield from iter_strings(
                    item,
                    json_path=child_json_path,
                    message_index=message_index,
                    in_content=True,
                )
                continue
            if key_text == "messages":
                yield from iter_strings(
                    item,
                    json_path=child_json_path,
                    message_index=message_index,
                    in_content=False,
                )


def add_finding(
    result: ForbiddenCommandResult,
    *,
    session_path: Path,
    message_index: int | None,
    json_path: str,
    token: str,
    reason: str,
    content: str,
) -> None:
    result.findings.append(
        Finding(
            file_path=str(session_path),
            message_index=message_index,
            json_path=json_path,
            token=token,
            reason=reason,
            content=text_preview(content),
        )
    )


def check_one_file(project_dir: Path, session_path: Path) -> ForbiddenCommandResult:
    result = ForbiddenCommandResult(project_dir=project_dir.name, session_path=str(session_path))
    if not session_path.exists():
        result.valid_json = False
        result.error = "session file does not exist"
        add_finding(
            result,
            session_path=session_path,
            message_index=None,
            json_path="",
            token="",
            reason=result.error,
            content="",
        )
        return result

    try:
        payload = load_json_payload(session_path)
    except (OSError, json.JSONDecodeError) as exc:
        result.valid_json = False
        result.error = str(exc)
        add_finding(
            result,
            session_path=session_path,
            message_index=None,
            json_path="",
            token="",
            reason=result.error,
            content="",
        )
        return result

    seen: set[tuple[str, int | None, str]] = set()
    for json_path, value, message_index in iter_strings(payload):
        result.string_values_checked += 1
        for match in COMMAND_RE.finditer(value):
            token = match.group(1)
            key = (json_path, message_index, token.lower())
            if key in seen:
                continue
            seen.add(key)
            add_finding(
                result,
                session_path=session_path,
                message_index=message_index,
                json_path=json_path,
                token=token,
                reason=f"发现禁止 slash 命令 {token}。",
                content=value,
            )
    return result


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


def file_id_for_session(source_root: Path, project_id: str, session_path: Path) -> str:
    for base in (source_root / project_id, source_root):
        try:
            return session_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return session_path.name


def project_id_for_report(source_root: Path, results: list[ForbiddenCommandResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def message_at_index(session_path: Path, message_index: int | None) -> tuple[dict[str, Any] | None, int | None]:
    if message_index is None or message_index < 1:
        return None, None
    try:
        payload = load_json_payload(session_path)
    except (OSError, json.JSONDecodeError):
        return None, None
    messages = extract_messages(payload)
    if message_index > len(messages):
        return None, None
    lines = messages_element_lines(session_path)
    line = lines[message_index - 1] if message_index - 1 < len(lines) else None
    return messages[message_index - 1], line


def build_json_report(source_root: Path, results: list[ForbiddenCommandResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    token_counts = Counter[str]()
    tasks: list[dict[str, Any]] = []
    for result in results:
        for finding in result.findings:
            if finding.token:
                token_counts[finding.token] += 1
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
                    "token": finding.token,
                    "json_path": finding.json_path,
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
            "string_values_checked": sum(result.string_values_checked for result in results),
            "invalid_json_failed": sum(1 for result in results if not result.valid_json),
            "forbidden_tokens": len(token_counts),
        },
        "forbidden_commands": [
            {"token": token, "出现次数": count}
            for token, count in token_counts.most_common()
        ],
        "tasks": tasks,
    }


def check_forbidden_commands(find_path: Path | str, session_json_path: Path | str, output_dir: Path | str) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"scan path does not exist or is not a directory: {root_dir}")

    session_relative_path = Path(session_json_path)
    results: list[ForbiddenCommandResult] = []
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
            print(f"checking forbidden commands: {relative_name} ... ", end="", flush=True)
            result = check_one_file(project_dir, session_path)
            result.project_dir = project_dir.name
            print("pass" if result.all_pass else "fail")
            results.append(result)

    report_output_dir = Path(output_dir).expanduser().resolve()
    report_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_output_dir / REPORT_NAME
    passed = sum(1 for result in results if result.all_pass)
    failed = len(results) - passed
    if failed:
        report_path.write_text(
            json.dumps(build_json_report(root_dir, results), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nJSON report written: {report_path}")
    else:
        if report_path.exists():
            report_path.unlink()
        print("\nNo forbidden commands found; JSON report not generated.")
    print(f"scanned files: {len(results)}")
    print(f"passed: {passed}")
    print(f"failed: {failed}")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check forbidden slash commands in session JSON files.")
    parser.add_argument("-r", dest="find_path", type=Path, required=True, help="Root containing project subdirectories.")
    parser.add_argument("-path", dest="session_json_path", type=Path, required=True, help="Session file or session directory path relative to each project directory.")
    parser.add_argument("-o", dest="output_dir", type=Path, required=True, help=f"Report output directory. Generates {REPORT_NAME}.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        check_forbidden_commands(args.find_path, args.session_json_path, args.output_dir)
    except Exception as exc:  # noqa: BLE001 - CLI should print actionable failures.
        print(f"check_forbidden_commands failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

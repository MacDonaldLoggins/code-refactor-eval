#!/usr/bin/env python3
"""Validate canonical session JSON structure and message content."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPORT_NAME = "check_json_structure.json"
REPORT_TYPE = "check_json_structure_report"
SKIPPED_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
TIMESTAMP_ORDER_TOLERANCE = timedelta(seconds=1)


@dataclass(frozen=True)
class Finding:
    reason: str
    message_index: int | None = None


@dataclass
class SessionResult:
    project_dir: str
    session_path: str
    valid_json: bool = True
    findings: list[Finding] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return self.valid_json and not self.findings


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def has_tool_calls(message: dict[str, Any]) -> bool:
    tool_calls = message.get("tool_calls")
    return isinstance(tool_calls, list) and bool(tool_calls)


def participates_in_timestamp_order(message: dict[str, Any]) -> bool:
    """Return whether a message participates in chronological-order checks."""
    return message.get("role") in {"user", "assistant"} and message.get("agent") == "main"


def validate_payload(payload: Any) -> list[Finding]:
    findings: list[Finding] = []
    if not isinstance(payload, dict):
        return [Finding("顶层必须为 JSON object")]

    for field_name, expected_type in (("messages", list), ("tools", list), ("meta", dict)):
        value = payload.get(field_name)
        if field_name not in payload:
            findings.append(Finding(f"顶层缺少 {field_name} 字段"))
        elif not isinstance(value, expected_type):
            findings.append(Finding(f"顶层 {field_name} 必须为 {expected_type.__name__}"))

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return findings
    if not messages:
        findings.append(Finding("messages 不能为空"))
        return findings

    previous_timestamp: datetime | None = None
    previous_index: int | None = None
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            findings.append(Finding("messages 元素必须为 JSON object", index))
            continue

        content = message.get("content")
        if "content" not in message:
            findings.append(Finding("消息缺少 content 字段", index))
        elif not isinstance(content, str):
            findings.append(Finding("content 必须为字符串", index))
        elif not content.strip() and not (message.get("role") == "assistant" and has_tool_calls(message)):
            findings.append(Finding("content 不能为空；仅带非空 tool_calls 的 assistant 消息允许为空", index))

        if message.get("role") == "assistant":
            reasoning_content = message.get("reasoning_content")
            if not isinstance(reasoning_content, str) or not reasoning_content.strip():
                findings.append(Finding("assistant 消息的 reasoning_content 必须为非空字符串", index))

        metadata = message.get("_metadata")
        timestamp = metadata.get("timestamp") if isinstance(metadata, dict) else None
        parsed_timestamp = parse_timestamp(timestamp)
        if parsed_timestamp is None:
            findings.append(Finding("消息 _metadata.timestamp 必须为合法 ISO 8601 时间戳", index))
            continue
        if not participates_in_timestamp_order(message):
            continue
        if (
            previous_timestamp is not None
            and parsed_timestamp < previous_timestamp - TIMESTAMP_ORDER_TOLERANCE
        ):
            findings.append(
                Finding(
                    f"时间戳未按序排列（允许 1 秒容差）：第 {previous_index} 条为 "
                    f"{previous_timestamp.isoformat()}，当前第 {index} 条为 "
                    f"{parsed_timestamp.isoformat()}，当前早于上一条超过 1 秒",
                    index,
                )
            )
        previous_timestamp = parsed_timestamp
        previous_index = index
    return findings


def is_candidate_session_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() == ".json"
        and not path.name.startswith(".")
        and not path.name.endswith(".meta.json")
        and path.name != "agent_mappings.json"
        and not any(part in SKIPPED_DIR_NAMES for part in path.parts)
    )


def iter_session_paths(project_dir: Path, session_path_arg: Path) -> list[Path]:
    target = project_dir / session_path_arg
    if target.is_file():
        return [target] if is_candidate_session_file(target) else []
    if target.is_dir():
        return sorted((path for path in target.rglob("*") if is_candidate_session_file(path)), key=str)
    return [target]


def check_one_session(project_dir: Path, session_path: Path) -> SessionResult:
    result = SessionResult(project_dir=project_dir.name, session_path=str(session_path))
    if not session_path.is_file():
        result.valid_json = False
        result.findings.append(Finding("session 文件不存在"))
        return result
    try:
        payload = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.valid_json = False
        result.findings.append(Finding(f"无法解析 JSON：{exc}"))
        return result
    result.findings.extend(validate_payload(payload))
    return result


def file_id(source_root: Path, result: SessionResult) -> str:
    session_path = Path(result.session_path)
    for base in (source_root / result.project_dir, source_root):
        try:
            return session_path.relative_to(base).as_posix()
        except ValueError:
            pass
    return session_path.name


def build_report(source_root: Path, results: list[SessionResult]) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    for result in results:
        for finding in result.findings:
            tasks.append(
                {
                    "产物id": result.project_dir,
                    "文件id": file_id(source_root, result),
                    "原因": finding.reason,
                    "行号": finding.message_index or 1,
                    "content 原文": "",
                }
            )
    failed = sum(1 for result in results if not result.all_pass)
    return {
        "report_type": REPORT_TYPE,
        "source_root": str(source_root),
        "summary": {
            "files": len(results),
            "passed": len(results) - failed,
            "failed": failed,
            "issues": len(tasks),
            "invalid_json_failed": sum(1 for result in results if not result.valid_json),
        },
        "tasks": tasks,
    }


def json_structure(find_path: Path | str, session_json_path: Path | str, output_dir: Path | str) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.is_dir():
        raise FileNotFoundError(f"执行路径不存在或不是目录: {root_dir}")
    session_path_arg = Path(session_json_path)
    project_dirs = [root_dir] if (root_dir / session_path_arg).exists() else sorted(
        (path for path in root_dir.iterdir() if path.is_dir() and not path.name.startswith(".")), key=lambda path: path.name
    )
    results: list[SessionResult] = []
    for project_dir in project_dirs:
        session_paths = iter_session_paths(project_dir, session_path_arg)
        if not session_paths:
            results.append(check_one_session(project_dir, project_dir / session_path_arg))
            continue
        results.extend(check_one_session(project_dir, path) for path in session_paths)

    report_path = Path(output_dir).expanduser().resolve() / REPORT_NAME
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(build_report(root_dir, results), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查 session JSON 顶层结构、消息内容和时间戳顺序")
    parser.add_argument("-r", dest="find_path", type=Path, required=True)
    parser.add_argument("-path", dest="session_json_path", type=Path, required=True)
    parser.add_argument("-o", dest="output_dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    json_structure(args.find_path, args.session_json_path, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

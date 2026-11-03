#!/usr/bin/env python3
"""session.json 工具调用/工具响应对齐检查脚本。

检查内容：
  1. assistant 消息中的每个 tool_calls[].id 都必须有对应的 tool response。
  2. tool 消息中的每个 tool_call_id 都必须能找到对应的 tool call。
  3. 对齐时按 agent + tool_call_id 维度匹配，避免 main 与 subagent 互相污染。
  4. 按 main/subagent 和普通工具/subagent 工具拆分 8 类缺失场景。

使用方式：
  python check\\check_tools_call_result.py \
    -r <执行路径> \
    -path <session.json 相对路径> \
    -o <报告输出目录>

示例：
  python check\\check_tools_call_result.py \
    -r C:\\Users\\15837\\Desktop\\workspace\\sessions_root \
    -path sessions\\session.json \
    -o C:\\Users\\15837\\Desktop\\workspace\\reports

参数说明：
  -r
    执行路径。脚本会遍历该目录下的每个子文件夹，并把每个子文件夹视为一个项目。

  -path
    session.json 的相对路径。该路径是相对于 -r 下每个项目子文件夹的路径。
    例如传入 sessions\\session.json 时，会检查：
      <执行路径>\\项目A\\sessions\\session.json
      <执行路径>\\项目B\\sessions\\session.json

  -o
    报告输出目录。脚本会在该目录下生成 check_tools_call_result.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from trajectory_utils import discover_trajectory_group_files as discover_group_files
except ImportError:  # pragma: no cover
    from check.trajectory_utils import discover_trajectory_group_files as discover_group_files


REPORT_NAME = "check_tools_call_result.json"
REPORT_TYPE = "check_tools_call_result_report"
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
SUBAGENT_DIR_NAMES = {"subagent", "subagents"}
SUBAGENT_TOOL_NAMES = {"agent", "subagent", "spawn_agent", "spawnagent"}
ALIGN_RESPONSE_OK_MESSAGE = "工具 tool_calls 与 tool_call_id 已对齐"
ALIGN_RESPONSE_ERROR_MESSAGE = "工具 tool_calls 与 tool_call_id 未对齐"
SCENARIO_TITLES = {
    "main_tool_missing_response": "main agent 调工具：工具响应缺失",
    "main_subagent_missing_response": "main agent 调 subagent：工具响应缺失",
    "subagent_tool_missing_response": "subagent 调工具：工具响应缺失",
    "subagent_subagent_missing_response": "subagent 调 subagent：工具响应缺失",
    "main_tool_missing_call": "main agent 工具响应：工具调用缺失",
    "main_subagent_missing_call": "main agent subagent 响应：工具调用缺失",
    "subagent_tool_missing_call": "subagent 工具响应：工具调用缺失",
    "subagent_subagent_missing_call": "subagent subagent 响应：工具调用缺失",
}


@dataclass(frozen=True)
class Finding:
    scenario: str
    scenario_label: str
    message_index: int
    agent: str
    tool_call_id: str
    tool_name: str
    timestamp: str | None
    content_preview: str


@dataclass(frozen=True)
class CallRecord:
    message_index: int
    agent: str
    tool_call_id: str
    tool_name: str
    timestamp: str | None
    content_preview: str


@dataclass(frozen=True)
class ResponseRecord:
    message_index: int
    agent: str
    tool_call_id: str
    timestamp: str | None
    content_preview: str


@dataclass
class SessionToolResult:
    project_dir: str
    session_path: str
    valid_json: bool = True
    error: str = ""
    findings: list[Finding] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return self.valid_json and not self.findings


def load_messages(session_path: Path, default_agent: str | None = None) -> list[dict[str, Any]]:
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{session_path} 顶层不是 JSON object")

    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError(f"{session_path} 缺少 messages 数组")

    normalized_messages: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if default_agent and not isinstance(message.get("agent"), str):
            metadata = message.get("_metadata")
            has_subagent_id = isinstance(metadata, dict) and isinstance(metadata.get("subagent_id"), str)
            if not has_subagent_id:
                message = dict(message)
                message["agent"] = default_agent
        normalized_messages.append(message)
    return normalized_messages


def discover_trajectory_group_files(main_session_path: Path) -> list[Path]:
    return discover_group_files(
        main_session_path,
        is_candidate_session_file=is_candidate_session_file,
        is_main_session_file=is_main_session_file,
        subagent_dir_names=SUBAGENT_DIR_NAMES,
    )


def load_trajectory_group_messages(main_session_path: Path) -> tuple[list[dict[str, Any]], list[Path]]:
    group_files = discover_trajectory_group_files(main_session_path)

    messages: list[dict[str, Any]] = []
    for index, session_file in enumerate(group_files):
        default_agent = None if index == 0 else session_file.stem
        messages.extend(load_messages(session_file, default_agent=default_agent))
    return messages, group_files


def message_agent(message: dict[str, Any]) -> str:
    agent = message.get("agent")
    if isinstance(agent, str) and agent.strip():
        return agent.strip()

    metadata = message.get("_metadata")
    if isinstance(metadata, dict):
        subagent_id = metadata.get("subagent_id")
        if isinstance(subagent_id, str) and subagent_id.strip():
            return subagent_id.strip()

    return "main"


def content_preview(message: dict[str, Any], limit: int = 120) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        content = json.dumps(content, ensure_ascii=False)
    elif not isinstance(content, str):
        content = "" if content is None else str(content)

    normalized = " ".join(content.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def iter_tool_calls(message: dict[str, Any]) -> list[tuple[str, str]]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []

    normalized: list[tuple[str, str]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_call_id = tool_call.get("id")
        function = tool_call.get("function")
        tool_name = function.get("name") if isinstance(function, dict) else None
        if isinstance(tool_call_id, str) and tool_call_id and isinstance(tool_name, str) and tool_name:
            normalized.append((tool_call_id, tool_name))
    return normalized


def is_subagent_tool_name(tool_name: str) -> bool:
    return tool_name.strip().lower() in SUBAGENT_TOOL_NAMES


def build_missing_response_scenario(agent: str, tool_name: str) -> tuple[str, str]:
    if agent == "main":
        scenario = "main_subagent_missing_response" if is_subagent_tool_name(tool_name) else "main_tool_missing_response"
    else:
        scenario = "subagent_subagent_missing_response" if is_subagent_tool_name(tool_name) else "subagent_tool_missing_response"
    return scenario, SCENARIO_TITLES[scenario]


def build_missing_call_scenario(agent: str, tool_name: str | None) -> tuple[str, str]:
    if not tool_name:
        scenario = "main_tool_missing_call" if agent == "main" else "subagent_tool_missing_call"
        return scenario, SCENARIO_TITLES[scenario]

    if agent == "main":
        scenario = "main_subagent_missing_call" if is_subagent_tool_name(tool_name) else "main_tool_missing_call"
    else:
        scenario = "subagent_subagent_missing_call" if is_subagent_tool_name(tool_name) else "subagent_tool_missing_call"
    return scenario, SCENARIO_TITLES[scenario]


def collect_call_records(messages: list[dict[str, Any]]) -> list[CallRecord]:
    calls: list[CallRecord] = []
    for index, message in enumerate(messages, start=1):
        if message.get("role") != "assistant":
            continue

        metadata = message.get("_metadata")
        timestamp = metadata.get("timestamp") if isinstance(metadata, dict) and isinstance(metadata.get("timestamp"), str) else None

        for tool_call_id, tool_name in iter_tool_calls(message):
            calls.append(
                CallRecord(
                    message_index=index,
                    agent=message_agent(message),
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    timestamp=timestamp,
                    content_preview=content_preview(message),
                )
            )
    return calls


def collect_response_records(messages: list[dict[str, Any]]) -> list[ResponseRecord]:
    responses: list[ResponseRecord] = []
    for index, message in enumerate(messages, start=1):
        if message.get("role") != "tool":
            continue

        tool_call_id = message.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            continue

        metadata = message.get("_metadata")
        timestamp = metadata.get("timestamp") if isinstance(metadata, dict) and isinstance(metadata.get("timestamp"), str) else None
        responses.append(
            ResponseRecord(
                message_index=index,
                agent=message_agent(message),
                tool_call_id=tool_call_id,
                timestamp=timestamp,
                content_preview=content_preview(message),
            )
        )
    return responses


def infer_tool_name_by_id(calls: list[CallRecord]) -> dict[str, str | None]:
    tool_names_by_id: dict[str, set[str]] = defaultdict(set)
    for call in calls:
        tool_names_by_id[call.tool_call_id].add(call.tool_name)

    inferred: dict[str, str | None] = {}
    for tool_call_id, tool_names in tool_names_by_id.items():
        inferred[tool_call_id] = next(iter(tool_names)) if len(tool_names) == 1 else None
    return inferred


def collect_findings(messages: list[dict[str, Any]]) -> list[Finding]:
    calls = collect_call_records(messages)
    responses = collect_response_records(messages)
    tool_name_by_id = infer_tool_name_by_id(calls)

    calls_by_key: dict[tuple[str, str], list[CallRecord]] = defaultdict(list)
    responses_by_key: dict[tuple[str, str], list[ResponseRecord]] = defaultdict(list)
    for call in calls:
        calls_by_key[(call.agent, call.tool_call_id)].append(call)
    for response in responses:
        responses_by_key[(response.agent, response.tool_call_id)].append(response)

    findings: list[Finding] = []
    for key in sorted(set(calls_by_key) | set(responses_by_key)):
        key_calls = calls_by_key.get(key, [])
        key_responses = responses_by_key.get(key, [])
        paired_count = min(len(key_calls), len(key_responses))

        for call in key_calls[paired_count:]:
            scenario, scenario_label = build_missing_response_scenario(call.agent, call.tool_name)
            findings.append(
                Finding(
                    scenario=scenario,
                    scenario_label=scenario_label,
                    message_index=call.message_index,
                    agent=call.agent,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    timestamp=call.timestamp,
                    content_preview=call.content_preview,
                )
            )

        for response in key_responses[paired_count:]:
            tool_name = tool_name_by_id.get(response.tool_call_id)
            scenario, scenario_label = build_missing_call_scenario(response.agent, tool_name)
            findings.append(
                Finding(
                    scenario=scenario,
                    scenario_label=scenario_label,
                    message_index=response.message_index,
                    agent=response.agent,
                    tool_call_id=response.tool_call_id,
                    tool_name=tool_name or "",
                    timestamp=response.timestamp,
                    content_preview=response.content_preview,
                )
            )

    return sorted(findings, key=lambda item: (item.message_index, item.tool_call_id, item.scenario))


def check_one_session(project_dir: Path, session_path: Path) -> SessionToolResult:
    result = SessionToolResult(project_dir=project_dir.name, session_path=str(session_path))
    if not session_path.exists():
        result.valid_json = False
        result.error = "session.json 文件不存在"
        return result

    try:
        messages, group_files = load_trajectory_group_messages(session_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.valid_json = False
        result.error = str(exc)
        return result

    if len(group_files) > 1:
        result.session_path = "\n".join(str(path) for path in group_files)
    result.findings = collect_findings(messages)
    return result


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def primary_session_path(result: SessionToolResult) -> Path:
    first_path = result.session_path.splitlines()[0] if result.session_path.splitlines() else result.session_path
    return Path(first_path)


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
    if not message:
        return ""
    agent = message_agent(message)
    return "" if agent == "main" else agent


def content_original(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        if "tool_calls" in message:
            return json.dumps(message.get("tool_calls"), ensure_ascii=False)
        return ""
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def group_message_at_index(main_session_path: Path, message_index: int) -> tuple[Path, dict[str, Any] | None, int | None]:
    cursor = 0
    for index, session_file in enumerate(discover_trajectory_group_files(main_session_path)):
        default_agent = None if index == 0 else session_file.stem
        try:
            messages = load_messages(session_file, default_agent=default_agent)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if message_index <= cursor + len(messages):
            local_index = message_index - cursor
            message = messages[local_index - 1] if local_index > 0 else None
            lines = messages_element_lines(session_file)
            line = lines[local_index - 1] if local_index - 1 < len(lines) else None
            return session_file, message, line
        cursor += len(messages)
    return main_session_path, None, None


def file_id_for_session(source_root: Path, project_id: str, session_path: Path) -> str:
    for base in (source_root / project_id, source_root):
        try:
            return session_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return session_path.name


def project_id_for_report(source_root: Path, results: list[SessionToolResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def build_json_report(source_root: Path, results: list[SessionToolResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    failed = total - passed
    total_findings = sum(len(result.findings) for result in results)
    scenario_counts: dict[str, int] = {}
    tasks: list[dict[str, Any]] = []

    for result in results:
        main_session_path = primary_session_path(result)
        if result.error:
            tasks.append(
                {
                    "产物id": result.project_dir,
                    "文件id": file_id_for_session(source_root, result.project_dir, main_session_path),
                    "原因": result.error,
                    "行号": 1,
                    "role": "",
                    "agent": "",
                    "timestamp": "",
                    "工具id": "",
                    "subagent": "",
                    "content 原文": "",
                }
            )
        for finding in result.findings:
            scenario_counts[finding.scenario] = scenario_counts.get(finding.scenario, 0) + 1
            session_path, message, line = group_message_at_index(main_session_path, finding.message_index)
            reason = (
                f"{finding.scenario_label}；agent={finding.agent}；"
                f"tool={finding.tool_name or '未知'}；tool_call_id={finding.tool_call_id}"
            )
            tasks.append(
                {
                    "产物id": result.project_dir,
                    "文件id": file_id_for_session(source_root, result.project_dir, session_path),
                    "原因": reason,
                    "行号": line,
                    "role": str(message.get("role", "")) if message else "",
                    "agent": message_agent(message) if message else finding.agent,
                    "timestamp": message_timestamp(message) or (finding.timestamp or ""),
                    "工具id": message_tool_id(message) or finding.tool_call_id,
                    "subagent": message_subagent(message),
                    "content 原文": content_original(message) or finding.content_preview,
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
            "failed": failed,
            "issues": len(tasks),
            "tool_alignment_findings": total_findings,
            "invalid_json_failed": sum(1 for result in results if not result.valid_json),
            "scenario_counts": scenario_counts,
        },
        "tasks": tasks,
    }


def should_scan_project_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if path.name == "__pycache__":
        return False
    if path.name.startswith("."):
        return False
    return True


def is_candidate_session_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name.startswith("."):
        return False
    if path.name.endswith(".meta.json"):
        return False
    if path.suffix.lower() not in SESSION_FILE_SUFFIXES:
        return False
    if any(part in SKIPPED_SESSION_DIR_NAMES for part in path.parts):
        return False
    return True


def is_main_session_file(path: Path) -> bool:
    if not is_candidate_session_file(path):
        return False
    return not any(part in SUBAGENT_DIR_NAMES for part in path.parts)


def iter_session_paths(project_dir: Path, session_path_arg: Path) -> list[Path]:
    target = project_dir / session_path_arg
    if target.is_file():
        return [target] if is_main_session_file(target) else []
    if target.is_dir():
        return sorted(
            (path for path in target.rglob("*") if is_main_session_file(path)),
            key=lambda path: str(path.relative_to(project_dir)),
        )
    return [target]


def has_session_target(project_dir: Path, session_path_arg: Path) -> bool:
    return (project_dir / session_path_arg).exists()


def check_tools_call_result(find_path: Path | str, session_json_path: Path | str, output_dir: Path | str) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"执行路径不存在或不是目录: {root_dir}")

    session_relative_path = Path(session_json_path)
    report_output_dir = Path(output_dir).expanduser().resolve()

    results: list[SessionToolResult] = []
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
            print(f"检查: {project_dir.name} ... ", end="", flush=True)
            result = check_one_session(project_dir, missing_session_path)
            print("通过" if result.all_pass else "不通过")
            results.append(result)
            continue

        for session_path in session_paths:
            relative_session_path = session_path.relative_to(project_dir)
            result_name = f"{project_dir.name}/{relative_session_path.as_posix()}"
            print(f"检查: {result_name} ... ", end="", flush=True)
            result = check_one_session(project_dir, session_path)
            result.project_dir = project_dir.name
            print("通过" if result.all_pass else "不通过")
            results.append(result)

    report_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_output_dir / REPORT_NAME
    report_path.write_text(
        json.dumps(build_json_report(root_dir, results), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    passed = sum(1 for result in results if result.all_pass)
    print(f"\nJSON 报告已写入: {report_path}")
    print(f"扫描项目数: {len(results)}")
    print(f"通过数: {passed}")
    print(f"不通过数: {len(results) - passed}")

    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量检查 session.json 中工具调用和工具响应是否对齐")
    parser.add_argument(
        "-r",
        dest="find_path",
        type=Path,
        required=True,
        help="执行路径：遍历该目录下的每个子文件夹。",
    )
    parser.add_argument(
        "-path",
        dest="session_json_path",
        type=Path,
        required=True,
        help="每个项目子文件夹里的 session 文件或 session 目录相对路径。目录会递归搜索。",
    )
    parser.add_argument(
        "-o",
        dest="output_dir",
        type=Path,
        required=True,
        help=f"报告输出目录，生成 {REPORT_NAME}。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        check_tools_call_result(args.find_path, args.session_json_path, args.output_dir)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

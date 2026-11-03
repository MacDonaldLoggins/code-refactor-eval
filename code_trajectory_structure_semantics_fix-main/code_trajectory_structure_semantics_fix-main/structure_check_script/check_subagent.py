#!/usr/bin/env python3
r"""session.json subagent 来源映射检查脚本。

检查内容：
  1. 主 session 和 subagent session 中的 _metadata.agent 映射必须使用真实存在的 tool_calls[].id。
  2. _metadata.agent 映射指向的 subagent_id 必须能找到对应的 subagent JSON 文件。
  3. 每个 subagent JSON 文件都必须能被某个父级 _metadata.agent 映射引用。
  4. subagent JSON 文件内部的 agent / _metadata.subagent_id 必须与文件名对应。

说明：
  这个脚本检查的是 subagent 的“来源关系”，也就是父级 assistant 消息是否在
  _metadata.agent 中明确记录了 tool_call_id -> subagent_id 的调用映射。
  它不要求主 session 与 subagent 文件合并；脚本会自动读取主 session 同级的
  subagent/ 或 subagents/ 目录。

使用方式：
  python check\check_subagent.py \
    -r <执行路径> \
    -path <session.json 或 session 目录相对路径> \
    -o <报告输出目录>

示例：
  python check\check_subagent.py \
    -r C:\Users\15837\Desktop\workspace\sessions_root \
    -path sessions \
    -o C:\Users\15837\Desktop\workspace\reports

参数说明：
  -r
    执行路径。脚本会遍历该目录下的每个子文件夹，并把每个子文件夹视为一个 TASK。

  -path
    session 文件或 session 目录的相对路径。该路径相对于 -r 下的每个 TASK 子目录。
    如果传入目录，脚本会递归查找其中的主 session JSON，并自动跳过 subagent 文件。

  -o
    报告输出目录。脚本会在该目录下生成 check_subagent.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from trajectory_utils import discover_trajectory_group_files as discover_group_files
except ImportError:  # pragma: no cover
    from check.trajectory_utils import discover_trajectory_group_files as discover_group_files


REPORT_NAME = "check_subagent.json"
REPORT_TYPE = "check_subagent_report"
CONTENT_PREVIEW_LIMIT = 500
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
SUBAGENT_DIR_NAMES = {"subagent", "subagents"}

CHECK_TITLES = {
    "SUBAGENT-001": "_metadata.agent 映射键必须来自当前消息的 tool_calls[].id 或 tool_call_id",
    "SUBAGENT-002": "_metadata.agent 映射值必须存在对应 subagent 文件",
    "SUBAGENT-003": "每个 subagent 文件必须能追溯到父级 _metadata.agent 映射",
    "SUBAGENT-004": "subagent 文件内部身份必须与文件名一致",
    "SUBAGENT-005": "subagent 工具调用必须有对应的主 session 工具响应映射",
    "SUBAGENT-006": "subagent 工具响应必须能反查到主 session 工具调用映射",
}


@dataclass(frozen=True)
class LoadedMessage:
    source_file: Path
    local_index: int
    global_index: int
    is_subagent_file: bool
    file_agent_id: str | None
    session_agent_id: str | None
    message: dict[str, Any]


@dataclass(frozen=True)
class MappingRecord:
    source_file: Path
    message_index: int
    parent_agent: str
    role: str
    tool_call_id: str
    subagent_id: str
    canonical_subagent_id: str
    tool_call_exists: bool
    timestamp: str | None
    content_preview: str


@dataclass(frozen=True)
class SubagentFileRecord:
    path: Path
    file_agent_id: str
    canonical_agent_id: str
    session_agent_id: str | None


@dataclass(frozen=True)
class SubagentFinding:
    check_id: str
    check_name: str
    source_file: str
    message_index: int | None
    role: str
    agent: str
    tool_call_id: str
    expected: str
    actual: str
    timestamp: str | None
    content_preview: str
    reason: str


@dataclass
class SessionSubagentResult:
    project_dir: str
    session_path: str
    valid_json: bool = True
    error: str = ""
    group_files: list[Path] = field(default_factory=list)
    subagent_files: list[SubagentFileRecord] = field(default_factory=list)
    mappings: list[MappingRecord] = field(default_factory=list)
    findings: list[SubagentFinding] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return self.valid_json and not self.findings


def canonical_agent_id(agent_id: str) -> str:
    value = agent_id.strip()
    return value[6:] if value.startswith("agent-") else value


def subagent_id_from_path(path: Path) -> str:
    return canonical_agent_id(path.stem)


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


def is_subagent_session_file(path: Path) -> bool:
    return is_candidate_session_file(path) and any(part in SUBAGENT_DIR_NAMES for part in path.parts)


def discover_trajectory_group_files(main_session_path: Path) -> list[Path]:
    return discover_group_files(
        main_session_path,
        is_candidate_session_file=is_candidate_session_file,
        is_main_session_file=is_main_session_file,
        subagent_dir_names=SUBAGENT_DIR_NAMES,
    )


def load_json_payload(session_path: Path) -> dict[str, Any]:
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{session_path} 顶层不是 JSON object")
    return payload


def messages_from_payload(session_path: Path, payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError(f"{session_path} 缺少 messages 数组")

    return [message for message in messages if isinstance(message, dict)]


def session_meta_agent_id(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    session_meta = meta.get("session_meta") if isinstance(meta, dict) else None
    agent = session_meta.get("agent") if isinstance(session_meta, dict) else None
    return agent.strip() if isinstance(agent, str) and agent.strip() else None


def load_json_messages(session_path: Path) -> list[dict[str, Any]]:
    payload = load_json_payload(session_path)
    return messages_from_payload(session_path, payload)


def load_trajectory_group(main_session_path: Path) -> tuple[list[LoadedMessage], list[Path], list[SubagentFileRecord]]:
    group_files = discover_trajectory_group_files(main_session_path)
    loaded_messages: list[LoadedMessage] = []
    subagent_files: list[SubagentFileRecord] = []
    global_index = 0

    for session_file in group_files:
        payload = load_json_payload(session_file)
        messages = messages_from_payload(session_file, payload)
        is_subagent = is_subagent_session_file(session_file)
        file_agent_id = subagent_id_from_path(session_file) if is_subagent else None
        session_agent_id = session_meta_agent_id(payload) if is_subagent else None
        if is_subagent and file_agent_id:
            subagent_files.append(
                SubagentFileRecord(
                    path=session_file,
                    file_agent_id=session_file.stem,
                    canonical_agent_id=file_agent_id,
                    session_agent_id=session_agent_id,
                )
            )

        for local_index, message in enumerate(messages, start=1):
            global_index += 1
            loaded_messages.append(
                LoadedMessage(
                    source_file=session_file,
                    local_index=local_index,
                    global_index=global_index,
                    is_subagent_file=is_subagent,
                    file_agent_id=file_agent_id,
                    session_agent_id=session_agent_id,
                    message=message,
                )
            )

    return loaded_messages, group_files, subagent_files


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


def message_role(message: dict[str, Any]) -> str:
    role = message.get("role")
    return role if isinstance(role, str) else ""


def message_timestamp(message: dict[str, Any]) -> str | None:
    metadata = message.get("_metadata")
    timestamp = metadata.get("timestamp") if isinstance(metadata, dict) else None
    return timestamp if isinstance(timestamp, str) else None


def content_preview(message: dict[str, Any], limit: int = CONTENT_PREVIEW_LIMIT) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        content = json.dumps(content, ensure_ascii=False)
    elif not isinstance(content, str):
        content = "" if content is None else str(content)

    normalized = " ".join(content.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def tool_reference_ids(message: dict[str, Any]) -> set[str]:
    ids: set[str] = set()

    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id.strip():
        ids.add(tool_call_id.strip())

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return ids

    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_call_id = tool_call.get("id")
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            ids.add(tool_call_id.strip())
    return ids


def collect_mapping_records(messages: list[LoadedMessage]) -> list[MappingRecord]:
    records: list[MappingRecord] = []
    for loaded in messages:
        message = loaded.message
        metadata = message.get("_metadata")
        metadata_agents = metadata.get("agent") if isinstance(metadata, dict) else None
        if not isinstance(metadata_agents, dict):
            continue

        current_tool_reference_ids = tool_reference_ids(message)
        parent_agent = message_agent(message)
        role = message_role(message)
        for tool_call_id, subagent_id in metadata_agents.items():
            if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                continue
            if not isinstance(subagent_id, str) or not subagent_id.strip():
                continue
            if subagent_id.strip() == "main":
                continue

            records.append(
                MappingRecord(
                    source_file=loaded.source_file,
                    message_index=loaded.local_index,
                    parent_agent=parent_agent,
                    role=role,
                    tool_call_id=tool_call_id.strip(),
                    subagent_id=subagent_id.strip(),
                    canonical_subagent_id=canonical_agent_id(subagent_id),
                    tool_call_exists=tool_call_id.strip() in current_tool_reference_ids,
                    timestamp=message_timestamp(message),
                    content_preview=content_preview(message),
                )
            )
    return records


def finding(
    *,
    check_id: str,
    source_file: Path,
    message_index: int | None = None,
    role: str = "",
    agent: str = "",
    tool_call_id: str = "",
    expected: str = "",
    actual: str = "",
    timestamp: str | None = None,
    content_preview: str = "",
    reason: str,
) -> SubagentFinding:
    return SubagentFinding(
        check_id=check_id,
        check_name=CHECK_TITLES[check_id],
        source_file=str(source_file),
        message_index=message_index,
        role=role,
        agent=agent,
        tool_call_id=tool_call_id,
        expected=expected,
        actual=actual,
        timestamp=timestamp,
        content_preview=content_preview,
        reason=reason,
    )


def collect_findings(
    loaded_messages: list[LoadedMessage],
    subagent_files: list[SubagentFileRecord],
    mappings: list[MappingRecord],
) -> list[SubagentFinding]:
    findings: list[SubagentFinding] = []
    subagent_file_ids = {record.canonical_agent_id for record in subagent_files}
    mapped_subagent_ids = {record.canonical_subagent_id for record in mappings}

    for record in mappings:
        if not record.tool_call_exists:
            findings.append(
                finding(
                    check_id="SUBAGENT-001",
                    source_file=record.source_file,
                    message_index=record.message_index,
                    role=record.role,
                    agent=record.parent_agent,
                    tool_call_id=record.tool_call_id,
                    expected="当前消息 tool_calls[].id 或 tool_call_id 中存在该 ID",
                    actual="未找到",
                    timestamp=record.timestamp,
                    content_preview=record.content_preview,
                    reason="_metadata.agent 的 key 没有出现在同一条消息的 tool_calls[].id 或 tool_call_id 中。",
                )
            )

        if record.canonical_subagent_id not in subagent_file_ids:
            findings.append(
                finding(
                    check_id="SUBAGENT-002",
                    source_file=record.source_file,
                    message_index=record.message_index,
                    role=record.role,
                    agent=record.parent_agent,
                    tool_call_id=record.tool_call_id,
                    expected=f"subagents/agent-{record.canonical_subagent_id}.json 或同名 subagent 文件",
                    actual=record.subagent_id,
                    timestamp=record.timestamp,
                    content_preview=record.content_preview,
                    reason="_metadata.agent 指向的 subagent_id 没有对应的 subagent JSON 文件。",
                )
            )

    call_records = {
        (record.source_file, record.tool_call_id, record.canonical_subagent_id): record
        for record in mappings
        if record.role == "assistant"
    }
    response_records = {
        (record.source_file, record.tool_call_id, record.canonical_subagent_id): record
        for record in mappings
        if record.role == "tool"
    }

    for key, record in call_records.items():
        if key not in response_records:
            findings.append(
                finding(
                    check_id="SUBAGENT-005",
                    source_file=record.source_file,
                    message_index=record.message_index,
                    role=record.role,
                    agent=record.parent_agent,
                    tool_call_id=record.tool_call_id,
                    expected=f"同一主 session 内存在 tool 响应，并在 _metadata.agent 中映射到 {record.subagent_id}",
                    actual="未找到对应 tool 响应映射",
                    timestamp=record.timestamp,
                    content_preview=record.content_preview,
                    reason="assistant 发起 subagent 工具调用后，没有找到同一 tool_call_id 和 subagent_id 的 tool 响应映射。",
                )
            )

    for key, record in response_records.items():
        if key not in call_records:
            findings.append(
                finding(
                    check_id="SUBAGENT-006",
                    source_file=record.source_file,
                    message_index=record.message_index,
                    role=record.role,
                    agent=record.parent_agent,
                    tool_call_id=record.tool_call_id,
                    expected=f"同一主 session 内存在 assistant 工具调用，并在 _metadata.agent 中映射到 {record.subagent_id}",
                    actual="未找到对应 assistant 工具调用映射",
                    timestamp=record.timestamp,
                    content_preview=record.content_preview,
                    reason="tool 响应声明了 subagent 映射，但没有找到同一 tool_call_id 和 subagent_id 的 assistant 调用映射。",
                )
            )

    for subagent_file in subagent_files:
        if subagent_file.canonical_agent_id not in mapped_subagent_ids:
            findings.append(
                finding(
                    check_id="SUBAGENT-003",
                    source_file=subagent_file.path,
                    expected="至少一个父级 _metadata.agent 映射引用该 subagent",
                    actual=subagent_file.file_agent_id,
                    reason="发现 subagent 文件，但没有任何父级 _metadata.agent 映射指向它。",
                )
            )

        if subagent_file.session_agent_id and canonical_agent_id(subagent_file.session_agent_id) != subagent_file.canonical_agent_id:
            findings.append(
                finding(
                    check_id="SUBAGENT-004",
                    source_file=subagent_file.path,
                    expected=subagent_file.file_agent_id,
                    actual=subagent_file.session_agent_id,
                    reason="文件顶层 meta.session_meta.agent 与 subagent 文件名不一致。",
                )
            )

    for loaded in loaded_messages:
        if not loaded.is_subagent_file or not loaded.file_agent_id:
            continue

        message = loaded.message
        expected = loaded.file_agent_id
        role = message_role(message)
        timestamp = message_timestamp(message)
        preview = content_preview(message)

        agent = message.get("agent")
        if isinstance(agent, str) and agent.strip():
            if canonical_agent_id(agent) != loaded.file_agent_id:
                findings.append(
                    finding(
                        check_id="SUBAGENT-004",
                        source_file=loaded.source_file,
                        message_index=loaded.local_index,
                        role=role,
                        agent=agent,
                        expected=expected,
                        actual=agent,
                        timestamp=timestamp,
                        content_preview=preview,
                        reason="消息 agent 与 subagent 文件名不一致。",
                    )
                )

        metadata = message.get("_metadata")
        metadata_subagent_id = metadata.get("subagent_id") if isinstance(metadata, dict) else None
        if isinstance(metadata_subagent_id, str) and metadata_subagent_id.strip():
            if canonical_agent_id(metadata_subagent_id) != loaded.file_agent_id:
                findings.append(
                    finding(
                        check_id="SUBAGENT-004",
                        source_file=loaded.source_file,
                        message_index=loaded.local_index,
                        role=role,
                        agent=message_agent(message),
                        expected=expected,
                        actual=metadata_subagent_id,
                        timestamp=timestamp,
                        content_preview=preview,
                        reason="消息 _metadata.subagent_id 与 subagent 文件名不一致。",
                    )
                )
        elif not isinstance(agent, str) or not agent.strip():
            if role != "system":
                findings.append(
                    finding(
                        check_id="SUBAGENT-004",
                        source_file=loaded.source_file,
                        message_index=loaded.local_index,
                        role=role,
                        expected=expected,
                        actual="缺少 agent 和 _metadata.subagent_id",
                        timestamp=timestamp,
                        content_preview=preview,
                        reason="subagent 非 system 消息缺少可用于识别身份的 agent 或 _metadata.subagent_id。",
                    )
                )

    return sorted(
        findings,
        key=lambda item: (
            item.check_id,
            item.source_file,
            item.message_index if item.message_index is not None else -1,
            item.tool_call_id,
        ),
    )


def check_one_session(project_dir: Path, session_path: Path) -> SessionSubagentResult:
    result = SessionSubagentResult(project_dir=project_dir.name, session_path=str(session_path))
    if not session_path.exists():
        result.valid_json = False
        result.error = "session 文件不存在"
        return result

    try:
        loaded_messages, group_files, subagent_files = load_trajectory_group(session_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.valid_json = False
        result.error = str(exc)
        return result

    result.group_files = group_files
    result.subagent_files = subagent_files
    result.mappings = collect_mapping_records(loaded_messages)
    result.findings = collect_findings(loaded_messages, subagent_files, result.mappings)
    if len(group_files) > 1:
        result.session_path = "\n".join(str(path) for path in group_files)
    return result


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def primary_session_path(result: SessionSubagentResult) -> Path:
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


def message_at_index(session_path: Path, message_index: int | None) -> tuple[dict[str, Any] | None, int | None]:
    if message_index is None or message_index < 1:
        return None, None
    try:
        messages = load_json_messages(session_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None, None
    if message_index > len(messages):
        return None, None
    lines = messages_element_lines(session_path)
    line = lines[message_index - 1] if message_index - 1 < len(lines) else None
    return messages[message_index - 1], line


def message_line_range(session_path: Path, message_index: int | None) -> tuple[int | None, int | None]:
    if message_index is None or message_index < 1:
        return None, None
    starts = messages_element_lines(session_path)
    if message_index > len(starts):
        return None, None
    try:
        total_lines = len(session_path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return starts[message_index - 1], None
    start = starts[message_index - 1]
    end = starts[message_index] - 1 if message_index < len(starts) else total_lines
    return start, end


def find_line_containing(session_path: Path, needles: list[str], start: int | None = None, end: int | None = None) -> int | None:
    try:
        lines = session_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    first = max(start or 1, 1)
    last = min(end or len(lines), len(lines))
    for line_number in range(first, last + 1):
        line = lines[line_number - 1]
        if all(needle in line for needle in needles):
            return line_number
    return None


def find_json_field_line(
    session_path: Path,
    field_name: str,
    value: str,
    start: int | None = None,
    end: int | None = None,
) -> int | None:
    return find_line_containing(
        session_path,
        [json.dumps(field_name, ensure_ascii=False), json.dumps(value, ensure_ascii=False)],
        start,
        end,
    )


def find_agent_mapping_line(
    session_path: Path,
    tool_call_id: str,
    subagent_id: str,
    start: int | None = None,
    end: int | None = None,
) -> int | None:
    return find_line_containing(
        session_path,
        [json.dumps(tool_call_id, ensure_ascii=False), json.dumps(subagent_id, ensure_ascii=False)],
        start,
        end,
    )


def find_session_meta_agent_line(session_path: Path, agent_id: str) -> int | None:
    try:
        lines = session_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    session_meta_line = None
    for index, line in enumerate(lines, start=1):
        if '"session_meta"' in line:
            session_meta_line = index
            break
    if session_meta_line is not None:
        line = find_json_field_line(session_path, "agent", agent_id, session_meta_line, min(session_meta_line + 30, len(lines)))
        if line is not None:
            return line
    return find_json_field_line(session_path, "agent", agent_id)


def finding_line_number(session_path: Path, item: SubagentFinding, fallback_line: int | None) -> int | None:
    start, end = message_line_range(session_path, item.message_index)

    if item.tool_call_id and item.actual and not item.actual.startswith("未找到"):
        line = find_agent_mapping_line(session_path, item.tool_call_id, item.actual, start, end)
        if line is not None:
            return line

    if item.tool_call_id:
        for field_name in ("tool_call_id", "id"):
            line = find_json_field_line(session_path, field_name, item.tool_call_id, start, end)
            if line is not None:
                return line

    if item.check_id == "SUBAGENT-004":
        if item.message_index is None and item.actual:
            line = find_session_meta_agent_line(session_path, item.actual)
            if line is not None:
                return line

        if item.actual and not item.actual.startswith("缺少"):
            for field_name in ("agent", "subagent_id"):
                line = find_json_field_line(session_path, field_name, item.actual, start, end)
                if line is not None:
                    return line

        if item.agent:
            line = find_json_field_line(session_path, "agent", item.agent, start, end)
            if line is not None:
                return line

    return fallback_line


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


def file_id_for_session(source_root: Path, project_id: str, session_path: Path) -> str:
    for base in (source_root / project_id, source_root):
        try:
            return session_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return session_path.name


def project_id_for_report(source_root: Path, results: list[SessionSubagentResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def build_json_report(source_root: Path, results: list[SessionSubagentResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    failed = total - passed
    total_findings = sum(len(result.findings) for result in results)
    check_counts: dict[str, int] = {}
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
        for item in result.findings:
            check_counts[item.check_id] = check_counts.get(item.check_id, 0) + 1
            source_file = Path(item.source_file)
            message, line = message_at_index(source_file, item.message_index)
            line = finding_line_number(source_file, item, line)
            reason = f"{item.check_id} {item.reason}"
            tasks.append(
                {
                    "产物id": result.project_dir,
                    "文件id": file_id_for_session(source_root, result.project_dir, source_file),
                    "原因": reason,
                    "行号": line,
                    "role": str(message.get("role", item.role)) if message else item.role,
                    "agent": message_agent(message) if message else item.agent,
                    "timestamp": message_timestamp(message) or (item.timestamp or "") if message else (item.timestamp or ""),
                    "工具id": message_tool_id(message) or item.tool_call_id,
                    "subagent": message_subagent(message),
                    "content 原文": content_original(message) or item.content_preview,
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
            "subagent_findings": total_findings,
            "invalid_json_failed": sum(1 for result in results if not result.valid_json),
            "check_counts": check_counts,
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


def check_subagent(find_path: Path | str, session_json_path: Path | str, output_dir: Path | str) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"执行路径不存在或不是目录: {root_dir}")

    session_relative_path = Path(session_json_path)
    report_output_dir = Path(output_dir).expanduser().resolve()

    results: list[SessionSubagentResult] = []
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
            print(f"检查 {project_dir.name} ... ", end="", flush=True)
            result = check_one_session(project_dir, missing_session_path)
            print("通过" if result.all_pass else "不通过")
            results.append(result)
            continue

        for session_path in session_paths:
            relative_session_path = session_path.relative_to(project_dir)
            result_name = f"{project_dir.name}/{relative_session_path.as_posix()}"
            print(f"检查 {result_name} ... ", end="", flush=True)
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
    print(f"扫描对象数: {len(results)}")
    print(f"通过数: {passed}")
    print(f"不通过数: {len(results) - passed}")

    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量检查 session.json 中 subagent 是否能从 metadata 映射追溯来源")
    parser.add_argument(
        "-r",
        dest="find_path",
        type=Path,
        required=True,
        help="执行路径：遍历该目录下的每个 TASK 子文件夹。",
    )
    parser.add_argument(
        "-path",
        dest="session_json_path",
        type=Path,
        required=True,
        help="每个 TASK 子文件夹里的 session 文件或 session 目录相对路径。目录会递归搜索。",
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
        check_subagent(args.find_path, args.session_json_path, args.output_dir)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


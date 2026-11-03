#!/usr/bin/env python3
"""session.json OpenAI Chat Completions 规范检查脚本。

检查内容：
  1. 文件格式须为标准 JSON，且顶层包含 messages 数组。
  2. 每条 message 的 role、content、tool_calls 等字段须符合 OpenAI Chat Completions 格式。
  3. 对话顺序须符合规范，例如 system 只能出现在最前面，不能连续出现 user。
  4. tool call 与 tool response 的 id、数量和位置须符合规范。
  5. 支持 subagent 场景下的 agent 维度规则检查，避免不同 agent 的工具调用互相污染。

使用方式：
  python check_openaI_chat_completions.py \
    -r <执行路径> \
    -path <session.json 相对路径> \
    -o <报告输出目录>

示例：
  python check_openaI_chat_completions.py \
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
    报告输出目录。脚本会在该目录下生成 openaI_chat_completions_report.json。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path("/Users/qijian/Desktop/workspace/质检项目/pass_products/transfer")
REPORT_NAME = "openaI_chat_completions_report.json"
REPORT_TYPE = "check_openaI_chat_completions_report"
OPENAI_CHAT_COMPLETIONS_SWITCH = True
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
SESSION_TYPE_LABELS = {"develop", "test", "bugfix"}
MISSING = object()
ALLOWED_ROLES = {"system", "user", "assistant", "tool"}
SUBAGENT_TOOL_NAMES = {"agent", "subagent", "spawn_agent", "spawnagent"}
COMMAND_MARKERS = ("<command-message>", "<command-name>", "<command-args>")
is_command = False

_API_ERROR_WITH_STATUS_RE = re.compile(r"\bapi[\s_-]?error\b\s*[:：]?\s*(4\d{2}|5\d{2})\b", re.IGNORECASE)
_REMOTE_ERROR_HINTS = (
    "request id", "failed to authenticate", "token has no access",
    "no access to model", "channel has been disabled", "bad response status code",
    "oneapi", "new-api", "无权访问模型",
)
_REMOTE_ERROR_HINTS_SINGLE = (
    "no response requested.",
    "[request interrupted by user]",
    "[request interrupted by user for tool use]"
)

@dataclass(frozen=True)
class RuleDefinition:
    number: int
    title: str


@dataclass(frozen=True)
class Issue:
    file_name: str
    line: int | None
    reason: str


@dataclass
class RuleResult:
    definition: RuleDefinition
    issues: list[Issue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues

    @property
    def status(self) -> str:
        return "正确" if self.passed else "错误"


@dataclass(frozen=True)
class ParsedToolCall:
    assistant_index: int
    line: int
    raw_index: int
    tool_call_id: str | None


@dataclass(frozen=True)
class MessageInfo:
    index: int
    line: int
    raw: Any
    role: str | None
    content: Any
    has_content: bool
    tool_calls: Any
    has_tool_calls: bool


@dataclass
class SessionOpenAIResult:
    project_dir: str
    session_path: str
    results: dict[int, RuleResult]

    @property
    def all_pass(self) -> bool:
        return all(self.results[number].passed for number in OPENAI_REPORT_RULE_NUMBERS)


RULE_DEFINITIONS = [
    RuleDefinition(1, "文件必须是合法 JSON"),
    RuleDefinition(2, "`messages` 字段必须是数组"),
    RuleDefinition(3, "`messages` 数组不能为空"),
    RuleDefinition(4, "每条 message 必须含 role 字段且类型为 string"),
    RuleDefinition(5, "role 取值只能是 system/user/assistant/tool"),
    RuleDefinition(6, "每条 message 必须含 content 或 tool_calls"),
    RuleDefinition(7, "content 类型必须为 string/null/数组"),
    RuleDefinition(8, "第一条消息必须是 system 或 user"),
    RuleDefinition(9, "system 消息只能出现在最前面"),
    RuleDefinition(10, "tool 消息前必须有对应的 assistant + tool_calls"),
    RuleDefinition(11, "不能出现连续的 user 消息"),
    RuleDefinition(13, "tool_calls 必须是非空数组"),
    RuleDefinition(14, "每个 tool_call 必须有非空字符串 id"),
    RuleDefinition(15, "tool_call.type 必须为 function"),
    RuleDefinition(16, "tool_call 必须含 function.name 和 function.arguments"),
    RuleDefinition(17, "function.name 必须是非空字符串"),
    RuleDefinition(18, "function.arguments 必须是字符串"),
    RuleDefinition(19, "function.arguments 必须是合法 JSON 字符串"),
    RuleDefinition(20, "同一 agent 范围内 tool_call.id 必须唯一"),
    RuleDefinition(21, "tool 消息必须含 string 类型的 tool_call_id"),
    RuleDefinition(22, "tool_call_id 必须指向已存在的 tool_call"),
    RuleDefinition(23, "tool 消息必须含 string 类型的 content"),
    RuleDefinition(24, "tool 消息不能包含 tool_calls"),
    RuleDefinition(25, "同一 agent 范围内每个 tool_call 必须有且仅有一个对应 tool result"),
    RuleDefinition(26, "tool result 不能孤立存在"),
    RuleDefinition(27, "tool results 必须紧跟对应 assistant 消息之后"),
    RuleDefinition(28, "assistant 的多个 tool_calls 的 results 必须全部给出"),
    RuleDefinition(30, "content 为数组时每个 part 必须含 type"),
    RuleDefinition(31, "type=text 时必须含 string 类型的 text"),
    RuleDefinition(32, "type=image_url 时必须含 image_url.url"),
    RuleDefinition(33, "content parts 数组不能为空"),
    RuleDefinition(34, "system 和 tool 消息的 content 不能是数组"),
    RuleDefinition(35, "name 字段如存在必须是字符串"),
    RuleDefinition(36, "不应出现 function_call 字段"),
    RuleDefinition(37, "assistant.refusal 如存在必须是 string 或 null"),
    RuleDefinition(38, "对话不能以 tool 消息结尾"),
    RuleDefinition(39, "对话不能以未处理的 tool_calls 结尾"),
    RuleDefinition(40, "assistant 不能同时 content 和 tool_calls 都为空"),
    RuleDefinition(41, "不应有未识别的 role 值"),
    RuleDefinition(42, "不应有空对象或非对象 message"),
    RuleDefinition(43, "tool_calls 不能出现在非 assistant 消息上"),
    RuleDefinition(44, "JSON 编码必须正确且不能带 BOM"),
    RuleDefinition(45, "同一 agent 范围内同一 tool_call_id 不能被多条 tool 消息引用"),
    RuleDefinition(46, "同一 assistant 消息内各 tool_call.id 必须互不相同"),
    RuleDefinition(47, "无参数函数 arguments 应为 {} 而不是空字符串或 null"),
    RuleDefinition(48, "arguments 解析后必须是 JSON Object"),
    RuleDefinition(49, "tool result 块数量必须等于前一条 assistant 的 tool_calls 数量"),
    RuleDefinition(50, "重复声明的 tool_call.id 后续缺少对应 tool result"),
]


DEFINED_RULE_NUMBERS = {definition.number for definition in RULE_DEFINITIONS}
# 规则 20 只报告重复 id 本身；规则 50 会定位重复声明后缺少新 tool result 的具体异常形态。
# 规则 25/45/49 是 tool result 数量/重复引用规则，当前由 20/27/28/50 等更关键规则覆盖。
# 规则 38/40 按当前清洗后轨迹要求关闭。
OPENAI_AGENT_SCOPED_RULES = {20, 25, 45, 50}
OPENAI_DISABLED_RULES = {20, 25, 38, 40, 45, 49}
OPENAI_REPORT_RULE_NUMBERS = DEFINED_RULE_NUMBERS - OPENAI_DISABLED_RULES


def add_issue(results: dict[int, RuleResult], file_name: str, rule_number: int, line: int | None, reason: str) -> None:
    issue = Issue(file_name=file_name, line=line, reason=reason)
    if issue not in results[rule_number].issues:
        results[rule_number].issues.append(issue)


def build_results() -> dict[int, RuleResult]:
    return {definition.number: RuleResult(definition=definition) for definition in RULE_DEFINITIONS}


def is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and value != ""


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


def message_info_agent(info: MessageInfo) -> str:
    if isinstance(info.raw, dict):
        return message_agent(info.raw)
    return "main"


def is_subagent_tool_name(tool_name: str) -> bool:
    return tool_name.strip().lower() in SUBAGENT_TOOL_NAMES


def contains_command_marker(value: Any) -> bool:
    if isinstance(value, str):
        return any(marker in value for marker in COMMAND_MARKERS)
    if isinstance(value, list):
        return any(contains_command_marker(item) for item in value)
    if isinstance(value, dict):
        return any(contains_command_marker(item) for item in value.values())
    return False


def is_command_user_message(info: MessageInfo) -> bool:
    return is_command and info.role == "user" and contains_command_marker(info.content)


def convert_session_name(name: str) -> str:
    suffix = ".json" if name.lower().endswith(".json") else ""
    stem = name[:-5] if suffix else name
    parts = stem.split("-", 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[1] in {"develop", "test", "bugfix"}:
        return f"{parts[0]}-{parts[2]}{suffix}"
    return name


def find_project_root_with_convert_sessions(input_path: Path) -> Path | None:
    for parent in input_path.parents:
        if (parent / "convert-sessions").is_dir():
            return parent
    return None


def relative_parts_after_session_container(input_path: Path, project_root: Path) -> tuple[str, tuple[str, ...]] | None:
    try:
        rel_parts = input_path.relative_to(project_root).parts
    except ValueError:
        return None
    for container in ("sessions", "data_success"):
        if container in rel_parts:
            index = rel_parts.index(container)
            after = tuple(rel_parts[index + 1 :])
            if after:
                return container, after
    return None


def find_convert_session_path(input_path: Path) -> Path | None:
    project_root = find_project_root_with_convert_sessions(input_path)
    if project_root is None:
        return None
    located = relative_parts_after_session_container(input_path, project_root)
    if located is None:
        return None
    _container, after = located
    convert_root = project_root / "convert-sessions"

    if "subagents" in after:
        subagent_index = after.index("subagents")
        if subagent_index == 0:
            return None
        main_session_name = after[0]
        agent_name = after[-1]
        candidate = convert_root / convert_session_name(main_session_name) / "subagents" / agent_name
        if candidate.is_file():
            return candidate
    else:
        candidate = convert_root / convert_session_name(after[-1])
        if candidate.is_file():
            return candidate

    for candidate in convert_root.rglob(input_path.name):
        if candidate.is_file():
            return candidate
    return None


def load_convert_messages(convert_path: Path) -> list[dict] | None:
    try:
        text = convert_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list):
            return messages if messages else None
    return None


def check_convert_has_assistant_between(
    messages: list[dict], prev_ts: str, curr_ts: str,
) -> tuple[bool, str]:
    prev_idx: int | None = None
    curr_idx: int | None = None
    for i, msg in enumerate(messages):
        ts = message_timestamp(msg)
        if ts == prev_ts and prev_idx is None:
            prev_idx = i
        elif ts == curr_ts and curr_idx is None and prev_idx is not None:
            curr_idx = i
            break

    if prev_idx is None or curr_idx is None:
        return True, "未在convert-sessions中找到对应时间戳"

    between = messages[prev_idx + 1 : curr_idx]
    assistant_msgs = [m for m in between if m.get("role") == "assistant"]
    if not assistant_msgs:
        return False, "convert-sessions两条user之间不存在assistant消息"

    for msg in assistant_msgs:
        if msg.get('content'):
            text = content_original(msg).lower()
            has_status = bool(_API_ERROR_WITH_STATUS_RE.search(text))
            has_hint = any(hint in text for hint in _REMOTE_ERROR_HINTS)
            is_hint_single = any(hint == text for hint in _REMOTE_ERROR_HINTS_SINGLE)
            if not (has_status and has_hint ):
                if is_hint_single:
                    continue
                return True, "convert-sessions两条user之间存在assistant消息"
    return False, "convert-sessions两条user之间的assistant消息均为API错误"


def decode_text(input_path: Path, results: dict[int, RuleResult]) -> str | None:
    raw_bytes = input_path.read_bytes()
    file_name = input_path.name

    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        add_issue(results, file_name, 44, 1, "文件含 UTF-8 BOM，OpenAI 格式文件不应携带 BOM。")

    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        line = raw_bytes[: error.start].count(b"\n") + 1
        add_issue(results, file_name, 44, line, f"文件不是合法 UTF-8 编码：{error.reason}。")
        add_issue(results, file_name, 1, line, "文件无法按 UTF-8 文本读取，不能继续解析 JSON。")
        return None


def top_level_element_lines(text: str) -> list[int]:
    lines: list[int] = []
    array_depth = 0
    object_depth = 0
    line = 1
    in_string = False
    escape = False
    expecting_value = False

    for char in text:
        if array_depth == 1 and object_depth == 0 and expecting_value:
            if char in " \t\r\n":
                pass
            elif char == "]":
                expecting_value = False
            else:
                lines.append(line)
                expecting_value = False

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "[":
                array_depth += 1
                if array_depth == 1:
                    expecting_value = True
            elif char == "]":
                array_depth = max(0, array_depth - 1)
            elif char == "{":
                object_depth += 1
            elif char == "}":
                object_depth = max(0, object_depth - 1)
            elif char == "," and array_depth == 1 and object_depth == 0:
                expecting_value = True

        if char == "\n":
            line += 1

    return lines


def find_messages_field_line(text: str) -> int:
    match = re.search(r'"messages"\s*:', text)
    if not match:
        return 1
    return text.count("\n", 0, match.start()) + 1


def messages_element_lines(text: str) -> list[int]:
    match = re.search(r'"messages"\s*:', text)
    if not match:
        return []

    array_start = text.find("[", match.end())
    if array_start == -1:
        return []

    relative_lines = top_level_element_lines(text[array_start:])
    line_offset = text.count("\n", 0, array_start)
    return [line_offset + line for line in relative_lines]


def normalize_message_infos(payload: list[Any], text: str) -> list[MessageInfo]:
    element_lines = messages_element_lines(text)
    infos: list[MessageInfo] = []

    for index, raw in enumerate(payload, start=1):
        line = element_lines[index - 1] if index - 1 < len(element_lines) else 1
        if isinstance(raw, dict):
            role = raw.get("role") if isinstance(raw.get("role"), str) else None
            has_content = "content" in raw
            has_tool_calls = "tool_calls" in raw
            content = raw.get("content", MISSING)
            tool_calls = raw.get("tool_calls", MISSING)
        else:
            role = None
            has_content = False
            has_tool_calls = False
            content = MISSING
            tool_calls = MISSING

        infos.append(
            MessageInfo(
                index=index,
                line=line,
                raw=raw,
                role=role,
                content=content,
                has_content=has_content,
                tool_calls=tool_calls,
                has_tool_calls=has_tool_calls,
            )
        )

    return infos


def validate_message_fields(infos: list[MessageInfo], results: dict[int, RuleResult], file_name: str) -> tuple[list[ParsedToolCall], dict[str, list[ParsedToolCall]], dict[str, list[MessageInfo]]]:
    parsed_tool_calls: list[ParsedToolCall] = []
    tool_calls_by_id: dict[str, list[ParsedToolCall]] = {}
    tool_messages_by_id: dict[str, list[MessageInfo]] = {}

    for info in infos:
        raw = info.raw
        if not isinstance(raw, dict):
            add_issue(results, file_name, 42, info.line, "`messages` 数组元素不是 message 对象。")
            add_issue(results, file_name, 4, info.line, "message 不是对象，无法提供 role 字段。")
            add_issue(results, file_name, 6, info.line, "message 不是对象，无法提供 content 或 tool_calls。")
            continue

        if raw == {}:
            add_issue(results, file_name, 42, info.line, "message 为空对象 {}。")

        if "role" not in raw or not isinstance(raw.get("role"), str):
            add_issue(results, file_name, 4, info.line, "message 缺少 role 字段或 role 不是字符串。")
        elif raw["role"] not in ALLOWED_ROLES:
            add_issue(results, file_name, 5, info.line, f"role 值 `{raw['role']}` 不在允许范围内。")
            add_issue(results, file_name, 41, info.line, f"检测到未识别 role 值 `{raw['role']}`。")

        if "name" in raw and not isinstance(raw.get("name"), str):
            add_issue(results, file_name, 35, info.line, "name 字段存在但不是字符串。")

        if "function_call" in raw:
            add_issue(results, file_name, 36, info.line, "检测到已废弃字段 function_call，应改用 tool_calls。")

        if info.role == "assistant" and "refusal" in raw and raw.get("refusal") is not None and not isinstance(raw.get("refusal"), str):
            add_issue(results, file_name, 37, info.line, "assistant.refusal 存在但不是 string 或 null。")

        has_content_or_tool_calls = info.has_content or info.has_tool_calls
        if not has_content_or_tool_calls:
            add_issue(results, file_name, 6, info.line, "message 同时缺少 content 和 tool_calls。")

        if info.has_content:
            if not isinstance(info.content, (str, list)) and info.content is not None:
                add_issue(results, file_name, 7, info.line, "content 既不是 string，也不是 null 或数组。")
            if isinstance(info.content, list):
                if info.role in {"system", "tool"}:
                    add_issue(results, file_name, 34, info.line, f"{info.role} 消息的 content 不能是数组。")
                if not info.content:
                    add_issue(results, file_name, 33, info.line, "content parts 数组不能为空。")
                for part in info.content:
                    if not isinstance(part, dict):
                        add_issue(results, file_name, 30, info.line, "content part 不是对象，缺少 type 字段。")
                        continue
                    if "type" not in part:
                        add_issue(results, file_name, 30, info.line, "content part 缺少 type 字段。")
                        continue
                    part_type = part.get("type")
                    if part_type == "text":
                        if not isinstance(part.get("text"), str):
                            add_issue(results, file_name, 31, info.line, "type=text 的 content part 缺少字符串 text。")
                    if part_type == "image_url":
                        image_url = part.get("image_url")
                        if not isinstance(image_url, dict) or not isinstance(image_url.get("url"), str):
                            add_issue(results, file_name, 32, info.line, "type=image_url 的 content part 缺少 image_url.url。")

        if info.has_tool_calls and info.role != "assistant":
            add_issue(results, file_name, 43, info.line, f"{info.role or '未知'} 消息不允许携带 tool_calls。")
            if info.role == "tool":
                add_issue(results, file_name, 24, info.line, "tool 消息不能包含 tool_calls 字段。")

        if info.role == "assistant" and info.has_tool_calls:
            if not isinstance(info.tool_calls, list) or not info.tool_calls:
                add_issue(results, file_name, 13, info.line, "assistant.tool_calls 必须是非空数组。")
            if isinstance(info.tool_calls, list):
                current_ids: list[str] = []
                for raw_index, tool_call in enumerate(info.tool_calls, start=1):
                    tool_call_id: str | None = None
                    if not isinstance(tool_call, dict):
                        add_issue(results, file_name, 14, info.line, "tool_call 不是对象，缺少合法 id。")
                        add_issue(results, file_name, 16, info.line, "tool_call 不是对象，缺少 function.name 和 function.arguments。")
                        continue

                    if not is_non_empty_string(tool_call.get("id")):
                        add_issue(results, file_name, 14, info.line, "tool_call.id 缺失、为空或不是字符串。")
                    else:
                        tool_call_id = tool_call["id"]
                        current_ids.append(tool_call_id)
                        parsed_call = ParsedToolCall(
                            assistant_index=info.index,
                            line=info.line,
                            raw_index=raw_index,
                            tool_call_id=tool_call_id,
                        )
                        parsed_tool_calls.append(parsed_call)
                        tool_calls_by_id.setdefault(tool_call_id, []).append(parsed_call)

                    if tool_call.get("type") != "function":
                        add_issue(results, file_name, 15, info.line, "tool_call.type 必须等于 function。")

                    function = tool_call.get("function")
                    if not isinstance(function, dict) or "name" not in function or "arguments" not in function:
                        add_issue(results, file_name, 16, info.line, "tool_call.function 必须同时包含 name 和 arguments。")
                        continue

                    if not is_non_empty_string(function.get("name")):
                        add_issue(results, file_name, 17, info.line, "function.name 缺失、为空或不是字符串。")

                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        add_issue(results, file_name, 18, info.line, "function.arguments 必须是字符串。")
                        continue

                    if arguments in {"", "null"}:
                        add_issue(results, file_name, 47, info.line, "无参数函数 arguments 应为 `{}`，不能是空字符串或 `null`。")

                    try:
                        parsed_arguments = json.loads(arguments)
                    except json.JSONDecodeError as error:
                        add_issue(results, file_name, 19, info.line, f"function.arguments 不是合法 JSON 字符串：{error.msg}。")
                        continue

                    if not isinstance(parsed_arguments, dict):
                        add_issue(results, file_name, 48, info.line, "function.arguments 解析后必须是 JSON Object。")

                if len(current_ids) != len(set(current_ids)):
                    add_issue(results, file_name, 46, info.line, "同一 assistant 消息内存在重复的 tool_call.id。")

        if info.role == "tool":
            tool_call_id = raw.get("tool_call_id")
            if not isinstance(tool_call_id, str):
                add_issue(results, file_name, 21, info.line, "tool 消息缺少 string 类型的 tool_call_id。")
            else:
                tool_messages_by_id.setdefault(tool_call_id, []).append(info)
            if "content" not in raw or not isinstance(raw.get("content"), str):
                add_issue(results, file_name, 23, info.line, "tool 消息必须包含 string 类型的 content。")

        if info.role == "assistant":
            content_empty = not info.has_content or info.content is None or info.content == "" or info.content == []
            tool_calls_empty = not info.has_tool_calls or info.tool_calls is None or info.tool_calls == []
            if content_empty and tool_calls_empty:
                add_issue(results, file_name, 40, info.line, "assistant 的 content 和 tool_calls 不能同时为空。")

    for tool_call_id, occurrences in tool_calls_by_id.items():
        if len(occurrences) > 1:
            for occurrence in occurrences:
                add_issue(results, file_name, 20, occurrence.line, f"tool_call.id `{tool_call_id}` 在对话中重复出现。")

    for tool_call_id, messages in tool_messages_by_id.items():
        if len(messages) > 1:
            for message in messages:
                add_issue(results, file_name, 45, message.line, f"tool_call_id `{tool_call_id}` 被多条 tool 消息重复引用。")

    return parsed_tool_calls, tool_calls_by_id, tool_messages_by_id


def subagent_ids_for_tool_calls(info: MessageInfo, expected_ids: list[str | None]) -> set[str]:
    if not isinstance(info.raw, dict) or not isinstance(info.tool_calls, list):
        return set()

    metadata = info.raw.get("_metadata")
    metadata_agents = metadata.get("agent") if isinstance(metadata, dict) else None
    if not isinstance(metadata_agents, dict):
        return set()

    expected_id_set = {tool_call_id for tool_call_id in expected_ids if isinstance(tool_call_id, str)}
    subagent_ids: set[str] = set()
    for tool_call in info.tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_call_id = tool_call.get("id")
        if not isinstance(tool_call_id, str) or tool_call_id not in expected_id_set:
            continue
        function = tool_call.get("function")
        tool_name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(tool_name, str) or not is_subagent_tool_name(tool_name):
            continue
        subagent_id = metadata_agents.get(tool_call_id)
        if isinstance(subagent_id, str) and subagent_id and subagent_id != "main":
            subagent_ids.add(subagent_id)
    return subagent_ids


def build_subagent_children_by_agent(infos: list[MessageInfo]) -> dict[str, set[str]]:
    children_by_agent: dict[str, set[str]] = defaultdict(set)
    for info in infos:
        if not isinstance(info.raw, dict) or not isinstance(info.tool_calls, list):
            continue

        metadata = info.raw.get("_metadata")
        metadata_agents = metadata.get("agent") if isinstance(metadata, dict) else None
        if not isinstance(metadata_agents, dict):
            continue

        parent_agent = message_info_agent(info)
        for tool_call in info.tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = tool_call.get("id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            function = tool_call.get("function")
            tool_name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(tool_name, str) or not is_subagent_tool_name(tool_name):
                continue
            child_agent = metadata_agents.get(tool_call_id)
            if isinstance(child_agent, str) and child_agent and child_agent != parent_agent:
                children_by_agent[parent_agent].add(child_agent)
    return children_by_agent


def expand_subagent_ids(
    direct_subagent_ids: set[str],
    children_by_agent: dict[str, set[str]],
) -> set[str]:
    expanded = set(direct_subagent_ids)
    pending = list(direct_subagent_ids)
    while pending:
        agent = pending.pop()
        for child_agent in children_by_agent.get(agent, set()):
            if child_agent in expanded:
                continue
            expanded.add(child_agent)
            pending.append(child_agent)
    return expanded


def collect_adjacent_tool_results(
    infos: list[MessageInfo],
    start: int,
    assistant_agent: str,
    allowed_subagent_ids: set[str],
) -> tuple[list[MessageInfo], int]:
    tools: list[MessageInfo] = []
    cursor = start
    while cursor < len(infos):
        current = infos[cursor]
        current_agent = message_info_agent(current)
        if allowed_subagent_ids and current_agent in allowed_subagent_ids:
            cursor += 1
            continue
        if current.role == "tool" and current_agent == assistant_agent:
            tools.append(current)
            cursor += 1
            continue
        break
    return tools, cursor


def validate_sequence(
    infos: list[MessageInfo],
    parsed_tool_calls: list[ParsedToolCall],
    tool_calls_by_id: dict[str, list[ParsedToolCall]],
    tool_messages_by_id: dict[str, list[MessageInfo]],
    results: dict[int, RuleResult],
    file_name: str,
    input_path: Path,
) -> None:
    if not infos:
        add_issue(results, file_name, 8, 1, "数组为空，缺少第一条消息。")
        return

    first_role = infos[0].role
    if first_role not in {"system", "user"}:
        add_issue(results, file_name, 8, infos[0].line, "第一条消息必须是 system 或 user。")

    seen_non_system = False
    for info in infos:
        if info.role == "system":
            if seen_non_system:
                add_issue(results, file_name, 9, info.line, "system 消息只能位于对话开头。")
        elif info.role is not None:
            seen_non_system = True
    

    convert_path = find_convert_session_path(input_path)
    convert_messages = load_convert_messages(convert_path) if convert_path else None

    previous_role: str | None = None
    previous_info: MessageInfo | None = None
    for info in infos:
        if (
            previous_role == "user"
            and info.role == "user"
            and not is_command_user_message(info)
            and not (previous_info is not None and is_command_user_message(previous_info))
        ):
            should_add = True
            if convert_messages is not None:
                prev_ts = message_timestamp(previous_info.raw if isinstance(previous_info.raw, dict) else None)
                curr_ts = message_timestamp(info.raw if isinstance(info.raw, dict) else None)
                if prev_ts and curr_ts:
                    should_add, msg = check_convert_has_assistant_between(convert_messages, prev_ts, curr_ts)
                    if not should_add:
                        print(f"[INFO] {file_name}:{info.line} 跳过连续user检测: {msg}")
                    elif msg:
                        print(f"[WARNING] {file_name}:{info.line} {msg}，照常检测连续user")
                else:
                    print(f"[WARNING] {file_name}:{info.line} 无法获取消息时间戳，照常检测连续user")
            elif convert_path is None and '-' in file_name:
                print(f"[WARNING] {file_name}:{info.line} 未找到convert-sessions文件，照常检测连续user")

            if should_add:
                add_issue(results, file_name, 11, info.line, "检测到连续的 user 消息。")
        previous_role = info.role
        previous_info = info

    parsed_tool_calls_by_assistant: dict[int, list[ParsedToolCall]] = {}
    for parsed_tool_call in parsed_tool_calls:
        parsed_tool_calls_by_assistant.setdefault(parsed_tool_call.assistant_index, []).append(parsed_tool_call)

    children_by_agent = build_subagent_children_by_agent(infos)
    assistant_expected_ids: dict[int, list[str | None]] = {}
    assistant_expected_counts: dict[int, int] = {}
    for info in infos:
        if info.role == "assistant" and isinstance(info.tool_calls, list) and info.tool_calls:
            assistant_expected_counts[info.index] = len(info.tool_calls)
            expected_ids: list[str | None] = []
            for tool_call in info.tool_calls:
                if isinstance(tool_call, dict) and is_non_empty_string(tool_call.get("id")):
                    expected_ids.append(tool_call["id"])
                else:
                    expected_ids.append(None)
            assistant_expected_ids[info.index] = expected_ids

    for i, info in enumerate(infos):
        if info.role != "assistant" or info.index not in assistant_expected_counts:
            continue

        expected_count = assistant_expected_counts[info.index]
        expected_ids = assistant_expected_ids[info.index]
        assistant_agent = message_info_agent(info)
        contiguous_tools, cursor = collect_adjacent_tool_results(
            infos,
            i + 1,
            assistant_agent,
            expand_subagent_ids(
                subagent_ids_for_tool_calls(info, expected_ids),
                children_by_agent,
            ),
        )

        if len(contiguous_tools) != expected_count:
            add_issue(
                results,
                file_name,
                49,
                info.line,
                f"紧随其后的 tool result 数量为 {len(contiguous_tools)}，但前一条 assistant 声明了 {expected_count} 个 tool_calls。",
            )

        if expected_count > 1 and len(contiguous_tools) != expected_count:
            add_issue(results, file_name, 28, info.line, "assistant 的多个 tool_calls 没有完整收到全部 tool results。")

        expected_valid_id_set = {tool_call_id for tool_call_id in expected_ids if isinstance(tool_call_id, str)}
        for tool_message in contiguous_tools:
            tool_call_id = tool_message.raw.get("tool_call_id") if isinstance(tool_message.raw, dict) else None
            if isinstance(tool_call_id, str) and tool_call_id not in expected_valid_id_set:
                add_issue(results, file_name, 27, tool_message.line, "tool result 没有紧跟并匹配当前 assistant 的 tool_calls。")

        for later_info in infos[cursor:]:
            if later_info.role != "tool":
                continue
            if message_info_agent(later_info) != message_info_agent(info):
                continue
            tool_call_id = later_info.raw.get("tool_call_id") if isinstance(later_info.raw, dict) else None
            if isinstance(tool_call_id, str) and tool_call_id in expected_valid_id_set:
                add_issue(results, file_name, 27, later_info.line, "tool result 跨越了其他 assistant 或 user 消息。")

    tool_result_counts = {tool_call_id: len(messages) for tool_call_id, messages in tool_messages_by_id.items()}
    for tool_call_id, occurrences in tool_calls_by_id.items():
        count = tool_result_counts.get(tool_call_id, 0)
        if count != 1:
            for occurrence in occurrences:
                add_issue(results, file_name, 25, occurrence.line, f"tool_call.id `{tool_call_id}` 对应的 tool result 数量为 {count}，必须恰好为 1。")

    for info in infos:
        if info.role != "tool":
            continue

        raw = info.raw if isinstance(info.raw, dict) else {}
        tool_call_id = raw.get("tool_call_id")
        if not isinstance(tool_call_id, str):
            continue

        tool_agent = message_info_agent(info)
        previous_occurrences = [
            occurrence
            for occurrence in tool_calls_by_id.get(tool_call_id, [])
            if occurrence.assistant_index < info.index
            and message_info_agent(infos[occurrence.assistant_index - 1]) == tool_agent
        ]
        if not previous_occurrences:
            add_issue(results, file_name, 10, info.line, "tool 消息前缺少对应的 assistant tool_calls。")
            add_issue(results, file_name, 22, info.line, f"tool_call_id `{tool_call_id}` 在此前 assistant.tool_calls 中不存在。")
            add_issue(results, file_name, 26, info.line, f"tool result `{tool_call_id}` 孤立存在，找不到可配对的 tool_call。")

    last_info = infos[-1]
    if last_info.role == "tool":
        add_issue(results, file_name, 38, last_info.line, "对话以 tool 消息结尾，缺少后续 assistant 回复。")

    if last_info.role == "assistant" and isinstance(last_info.tool_calls, list) and last_info.tool_calls:
        add_issue(results, file_name, 39, last_info.line, "对话以带 tool_calls 的 assistant 结尾，后续 tool results 缺失。")


def validate_payload(payload: Any, text: str, input_path: Path, results: dict[int, RuleResult]) -> None:
    file_name = input_path.name
    messages_line = find_messages_field_line(text)

    if not isinstance(payload, dict):
        add_issue(results, file_name, 2, 1, "顶层 JSON 不是对象，无法读取 `messages` 数组。")
        dependent_rules = sorted(number for number in DEFINED_RULE_NUMBERS if number not in {1, 2, 44})
        for number in dependent_rules:
            add_issue(results, file_name, number, 1, "无法读取 `messages` 数组，不能继续完成该规则校验。")
        return

    messages = payload.get("messages", MISSING)
    if not isinstance(messages, list):
        add_issue(results, file_name, 2, messages_line, "`messages` 字段不是数组。")
        dependent_rules = sorted(number for number in DEFINED_RULE_NUMBERS if number not in {1, 2, 44})
        for number in dependent_rules:
            add_issue(results, file_name, number, messages_line, "`messages` 字段不是数组，无法按 message 列表规则校验。")
        return

    if not messages:
        add_issue(results, file_name, 3, messages_line, "`messages` 数组为空。")

    infos = normalize_message_infos(messages, text)
    parsed_tool_calls, tool_calls_by_id, tool_messages_by_id = validate_message_fields(infos, results, file_name)
    validate_sequence(infos, parsed_tool_calls, tool_calls_by_id, tool_messages_by_id, results, file_name, input_path)


def check_agent_scoped_rules(
    messages: list[Any],
    message_lines: list[int],
    results: dict[int, RuleResult],
    file_name: str,
) -> None:
    agent_tool_calls: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    agent_tool_results: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    agent_tool_call_positions: dict[str, dict[str, list[tuple[int, int]]]] = defaultdict(lambda: defaultdict(list))
    agent_tool_result_positions: dict[str, dict[str, list[tuple[int, int]]]] = defaultdict(lambda: defaultdict(list))

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        agent = message_agent(message)
        line = message_lines[index] if index < len(message_lines) else 1

        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
            for tool_call in message["tool_calls"]:
                if not isinstance(tool_call, dict):
                    continue
                tool_call_id = tool_call.get("id")
                if isinstance(tool_call_id, str) and tool_call_id:
                    agent_tool_calls[agent][tool_call_id].append(line)
                    agent_tool_call_positions[agent][tool_call_id].append((index, line))

        if message.get("role") == "tool":
            tool_call_id = message.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                agent_tool_results[agent][tool_call_id].append(line)
                agent_tool_result_positions[agent][tool_call_id].append((index, line))

    for agent, tool_call_ids in agent_tool_calls.items():
        for tool_call_id, lines in tool_call_ids.items():
            if len(lines) > 1:
                for line in lines:
                    add_issue(results, file_name, 20, line, f"agent `{agent}` 下 tool_call.id `{tool_call_id}` 重复出现。")

    for agent, tool_call_ids in agent_tool_call_positions.items():
        result_positions = agent_tool_result_positions.get(agent, {})
        for tool_call_id, call_positions in tool_call_ids.items():
            if len(call_positions) <= 1:
                continue
            tool_results = result_positions.get(tool_call_id, [])
            if not tool_results:
                continue
            for occurrence_index in range(1, len(call_positions)):
                current_message_index, current_line = call_positions[occurrence_index]
                next_message_index = (
                    call_positions[occurrence_index + 1][0]
                    if occurrence_index + 1 < len(call_positions)
                    else len(messages)
                )
                current_interval_results = [
                    result_line
                    for result_index, result_line in tool_results
                    if current_message_index < result_index < next_message_index
                ]
                if current_interval_results:
                    continue
                previous_results = [
                    result_line
                    for result_index, result_line in tool_results
                    if result_index < current_message_index
                ]
                if not previous_results:
                    continue
                add_issue(
                    results,
                    file_name,
                    50,
                    current_line,
                    (
                        f"agent `{agent}` 下 tool_call.id `{tool_call_id}` 被 assistant 重复声明；"
                        f"前序同 id 声明后已有 {len(previous_results)} 条 tool result，"
                        "但本次声明后到下一次同 id 声明前没有新的对应 tool result。"
                    ),
                )

    for agent, tool_call_ids in agent_tool_calls.items():
        result_ids = agent_tool_results.get(agent, {})
        for tool_call_id, call_lines in tool_call_ids.items():
            result_count = len(result_ids.get(tool_call_id, []))
            if result_count != 1:
                for line in call_lines:
                    add_issue(results, file_name, 25, line, f"agent `{agent}` 下 tool_call.id `{tool_call_id}` 对应 tool result 数量为 {result_count}。")

    for agent, result_ids in agent_tool_results.items():
        for tool_call_id, lines in result_ids.items():
            if len(lines) > 1:
                for line in lines:
                    add_issue(results, file_name, 45, line, f"agent `{agent}` 下 tool_call_id `{tool_call_id}` 被多条 tool 消息重复引用。")


def apply_agent_scoped_rules(payload: Any, text: str, input_path: Path, results: dict[int, RuleResult]) -> None:
    for rule_number in OPENAI_AGENT_SCOPED_RULES:
        results[rule_number].issues.clear()

    if not isinstance(payload, dict):
        return

    messages = payload.get("messages", MISSING)
    if not isinstance(messages, list):
        return

    check_agent_scoped_rules(messages, messages_element_lines(text), results, input_path.name)


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def validate_input_file(input_file: Path) -> dict[int, RuleResult]:
    results = build_results()
    if not input_file.exists():
        for number in DEFINED_RULE_NUMBERS:
            add_issue(results, input_file.name, number, 1, "待校验的 JSON 文件不存在，无法完成该规则校验。")
        return results

    try:
        text = decode_text(input_file, results)
    except OSError as error:
        for number in DEFINED_RULE_NUMBERS:
            add_issue(results, input_file.name, number, 1, f"文件读取失败：{error}")
        return results

    if text is None:
        file_name = input_file.name
        parse_blocked_rules = sorted(number for number in DEFINED_RULE_NUMBERS if number not in {1, 44})
        for number in parse_blocked_rules:
            add_issue(results, file_name, number, 1, "文件无法解析 JSON，无法继续完成该规则校验。")
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            add_issue(results, input_file.name, 1, error.lineno, f"JSON 解析失败：{error.msg}。")
            parse_blocked_rules = sorted(number for number in DEFINED_RULE_NUMBERS if number not in {1, 44})
            for number in parse_blocked_rules:
                add_issue(results, input_file.name, number, error.lineno, "文件不是合法 JSON，无法继续完成该规则校验。")
        else:
            validate_payload(payload, text, input_file, results)
            apply_agent_scoped_rules(payload, text, input_file, results)

    return results


def message_for_line(session_path: Path, line: int | None) -> dict[str, Any] | None:
    if line is None:
        return None
    try:
        text = session_path.read_text(encoding="utf-8", errors="replace")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return None

    element_lines = messages_element_lines(text)
    selected_index: int | None = None
    for index, start_line in enumerate(element_lines):
        if line < start_line:
            break
        selected_index = index
    if selected_index is None or selected_index >= len(messages):
        return None
    message = messages[selected_index]
    return message if isinstance(message, dict) else {"content": message}


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
    metadata = message.get("_metadata")
    if isinstance(metadata, dict):
        subagent_id = metadata.get("subagent_id")
        if isinstance(subagent_id, str):
            return subagent_id
    agent = message_agent(message)
    return "" if agent == "main" else agent


def file_id_for_session(source_root: Path, project_id: str, session_path: Path) -> str:
    for base in (source_root / project_id, source_root):
        try:
            return session_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return session_path.name


def project_id_for_report(source_root: Path, results: list[SessionOpenAIResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def build_json_report(source_root: Path, results: list[SessionOpenAIResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    failed = total - passed
    failed_rule_numbers = sorted(
        {
            number
            for result in results
            for number, rule_result in result.results.items()
            if number in OPENAI_REPORT_RULE_NUMBERS and not rule_result.passed
        }
    )
    tasks: list[dict[str, Any]] = []
    for session_result in results:
        session_path = Path(session_result.session_path)
        file_id = file_id_for_session(source_root, session_result.project_dir, session_path)
        for number, rule_result in sorted(session_result.results.items()):
            if number not in OPENAI_REPORT_RULE_NUMBERS or rule_result.passed:
                continue
            for issue in rule_result.issues:
                reason = f"规则 {rule_result.definition.number}: {rule_result.definition.title}；{issue.reason}"
                message = message_for_line(session_path, issue.line)
                tasks.append(
                    {
                        "产物id": session_result.project_dir,
                        "文件id": file_id,
                        "原因": reason,
                        "行号": issue.line,
                        "role": str(message.get("role", "")) if message else "",
                        "agent": message_agent(message) if message else "",
                        "timestamp": message_timestamp(message),
                        "工具id": message_tool_id(message),
                        "subagent": message_subagent(message),
                        "content 原文": content_original(message),
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
            "rules": len(OPENAI_REPORT_RULE_NUMBERS),
            "failed_rules": failed_rule_numbers,
            "issues": len(tasks),
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


def openaI_chat_completions(
    find_path: Path | str,
    session_json_path: Path | str,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> Path:
    """批量校验 OpenAI Chat Completions 消息格式，并生成汇总 JSON 报告。"""
    # OpenAI Chat Completions 规范检查开关，可以按需改为从配置文件或环境变量读取。
    openai_chat_completions_switch = OPENAI_CHAT_COMPLETIONS_SWITCH
    root_dir = Path(find_path).expanduser().resolve()
    if not openai_chat_completions_switch:
        print("OpenAI Chat Completions 规范检查已关闭，跳过检查。")
        report_output_dir = Path(output_dir).expanduser().resolve()
        report_output_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_output_dir / REPORT_NAME
        report_path.write_text(
            json.dumps(build_json_report(root_dir, []), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return report_path

    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"执行路径不存在或不是目录: {root_dir}")

    session_relative_path = Path(session_json_path)
    report_output_dir = Path(output_dir).expanduser().resolve()

    session_results: list[SessionOpenAIResult] = []
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
            rule_results = validate_input_file(missing_session_path)
            session_result = SessionOpenAIResult(
                project_dir=project_dir.name,
                session_path=str(missing_session_path),
                results=rule_results,
            )
            print("通过" if session_result.all_pass else "不通过")
            session_results.append(session_result)
            continue

        for session_path in session_paths:
            relative_session_path = session_path.relative_to(project_dir)
            result_name = f"{project_dir.name}/{relative_session_path.as_posix()}"
            print(f"检查: {result_name} ... ", end="", flush=True)
            rule_results = validate_input_file(session_path)
            session_result = SessionOpenAIResult(
                project_dir=project_dir.name,
                session_path=str(session_path),
                results=rule_results,
            )
            print("通过" if session_result.all_pass else "不通过")
            session_results.append(session_result)

    report_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_output_dir / REPORT_NAME
    report_path.write_text(
        json.dumps(build_json_report(root_dir, session_results), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    passed = sum(1 for result in session_results if result.all_pass)
    print(f"\nJSON 报告已写入: {report_path}")
    print(f"扫描项目数: {len(session_results)}")
    print(f"通过数: {passed}")
    print(f"不通过数: {len(session_results) - passed}")

    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量校验 OpenAI Chat Completions 消息数组格式并生成 JSON 报告")
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
    args = parse_args(argv)
    try:
        openaI_chat_completions(args.find_path, args.session_json_path, args.output_dir)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

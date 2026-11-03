#!/usr/bin/env python3
"""session.json 工具定义与工具调用格式检查脚本。

检查内容：
  1. 支持 -path 指定单个主轨迹文件，或指定 session 目录并递归搜索主轨迹文件。
  2. 以“主轨迹文件 + 同级 subagent/subagents 目录下文件”为一个检查单元。
  3. 每个主 session 和 subagent 文件都必须在顶层包含 tools 工具定义。
  4. 检查各文件的 tools 工具集合是否与主 session 一致。
  5. 检查每个 assistant.tool_calls[] 是否符合 OpenAI 工具调用格式。
  6. 检查每个工具调用的 function.name 是否存在于当前文件的工具定义。
  7. 检查 function.arguments 是否为合法 JSON object，并按对应工具 parameters
     校验 required、additionalProperties、基础类型、enum、minimum、maximum 等规则。

使用方式：
  python check\\check_tool_definitions.py \
    -r <执行路径> \
    -path <session.json 相对路径或 session 目录> \
    -o <报告输出目录>

示例：
  python check\\check_tool_definitions.py \
    -r C:\\Users\\15837\\Desktop\\workspace\\sessions_root \
    -path sessions \
    -o C:\\Users\\15837\\Desktop\\workspace\\reports

参数说明：
  -r
    执行路径。脚本会遍历该目录下的每个子文件夹，并把每个子文件夹视为一个项目。
  -path
    主轨迹文件或 session 目录的相对路径。目录会递归搜索主轨迹 .json 文件。
  -o
    报告输出目录。脚本会在该目录下生成 check_tool_definitions.json。
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


REPORT_NAME = "check_tool_definitions.json"
REPORT_TYPE = "check_tool_definitions_report"
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
SUBAGENT_DIR_NAMES = {"subagent", "subagents"}
REDACTED_PLACEHOLDER = "****"
REDACTED_MODEL_PLACEHOLDER = "internal-model"
REDACTED_MODEL_ARGUMENT_KEYS = {"model", "modelID"}
FAILED_TOOL_RESULT_MARKERS = ("InputValidationError", "<tool_use_error>")
# 全局开关：默认忽略 "function.name 不存在于已知工具定义中" 的错误
IGNORE_UNDEFINED_TOOL_ERRORS = True


def _load_undefined_tool_name_exemptions() -> set[str]:
    """加载 tool_definitions 未定义工具名豁免配置。"""
    config_path = Path(__file__).parent / "exemption_keywords.json"
    try:
        if config_path.exists():
            content = json.loads(config_path.read_text(encoding="utf-8"))
            tool_definitions = content.get("tool_definitions", {})
            values = tool_definitions.get("undefined_tool_name_exemptions", [])
            if isinstance(values, list):
                return {item.strip() for item in values if isinstance(item, str) and item.strip()}
    except (OSError, json.JSONDecodeError):
        pass
    return set()


UNDEFINED_TOOL_NAME_EXEMPTIONS = _load_undefined_tool_name_exemptions()


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    parameters: dict[str, Any]
    strict: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class Finding:
    file_path: str
    message_index: int
    tool_call_index: int
    tool_name: str
    reason: str


@dataclass
class SessionToolDefinitionResult:
    project_dir: str
    session_path: str
    valid_json: bool = True
    definitions_loaded: bool = True
    definitions_source: str = ""
    known_tool_count: int = 0
    checked_tool_call_count: int = 0
    group_files: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    error: str = ""

    @property
    def all_pass(self) -> bool:
        return self.valid_json and self.definitions_loaded and not self.findings


def load_json_payload(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"{path} 文件为空")

    return json.loads(stripped)


def extract_messages(payload: Any, path: Path) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise ValueError(f"{path} 缺少 messages 数组")
        return [message for message in messages if isinstance(message, dict)]

    if isinstance(payload, list):
        messages: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("messages"), list):
                messages.extend(message for message in item["messages"] if isinstance(message, dict))
            elif isinstance(item, dict):
                messages.append(item)
        return messages

    raise ValueError(f"{path} 顶层不是 JSON object 或 JSON array")


def load_messages(path: Path, default_agent: str | None = None) -> list[dict[str, Any]]:
    messages = extract_messages(load_json_payload(path), path)
    if default_agent is None:
        return messages

    normalized_messages: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message.get("agent"), str):
            metadata = message.get("_metadata")
            has_subagent_id = isinstance(metadata, dict) and isinstance(metadata.get("subagent_id"), str)
            if not has_subagent_id:
                message = dict(message)
                message["agent"] = default_agent
        normalized_messages.append(message)
    return normalized_messages


def extract_tools(payload: Any, path: Path) -> list[Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 顶层不是 JSON object，无法读取 tools")
    if "tools" not in payload:
        raise ValueError(f"{path} 缺少顶层 tools 工具定义")
    tools = payload.get("tools")
    if not isinstance(tools, list):
        raise ValueError(f"{path} 顶层 tools 不是数组")
    if not tools:
        raise ValueError(f"{path} 顶层 tools 数组为空")
    return tools


def normalize_tool_definition(raw_tool: Any) -> ToolDefinition | None:
    if not isinstance(raw_tool, dict):
        return None

    if isinstance(raw_tool.get("function"), dict):
        function = raw_tool["function"]
        name = function.get("name")
        parameters = function.get("parameters", {})
        strict = bool(function.get("strict", raw_tool.get("strict", False)))
    else:
        name = raw_tool.get("name")
        parameters = raw_tool.get("parameters", {})
        strict = bool(raw_tool.get("strict", False))

    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(parameters, dict):
        parameters = {}

    return ToolDefinition(name=name.strip(), parameters=parameters, strict=strict, raw=raw_tool)


def load_tool_definitions_from_payload(session_path: Path, payload: Any) -> tuple[dict[str, ToolDefinition], str]:
    raw_tools = extract_tools(payload, session_path)
    definitions: dict[str, ToolDefinition] = {}
    invalid_count = 0
    for raw_tool in raw_tools:
        definition = normalize_tool_definition(raw_tool)
        if definition is None:
            invalid_count += 1
            continue
        definitions[definition.name] = definition

    if invalid_count:
        raise ValueError(f"{session_path} 顶层 tools 存在 {invalid_count} 个无效工具定义")
    if not definitions:
        raise ValueError(f"{session_path} 顶层 tools 未解析出任何有效工具定义")
    return definitions, f"{session_path} 顶层 tools"


def load_tool_definitions(session_path: Path) -> tuple[dict[str, ToolDefinition], str]:
    return load_tool_definitions_from_payload(session_path, load_json_payload(session_path))


def should_scan_project_dir(path: Path) -> bool:
    return (
        path.is_dir()
        and path.name not in SKIPPED_SESSION_DIR_NAMES
        and not path.name.startswith(".")
    )


def is_candidate_session_file(path: Path) -> bool:
    return (
        path.is_file()
        and not path.name.startswith(".")
        and not path.name.endswith(".meta.json")
        and path.suffix.lower() in SESSION_FILE_SUFFIXES
        and not any(part in SKIPPED_SESSION_DIR_NAMES for part in path.parts)
    )


def is_main_session_file(path: Path) -> bool:
    return is_candidate_session_file(path) and not any(part in SUBAGENT_DIR_NAMES for part in path.parts)


def iter_session_paths(project_dir: Path, session_path_arg: Path) -> list[Path]:
    target = project_dir / session_path_arg
    if target.is_file():
        return [target] if is_main_session_file(target) else []

    if not target.is_dir():
        return [target]

    return sorted(
        (path for path in target.rglob("*") if is_main_session_file(path)),
        key=lambda path: str(path.relative_to(project_dir)).lower(),
    )


def has_session_target(project_dir: Path, session_path_arg: Path) -> bool:
    return (project_dir / session_path_arg).exists()


def discover_trajectory_group_files(main_session_path: Path) -> list[Path]:
    return discover_group_files(
        main_session_path,
        is_candidate_session_file=is_candidate_session_file,
        is_main_session_file=is_main_session_file,
        subagent_dir_names=SUBAGENT_DIR_NAMES,
    )


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return value is None
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return True


def schema_path_leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[", 1)[0]


def is_redacted_model_argument(value: Any, path: str) -> bool:
    return (
        isinstance(value, str)
        and value in {REDACTED_PLACEHOLDER, REDACTED_MODEL_PLACEHOLDER}
        and schema_path_leaf(path) in REDACTED_MODEL_ARGUMENT_KEYS
    )


def validate_schema(value: Any, schema: Any, path: str = "arguments") -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return []

    errors: list[str] = []

    if "allOf" in schema and isinstance(schema["allOf"], list):
        for index, sub_schema in enumerate(schema["allOf"], start=1):
            errors.extend(validate_schema(value, sub_schema, f"{path}.allOf[{index}]"))

    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        alternatives = [validate_schema(value, sub_schema, path) for sub_schema in schema["anyOf"]]
        if alternatives and not any(not alt_errors for alt_errors in alternatives):
            errors.append(f"{path} 不满足 anyOf 中任一分支")

    if "oneOf" in schema and isinstance(schema["oneOf"], list):
        alternatives = [validate_schema(value, sub_schema, path) for sub_schema in schema["oneOf"]]
        passed = sum(1 for alt_errors in alternatives if not alt_errors)
        if passed != 1:
            errors.append(f"{path} 应只满足 oneOf 中一个分支，实际满足 {passed} 个")

    expected_type = schema.get("type")
    expected_types: list[str] = []
    if isinstance(expected_type, str):
        expected_types = [expected_type]
    elif isinstance(expected_type, list):
        expected_types = [item for item in expected_type if isinstance(item, str)]

    if expected_types and not any(type_matches(value, item) for item in expected_types):
        errors.append(f"{path} 类型为 {json_type_name(value)}，期望 {expected_types}")
        return errors

    if (
        "enum" in schema
        and isinstance(schema["enum"], list)
        and value not in schema["enum"]
        and not is_redacted_model_argument(value, path)
    ):
        errors.append(f"{path} 取值 {value!r} 不在 enum {schema['enum']!r} 中")

    if "const" in schema and value != schema["const"] and not is_redacted_model_argument(value, path):
        errors.append(f"{path} 取值 {value!r} 不等于 const {schema['const']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}={value} 小于 minimum={minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}={value} 大于 maximum={maximum}")

    if isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}

        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    errors.append(f"{path}.{key} 缺少必填参数")

        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in properties:
                errors.extend(validate_schema(item, properties[key], child_path))
                continue

            additional = schema.get("additionalProperties", True)
            if additional is False:
                errors.append(f"{child_path} 未在工具定义 properties 中声明")
            elif isinstance(additional, dict):
                errors.extend(validate_schema(item, additional, child_path))

    if isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, items_schema, f"{path}[{index}]"))

    return errors


def is_failed_tool_result(message: dict[str, Any]) -> bool:
    if message.get("role") != "tool":
        return False
    content = message.get("content")
    return isinstance(content, str) and any(marker in content for marker in FAILED_TOOL_RESULT_MARKERS)


def failed_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    failed_ids: set[str] = set()
    for message in messages:
        if not is_failed_tool_result(message):
            continue
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            failed_ids.add(tool_call_id)
    return failed_ids


def is_successful_tool_result(message: dict[str, Any]) -> bool:
    if message.get("role") != "tool":
        return False
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return False

    # 1. 明确包含失败标记 → 不是成功
    if any(marker in content for marker in FAILED_TOOL_RESULT_MARKERS):
        return False

    # 2. 尝试解析 JSON
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        # 非 JSON 纯文本 → 只要不是空，就视为成功
        return True

    # 3. JSON 内容
    if isinstance(payload, dict):
        # 如果 dict 里有明确的 error 字段且非空/非 False，不视为成功
        error = payload.get("error")
        if error is not None and error not in (False, 0, "", [], {}):
            return False
        # 否则（包括没有 error 字段、error 为 false/空等）视为成功
        return True

    # 4. JSON 数组或其他类型 → 也视为成功（结构化返回即表示执行完成）
    return True


def successful_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    successful_ids: set[str] = set()
    for message in messages:
        if not is_successful_tool_result(message):
            continue
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            successful_ids.add(tool_call_id)
    return successful_ids


def is_additional_property_schema_error(reason: str) -> bool:
    return reason.endswith(" 未在工具定义 properties 中声明")


def is_type_mismatch_schema_error(reason: str) -> bool:
    return " 类型为 " in reason and "，期望 " in reason


def is_required_missing_schema_error(reason: str) -> bool:
    return reason.endswith(" 缺少必填参数")


def first_letter_case_tool_definition(
    tool_name: str,
    definitions: dict[str, ToolDefinition],
) -> ToolDefinition | None:
    if not tool_name:
        return None

    first = tool_name[0]
    if first.islower():
        candidate = first.upper() + tool_name[1:]
    elif first.isupper():
        candidate = first.lower() + tool_name[1:]
    else:
        return None

    return definitions.get(candidate)


def validate_tool_call(
    tool_call: Any,
    file_path: Path,
    message_index: int,
    tool_call_index: int,
    definitions: dict[str, ToolDefinition],
    failed_tool_ids: set[str] | None = None,
    successful_tool_ids: set[str] | None = None,
    undefined_tool_name_exemptions: set[str] | None = None,
) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    checked_count = 0
    tool_name = ""
    tool_call_id = ""

    def add(reason: str) -> None:
        findings.append(
            Finding(
                file_path=str(file_path),
                message_index=message_index,
                tool_call_index=tool_call_index,
                tool_name=tool_name,
                reason=reason,
            )
        )

    if not isinstance(tool_call, dict):
        add("tool_calls[] 元素不是对象")
        return findings, checked_count

    if isinstance(tool_call.get("id"), str):
        tool_call_id = tool_call["id"].strip()
    if not tool_call_id:
        add("tool_call.id 缺失、为空或不是字符串")

    if tool_call.get("type") != "function":
        add("tool_call.type 必须等于 function")

    function = tool_call.get("function")
    if not isinstance(function, dict):
        add("tool_call.function 缺失或不是对象")
        return findings, checked_count

    raw_name = function.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        add("function.name 缺失、为空或不是字符串")
        return findings, checked_count

    tool_name = raw_name.strip()
    definition = definitions.get(tool_name)
    if definition is None and tool_call_id in (failed_tool_ids or set()):
        definition = first_letter_case_tool_definition(tool_name, definitions)
    if definition is None:
        if tool_name in (undefined_tool_name_exemptions or set()):
            return findings, checked_count + 1
        if IGNORE_UNDEFINED_TOOL_ERRORS:
            return findings, checked_count
        add(f"function.name `{tool_name}` 不存在于已知工具定义中")
        return findings, checked_count + 1

    if "arguments" not in function:
        add("function.arguments 缺失")
        return findings, checked_count + 1

    raw_arguments = function.get("arguments")
    if not isinstance(raw_arguments, str):
        add("function.arguments 必须是 JSON 字符串")
        if isinstance(raw_arguments, dict):
            parsed_arguments = raw_arguments
        else:
            return findings, checked_count + 1
    else:
        try:
            parsed_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            add(f"function.arguments 不是合法 JSON 字符串：{exc.msg}")
            return findings, checked_count + 1

    if not isinstance(parsed_arguments, dict):
        add("function.arguments 解析后必须是 JSON object")
        return findings, checked_count + 1

    if tool_call_id not in (failed_tool_ids or set()):
        schema_errors = validate_schema(parsed_arguments, definition.parameters)
        for schema_error in schema_errors:
            if tool_call_id in (successful_tool_ids or set()):
                if (
                    is_additional_property_schema_error(schema_error)
                    or is_type_mismatch_schema_error(schema_error)
                    or is_required_missing_schema_error(schema_error)
                ):
                    continue
            add(schema_error)

    return findings, checked_count + 1


def check_one_session(project_dir: Path, session_path: Path) -> SessionToolDefinitionResult:
    result = SessionToolDefinitionResult(project_dir=project_dir.name, session_path=str(session_path))
    if not session_path.exists():
        result.valid_json = False
        result.definitions_loaded = False
        result.error = "session 文件不存在"
        return result

    group_files = discover_trajectory_group_files(session_path)
    result.group_files = [str(path) for path in group_files]
    result.definitions_source = "各轨迹文件顶层 tools"

    reference_tool_names: set[str] | None = None
    try:
        for index, group_file in enumerate(group_files):
            default_agent = None if index == 0 else group_file.stem
            payload = load_json_payload(group_file)
            messages = extract_messages(payload, group_file)
            if default_agent:
                normalized_messages: list[dict[str, Any]] = []
                for message in messages:
                    if not isinstance(message.get("agent"), str):
                        metadata = message.get("_metadata")
                        has_subagent_id = isinstance(metadata, dict) and isinstance(metadata.get("subagent_id"), str)
                        if not has_subagent_id:
                            message = dict(message)
                            message["agent"] = default_agent
                    normalized_messages.append(message)
                messages = normalized_messages

            try:
                definitions, source = load_tool_definitions_from_payload(group_file, payload)
            except ValueError as exc:
                result.definitions_loaded = False
                result.findings.append(
                    Finding(
                        file_path=str(group_file),
                        message_index=0,
                        tool_call_index=0,
                        tool_name="",
                        reason=f"工具定义读取失败：{exc}",
                    )
                )
                continue

            failed_tool_ids = failed_tool_call_ids(messages)
            successful_tool_ids = successful_tool_call_ids(messages)
            current_tool_names = set(definitions)
            if reference_tool_names is None:
                reference_tool_names = current_tool_names
                result.definitions_source = source
                result.known_tool_count = len(definitions)
            elif current_tool_names != reference_tool_names:
                result.definitions_loaded = False
                missing = sorted(reference_tool_names - current_tool_names)
                extra = sorted(current_tool_names - reference_tool_names)
                detail_parts = []
                if missing:
                    detail_parts.append(f"缺少工具 {missing}")
                if extra:
                    detail_parts.append(f"额外工具 {extra}")
                result.findings.append(
                    Finding(
                        file_path=str(group_file),
                        message_index=0,
                        tool_call_index=0,
                        tool_name="",
                        reason="当前文件 tools 与主 session 不一致：" + "；".join(detail_parts),
                    )
                )

            for message_index, message in enumerate(messages, start=1):
                tool_calls = message.get("tool_calls")
                if tool_calls is None:
                    continue
                if not isinstance(tool_calls, list):
                    result.findings.append(
                        Finding(
                            file_path=str(group_file),
                            message_index=message_index,
                            tool_call_index=0,
                            tool_name="",
                            reason="tool_calls 存在但不是数组",
                        )
                    )
                    continue
                for tool_call_index, tool_call in enumerate(tool_calls, start=1):
                    findings, checked_count = validate_tool_call(
                        tool_call,
                        group_file,
                        message_index,
                        tool_call_index,
                        definitions,
                        failed_tool_ids,
                        successful_tool_ids,
                        UNDEFINED_TOOL_NAME_EXEMPTIONS,
                    )
                    result.findings.extend(findings)
                    result.checked_tool_call_count += checked_count
    except (OSError, ValueError) as exc:
        result.valid_json = False
        result.error = str(exc)

    return result


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


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


def message_role(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    role = message.get("role")
    return role if isinstance(role, str) else ""


def message_timestamp(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    metadata = message.get("_metadata")
    timestamp = metadata.get("timestamp") if isinstance(metadata, dict) else None
    return timestamp if isinstance(timestamp, str) else ""


def message_tool_id(message: dict[str, Any] | None, tool_call_index: int = 0) -> str:
    if not message:
        return ""
    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str):
        return tool_call_id
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return ""
    if tool_call_index > 0 and tool_call_index <= len(tool_calls):
        tool_call = tool_calls[tool_call_index - 1]
        if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str):
            return tool_call["id"]
    ids = [
        tool_call.get("id")
        for tool_call in tool_calls
        if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str)
    ]
    return ",".join(ids)


def message_subagent(message: dict[str, Any] | None) -> str:
    agent = message_agent(message)
    return "" if agent in ("", "main") else agent


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
        messages = load_messages(session_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None, None
    if message_index > len(messages):
        return None, None
    lines = messages_element_lines(session_path)
    line = lines[message_index - 1] if message_index - 1 < len(lines) else None
    return messages[message_index - 1], line


def file_id_for_session(source_root: Path, project_id: str, session_path: Path) -> str:
    for base in (source_root / project_id, source_root):
        try:
            return session_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return session_path.name


def project_id_for_report(source_root: Path, results: list[SessionToolDefinitionResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def build_json_report(find_path: Path, session_path_arg: Path, results: list[SessionToolDefinitionResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    failed = total - passed
    total_tool_calls = sum(result.checked_tool_call_count for result in results)
    total_findings = sum(len(result.findings) for result in results)
    tasks: list[dict[str, Any]] = []
    undefined_tools: dict[str, dict[str, Any]] = {}
    message_cache: dict[Path, list[dict[str, Any]]] = {}
    line_cache: dict[Path, list[int]] = {}

    def cached_message_at_index(session_path: Path, message_index: int | None) -> tuple[dict[str, Any] | None, int | None]:
        if message_index is None or message_index < 1:
            return None, None
        cache_key = session_path.resolve()
        if cache_key not in message_cache:
            try:
                message_cache[cache_key] = load_messages(session_path)
            except (OSError, json.JSONDecodeError, ValueError):
                message_cache[cache_key] = []
        messages = message_cache[cache_key]
        if message_index > len(messages):
            return None, None
        if cache_key not in line_cache:
            line_cache[cache_key] = messages_element_lines(session_path)
        lines = line_cache[cache_key]
        line = lines[message_index - 1] if message_index - 1 < len(lines) else None
        return messages[message_index - 1], line

    for result in results:
        if result.error:
            session_path = Path(result.session_path)
            tasks.append(
                {
                    "产物id": result.project_dir,
                    "文件id": file_id_for_session(find_path, result.project_dir, session_path),
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
            reason = f"{finding.reason}；tool={finding.tool_name or '未知'}；tool_call={finding.tool_call_index}"
            source_file = Path(finding.file_path)
            message, line = cached_message_at_index(source_file, finding.message_index)
            task = {
                "产物id": result.project_dir,
                "文件id": file_id_for_session(find_path, result.project_dir, source_file),
                "原因": reason,
                "行号": line or 1,
                "role": message_role(message),
                "agent": message_agent(message),
                "timestamp": message_timestamp(message),
                "工具id": message_tool_id(message, finding.tool_call_index),
                "subagent": message_subagent(message),
                "content 原文": content_original(message),
            }
            tasks.append(task)
            if "不存在于已知工具定义" in finding.reason:
                tool_name = finding.tool_name or "未知"
                bucket = undefined_tools.setdefault(
                    tool_name,
                    {
                        "工具名称": tool_name,
                        "出现次数": 0,
                        "预览上限": 5,
                        "预览": [],
                    },
                )
                bucket["出现次数"] += 1
                if len(bucket["预览"]) < bucket["预览上限"]:
                    bucket["预览"].append(
                        {
                            "文件id": task["文件id"],
                            "行号": task["行号"],
                            "role": task["role"],
                            "agent": task["agent"],
                            "工具id": task["工具id"],
                            "content 原文": task["content 原文"],
                        }
                    )

    undefined_tool_rows = sorted(
        undefined_tools.values(),
        key=lambda item: (-int(item["出现次数"]), str(item["工具名称"])),
    )
    undefined_tool_total = sum(int(item["出现次数"]) for item in undefined_tool_rows)

    return {
        "report_type": REPORT_TYPE,
        "project_id": project_id_for_report(find_path, results),
        "source_root": str(find_path),
        "generated_at": generated_at_text(),
        "summary": {
            "files": total,
            "passed": passed,
            "failed": failed,
            "issues": len(tasks),
            "tool_calls": total_tool_calls,
            "tool_definition_findings": total_findings,
            "undefined_tool_calls": undefined_tool_total,
            "undefined_tool_names": len(undefined_tool_rows),
            "invalid_json_failed": sum(1 for result in results if not result.valid_json),
            "definitions_failed": sum(1 for result in results if not result.definitions_loaded),
            "session_path": str(session_path_arg),
        },
        "undefined_tools": undefined_tool_rows,
        "tasks": tasks,
    }


def tool_usage(find_path: Path | str, session_json_path: Path | str, output_dir: Path | str) -> Path:
    root = Path(find_path)
    session_path_arg = Path(session_json_path)
    report_output_dir = Path(output_dir)

    results: list[SessionToolDefinitionResult] = []
    if not root.exists() or not root.is_dir():
        results.append(
            SessionToolDefinitionResult(
                project_dir=str(root),
                session_path=str(session_path_arg),
                valid_json=False,
                definitions_loaded=False,
                error="执行路径不存在或不是目录",
            )
        )
    else:
        root_session_paths = iter_session_paths(root, session_path_arg)
        project_dirs = (
            [root]
            if root_session_paths or has_session_target(root, session_path_arg)
            else [path for path in sorted(root.iterdir(), key=lambda item: item.name.lower()) if should_scan_project_dir(path)]
        )
        if not project_dirs:
            project_dirs = [root]

        for project_dir in project_dirs:
            session_paths = iter_session_paths(project_dir, session_path_arg)
            if not session_paths:
                missing_path = project_dir / session_path_arg
                if missing_path.exists():
                    continue
                results.append(check_one_session(project_dir, missing_path))
                continue

            for session_path in session_paths:
                result = check_one_session(project_dir, session_path)
                result.project_dir = project_dir.name
                results.append(result)

    report_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_output_dir / REPORT_NAME
    report_path.write_text(
        json.dumps(build_json_report(root.resolve(), session_path_arg, results), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量检查工具调用是否存在于已知工具定义，并校验参数格式")
    parser.add_argument(
        "-r",
        dest="find_path",
        required=True,
        help="执行路径：遍历该目录下的每个子文件夹。",
    )
    parser.add_argument(
        "-path",
        dest="session_json_path",
        required=True,
        help="每个项目子文件夹里的主轨迹文件或 session 目录相对路径。目录会递归搜索主轨迹。",
    )
    parser.add_argument(
        "-o",
        dest="output_dir",
        required=True,
        help=f"报告输出目录，生成 {REPORT_NAME}。",
    )
    parser.add_argument(
        "--no-ignore-undefined-tools",
        action="store_true",
        help="默认忽略 function.name 不存在于已知工具定义中的错误；使用该参数可恢复检查。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global IGNORE_UNDEFINED_TOOL_ERRORS
    if getattr(args, "no_ignore_undefined_tools", False):
        IGNORE_UNDEFINED_TOOL_ERRORS = False
    report_path = tool_usage(args.find_path, args.session_json_path, args.output_dir)
    print(f"检查完成，报告已生成：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""session.json 基础验收检查脚本。

检查内容：
  1. 文件格式须为标准 JSON，且顶层包含 messages 数组。主轨迹和 subagent 都检查。
  2. 计算得到的 main user 对话轮次值 > 4。仅主轨迹检查。
  3. 第一个 user 对话内容长度 >= 20 个字符。若首字符为 / 则跳过该项。主轨迹和 subagent 都检查。
  4. 对话中的第一个节点须为 system 系统提示词。仅主轨迹检查。
  5. 主轨迹首个 system content 与首个 user content 拼接后须可计算 SHA-256。
     SHA-256 重复是否作为异常由 SHA256_DUPLICATE_CHECK_MODE 控制。
     subagent 不参与该项。

使用方式：
  python check_basic_acceptance.py \
    -r <执行路径> \
    -path <session.json 相对路径> \
    -o <报告输出目录>

示例：
  python check_basic_acceptance.py \
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
    报告输出目录。脚本会在该目录下生成 check_basic_acceptance.json。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def _load_long_check_keywords() -> list[str]:
    """加载豁免关键词配置文件中的 basic_acceptance.long_check_keywords"""
    config_path = Path(__file__).parent / "exemption_keywords.json"
    default_keywords = ["/"]
    try:
        if config_path.exists():
            content = json.loads(config_path.read_text(encoding="utf-8"))
            return content.get("basic_acceptance", {}).get("long_check_keywords", default_keywords)
    except (OSError, json.JSONDecodeError):
        pass
    return default_keywords


LONG_CHECK_KEYWORDS = _load_long_check_keywords()
REPORT_NAME = "check_basic_acceptance.json"
REPORT_TYPE = "check_basic_acceptance_report"
BASIC_ACCEPTANCE_SWITCH = True
SHA256_MODE_WITHIN_TASK_ONLY = "within_task_only"
SHA256_MODE_CROSS_TASK_ONLY = "cross_task_only"
SHA256_CLI_MODE_OFF = "off"
SHA256_CLI_MODE_FALSE = "false"
SHA256_DUPLICATE_CHECK_MODES = {
    SHA256_MODE_WITHIN_TASK_ONLY,
    SHA256_MODE_CROSS_TASK_ONLY,
    False,
}
SHA256_CLI_MODE_CHOICES = (
    SHA256_MODE_CROSS_TASK_ONLY,
    SHA256_MODE_WITHIN_TASK_ONLY,
    SHA256_CLI_MODE_OFF,
    SHA256_CLI_MODE_FALSE,
)
# SHA-256 重复检查模式。
# 默认模式在这里调整；check_pipeline.py 会读取这个值。
# 运行 check_pipeline.py 时如未显式覆盖，也会使用这里配置的默认模式。
# within_task_only: 仅同一个 TASK 内部 session 重复记为 basic_acceptance 异常。
# cross_task_only: 同一个 TASK 内部重复不记异常，只输出 sha256_records 供 pipeline 做跨 TASK 检查。
# False: 完全关闭 SHA-256 计算、记录和重复检查。
SHA256_DUPLICATE_CHECK_MODE: str | bool = False
# 兼容旧开关名。实际逻辑由 SHA256_DUPLICATE_CHECK_MODE 决定。
SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH = False
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}


def normalize_sha256_duplicate_check_mode(mode: str | bool) -> str | bool:
    if mode is False:
        return False
    if isinstance(mode, str):
        normalized = mode.strip().lower()
        if normalized in {SHA256_CLI_MODE_OFF, SHA256_CLI_MODE_FALSE}:
            return False
        if normalized in {SHA256_MODE_WITHIN_TASK_ONLY, SHA256_MODE_CROSS_TASK_ONLY}:
            return normalized

    allowed = ", ".join(SHA256_CLI_MODE_CHOICES)
    raise ValueError(f"invalid SHA-256 duplicate check mode: {mode}; expected one of: {allowed}")


def set_sha256_duplicate_check_mode(mode: str | bool) -> None:
    global SHA256_DUPLICATE_CHECK_MODE
    global SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH

    mode = normalize_sha256_duplicate_check_mode(mode)
    SHA256_DUPLICATE_CHECK_MODE = mode
    SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH = mode == SHA256_MODE_WITHIN_TASK_ONLY


def sha256_duplicate_check_enabled() -> bool:
    set_sha256_duplicate_check_mode(SHA256_DUPLICATE_CHECK_MODE)
    return SHA256_DUPLICATE_CHECK_MODE is not False


def sha256_within_project_duplicate_enabled() -> bool:
    set_sha256_duplicate_check_mode(SHA256_DUPLICATE_CHECK_MODE)
    return SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH


@dataclass
class BasicAcceptanceResult:
    project_dir: str
    session_path: str
    is_subagent: bool = False
    turns_applicable: bool = True
    system_first_applicable: bool = True
    valid_json: bool = True
    valid_json_error: str = ""
    turns_count: int = 0
    turns_pass: bool = True
    first_user_content_len: int = 0
    first_user_content_pass: bool = True
    first_user_content_error: str = ""
    system_first: bool = True
    system_first_error: str = ""
    sha256_applicable: bool = True
    combined_content_sha256: str = ""
    system_content_index: int | None = None
    user_content_index: int | None = None
    sha256_pass: bool = True
    sha256_error: str = ""
    details: list[str] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return (
            self.valid_json
            and self.turns_pass
            and self.first_user_content_pass
            and self.system_first
            and self.sha256_pass
        )


@dataclass(frozen=True)
class ContentHashRecord:
    sha256: str
    result: BasicAcceptanceResult
    message_index: int


def normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                nested_content = item.get("content")
                if isinstance(nested_content, str):
                    parts.append(nested_content)
                    continue
                parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def load_session(session_path: Path, result: BasicAcceptanceResult) -> dict[str, Any] | None:
    try:
        text = session_path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        result.valid_json = False
        result.valid_json_error = str(exc)
        result.turns_pass = False
        result.first_user_content_pass = False
        result.system_first = False
        if result.sha256_applicable:
            result.sha256_pass = False
            result.sha256_error = "invalid JSON; cannot calculate main system/user content SHA-256"
        return None

    if not isinstance(payload, dict):
        result.valid_json = False
        result.valid_json_error = "top-level JSON is not an object"
        return None

    messages = payload.get("messages")
    if not isinstance(messages, list):
        result.valid_json = False
        result.valid_json_error = "`messages` field is missing or is not an array"
        return None

    return payload


def is_subagent_path(session_path: Path) -> bool:
    return any(part.lower() in {"subagent", "subagents"} for part in session_path.parts)


def first_message_index_for_role_in_messages(messages: list[Any], role: str) -> int | None:
    for index, message in enumerate(messages, start=1):
        if isinstance(message, dict) and message.get("role") == role:
            return index
    return None


def content_sha256(*values: Any) -> str:
    combined_content = "".join(normalize_content(value) for value in values)
    return hashlib.sha256(combined_content.encode("utf-8")).hexdigest()


def calculate_main_content_hashes(result: BasicAcceptanceResult, messages: list[Any]) -> None:
    if result.is_subagent or not sha256_duplicate_check_enabled():
        result.sha256_applicable = False
        result.sha256_pass = True
        return

    errors: list[str] = []
    result.system_content_index = first_message_index_for_role_in_messages(messages, "system")
    result.user_content_index = first_message_index_for_role_in_messages(messages, "user")

    system_content: Any = ""
    user_content: Any = ""

    if result.system_content_index is None:
        errors.append("missing first system message for SHA-256")
    else:
        system_message = messages[result.system_content_index - 1]
        if isinstance(system_message, dict):
            system_content = system_message.get("content")

    if result.user_content_index is None:
        errors.append("missing first user message for SHA-256")
    else:
        user_message = messages[result.user_content_index - 1]
        if isinstance(user_message, dict):
            user_content = user_message.get("content")

    result.sha256_pass = not errors
    result.sha256_error = "；".join(errors)
    if result.sha256_pass:
        result.combined_content_sha256 = content_sha256(system_content, user_content)


def basic_acceptance(project_dir: Path, session_path: Path) -> BasicAcceptanceResult:
    result = BasicAcceptanceResult(
        project_dir=project_dir.name,
        session_path=str(session_path),
        is_subagent=is_subagent_path(session_path),
    )
    result.turns_applicable = not result.is_subagent
    result.system_first_applicable = not result.is_subagent
    if not sha256_duplicate_check_enabled():
        result.sha256_applicable = False
        result.sha256_pass = True

    # 验收标准开关，可以按需改为从配置文件或环境变量读取。
    acceptance_switch = BASIC_ACCEPTANCE_SWITCH
    if not acceptance_switch:
        print("基础验收标准已关闭，跳过验收。")
        return result

    if not session_path.exists():
        result.valid_json = False
        result.valid_json_error = "session.json file does not exist"
        result.turns_pass = False
        result.first_user_content_pass = False
        result.system_first = False
        if result.sha256_applicable:
            result.sha256_pass = False
            result.sha256_error = "session.json file does not exist"
        return result

    payload = load_session(session_path, result)
    if payload is None:
        return result

    messages = payload["messages"]

    user_messages = [
        message
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "user"
    ]
    main_user_messages = [
        message
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "user"
        and message.get("agent") == "main"
    ]
    if result.turns_applicable:
        result.turns_count = len(main_user_messages)
        result.turns_pass = result.turns_count > 4
    else:
        result.turns_count = len(user_messages)
        result.turns_pass = True

    first_user_content = None
    if user_messages:
        first_user_content = normalize_content(user_messages[0].get("content"))

    if first_user_content is None:
        result.first_user_content_len = 0
        result.first_user_content_pass = False
        result.first_user_content_error = "missing first user message"
    else:
        result.first_user_content_len = len(first_user_content)
        if any(first_user_content.startswith(kw) for kw in LONG_CHECK_KEYWORDS):
            result.first_user_content_pass = True
        else:
            result.first_user_content_pass = result.first_user_content_len >= 20
            if not result.first_user_content_pass:
                result.first_user_content_error = (
                    f"first user content length is {result.first_user_content_len}, less than 20"
                )

    if not result.system_first_applicable:
        result.system_first = True
    elif messages:
        first_message = messages[0] if isinstance(messages[0], dict) else {}
        if first_message.get("role") != "system":
            result.system_first = False
            result.system_first_error = (
                f"first message role is {first_message.get('role', 'missing')!r}, not 'system'"
            )
    else:
        result.system_first = False
        result.system_first_error = "`messages` array is empty"

    calculate_main_content_hashes(result, messages)

    return result


def iter_content_hash_records(results: list[BasicAcceptanceResult]) -> list[ContentHashRecord]:
    records: list[ContentHashRecord] = []
    for result in results:
        if not result.sha256_applicable:
            continue
        if result.combined_content_sha256 and result.user_content_index is not None:
            records.append(
                ContentHashRecord(
                    sha256=result.combined_content_sha256,
                    result=result,
                    message_index=result.user_content_index,
                )
            )
    return records


def find_duplicates(results: list[BasicAcceptanceResult]) -> dict[str, list[ContentHashRecord]]:
    grouped: dict[str, list[ContentHashRecord]] = defaultdict(list)
    for record in iter_content_hash_records(results):
        grouped[record.sha256].append(record)
    return {sha: items for sha, items in grouped.items() if len(items) > 1}


def find_within_project_duplicates(results: list[BasicAcceptanceResult]) -> list[tuple[str, list[ContentHashRecord]]]:
    grouped: dict[tuple[str, str], list[ContentHashRecord]] = defaultdict(list)
    for record in iter_content_hash_records(results):
        grouped[(record.result.project_dir, record.sha256)].append(record)
    duplicate_groups = [
        (sha, records)
        for (_project_dir, sha), records in grouped.items()
        if len(records) > 1
    ]
    return sorted(duplicate_groups, key=lambda item: (item[1][0].result.project_dir, item[0]))


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


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


def load_messages(session_path: Path) -> list[Any]:
    try:
        payload = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return messages if isinstance(messages, list) else []


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


def message_at_index(session_path: Path, message_index: int | None) -> tuple[dict[str, Any] | None, int | None]:
    if message_index is None or message_index < 1:
        return None, None
    messages = load_messages(session_path)
    if message_index > len(messages):
        return None, None
    message = messages[message_index - 1]
    lines = messages_element_lines(session_path)
    line = lines[message_index - 1] if message_index - 1 < len(lines) else None
    return (message if isinstance(message, dict) else {"content": message}), line


def first_message_index_for_role(session_path: Path, role: str, *, agent: str | None = None) -> int | None:
    for index, message in enumerate(load_messages(session_path), start=1):
        if not isinstance(message, dict) or message.get("role") != role:
            continue
        if agent is not None and message.get("agent") != agent:
            continue
        return index
    return None


def file_id_for_session(source_root: Path, project_id: str, session_path: Path) -> str:
    for base in (source_root / project_id, source_root):
        try:
            return session_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return session_path.name


def project_id_for_report(source_root: Path, results: list[BasicAcceptanceResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def task_record(
    *,
    result: BasicAcceptanceResult,
    source_root: Path,
    reason: str,
    message_index: int | None = None,
    line: int | None = None,
) -> dict[str, Any]:
    session_path = Path(result.session_path)
    message, message_line = message_at_index(session_path, message_index)
    selected_line = line if line is not None else message_line
    return {
        "产物id": result.project_dir,
        "文件id": file_id_for_session(source_root, result.project_dir, session_path),
        "原因": reason,
        "行号": selected_line,
        "role": str(message.get("role", "")) if message else "",
        "agent": message_agent(message),
        "timestamp": message_timestamp(message),
        "工具id": message_tool_id(message),
        "subagent": message_subagent(message),
        "content 原文": content_original(message),
    }


def build_json_report(source_root: Path, results: list[BasicAcceptanceResult]) -> dict[str, Any]:
    total = len(results)
    within_project_duplicate_enabled = sha256_within_project_duplicate_enabled()
    within_project_duplicates = (
        find_within_project_duplicates(results)
        if within_project_duplicate_enabled
        else []
    )
    duplicate_result_paths = {
        record.result.session_path
        for _sha, records in within_project_duplicates
        for record in records
    }
    passed = sum(
        1
        for result in results
        if result.all_pass and result.session_path not in duplicate_result_paths
    )
    failed = total - passed
    tasks: list[dict[str, Any]] = []
    sha256_records: list[dict[str, Any]] = []

    for result in results:
        if result.valid_json_error:
            tasks.append(
                task_record(
                    result=result,
                    source_root=source_root,
                    reason=result.valid_json_error,
                    line=1,
                )
            )
        if result.turns_applicable and not result.turns_pass:
            user_index = first_message_index_for_role(Path(result.session_path), "user", agent="main")
            tasks.append(
                task_record(
                    result=result,
                    source_root=source_root,
                    reason=f"main user 对话轮次为 {result.turns_count}，不满足 > 4",
                    message_index=user_index,
                )
            )
        if result.first_user_content_error:
            user_index = first_message_index_for_role(Path(result.session_path), "user")
            tasks.append(
                task_record(
                    result=result,
                    source_root=source_root,
                    reason=result.first_user_content_error,
                    message_index=user_index,
                )
            )
        if result.system_first_applicable and result.system_first_error:
            tasks.append(
                task_record(
                    result=result,
                    source_root=source_root,
                    reason=result.system_first_error,
                    message_index=1,
                )
            )
        if result.sha256_error:
            tasks.append(
                task_record(
                    result=result,
                    source_root=source_root,
                    reason=result.sha256_error,
                    line=1,
                )
            )

    for sha, records in within_project_duplicates:
        for record in records:
            tasks.append(
                task_record(
                    result=record.result,
                    source_root=source_root,
                    reason=f"主 session 首个 system content + 首个 user content 合并 SHA-256 与其他主 session 重复: {sha}",
                    message_index=record.message_index,
                )
            )

    sha256_records = [
        {
            "project_dir": result.project_dir,
            "session_path": file_id_for_session(source_root, result.project_dir, Path(result.session_path)),
            "sha256": result.combined_content_sha256,
        }
        for result in results
        if result.sha256_applicable and result.combined_content_sha256
    ]

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
            "sha256_duplicate_check_mode": SHA256_DUPLICATE_CHECK_MODE,
            "sha256_enabled": sha256_duplicate_check_enabled(),
            "sha256_within_project_duplicate_enabled": within_project_duplicate_enabled,
            "duplicate_sha256_groups": len(within_project_duplicates),
            "main_combined_content_sha256_items": len(iter_content_hash_records(results)),
            "valid_json_failed": sum(1 for result in results if not result.valid_json),
            "turns_failed": sum(1 for result in results if result.turns_applicable and not result.turns_pass),
            "first_user_content_failed": sum(1 for result in results if not result.first_user_content_pass),
            "system_first_failed": sum(1 for result in results if result.system_first_applicable and not result.system_first),
            "sha256_failed": sum(1 for result in results if result.sha256_applicable and not result.sha256_pass),
            "sha256_duplicate_failed": len(duplicate_result_paths),
        },
        "sha256_records": sha256_records,
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


def run(
    find_path: Path,
    session_json_path: Path,
    output_dir: Path,
    *,
    sha256_mode: str | bool | None = None,
) -> Path:
    if sha256_mode is not None:
        set_sha256_duplicate_check_mode(sha256_mode)

    root_dir = find_path.resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"scan path does not exist or is not a directory: {root_dir}")

    results: list[BasicAcceptanceResult] = []
    root_session_paths = iter_session_paths(root_dir, session_json_path)
    project_dirs = (
        [root_dir]
        if root_session_paths or has_session_target(root_dir, session_json_path)
        else sorted((path for path in root_dir.iterdir() if should_scan_project_dir(path)), key=lambda path: path.name)
    )
    for project_dir in project_dirs:
        session_paths = iter_session_paths(project_dir, session_json_path)
        if not session_paths:
            missing_session_path = project_dir / session_json_path
            if missing_session_path.exists():
                continue
            print(f"checking: {project_dir.name} ... ", end="", flush=True)
            result = basic_acceptance(project_dir, missing_session_path)
            result.project_dir = project_dir.name
            print("pass" if result.all_pass else "fail")
            results.append(result)
            continue

        for session_path in session_paths:
            relative_session_path = session_path.relative_to(project_dir)
            result_name = f"{project_dir.name}/{relative_session_path.as_posix()}"
            print(f"checking: {result_name} ... ", end="", flush=True)
            result = basic_acceptance(project_dir, session_path)
            result.project_dir = project_dir.name
            print("pass" if result.all_pass else "fail")
            results.append(result)

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / REPORT_NAME
    report_payload = build_json_report(root_dir, results)
    report_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    summary = report_payload.get("summary", {})
    passed = int(summary.get("passed", 0)) if isinstance(summary, dict) else 0
    print(f"\nJSON report written: {report_path}")
    print(f"scanned files: {len(results)}")
    print(f"passed: {passed}")
    print(f"failed: {len(results) - passed}")

    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Basic session.json acceptance checker.",
    )
    parser.add_argument(
        "-r",
        dest="find_path",
        type=Path,
        required=True,
        help="Root directory whose child project directories will be scanned.",
    )
    parser.add_argument(
        "-path",
        dest="session_json_path",
        type=Path,
        required=True,
        help="Relative session file or session directory path inside each project directory. Directories are searched recursively.",
    )
    parser.add_argument(
        "-o",
        dest="output_dir",
        type=Path,
        required=True,
        help=f"Report output directory. Generates {REPORT_NAME}.",
    )
    parser.add_argument(
        "--sha256-mode",
        choices=SHA256_CLI_MODE_CHOICES,
        default=SHA256_DUPLICATE_CHECK_MODE,
        help=(
            "SHA-256 duplicate mode: within_task_only only fails duplicate sessions "
            "inside one TASK; cross_task_only only records hashes for cross-TASK checks; "
            "off/false disables SHA-256 checks."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        run(args.find_path, args.session_json_path, args.output_dir, sha256_mode=args.sha256_mode)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""session.json 主轨迹最终 assistant 完成态检查脚本。

该检查器扫描项目目录、读取会话文件并使用模型来判断是否
最后一条助手消息清楚地表明状态已完成.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from model_config import build_chat_model, resolve_model_config
except ImportError:  # pragma: no cover - supports importing as check.check_completed_state
    from check.model_config import build_chat_model, resolve_model_config


REPORT_NAME = "check_completed_state.json"
REPORT_TYPE = "check_completed_state_report"
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
SUBAGENT_DIR_NAMES = {"subagent", "subagents"}
MODEL_TIMEOUT_SECONDS = 60
DEFAULT_WORKERS = 10
TERMINAL_STATES = {"complete", "partial_complete"}
MAX_CONTENT_CHARS = 15000
PREVIEW_CHARS = 240
MAX_DELETABLE_NON_TOOL_ASSISTANTS = 2

# 完成态模型判断开关：需要关闭时，手动改为 False。
is_model_open = True

SYSTEM_PROMPT = """你负责判断 Claude Code 主轨迹中某个 assistant 节点是否可以作为完成态结尾。

只根据给定 assistant content 判断，返回严格 JSON, 不要输出 Markdown。

返回字段：
- state: "complete" | "partial_complete" | "not_complete"
- confidence: 0 到 1 的数字
- reason: 中文简短原因
- evidence: 1 到 5 条中文证据

判定核心：
只有 assistant 的自然语言明确落在以下完成信号之一，才可以判为 complete 或 partial_complete：
- 任务或开发已经完成：明确说明需求、修复、实现、重构、改动已完成或已交付。
- 测试完成且成功：明确说明测试、验证、检查命令已运行且通过、成功或无失败。
- 开发文档已经完成：明确说明文档、说明、计划、设计文档等开发文档已编写、更新或交付。

判定标准：
1. complete:assistant 明确表示整体任务或开发工作已经完成，或以测试/验证成功作为最终结论，或明确交付了开发文档；语气是最终总结或交付。
2. partial_complete:assistant 明确表示当前这一轮开发改动、修复或开发文档更新已经完成，但整体仍可能需要用户批准、后续命令、额外验证或进一步操作；如果只是测试相关内容，必须是测试完成且成功才可判为 partial_complete。
3. not_complete:assistant 主要是在继续修改、准备执行工具、等待工具结果、请求权限、提出下一步行动，或内容为空、没有形成上述完成信号；测试失败、测试未运行、测试仍需继续或只说需要测试，通常是 not_complete。

注意：
- 如果节点只是发起工具调用且没有有意义的自然语言文本内容，通常是 not_complete。
- 但如果节点同时有 tool_calls 和有实质内容的文本（如总结、确认完成等），应优先根据文本内容判断，不因 tool_calls 存在而降级。
- 如果节点说"我还需要继续修改/继续测试/接下来我会..."，通常是 not_complete。
- 如果节点只报告"测试失败/还有报错/需要继续修复"，属于 not_complete。
- 如果节点说"修改已完成，但测试需要用户批准"，属于 partial_complete。
- 如果节点只说"准备请求批准运行测试"或"接下来运行测试"，属于 not_complete。
- 不要因为提到计划、下一步、待办、建议用户后续操作就判为完成；只有已经完成的任务、开发、成功测试或开发文档才算完成信号。
"""


@dataclass
class CompletedStateResult:
    project_dir: str
    session_path: str
    valid_session: bool = True
    message_count: int = 0
    assistant_count: int = 0
    last_index: int | None = None
    last_role: str = ""
    last_agent: str = ""
    selected_state: str = ""
    selected_message_index: int | None = None
    selected_reason: str = ""
    confidence: float = 0.0
    reason: str = ""
    content_excerpt: str = ""
    error: str = ""
    judgments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return self.valid_session and self.selected_state in TERMINAL_STATES and not self.error


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
        return "\n".join(part for part in parts if part)
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def excerpt(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


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


def extract_tool_call_ids(message: dict[str, Any]) -> list[str]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    ids: list[str] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_id = tool_call.get("id")
        if isinstance(tool_id, str) and tool_id.strip():
            ids.append(tool_id.strip())
    return ids


def extract_subagent_ids(message: dict[str, Any]) -> list[str]:
    metadata = message.get("_metadata")
    metadata_agent = metadata.get("agent") if isinstance(metadata, dict) else None
    if not isinstance(metadata_agent, dict):
        return []
    subagent_ids: list[str] = []
    for value in metadata_agent.values():
        if isinstance(value, str) and value.strip() and value.strip() != "main":
            subagent_ids.append(value.strip())
    return sorted(set(subagent_ids))


def is_empty_or_tool_only_assistant(message: dict[str, Any]) -> bool:
    def has_text(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return True
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        return True
            return False
        return False

    return not has_text(message.get("content")) and not has_text(message.get("reasoning_content"))


def valid_assistant_structure(message: dict[str, Any]) -> tuple[bool, str]:
    missing: list[str] = []
    if "content" not in message:
        missing.append("content")
    if "reasoning_content" not in message:
        missing.append("reasoning_content")

    metadata = message.get("_metadata")
    if not isinstance(metadata, dict):
        missing.append("_metadata")
    else:
        if "timestamp" not in metadata:
            missing.append("_metadata.timestamp")
        tokens = metadata.get("tokens")
        if not isinstance(tokens, dict):
            missing.append("_metadata.tokens")
        else:
            for key in ("input", "output", "reasoning"):
                if key not in tokens:
                    missing.append(f"_metadata.tokens.{key}")
            cache = tokens.get("cache")
            if not isinstance(cache, dict):
                missing.append("_metadata.tokens.cache")
            else:
                for key in ("read", "write"):
                    if key not in cache:
                        missing.append(f"_metadata.tokens.cache.{key}")

    if missing:
        return False, f"缺少必要字段: {', '.join(missing)}"
    return True, ""


def load_messages(session_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON is not an object")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("`messages` field is missing or is not an array")
    return [message for message in messages if isinstance(message, dict)]


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
    return is_candidate_session_file(path) and not any(part in SUBAGENT_DIR_NAMES for part in path.parts)


def should_scan_project_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and path.name not in SKIPPED_SESSION_DIR_NAMES


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


def build_agent(model: Any):
    try:
        from langchain.agents import create_agent
    except ImportError as exc:
        raise RuntimeError("langchain is not installed. Run: python -m pip install -r requirements.txt") from exc

    return create_agent(model=model, tools=[], system_prompt=SYSTEM_PROMPT)


def extract_last_ai_content(agent_result: Any) -> str:
    if isinstance(agent_result, dict):
        messages = agent_result.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            content = getattr(last, "content", None)
            if content is None and isinstance(last, dict):
                content = last.get("content")
            return normalize_content(content)
    return normalize_content(agent_result)


def parse_agent_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("agent response JSON is not an object")
    return parsed


def judge_completion_state(
    agent: Any,
    *,
    message_index: int,
    content: str,
    tool_call_ids: list[str],
    max_content_chars: int,
) -> tuple[str, float, str, list[str]]:
    prompt = (
        "请判断下面这个主轨迹 assistant 节点是否可作为完成态结尾。\n\n"
        f"message_index: {message_index}\n"
        f"tool_call_ids: {tool_call_ids}\n\n"
        "assistant content:\n"
        f"{excerpt(content, max_content_chars)}"
    )
    response = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    raw = extract_last_ai_content(response)
    parsed = parse_agent_json(raw)

    state = parsed.get("state")
    confidence = parsed.get("confidence", 0.0)
    reason = parsed.get("reason", "")
    evidence = parsed.get("evidence", [])
    if state not in {"complete", "partial_complete", "not_complete"}:
        raise ValueError("agent response `state` must be complete, partial_complete, or not_complete")
    if not isinstance(confidence, (int, float)):
        confidence = 0.0
    if not isinstance(reason, str) or not reason.strip():
        reason = "模型未返回明确原因。"
    if not isinstance(evidence, list):
        evidence = []
    normalized_evidence = [str(item) for item in evidence[:5] if str(item).strip()]
    return state, max(0.0, min(1.0, float(confidence))), reason.strip(), normalized_evidence


def check_one_session(
    project_dir: Path,
    session_path: Path,
    agent: Any,
    *,
    max_content_chars: int,
    excerpt_chars: int,
    require_main: bool,
) -> CompletedStateResult:
    result = CompletedStateResult(project_dir=project_dir.name, session_path=str(session_path))
    if not session_path.exists():
        result.valid_session = False
        result.error = "session file does not exist"
        result.reason = result.error
        return result

    try:
        messages = load_messages(session_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.valid_session = False
        result.error = str(exc)
        result.reason = result.error
        return result

    result.message_count = len(messages)
    message_nodes = [
        (index, message, message.get("role"))
        for index, message in enumerate(messages, start=1)
        if isinstance(message.get("role"), str) and message.get("role")
    ]
    if not message_nodes:
        result.valid_session = False
        result.error = "no message nodes found in session"
        result.reason = result.error
        return result

    last_index, last, last_role = message_nodes[-1]
    result.last_index = last_index
    result.last_role = last_role
    result.last_agent = message_agent(last)

    assistant_nodes = [
        (index, message)
        for index, message, role in message_nodes
        if role == "assistant" and (not require_main or message_agent(message) == "main")
    ]
    result.assistant_count = len(assistant_nodes)
    if not assistant_nodes:
        result.valid_session = False
        result.reason = "no main assistant nodes found in session" if require_main else "no assistant nodes found in session"
        result.error = result.reason
        return result

    trailing_non_assistant = result.last_role != "assistant"
    has_content_assistants = False
    model_call_failed = False
    skipped_empty_count = 0
    skipped_structure_count = 0
    deleted_non_tool_judgments: list[dict[str, Any]] = []

    for index, message in reversed(assistant_nodes):
        content = normalize_content(message.get("content"))
        tool_call_ids = extract_tool_call_ids(message)
        subagent_ids = extract_subagent_ids(message)
        base_record: dict[str, Any] = {
            "message_index": index,
            "tool_call_ids": tool_call_ids,
            "subagent_ids": subagent_ids,
            "content_preview": excerpt(content, PREVIEW_CHARS),
        }

        if is_empty_or_tool_only_assistant(message):
            skipped_empty_count += 1
            result.judgments.append(
                {
                    **base_record,
                    "state": "skipped_empty",
                    "confidence": 0.0,
                    "reason": "assistant 节点无有效文本内容（空 content 或仅 tool_calls），跳过判定。",
                    "evidence": [],
                    "deletion_recommended": True,
                    "deletion_reason": "空 content/tool-only assistant 不可作为完成态。",
                }
            )
            continue

        valid_structure, structure_reason = valid_assistant_structure(message)
        if not valid_structure:
            skipped_structure_count += 1
            result.judgments.append(
                {
                    **base_record,
                    "state": "skipped_structure",
                    "confidence": 0.0,
                    "reason": f"assistant 节点结构不完整，不可作为完成态候选。{structure_reason}",
                    "evidence": [],
                    "deletion_recommended": True,
                    "deletion_reason": f"结构不完整: {structure_reason}",
                }
            )
            continue

        error = ""
        try:
            state, confidence, reason, evidence = judge_completion_state(
                agent,
                message_index=index,
                content=content,
                tool_call_ids=tool_call_ids,
                max_content_chars=max_content_chars,
            )
        except Exception as exc:  # noqa: BLE001 - report model/API failures as check failures.
            state = "error"
            confidence = 0.0
            reason = f"completion-state agent failed or timed out after {MODEL_TIMEOUT_SECONDS}s: {exc}"
            evidence = []
            error = str(exc)
            model_call_failed = True

        has_content_assistants = True
        judgment_record: dict[str, Any] = {
            **base_record,
            "state": state,
            "confidence": round(confidence, 3),
            "reason": reason,
            "evidence": evidence,
            "deletion_recommended": state in {"not_complete", "error"},
            "deletion_reason": "",
            "error": error,
        }

        if state in TERMINAL_STATES:
            judgment_record["deletion_recommended"] = False
            result.judgments.append(judgment_record)
            result.selected_state = state
            result.selected_message_index = index
            result.selected_reason = reason
            result.confidence = round(confidence, 3)
            result.reason = reason
            result.content_excerpt = excerpt(content, excerpt_chars)
            break

        if len(deleted_non_tool_judgments) >= MAX_DELETABLE_NON_TOOL_ASSISTANTS:
            judgment_record["deletion_recommended"] = False
            judgment_record["deletion_reason"] = "第三个非 tool_calls assistant 仍未通过完成态判断，不再删除。"
            result.judgments.append(judgment_record)
            result.error = "第三个非 tool_calls assistant 仍未通过完成态判断"
            result.reason = result.error
            break

        if state == "not_complete":
            judgment_record["deletion_reason"] = (
                "该 assistant 不是完成态，清洗逻辑会删除它及其关联 subagent，"
                "然后继续向前寻找可保留的完成态。"
            )
            deleted_non_tool_judgments.append(judgment_record)
        elif state == "error":
            judgment_record["deletion_reason"] = "模型判定失败，因此不选择该 assistant 作为完成态。"

        result.judgments.append(judgment_record)

    if result.selected_state not in TERMINAL_STATES and not result.error:
        error_details: list[str] = []
        if trailing_non_assistant:
            error_details.append(
                f"最近的 assistant 后有尾部 role={result.last_role!r} 消息，且该 assistant 被判为非完成态"
            )
        if model_call_failed:
            error_details.append("模型调用失败导致部分 assistant 无法判定")
        if skipped_empty_count > 0 and not has_content_assistants:
            error_details.append(f"全部 {skipped_empty_count} 个被检查的 assistant 节点都是空 content 或仅有 tool_calls")
        elif skipped_structure_count > 0 and not has_content_assistants:
            error_details.append("被检查的 assistant 节点结构不完整")
        elif has_content_assistants:
            not_complete_count = sum(1 for judgment in result.judgments if judgment.get("state") == "not_complete")
            if not_complete_count:
                error_details.append(f"{not_complete_count} 个有内容的 assistant 被判为 not_complete")
        if not error_details:
            error_details.append("未找到 complete 或 partial_complete 的 assistant 节点")
        result.error = "；".join(error_details)
        result.reason = result.error
    return result


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def message_timestamp(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    metadata = message.get("_metadata")
    timestamp = metadata.get("timestamp") if isinstance(metadata, dict) else None
    return timestamp if isinstance(timestamp, str) else ""


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


def project_id_for_report(source_root: Path, results: list[CompletedStateResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def build_json_report(source_root: Path, results: list[CompletedStateResult], workers: int = DEFAULT_WORKERS) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    failed = total - passed
    tasks: list[dict[str, Any]] = []
    for result in results:
        if result.all_pass:
            continue
        session_path = Path(result.session_path)
        report_index = result.selected_message_index or result.last_index
        if not report_index and result.judgments:
            first_index = result.judgments[0].get("message_index")
            report_index = first_index if isinstance(first_index, int) else None
        message, line = message_at_index(session_path, report_index)
        tasks.append(
            {
                "产物id": result.project_dir,
                "文件id": file_id_for_session(source_root, result.project_dir, session_path),
                "原因": result.reason or result.error,
                "行号": line or 1,
                "role": str(message.get("role", result.last_role)) if message else result.last_role,
                "agent": message_agent(message) if message else result.last_agent,
                "timestamp": message_timestamp(message),
                "工具id": message_tool_id(message),
                "subagent": message_subagent(message),
                "content 原文": normalize_content(message.get("content")) if message else result.content_excerpt,
                "state": result.selected_state,
                "completed": result.selected_state in TERMINAL_STATES,
                "terminal_states": sorted(TERMINAL_STATES),
                "confidence": result.confidence,
                "selected_message_index": result.selected_message_index,
                "last_message_index": result.last_index,
                "last_role": result.last_role,
                "assistant_count": result.assistant_count,
                "judgments": result.judgments,
            }
        )
    state_counts: dict[str, int] = {}
    for result in results:
        key = result.selected_state or "none"
        state_counts[key] = state_counts.get(key, 0) + 1
    skipped_empty = sum(
        1 for result in results for judgment in result.judgments if judgment.get("state") == "skipped_empty"
    )
    skipped_structure = sum(
        1 for result in results for judgment in result.judgments if judgment.get("state") == "skipped_structure"
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
            "model_timeout_seconds": MODEL_TIMEOUT_SECONDS,
            "workers": workers,
            "terminal_states": sorted(TERMINAL_STATES),
            "max_content_chars": MAX_CONTENT_CHARS,
            "preview_chars": PREVIEW_CHARS,
            "max_deletable_non_tool_assistants": MAX_DELETABLE_NON_TOOL_ASSISTANTS,
            "invalid_json_failed": sum(1 for result in results if not result.valid_session),
            "terminal_state_passed": sum(1 for result in results if result.selected_state in TERMINAL_STATES),
            "complete": state_counts.get("complete", 0),
            "partial_complete": state_counts.get("partial_complete", 0),
            "no_terminal_state_failed": sum(
                1 for result in results if result.valid_session and result.selected_state not in TERMINAL_STATES
            ),
            "state_counts": state_counts,
            "skipped_empty": skipped_empty,
            "skipped_structure": skipped_structure,
        },
        "tasks": tasks,
    }


def write_report(
    output_dir: Path,
    source_root: Path,
    results: list[CompletedStateResult],
    *,
    workers: int = DEFAULT_WORKERS,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / REPORT_NAME
    report_path.write_text(
        json.dumps(build_json_report(source_root, results, workers), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


def session_display_name(project_dir: Path, session_path: Path) -> str:
    relative_name = project_dir.name
    if session_path.exists():
        try:
            relative_name = f"{project_dir.name}/{session_path.relative_to(project_dir).as_posix()}"
        except ValueError:
            relative_name = project_dir.name
    return relative_name


def collect_session_jobs(root_dir: Path, session_relative_path: Path) -> list[tuple[Path, Path]]:
    jobs: list[tuple[Path, Path]] = []
    root_session_paths = iter_session_paths(root_dir, session_relative_path)
    project_dirs = (
        [root_dir]
        if root_session_paths or has_session_target(root_dir, session_relative_path)
        else sorted((path for path in root_dir.iterdir() if should_scan_project_dir(path)), key=lambda p: p.name)
    )
    for project_dir in project_dirs:
        session_paths = iter_session_paths(project_dir, session_relative_path)
        if not session_paths:
            missing_session_path = project_dir / session_relative_path
            if missing_session_path.exists():
                continue
            session_paths = [missing_session_path]
        for session_path in session_paths:
            jobs.append((project_dir, session_path))
    return jobs


def completed_state(
    find_path: Path | str,
    session_json_path: Path | str,
    output_dir: Path | str,
    *,
    workers: int = DEFAULT_WORKERS,
) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"find path does not exist or is not a directory: {root_dir}")

    session_relative_path = Path(session_json_path)
    report_output_dir = Path(output_dir).expanduser().resolve()
    worker_count = max(1, int(workers))
    if not is_model_open:
        print("完成态模型判断已关闭，跳过检查。")
        return write_report(report_output_dir, root_dir, [], workers=worker_count)

    model_config = replace(resolve_model_config(), timeout=MODEL_TIMEOUT_SECONDS)
    llm = build_chat_model(model_config)
    agent = build_agent(llm)

    jobs = collect_session_jobs(root_dir, session_relative_path)
    results_by_index: dict[int, CompletedStateResult] = {}
    print(f"checking {len(jobs)} sessions with {worker_count} workers ...", flush=True)

    def run_job(index: int, project_dir: Path, session_path: Path) -> tuple[int, str, CompletedStateResult]:
        result = check_one_session(
            project_dir,
            session_path,
            agent,
            max_content_chars=MAX_CONTENT_CHARS,
            excerpt_chars=PREVIEW_CHARS,
            require_main=False,
        )
        result.project_dir = project_dir.name
        return index, session_display_name(project_dir, session_path), result

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(run_job, index, project_dir, session_path): index
            for index, (project_dir, session_path) in enumerate(jobs)
        }
        for future in as_completed(futures):
            index, relative_name, result = future.result()
            results_by_index[index] = result
            print(f"checked {relative_name} ... {'PASS' if result.all_pass else 'FAIL'}", flush=True)

    results = [results_by_index[index] for index in range(len(jobs))]

    report_path = write_report(report_output_dir, root_dir, results, workers=worker_count)
    passed = sum(1 for result in results if result.all_pass)
    print(f"\nJSON report: {report_path}")
    print(f"scanned: {len(results)}")
    print(f"passed: {passed}")
    print(f"failed: {len(results) - passed}")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check final assistant completion state with a LangChain agent.")
    parser.add_argument("-r", dest="find_path", type=Path, required=True, help="Root containing project subdirectories.")
    parser.add_argument(
        "-path",
        dest="session_json_path",
        type=Path,
        required=True,
        help="Session file or session directory path relative to each project directory.",
    )
    parser.add_argument("-o", dest="output_dir", type=Path, required=True, help="Report output directory.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Concurrent model checks. Default: {DEFAULT_WORKERS}.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        completed_state(
            args.find_path,
            args.session_json_path,
            args.output_dir,
            workers=args.workers,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print actionable failures.
        print(f"check_completed_state failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

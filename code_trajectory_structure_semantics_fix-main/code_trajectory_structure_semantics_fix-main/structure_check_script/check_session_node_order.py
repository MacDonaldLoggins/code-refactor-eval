#!/usr/bin/env python3
# 2025-05-19 slc 修改
r"""session 文件前三节点顺序校验脚本。

检查内容：
  1. 支持 -path 指定单个 session JSON 文件，或指定 session 目录并递归搜索主轨迹文件。
  2. 只检查主轨迹文件本身，不检查 sessions/subagent(s) 目录下的 subagent 文件。
  3. 主轨迹文件前三个消息节点顺序必须为：
       第 1 个节点：role == "system"
       第 2 个节点：role == "user"
       第 3 个节点：role == "assistant"
  4. 不满足则判定为不符合。

使用方式：
  python check\check_session_node_order.py \
    -r <执行路径> \
    -path <session.json 相对路径或 session 目录> \
    -o <报告输出目录>

示例：
  python check\check_session_node_order.py \
    -r C:\Users\15837\Desktop\workspace\sessions_root \
    -path sessions \
    -o C:\Users\15837\Desktop\workspace\reports

参数说明：
  -r
    执行路径。脚本会遍历该目录下的每个子文件夹，并把每个子文件夹视为一个 TASK。

  -path
    session 文件或 session 目录的相对路径。目录会递归搜索主轨迹 JSON 文件。

  -o
    报告输出目录。脚本会在该目录下生成 check_session_node_order.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def _load_exemption_keywords() -> dict[str, list[str]]:
    """加载豁免关键词配置文件"""
    config_path = Path(__file__).parent / "exemption_keywords.json"
    default_keywords = {
        "first_user_keywords": ["Base directory for this skill:", "[Image #"],
        "second_user_keywords": ["Base directory for this skill:", "[Image:"],
    }
    try:
        if config_path.exists():
            content = json.loads(config_path.read_text(encoding="utf-8"))
            session_node_order = content.get("session_node_order", {})
            return {
                "first_user_keywords": session_node_order.get("first_user_keywords", default_keywords["first_user_keywords"]),
                "second_user_keywords": session_node_order.get("second_user_keywords", default_keywords["second_user_keywords"]),
            }
    except (OSError, json.JSONDecodeError):
        pass
    return default_keywords


EXEMPTION_KEYWORDS = _load_exemption_keywords()
REPORT_NAME = "check_session_node_order.json"
REPORT_TYPE = "check_session_node_order_report"
PASSED_SESSIONS_FILE = Path(__file__).parent / "passed_sessions.json"
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
SUBAGENT_DIR_NAMES = {"subagent", "subagents"}
EXPECTED_ROLES = ("system", "user", "assistant")


@dataclass
class SessionNodeOrderResult:
    project_dir: str
    session_path: str
    valid_json: bool = True
    pass_order: bool = True
    error: str = ""
    message_count: int = 0
    first_role: str = ""
    second_role: str = ""
    third_role: str = ""

    @property
    def all_pass(self) -> bool:
        return self.valid_json and self.pass_order

    @property
    def actual_roles(self) -> tuple[str, str, str]:
        return (self.first_role, self.second_role, self.third_role)


def load_messages(session_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{session_path} 顶层不是 JSON object")

    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError(f"{session_path} 缺少 messages 数组")

    return [message for message in messages if isinstance(message, dict)]


def message_role(message: dict[str, Any] | None) -> str:
    if not isinstance(message, dict):
        return ""
    role = message.get("role")
    return role if isinstance(role, str) else ""


def _get_message_content(message: dict[str, Any] | None) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    return ""


def _add_passed_session(session_path: Path) -> None:
    """将通过豁免判定的session路径追加到passed_sessions.json"""
    try:
        if PASSED_SESSIONS_FILE.exists():
            content = json.loads(PASSED_SESSIONS_FILE.read_text(encoding="utf-8"))
            if not isinstance(content, list):
                content = []
        else:
            content = []
        session_str = str(session_path)
        if session_str not in content:
            content.append(session_str)
            PASSED_SESSIONS_FILE.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def check_one_session(project_dir: Path, session_path: Path) -> SessionNodeOrderResult:
    result = SessionNodeOrderResult(project_dir=project_dir.name, session_path=str(session_path))
    if not session_path.exists():
        result.valid_json = False
        result.pass_order = False
        result.error = "session 文件不存在"
        return result

    try:
        messages = load_messages(session_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.valid_json = False
        result.pass_order = False
        result.error = str(exc)
        return result

    result.message_count = len(messages)
    result.first_role = message_role(messages[0] if len(messages) > 0 else None)
    result.second_role = message_role(messages[1] if len(messages) > 1 else None)
    result.third_role = message_role(messages[2] if len(messages) > 2 else None)

    if len(messages) < 3:
        result.pass_order = False
        result.error = f"messages 有效节点不足 3 个，实际为 {len(messages)} 个"
        return result

    # 先检查标准顺序
    result.pass_order = result.actual_roles == EXPECTED_ROLES

    # 如果不符合标准顺序，检查是否为特殊情形
    if not result.pass_order:
        # 特殊情况：system -> user -> user
        if result.actual_roles == ("system", "user", "user"):
            second_content = _get_message_content(messages[1])
            third_content = _get_message_content(messages[2])

            # 条件1：第二个或第三个content以豁免关键词开头
            first_match = any(second_content.startswith(kw) for kw in EXEMPTION_KEYWORDS["first_user_keywords"])
            second_match = any(third_content.startswith(kw) for kw in EXEMPTION_KEYWORDS["second_user_keywords"])

            if first_match or second_match:
                result.pass_order = True
                _add_passed_session(session_path)

    if not result.pass_order:
        result.error = (
            f"前三个节点 role={result.actual_roles!r}，"
            f"期望 role={EXPECTED_ROLES!r}"
        )
    return result


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


def project_id_for_report(source_root: Path, results: list[SessionNodeOrderResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def build_json_report(source_root: Path, results: list[SessionNodeOrderResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    failed = total - passed
    tasks: list[dict[str, Any]] = []
    for result in results:
        if result.all_pass:
            continue
        session_path = Path(result.session_path)
        message, line = message_at_index(session_path, 1)
        tasks.append(
            {
                "产物id": result.project_dir,
                "文件id": file_id_for_session(source_root, result.project_dir, session_path),
                "原因": result.error,
                "行号": line or 1,
                "role": str(message.get("role", "")) if message else result.first_role,
                "agent": message_agent(message),
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
            "issues": len(tasks),
            "invalid_json_failed": sum(1 for result in results if not result.valid_json),
            "node_order_failed": sum(1 for result in results if result.valid_json and not result.pass_order),
        },
        "tasks": tasks,
    }


def session_node_order(find_path: Path | str, session_json_path: Path | str, output_dir: Path | str) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"执行路径不存在或不是目录: {root_dir}")

    session_relative_path = Path(session_json_path)
    report_output_dir = Path(output_dir).expanduser().resolve()

    results: list[SessionNodeOrderResult] = []
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
    print(f"扫描主轨迹数: {len(results)}")
    print(f"通过数: {passed}")
    print(f"不通过数: {len(results) - passed}")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量检查主轨迹前三个节点是否为 system -> user -> assistant")
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
        help="每个 TASK 子文件夹里的 session 文件或 session 目录相对路径。目录会递归搜索主轨迹。",
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
        session_node_order(args.find_path, args.session_json_path, args.output_dir)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

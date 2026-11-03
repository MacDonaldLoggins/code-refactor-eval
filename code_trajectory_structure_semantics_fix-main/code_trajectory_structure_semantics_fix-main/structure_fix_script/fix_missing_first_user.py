#!/usr/bin/env python3
"""Insert a missing first user message into sessions reported by pipeline_report_summary.md.

Usage:
  python fix_missing_first_user.py
  python fix_missing_first_user.py --limit 2
  python fix_missing_first_user.py --apply
  python fix_missing_first_user.py -r test/4-20260525-fail-1.md --output fix_missing_first_user_report.json

Arguments:
  -r PATH / --report PATH            Markdown report to scan. Default: test/4-20260525-fail-1.md
  --output PATH / -output PATH       JSON result report path. Default: fix_missing_first_user_report.json
  --min-chars N / -min-chars N        Minimum generated content length. Default: 20
  --limit N / -limit N               Process only the first N matched issues. Default 0 means all.
  --apply / -apply                   Write generated user message back to session JSON files.

Notes:
  - Without --apply this script only performs a dry run and writes a JSON report.
  - Model settings are loaded directly from the repository root .env by this standalone script.

The script finds rows like:
  基础验收标准 | missing first user message | ...json |

For each referenced session JSON, it reads the first assistant message (including
tool_calls) to infer what the user asked, then calls the configured OpenAI-compatible
model to generate the user message content and inserts it between the system message
and the first assistant message. By default this is a dry run; pass --apply to write files.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import requests


DEFAULT_REPORT_PATH = Path(__file__).resolve().parent.parent / "test" / "4-20260525-fail-1.md"
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "fix_missing_first_user_report.json"
ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"

DEFAULT_PROFILE = "LongCat"
LONGCAT_PROFILE = "LongCat"
MINDFLOW_PROFILE = "MindFlow_deepseek"

MODEL_TEMPERATURE = 0.2
MODEL_TIMEOUT_SECONDS = 60
MODEL_MAX_ATTEMPTS = 2
MIN_TARGET_CHARS = 20
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
CONCURRENCY = 16

MISSING_FIRST_USER_RE = re.compile(r"missing first user message(?!\s+for SHA-256)", re.I)
NODE_ORDER_RE = re.compile(r"前三个节点 role=\('system',\s*'assistant',\s*'tool'\)", re.I)

SYSTEM_PROMPT = """你负责根据 assistant 的第一条回复内容，推断并生成用户最初的提问内容。

只返回严格 JSON，不要输出 Markdown。

返回字段：
- content: 生成的用户提问内容
- reason: 简短说明推断依据

要求：
1. content 必须是英文，长度至少 20 个字符。
2. 必须根据 assistant 的回复内容（包括 tool_calls 调用的工具名、参数、描述等）推断用户真正想做的事情。
3. 表达应像用户原本提出的任务，不要出现"根据轨迹""我推断"等元说明。
4. 生成的内容应当自然、合理，与 assistant 的回复能形成合理的对话上下文。
5. 不要新增无依据的业务需求、技术方案、文件名或验收结果。
"""


def load_env_file(path: Path = ENV_FILE_PATH) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise ValueError(f"Missing required model config env var: {name}")


def model_profile() -> str:
    explicit = os.environ.get("CLAUDE_OPENAI_MODEL_PROFILE")
    if explicit:
        return explicit
    if all(os.environ.get(name) for name in ("M_MODEL_NAME", "M_MODEL_BASE_URL", "M_MODEL_API_KEY")):
        return LONGCAT_PROFILE
    if all(os.environ.get(name) for name in ("MODEL_NAME", "MODEL_BASE_URL", "MODEL_API_KEY")):
        return MINDFLOW_PROFILE
    return DEFAULT_PROFILE


def model_profile_config() -> tuple[str, str, str]:
    profile = model_profile()
    if profile == LONGCAT_PROFILE:
        return (
            required_env("M_MODEL_NAME"),
            required_env("M_MODEL_BASE_URL").rstrip("/"),
            required_env("M_MODEL_API_KEY"),
        )
    if profile == MINDFLOW_PROFILE:
        return (
            required_env("MODEL_NAME"),
            required_env("MODEL_BASE_URL").rstrip("/"),
            required_env("MODEL_API_KEY"),
        )
    raise ValueError(
        f"Unknown model profile {profile!r}; set CLAUDE_OPENAI_MODEL_PROFILE "
        f"to {LONGCAT_PROFILE!r} or {MINDFLOW_PROFILE!r}"
    )


def model_name() -> str:
    return model_profile_config()[0]


def model_base_url() -> str:
    return model_profile_config()[1]


def model_api_key() -> str:
    return model_profile_config()[2]


@dataclass
class MissingFirstUserIssue:
    task_id: str
    session_path: Path
    line_number: str
    anomaly_type: str = "missing_first_user"
    first_assistant_content: str = ""
    generated_content: str = ""
    reason: str = ""
    status: str = "pending"
    error: str = ""


def parse_markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return cells if len(cells) >= 4 else None


def find_missing_first_user_issues(report_path: Path) -> list[MissingFirstUserIssue]:
    issues: list[MissingFirstUserIssue] = []
    current_task_id = ""
    seen_paths: set[Path] = set()

    for raw_line in report_path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^###\s+(TASK-[A-Za-z0-9-]+|ERROR-TASK)\s*$", raw_line.strip())
        if heading:
            current_task_id = heading.group(1)
            continue

        cells = parse_markdown_table_row(raw_line)
        if not cells or cells[0] in {"检查项", "---"}:
            continue
        check_item, reason, session_path_text = cells[0], cells[1], cells[2]
        line_number = cells[3] if len(cells) > 3 else ""

        anomaly_type = ""
        if check_item == "基础验收标准" and MISSING_FIRST_USER_RE.search(reason):
            anomaly_type = "missing_first_user"
        elif check_item == "session 节点顺序" and NODE_ORDER_RE.search(reason):
            anomaly_type = "node_order"

        if not anomaly_type:
            continue

        session_path = Path(session_path_text)
        if session_path in seen_paths:
            continue
        seen_paths.add(session_path)
        issues.append(
            MissingFirstUserIssue(
                task_id=current_task_id,
                session_path=session_path,
                line_number=line_number,
                anomaly_type=anomaly_type,
            )
        )
    return issues


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
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return "\n".join(part for part in parts if part)
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def compact_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def extract_tool_calls_summary(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list):
        return ""
    parts: list[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function")
        if not isinstance(func, dict):
            continue
        name = func.get("name", "")
        args_raw = func.get("arguments", "")
        if isinstance(args_raw, str):
            try:
                args_dict = json.loads(args_raw)
            except json.JSONDecodeError:
                args_dict = {"raw": args_raw}
        elif isinstance(args_raw, dict):
            args_dict = args_raw
        else:
            args_dict = {}
        desc = args_dict.get("description", "")
        prompt = args_dict.get("prompt", "")
        summary = f"tool={name}"
        if desc:
            summary += f"; description={compact_text(str(desc), 300)}"
        if prompt:
            summary += f"; prompt={compact_text(str(prompt), 500)}"
        if not desc and not prompt:
            summary += f"; args={compact_text(json.dumps(args_dict, ensure_ascii=False), 500)}"
        parts.append(summary)
    return "\n".join(parts)


def collect_assistant_context(messages: list[Any], max_messages: int = 6) -> str:
    snippets: list[str] = []
    start_index = 0
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "system":
            start_index = i + 1
            break

    for offset, msg in enumerate(messages[start_index:start_index + max_messages], start=1):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        agent = msg.get("agent", "")
        content = compact_text(normalize_content(msg.get("content")), 500)
        reasoning = compact_text(normalize_content(msg.get("reasoning_content")), 300)
        tool_calls_summary = extract_tool_calls_summary(msg.get("tool_calls"))

        snippet = f"{offset}. role={role}; agent={agent}"
        if content:
            snippet += f"; content={content}"
        if reasoning:
            snippet += f"; reasoning={reasoning}"
        if tool_calls_summary:
            snippet += f"\n   tool_calls:\n   {tool_calls_summary}"
        snippets.append(snippet)
    return "\n".join(snippets)


def parse_model_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("model response is not a JSON object")
    return parsed


def post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int
) -> tuple[int, dict[str, Any]]:
    request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = requests.post(url, data=request_body, headers=headers, timeout=timeout)
    return response.status_code, json.loads(response.text)


def call_chat_completion(prompt: str) -> str:
    request_url = f"{model_base_url()}/chat/completions"
    headers = {
        "Authorization": f"Bearer {model_api_key()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name(),
        "temperature": MODEL_TEMPERATURE,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    last_error: Exception | None = None
    for attempt in range(1, MODEL_MAX_ATTEMPTS + 1):
        try:
            status_code, response_payload = post_json(request_url, headers, payload, MODEL_TIMEOUT_SECONDS)
            if status_code in RETRYABLE_HTTP_STATUS_CODES and attempt < MODEL_MAX_ATTEMPTS:
                time.sleep(attempt)
                continue
            if status_code < 200 or status_code >= 300:
                raise RuntimeError(f"model HTTP {status_code}: {response_payload}")
            choices = response_payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise RuntimeError("model response missing choices")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("model response missing message.content")
            return content
        except (requests.RequestException, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < MODEL_MAX_ATTEMPTS:
                time.sleep(attempt)
                continue
            break
    raise RuntimeError(f"model request failed: {last_error}")


load_env_file()


def generate_user_content(assistant_context: str, min_chars: int) -> tuple[str, str]:
    prompt = (
        "以下是一段会话轨迹中 system 消息之后的前几条消息。\n"
        "该会话缺少第一条 user 消息（system 后面直接是 assistant）。\n"
        "请根据 assistant 的回复内容推断用户最初的提问，并生成该 user 消息内容。\n\n"
        "会话轨迹摘要：\n"
        f"{assistant_context}\n\n"
        f"生成的 content 长度至少 {min_chars} 个字符。"
    )
    raw_content = call_chat_completion(prompt).strip()
    try:
        parsed = parse_model_json(raw_content)
        content = str(parsed.get("content", "")).strip()
        reason = str(parsed.get("reason", "")).strip()
    except (json.JSONDecodeError, ValueError):
        content = raw_content.strip()
        reason = "model returned plain text instead of JSON"
    if len(content) < min_chars:
        raise ValueError(f"generated content is only {len(content)} chars: {content!r}")
    return content, reason


def find_insert_index(messages: list[Any]) -> int:
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "system":
            return i + 1
    return 0


def build_user_message(content: str, first_assistant: dict[str, Any]) -> dict[str, Any]:
    user_msg: dict[str, Any] = {
        "role": "user",
        "agent": first_assistant.get("agent", "main"),
        "content": content,
    }
    metadata = first_assistant.get("_metadata")
    if isinstance(metadata, dict):
        ts = metadata.get("timestamp")
        if isinstance(ts, str):
            user_msg["_metadata"] = {"timestamp": ts}
    return user_msg


def insert_user_message(session_path: Path, generated_content: str, *, apply: bool) -> None:
    document = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
        raise ValueError("session JSON must be an object with messages array")
    messages = document["messages"]
    insert_idx = find_insert_index(messages)

    first_assistant = None
    for msg in messages[insert_idx:]:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            first_assistant = msg
            break
    if first_assistant is None:
        raise ValueError("no assistant message found after system")

    user_msg = build_user_message(generated_content, first_assistant)
    messages.insert(insert_idx, user_msg)

    if apply:
        session_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def process_issue(
    issue: MissingFirstUserIssue,
    *,
    apply: bool,
    min_chars: int,
) -> MissingFirstUserIssue:
    try:
        if not issue.session_path.exists():
            raise FileNotFoundError(f"session file not found: {issue.session_path}")
        document = json.loads(issue.session_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
            raise ValueError("session JSON must be an object with messages array")
        messages = document["messages"]

        insert_idx = find_insert_index(messages)
        first_role_after_system = None
        for m in messages[insert_idx:]:
            if isinstance(m, dict):
                first_role_after_system = m.get("role")
                break
        if first_role_after_system == "user":
            issue.status = "skipped_has_user"
            return issue

        assistant_context = collect_assistant_context(messages)
        if not assistant_context.strip():
            raise ValueError("no assistant context found to infer user intent")

        issue.first_assistant_content = compact_text(assistant_context, 500)
        issue.generated_content, issue.reason = generate_user_content(
            assistant_context, min_chars
        )
        insert_user_message(issue.session_path, issue.generated_content, apply=apply)
        issue.status = "updated" if apply else "dry_run"
    except Exception as exc:
        issue.status = "error"
        issue.error = str(exc)
    return issue


def write_report(output_path: Path, issues: list[MissingFirstUserIssue], *, apply: bool) -> None:
    payload = {
        "apply": apply,
        "model": {
            "model": model_name(),
            "base_url": model_base_url(),
            "temperature": MODEL_TEMPERATURE,
        },
        "summary": {
            "total": len(issues),
            "updated": sum(1 for i in issues if i.status == "updated"),
            "dry_run": sum(1 for i in issues if i.status == "dry_run"),
            "skipped": sum(1 for i in issues if i.status.startswith("skipped")),
            "errors": sum(1 for i in issues if i.status == "error"),
        },
        "results": [
            {
                **asdict(issue),
                "session_path": str(issue.session_path),
            }
            for issue in issues
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use AI to generate a missing first user message based on the first assistant response."
    )
    parser.add_argument("-r", "--report", type=Path, default=DEFAULT_REPORT_PATH, help=f"Default: {DEFAULT_REPORT_PATH}")
    parser.add_argument("--output", "-output", type=Path, default=DEFAULT_OUTPUT_PATH, help=f"Default: {DEFAULT_OUTPUT_PATH}")
    parser.add_argument("--min-chars", "-min-chars", type=int, default=MIN_TARGET_CHARS, help="Default: 20")
    parser.add_argument("--apply", "-apply", action="store_true", help="Write generated user message back to session JSON files.")
    parser.add_argument("--limit", "-limit", type=int, default=0, help="Only process the first N matched issues; 0 means all.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report_path = args.report.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    issues = find_missing_first_user_issues(report_path)
    if args.limit > 0:
        issues = issues[: args.limit]

    print(f"matched issues: {len(issues)}")

    min_chars = max(1, int(args.min_chars))
    completed = 0
    lock = Lock()

    def run_one(issue: MissingFirstUserIssue) -> MissingFirstUserIssue:
        nonlocal completed
        result = process_issue(issue, apply=args.apply, min_chars=min_chars)
        with lock:
            completed += 1
            index = completed
        print(f"[{index}/{len(issues)}] {issue.session_path}")
        print(f"  status: {issue.status}")
        if issue.generated_content:
            print(f"  generated: {compact_text(issue.generated_content, 120)!r}")
        if issue.reason:
            print(f"  reason: {issue.reason}")
        if issue.error:
            print(f"  error: {issue.error}", file=sys.stderr)
        return result

    if issues:
        with ThreadPoolExecutor(max_workers=min(CONCURRENCY, len(issues))) as executor:
            list(executor.map(run_one, issues))

    write_report(output_path, issues, apply=args.apply)
    print(f"\nreport: {output_path}")
    if not args.apply:
        print("dry run only; pass --apply to write session JSON files")
    return 1 if any(issue.status == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

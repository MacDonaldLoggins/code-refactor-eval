#!/usr/bin/env python3
"""Expand short first-user prompts reported by pipeline_report_summary.md.

Usage:
  python claude_openai_expand_short_first_user.py
  python claude_openai_expand_short_first_user.py --limit 2
  python claude_openai_expand_short_first_user.py --apply
  python claude_openai_expand_short_first_user.py -r 20260512/pipeline_report_summary.md --min-chars 30 --output expand_report.json

Arguments:
  -r PATH / --report PATH            Markdown report to scan. Default: 20260512/pipeline_report_summary.md
  --output PATH / -output PATH       JSON result report path. Default: claude_openai_expand_short_first_user_report.json
  --min-chars N / -min-chars N       Minimum expanded content length. Default: 30
  --limit N / -limit N               Process only the first N matched issues. Default 0 means all.
  --apply / -apply                   Write expanded content back to the referenced session JSON files.

Notes:
  - Without --apply this script only performs a dry run and writes a JSON report.
  - Model settings are loaded directly from the repository root .env by this standalone script.

The script finds rows like:
  基础验收标准 | first user content length is 10, less than 20 | ...json | 10

For each referenced session JSON, it asks the configured OpenAI-compatible model
to rewrite the first user content so it keeps the original intent and reaches at
least 30 characters. By default this is a dry run; pass --apply to write files.
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


DEFAULT_REPORT_PATH = Path(__file__).resolve().parent / "20260512" / "pipeline_report_summary.md"
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "claude_openai_expand_short_first_user_report.json"
ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"

DEFAULT_PROFILE = "LongCat"
LONGCAT_PROFILE = "LongCat"
MINDFLOW_PROFILE = "MindFlow_deepseek"

MODEL_TEMPERATURE = 0.2
MODEL_TIMEOUT_SECONDS = 60
MODEL_MAX_ATTEMPTS = 2
MIN_TARGET_CHARS = 30
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
CONCURRENCY = 16

SHORT_FIRST_USER_RE = re.compile(r"first user content length is\s+(\d+),\s+less than\s+20", re.I)

SYSTEM_PROMPT = """你负责扩写会话轨迹中的第一条用户需求。

只返回严格 JSON，不要输出 Markdown。

返回字段：
- content: 扩写后的用户内容
- reason: 简短说明扩写依据

要求：
1. content 必须是英文，长度至少 30 个字符。
2. 必须保留原始用户意图，不能新增无依据的业务需求、技术方案、文件名或验收结果。
3. 可以结合后续 assistant/tool 轨迹推断用户真正想做的事情。
4. 表达应像用户原本提出的任务，不要出现“根据轨迹”“我推断”等元说明。
5. 如果原始内容是中文，也要翻译并扩写成自然英文。
"""


def load_env_file(path: Path = ENV_FILE_PATH) -> None:
    """Load simple KEY=VALUE lines from the repository root .env."""
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
    profile_name, _profile_base_url, _profile_api_key = model_profile_config()
    return profile_name


def model_base_url() -> str:
    _profile_name, profile_base_url, _profile_api_key = model_profile_config()
    return profile_base_url


def model_api_key() -> str:
    _profile_name, _profile_base_url, profile_api_key = model_profile_config()
    return profile_api_key


@dataclass
class ShortContentIssue:
    task_id: str
    session_path: Path
    line_number: str
    reported_length: int
    original_content: str = ""
    expanded_content: str = ""
    reason: str = ""
    status: str = "pending"
    error: str = ""


def parse_markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return cells if len(cells) >= 4 else None


def find_short_first_user_issues(report_path: Path) -> list[ShortContentIssue]:
    issues: list[ShortContentIssue] = []
    current_task_id = ""
    seen_paths: set[Path] = set()

    for raw_line in report_path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^###\s+(TASK-[A-Za-z0-9-]+)\s*$", raw_line.strip())
        if heading:
            current_task_id = heading.group(1)
            continue

        cells = parse_markdown_table_row(raw_line)
        if not cells or cells[0] in {"检查项", "---"}:
            continue
        check_item, reason, session_path_text, line_number = cells[:4]
        match = SHORT_FIRST_USER_RE.search(reason)
        if check_item != "基础验收标准" or not match:
            continue

        session_path = Path(session_path_text)
        if session_path in seen_paths:
            continue
        seen_paths.add(session_path)
        issues.append(
            ShortContentIssue(
                task_id=current_task_id,
                session_path=session_path,
                line_number=line_number,
                reported_length=int(match.group(1)),
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


def find_first_user_message(messages: list[Any]) -> tuple[int, dict[str, Any]]:
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user" and message.get("agent", "main") == "main":
            return index, message
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return index, message
    raise ValueError("session has no user message")


def collect_context(messages: list[Any], first_user_index: int, max_messages: int = 8) -> str:
    snippets: list[str] = []
    for offset, message in enumerate(messages[first_user_index + 1 : first_user_index + 1 + max_messages], start=1):
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        agent = message.get("agent", "")
        content = compact_text(normalize_content(message.get("content")), 500)
        tool_names: list[str] = []
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict):
                function = tool_call.get("function")
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(name, str):
                    tool_names.append(name)
        tool_suffix = f"; tool_calls={tool_names}" if tool_names else ""
        snippets.append(f"{offset}. role={role}; agent={agent}{tool_suffix}; content={content}")
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


def parse_plain_model_content(text: str) -> tuple[str, str]:
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
        content = re.sub(r"\s*```$", "", content).strip()

    labeled = re.match(
        r'^content:\s*(?P<content>"(?:[^"\\]|\\.)*"|.*?)(?:\nreason:\s*(?P<reason>.*))?$',
        content,
        flags=re.I | re.S,
    )
    if labeled:
        content_text = labeled.group("content").strip()
        reason_text = (labeled.group("reason") or "").strip()
        try:
            content_text = json.loads(content_text)
        except json.JSONDecodeError:
            content_text = content_text.strip('"')
        return str(content_text).strip(), reason_text or "model returned labeled plain text"

    return content, "model returned plain text instead of JSON"


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


def expand_content(original_content: str, context: str, min_chars: int) -> tuple[str, str]:
    prompt = (
        f"原始第一条用户内容：{original_content!r}\n"
        f"目标最小长度：{min_chars} 个字符\n\n"
        "后续轨迹摘要：\n"
        f"{context}\n\n"
        "请扩写为一条自然的英文用户任务描述。"
    )
    raw_content = call_chat_completion(prompt).strip()
    try:
        parsed = parse_model_json(raw_content)
        content = str(parsed.get("content", "")).strip()
        reason = str(parsed.get("reason", "")).strip()
    except (json.JSONDecodeError, ValueError):
        content, reason = parse_plain_model_content(raw_content)
    if len(content) < min_chars:
        raise ValueError(f"expanded content is only {len(content)} chars: {content!r}")
    return content, reason


def update_first_user_content(session_path: Path, expanded_content: str, *, apply: bool) -> None:
    document = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
        raise ValueError("session JSON must be an object with messages array")
    _index, message = find_first_user_message(document["messages"])
    original = message.get("content")
    if isinstance(original, list) and original and isinstance(original[0], dict) and "text" in original[0]:
        original[0]["text"] = expanded_content
    else:
        message["content"] = expanded_content
    if apply:
        session_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def process_issue(
    issue: ShortContentIssue, *, apply: bool, min_chars: int
) -> ShortContentIssue:
    try:
        document = json.loads(issue.session_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
            raise ValueError("session JSON must be an object with messages array")
        first_user_index, first_user = find_first_user_message(document["messages"])
        issue.original_content = normalize_content(first_user.get("content")).strip()
        if len(issue.original_content) >= min_chars:
            issue.status = "skipped_already_long_enough"
            return issue
        context = collect_context(document["messages"], first_user_index)
        issue.expanded_content, issue.reason = expand_content(issue.original_content, context, min_chars)
        update_first_user_content(issue.session_path, issue.expanded_content, apply=apply)
        issue.status = "updated" if apply else "dry_run"
    except Exception as exc:  # noqa: BLE001 - keep processing other sessions.
        issue.status = "error"
        issue.error = str(exc)
    return issue


def write_report(output_path: Path, issues: list[ShortContentIssue], *, apply: bool) -> None:
    payload = {
        "apply": apply,
        "model": {
            "model": model_name(),
            "base_url": model_base_url(),
            "temperature": MODEL_TEMPERATURE,
        },
        "summary": {
            "total": len(issues),
            "updated": sum(1 for item in issues if item.status == "updated"),
            "dry_run": sum(1 for item in issues if item.status == "dry_run"),
            "skipped": sum(1 for item in issues if item.status.startswith("skipped")),
            "errors": sum(1 for item in issues if item.status == "error"),
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
        description="Use AI to expand too-short first user content listed in pipeline_report_summary.md."
    )
    parser.add_argument("-r", "--report", type=Path, default=DEFAULT_REPORT_PATH, help=f"Default: {DEFAULT_REPORT_PATH}")
    parser.add_argument("--output", "-output", type=Path, default=DEFAULT_OUTPUT_PATH, help=f"Default: {DEFAULT_OUTPUT_PATH}")
    parser.add_argument("--min-chars", "-min-chars", type=int, default=MIN_TARGET_CHARS, help="Default: 30")
    parser.add_argument("--apply", "-apply", action="store_true", help="Write expanded content back to session JSON files.")
    parser.add_argument("--limit", "-limit", type=int, default=0, help="Only process the first N matched issues; 0 means all.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report_path = args.report.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    issues = find_short_first_user_issues(report_path)
    if args.limit > 0:
        issues = issues[: args.limit]

    print(f"matched issues: {len(issues)}")

    min_chars = max(1, int(args.min_chars))
    completed = 0
    lock = Lock()

    def run_one(issue: ShortContentIssue) -> ShortContentIssue:
        nonlocal completed
        result = process_issue(issue, apply=args.apply, min_chars=min_chars)
        with lock:
            completed += 1
            index = completed
        print(f"[{index}/{len(issues)}] {issue.session_path}")
        print(f"  {issue.status}: {issue.original_content!r} -> {issue.expanded_content!r}")
        if issue.error:
            print(f"  error: {issue.error}", file=sys.stderr)
        return result

    if issues:
        with ThreadPoolExecutor(max_workers=min(CONCURRENCY, len(issues))) as executor:
            list(executor.map(run_one, issues))

    write_report(output_path, issues, apply=args.apply)
    print(f"report: {output_path}")
    if not args.apply:
        print("dry run only; pass --apply to write session JSON files")
    return 1 if any(issue.status == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

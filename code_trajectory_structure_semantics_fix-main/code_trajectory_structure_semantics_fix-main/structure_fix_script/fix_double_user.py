#!/usr/bin/env python3
"""Merge consecutive user messages (system, user, user) reported by pipeline_report_summary.md.

Usage:
  python fix_double_user.py
  python fix_double_user.py --limit 2
  python fix_double_user.py --apply
  python fix_double_user.py -r test/前三个节点.md --output fix_double_user_report.json

Arguments:
  -r PATH / --report PATH            Markdown report to scan. Default: test/前三个节点.md
  --output PATH / -output PATH       JSON result report path. Default: fix_double_user_report.json
  --min-chars N / -min-chars N        Minimum merged content length. Default: 20
  --limit N / -limit N               Process only the first N matched issues. Default 0 means all.
  --apply / -apply                   Write merged content back to the referenced session JSON files.

Notes:
  - Without --apply this script only performs a dry run and writes a JSON report.
  - Only handles the ('system', 'user', 'user') pattern; skips other role-order anomalies.
  - Model settings are loaded directly from the repository root .env by this standalone script.

The script finds rows like:
  session 节点顺序 | 前三个节点 role=('system', 'user', 'user')，... | ...json | 3

For each referenced session JSON, it asks the configured OpenAI-compatible model
whether the two consecutive user messages can be merged into one. If yes, the model
returns the merged content. The second user message is then removed. By default this
is a dry run; pass --apply to write files.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

# Fix Windows console UTF-8 encoding issues
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


DEFAULT_REPORT_PATH = Path(__file__).resolve().parent / "test" / "前三个节点.md"
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "fix_double_user_report.json"
ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"

DEFAULT_PROFILE = "LongCat"
LONGCAT_PROFILE = "LongCat"
MINDFLOW_PROFILE = "MindFlow_deepseek"

MODEL_TEMPERATURE = 0.2
MODEL_TIMEOUT_SECONDS = 60
MODEL_MAX_ATTEMPTS = 2
MIN_TARGET_CHARS = 20
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}

DOUBLE_USER_RE = re.compile(
    r"前三个节点\s+role=\('system',\s*'user',\s*'user'\)", re.I
)

SYSTEM_PROMPT = """你是一个会话数据质量审核员。你的任务是判断两条连续的 user 消息   是否可以合并为一条。

只返回严格 JSON，不要输出 Markdown。

返回字段：
- mergeable: boolean，是否可以合并
- content: 合并后的用户内容（仅 mergeable=true 时需要）
- reason: 简短说明判断依据

判断规则：
1. 如果两条消息表达相同意图（一条是格式化版本，另一条是纯文本），可以合并，保留信息更完整的版本。
2. 如果第二条消息很短（如 "." 或只有几个字符），明显是误触发，可以合并，保留第一条的内容。
3. 如果第一条是斜杠命令（如 "/plan"），第二条是实际任务内容，可以合并，保留第二条内容。
4. 如果第一条是简短/粗略表达，第二条是同一意图的更详细表达，可以合并，保留更详细的版本。
5. 如果两条消息表达完全不同的意图，不可以合并。

合并要求：
- content 必须是自然的用户任务描述（英文），长度至少 20 个字符。
- 必须保留原始用户意图，不能新增无依据的内容。
- 如果原始内容是中文，也要翻译成自然英文。
"""

SYSTEM_PROMPT_ASSISTANT_CHECK = """你是一个会话数据质量审核员。你的任务是根据 assistant 的回复内容，判断它实际回应了哪条 user 消息。

只返回严格 JSON，不要输出 Markdown。

返回字段：
- related_to: "both" | "user1" | "user2" | "neither"
- content: 合并后的用户内容（仅 related_to="both" 时需要）
- reason: 简短说明判断依据

判断规则：
1. 如果 assistant 的回复明确同时回应了两条 user 消息的意图，则 related_to="both"，并提供合并后的用户内容。
2. 如果 assistant 的回复只回应了第一条 user 消息的意图，则 related_to="user1"。
3. 如果 assistant 的回复只回应了第二条 user 消息的意图，则 related_to="user2"。
4. 如果 assistant 的回复与两条 user 消息都没有明显关联，则 related_to="neither"。

合并要求（仅 related_to="both" 时适用）：
- content 必须是自然的用户任务描述（英文），长度至少 20 个字符。
- 必须保留原始用户意图，不能新增无依据的内容。
- 如果原始内容是中文，也要翻译成自然英文。
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
    return model_profile_config()[0]


def model_base_url() -> str:
    return model_profile_config()[1]


def model_api_key() -> str:
    return model_profile_config()[2]


@dataclass
class DoubleUserIssue:
    task_id: str
    session_path: Path
    line_number: str
    user1_content: str = ""
    user2_content: str = ""
    merged_content: str = ""
    mergeable: bool = False
    reason: str = ""
    status: str = "pending"
    error: str = ""
    assistant_content: str = ""


def parse_markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return cells if len(cells) >= 4 else None


def find_double_user_issues(report_path: Path) -> list[DoubleUserIssue]:
    issues: list[DoubleUserIssue] = []
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
        if not DOUBLE_USER_RE.search(reason):
            continue

        session_path = Path(session_path_text)
        if session_path in seen_paths:
            continue
        seen_paths.add(session_path)
        issues.append(
            DoubleUserIssue(
                task_id=current_task_id,
                session_path=session_path,
                line_number=line_number,
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


def find_first_two_users(messages: list[Any]) -> tuple[int, dict, int, dict]:
    """Return (index1, msg1, index2, msg2) for the first two user messages after system."""
    user_indices: list[int] = []
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            user_indices.append(i)
            if len(user_indices) == 2:
                break
    if len(user_indices) < 2:
        raise ValueError("session does not have two consecutive user messages")
    i1, i2 = user_indices
    return i1, messages[i1], i2, messages[i2]


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


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> tuple[int, dict[str, Any]]:
    request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(url, data=request_body, headers=headers, method="POST")
    try:
        with request.urlopen(http_request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body)
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        try:
            response_payload = json.loads(response_body)
        except json.JSONDecodeError:
            response_payload = {"error": response_body}
        return exc.code, response_payload


def call_chat_completion(prompt: str, system_prompt: str = SYSTEM_PROMPT) -> str:
    request_url = f"{model_base_url()}/chat/completions"
    headers = {
        "Authorization": f"Bearer {model_api_key()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name(),
        "temperature": MODEL_TEMPERATURE,
        "messages": [
            {"role": "system", "content": system_prompt},
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
        except (error.URLError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < MODEL_MAX_ATTEMPTS:
                time.sleep(attempt)
                continue
            break
    raise RuntimeError(f"model request failed: {last_error}")


load_env_file()


def judge_and_merge(user1_content: str, user2_content: str, min_chars: int) -> tuple[bool, str, str]:
    """Ask LLM whether two user messages can be merged. Returns (mergeable, content, reason)."""
    prompt = (
        f"第一条 user 消息：\n{compact_text(user1_content, 1000)!r}\n\n"
        f"第二条 user 消息：\n{compact_text(user2_content, 1000)!r}\n\n"
        f"请判断这两条消息是否可以合并为一条。如果可以，返回合并后的内容（至少 {min_chars} 个字符）。"
    )
    raw = call_chat_completion(prompt).strip()
    parsed = parse_model_json(raw)
    mergeable = bool(parsed.get("mergeable", False))
    content = str(parsed.get("content", "")).strip()
    reason = str(parsed.get("reason", "")).strip()
    if mergeable and len(content) < min_chars:
        raise ValueError(f"merged content is only {len(content)} chars: {content!r}")
    return mergeable, content, reason


def judge_by_assistant_response(
    user1_content: str, user2_content: str, assistant_content: str, min_chars: int
) -> tuple[str, str, str]:
    """Use assistant response to decide which user message(s) it addresses.

    Returns (related_to, content, reason).
    """
    prompt = (
        f"第一条 user 消息：\n{compact_text(user1_content, 1000)!r}\n\n"
        f"第二条 user 消息：\n{compact_text(user2_content, 1000)!r}\n\n"
        f"assistant 的回复：\n{compact_text(assistant_content, 2000)!r}\n\n"
        f"请根据 assistant 的回复内容，判断它实际回应了哪条 user 消息。"
        f"如果 related_to=\"both\"，返回合并后的用户内容（至少 {min_chars} 个字符）。"
    )
    raw = call_chat_completion(prompt, system_prompt=SYSTEM_PROMPT_ASSISTANT_CHECK).strip()
    parsed = parse_model_json(raw)
    related_to = str(parsed.get("related_to", "neither")).strip().lower()
    if related_to not in ("both", "user1", "user2", "neither"):
        related_to = "neither"
    content = str(parsed.get("content", "")).strip()
    reason = str(parsed.get("reason", "")).strip()
    if related_to == "both" and len(content) < min_chars:
        raise ValueError(
            f"assistant-check merged content is only {len(content)} chars: {content!r}"
        )
    return related_to, content, reason


def apply_merge(session_path: Path, merged_content: str, *, apply: bool) -> None:
    """Replace first user message content with merged_content and remove the second user message."""
    document = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
        raise ValueError("session JSON must be an object with messages array")
    messages = document["messages"]
    idx1, msg1, idx2, _msg2 = find_first_two_users(messages)

    original = msg1.get("content")
    if isinstance(original, list) and original and isinstance(original[0], dict) and "text" in original[0]:
        original[0]["text"] = merged_content
    else:
        msg1["content"] = merged_content

    del messages[idx2]

    if apply:
        session_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def process_issue(issue: DoubleUserIssue, *, apply: bool, min_chars: int) -> DoubleUserIssue:
    try:
        document = json.loads(issue.session_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
            raise ValueError("session JSON must be an object with messages array")
        messages = document["messages"]
        _idx1, msg1, _idx2, msg2 = find_first_two_users(messages)
        issue.user1_content = normalize_content(msg1.get("content")).strip()
        issue.user2_content = normalize_content(msg2.get("content")).strip()

        issue.mergeable, issue.merged_content, issue.reason = judge_and_merge(
            issue.user1_content, issue.user2_content, min_chars
        )

        if not issue.mergeable:
            if (
                len(messages) > 3
                and isinstance(messages[3], dict)
                and messages[3].get("role") == "assistant"
            ):
                assistant_content = normalize_content(messages[3].get("content")).strip()
                if not assistant_content:
                    assistant_content = normalize_content(messages[3].get("reasoning_content")).strip()
                issue.assistant_content = assistant_content
                related_to, content, asst_reason = judge_by_assistant_response(
                    issue.user1_content, issue.user2_content, assistant_content, min_chars
                )
                if related_to == "both":
                    issue.mergeable = True
                    issue.merged_content = content
                    issue.reason = f"[assistant-check:both] {asst_reason}"
                    apply_merge(issue.session_path, content, apply=apply)
                    issue.status = "updated" if apply else "dry_run"
                    return issue
                elif related_to == "user1":
                    issue.mergeable = True
                    issue.merged_content = issue.user1_content
                    issue.reason = f"[assistant-check:user1] {asst_reason}"
                    apply_merge(issue.session_path, issue.user1_content, apply=apply)
                    issue.status = "updated" if apply else "dry_run"
                    return issue
                elif related_to == "user2":
                    issue.mergeable = True
                    issue.merged_content = issue.user2_content
                    issue.reason = f"[assistant-check:user2] {asst_reason}"
                    apply_merge(issue.session_path, issue.user2_content, apply=apply)
                    issue.status = "updated" if apply else "dry_run"
                    return issue

            issue.status = "skipped_not_mergeable"
            return issue

        apply_merge(issue.session_path, issue.merged_content, apply=apply)
        issue.status = "updated" if apply else "dry_run"
    except Exception as exc:  # noqa: BLE001
        issue.status = "error"
        issue.error = str(exc)
    return issue


def write_report(output_path: Path, issues: list[DoubleUserIssue], *, apply: bool) -> None:
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
        description="Use AI to merge consecutive user messages (system, user, user) listed in pipeline_report_summary.md."
    )
    parser.add_argument("-r", "--report", type=Path, default=DEFAULT_REPORT_PATH, help=f"Default: {DEFAULT_REPORT_PATH}")
    parser.add_argument("--output", "-output", type=Path, default=DEFAULT_OUTPUT_PATH, help=f"Default: {DEFAULT_OUTPUT_PATH}")
    parser.add_argument("--min-chars", "-min-chars", type=int, default=MIN_TARGET_CHARS, help="Default: 20")
    parser.add_argument("--apply", "-apply", action="store_true", help="Write merged content back to session JSON files.")
    parser.add_argument("--limit", "-limit", type=int, default=0, help="Only process the first N matched issues; 0 means all.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report_path = args.report.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    issues = find_double_user_issues(report_path)
    if args.limit > 0:
        issues = issues[: args.limit]

    print(f"matched issues: {len(issues)}")
    for index, issue in enumerate(issues, start=1):
        print(f"[{index}/{len(issues)}] {issue.session_path}")
        process_issue(issue, apply=args.apply, min_chars=max(1, int(args.min_chars)))
        print(f"  status: {issue.status}")
        print(f"  user1: {compact_text(issue.user1_content, 80)!r}")
        print(f"  user2: {compact_text(issue.user2_content, 80)!r}")
        if issue.merged_content:
            print(f"  merged: {compact_text(issue.merged_content, 80)!r}")
        print(f"  reason: {issue.reason}")
        if issue.error:
            print(f"  error: {issue.error}", file=sys.stderr)

    write_report(output_path, issues, apply=args.apply)
    print(f"\nreport: {output_path}")
    if not args.apply:
        print("dry run only; pass --apply to write session JSON files")
    return 1 if any(issue.status == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

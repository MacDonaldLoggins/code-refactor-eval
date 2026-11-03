#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detect and remove standalone meaningless dialogue turns from trajectory JSON.

The script only asks the model to classify user/assistant nodes. Once a node is
confirmed meaningless in context, deletion is expanded structurally to the whole
turn: the user message, assistant replies, assistant tool calls, and matching tool
result nodes up to the next user/system message.

Default mode writes changes. Pass --dry-run to preview without writing.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable


def configure_console_encoding() -> None:
    """避免非 UTF-8 控制台中断脚本，并让 stdout/stderr 行缓冲(实时刷出)。

    line_buffering=True: 父进程 stdout 被管道/重定向捕获(非 TTY)时，Python 默认对本进程
    stdout 块缓冲，长时运行(逐条 LLM 判断)会看起来卡死——输出堆在没刷出的缓冲里。行缓冲
    让每条换行结尾的 print 立即可见。与 clean_last_assistant.py 的同名 helper 保持一致。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass


configure_console_encoding()


class ProgressReporter:
    """节流进度上报器：只写 stderr、flush，两次输出至少间隔 min_interval 秒。

    - 走 stderr：main() 末尾把 `report:` 路径与 summary JSON 打到 stdout，进度落到 stdout
      会污染那段机读输出。
    - 不抛异常：process_file 的宽 except 会把回调里的任何异常吞成 status="error"，静默降级
      整个文件；故所有输出包在 try/except 里。
    - 节流：慢步骤逐条 LLM 判断，force=False 的更新按时间节流避免刷屏；关键节点用 force=True。
    """

    def __init__(self, label: str, *, enabled: bool = True, min_interval: float = 1.0) -> None:
        self.label = label
        self.enabled = enabled
        self.min_interval = min_interval
        self._last = 0.0

    def update(self, message: str, *, force: bool = False) -> None:
        if not self.enabled:
            return
        try:
            now = time.monotonic()
            if not force and (now - self._last) < self.min_interval:
                return
            self._last = now
            print(f"    · {self.label}: {message}", file=sys.stderr, flush=True)
        except Exception:
            pass


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SCRIPT_STEM = Path(__file__).stem  # 报告文件名用脚本名，与 ua/dedup 对齐

# 标准报告（语义类，按 TASK 分组）固定取值，对齐 ua_detect_and_repair。
REPORT_TYPE = "semantic_report"
ACTION_TYPE = "fix"                 # 写改动模式（删除无意义节点）
FUNCTION_LABEL = "无意义节点删除"


def _build_ssl_context() -> ssl.SSLContext:
    """Verify TLS against certifi's CA bundle (matches requests-based siblings).

    urllib defaults to the system OpenSSL store, which on some installs lacks the
    endpoint's CA chain and fails with CERTIFICATE_VERIFY_FAILED. certifi carries
    that chain, so we point urllib at it. Falls back to the default store if certifi
    is unavailable. Verification stays enabled either way.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CONTEXT = _build_ssl_context()


def find_project_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__).resolve()).parent
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    for candidate in (here, *here.parents):
        if (candidate / ".env").is_file():
            return candidate
    return PROJECT_ROOT


REPO_ROOT = find_project_root()
PROJECT_ENV_PATH = REPO_ROOT / ".env"
DEFAULT_INPUT_DIR = REPO_ROOT / "batch_cleaning_tasks"

RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
DETECTABLE_ROLES = {"user", "assistant"}


SYSTEM_CANDIDATE_PROMPT = """You audit software-agent trajectory JSON.

Classify exactly one user/assistant message as a possible standalone meaningless
dialogue node.

"Meaningless" means no-op chatter or UI/CLI control chatter that does not help
solve the task and can only be useful together with its paired acknowledgement.
Examples: user says "clear"; assistant says "Context cleared. How can I help
you?"; accidental punctuation; empty generic acknowledgement with no task value.

Also consider "redundant restatement" meaningless: a message that only repeats
information already reported in the immediately preceding turns (e.g. "Already
reported above — 308 passed"), contributing no new task value even if its
content looks like a summary or result. A system <task-notification> user
message followed by a "already reported" assistant reply is a common sign of
this pattern.

Do NOT mark substantive task requests, implementation work, tool-use reasoning,
tool calls, file contents, novel test results, novel summaries, final answers,
error handling, or decisions as meaningless. However, a message that only
echoes information already present in preceding messages (redundant
restatement) should be considered meaningless even if its content looks like a
summary or test result. If unsure, return candidate=false.

Return only JSON:
{
  "candidate": boolean,
  "confidence": number,
  "category": string,
  "reason": string
}
"""


SYSTEM_CONFIRM_PROMPT = """You audit software-agent trajectory JSON.

Decide whether TARGET TURN is a standalone meaningless dialogue turn that should
be deleted. The target turn will be deleted as a whole: user message, assistant
messages, assistant tool calls, and tool result nodes until the next user/system.

Delete only when the whole target turn carries no useful task information and the
surrounding context does not depend on it. Examples to delete: user "clear" plus
assistant "Context cleared. How can I help you?"; accidental no-op prompt plus a
generic acknowledgement; a system <task-notification> user message followed by
an assistant message that only echoes information already reported (e.g.
"Already reported above — 308/308 passed").

Keep if the target turn asks or answers a real task, invokes tools for real work,
records NEW useful status not already present in preceding turns, contributes
new information to later context, or if you are unsure. A message that merely
restates information from the immediately preceding messages does not count as
"useful status" or "contributing to context".

Return only JSON:
{
  "delete": boolean,
  "confidence": number,
  "reason": string,
  "meaningful_context": string
}
"""


@dataclass
class ModelConfig:
    model: str
    base_url: str
    api_key: str
    temperature: float = 0.0
    max_tokens: int = 1000
    timeout: int = 900
    retries: int = 3
    use_response_format: bool = True


@dataclass
class CandidateDecision:
    index: int
    role: str
    candidate: bool
    confidence: float
    category: str = ""
    reason: str = ""
    source: str = "model"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpanDecision:
    start: int
    end: int
    candidate_indices: list[int]
    delete: bool
    confidence: float
    reason: str = ""
    meaningful_context: str = ""
    source: str = "model"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def length(self) -> int:
        return max(0, self.end - self.start)


@dataclass
class FileResult:
    path: str
    status: str
    original_message_count: int = 0
    final_message_count: int = 0
    removed_message_count: int = 0
    passes: list[dict[str, Any]] = field(default_factory=list)
    output_path: str = ""
    error: str = ""


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
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
        values[key] = value
    return values


def env_value(dotenv: dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = dotenv.get(name)
        if value:
            return value
    return default


def resolve_model_config(args: argparse.Namespace) -> ModelConfig:
    dotenv = load_env_file(PROJECT_ENV_PATH)
    profile = env_value(dotenv, "CLAUDE_OPENAI_MODEL_PROFILE")

    if profile == "LongCat":
        model_names = ("M_MODEL_NAME", "MODEL_NAME", "OPENAI_MODEL", "MODEL")
        base_names = ("M_MODEL_BASE_URL", "MODEL_BASE_URL", "OPENAI_BASE_URL")
        key_names = ("M_MODEL_API_KEY", "MODEL_API_KEY", "OPENAI_API_KEY")
    else:
        model_names = ("MODEL_NAME", "OPENAI_MODEL", "MODEL", "M_MODEL_NAME")
        base_names = ("MODEL_BASE_URL", "OPENAI_BASE_URL", "M_MODEL_BASE_URL")
        key_names = ("MODEL_API_KEY", "OPENAI_API_KEY", "M_MODEL_API_KEY")

    model = env_value(dotenv, *model_names)
    base_url = env_value(dotenv, *base_names)
    api_key = env_value(dotenv, *key_names)
    missing = []
    if not model:
        missing.append("MODEL_NAME")
    if not base_url:
        missing.append("MODEL_BASE_URL")
    if not api_key:
        missing.append("MODEL_API_KEY")
    if missing:
        raise ValueError(
            "missing model config: "
            + ", ".join(missing)
            + f" in {PROJECT_ENV_PATH}"
            + " (or pass --heuristic-only for local clear/context-cleared cleanup)"
        )

    return ModelConfig(
        model=model,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        retries=args.retries,
        use_response_format=not args.no_response_format,
    )


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def extract_response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("model response missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise RuntimeError("model response choice is not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("model response missing choices[0].message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("model response missing message.content")
    return content


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")
    return value


class ModelClient:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.url = chat_completions_url(config.base_url)

    def call_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        last_error: Exception | None = None
        use_response_format = self.config.use_response_format
        for attempt in range(1, self.config.retries + 2):
            payload: dict[str, Any] = {
                "model": self.config.model,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if use_response_format:
                payload["response_format"] = {"type": "json_object"}
            try:
                response_payload = self._post(payload)
                return extract_json_object(extract_response_text(response_payload))
            except RuntimeError as exc:
                last_error = exc
                if use_response_format and "HTTP 400" in str(exc):
                    use_response_format = False
                    continue
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
            if attempt <= self.config.retries:
                time.sleep(min(attempt, 5))
        raise RuntimeError(f"model request failed: {last_error}")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.config.timeout, context=_SSL_CONTEXT
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model HTTP {exc.code}: {body[:2000]}") from exc


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
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content)


def compact_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def parse_tool_args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def summarize_tool_calls(message: dict[str, Any], limit: int) -> str:
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or not calls:
        return ""
    parts: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = function.get("name") if isinstance(function, dict) else ""
        args = parse_tool_args(function.get("arguments")) if isinstance(function, dict) else None
        args_text = compact_text(json.dumps(args, ensure_ascii=False, sort_keys=True), limit)
        parts.append(f"{name}({args_text})")
    return "; ".join(parts)


def message_summary(index: int, message: Any, *, char_limit: int = 1200) -> str:
    if not isinstance(message, dict):
        return f"[{index}] non-object message: {compact_text(str(message), char_limit)}"
    role = str(message.get("role", ""))
    agent = str(message.get("agent", ""))
    content = compact_text(normalize_content(message.get("content")), char_limit)
    reasoning = compact_text(normalize_content(message.get("reasoning_content")), max(200, char_limit // 2))
    tool_calls = summarize_tool_calls(message, max(200, char_limit // 2))
    tool_call_id = message.get("tool_call_id")

    parts = [f"[{index}] role={role}"]
    if agent:
        parts.append(f"agent={agent}")
    if tool_call_id:
        parts.append(f"tool_call_id={tool_call_id}")
    if content:
        parts.append(f"content={content!r}")
    if reasoning:
        parts.append(f"reasoning={reasoning!r}")
    if tool_calls:
        parts.append(f"tool_calls={tool_calls}")
    return "; ".join(parts)


def candidate_prompt(index: int, message: dict[str, Any], *, char_limit: int) -> str:
    return (
        "Classify this trajectory message.\n\n"
        f"{message_summary(index, message, char_limit=char_limit)}\n\n"
        "Remember: return candidate=true only for likely standalone no-op chatter."
    )


def confirmation_prompt(
    messages: list[Any],
    start: int,
    end: int,
    candidate_indices: list[int],
    *,
    context_radius: int,
    char_limit: int,
) -> str:
    before_start = max(0, start - context_radius)
    after_end = min(len(messages), end + context_radius)

    before = "\n".join(
        message_summary(i, messages[i], char_limit=char_limit)
        for i in range(before_start, start)
    ) or "(none)"
    target = "\n".join(
        message_summary(i, messages[i], char_limit=char_limit)
        for i in range(start, end)
    )
    after = "\n".join(
        message_summary(i, messages[i], char_limit=char_limit)
        for i in range(end, after_end)
    ) or "(none)"

    return (
        f"Candidate message indices: {candidate_indices}\n"
        f"Target deletion span if approved: [{start}, {end})\n\n"
        "BEFORE CONTEXT:\n"
        f"{before}\n\n"
        "TARGET TURN:\n"
        f"{target}\n\n"
        "AFTER CONTEXT:\n"
        f"{after}\n\n"
        "Should the TARGET TURN be deleted as standalone meaningless dialogue?"
    )


def detectable_indices(messages: list[Any]) -> list[int]:
    indices: list[int] = []
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") in DETECTABLE_ROLES:
            indices.append(index)
    return indices


def turn_span_for_candidate(messages: list[Any], index: int) -> tuple[int, int] | None:
    if index < 0 or index >= len(messages):
        return None
    message = messages[index]
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role not in DETECTABLE_ROLES:
        return None

    if role == "user":
        start = index
    else:
        start = index
        cursor = index - 1
        while cursor >= 0:
            previous = messages[cursor]
            previous_role = previous.get("role") if isinstance(previous, dict) else None
            if previous_role == "user":
                start = cursor
                break
            if previous_role == "system":
                break
            cursor -= 1

    end = start + 1
    while end < len(messages):
        current = messages[end]
        current_role = current.get("role") if isinstance(current, dict) else None
        if current_role in {"user", "system"}:
            break
        end += 1
    return start, end


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    normalized = sorted((start, end) for start, end in spans if start < end)
    if not normalized:
        return []
    merged: list[tuple[int, int]] = [normalized[0]]
    for start, end in normalized[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def delete_spans(messages: list[Any], spans: list[tuple[int, int]]) -> tuple[list[Any], list[int]]:
    merged = merge_spans(spans)
    delete_indices: set[int] = set()
    for start, end in merged:
        delete_indices.update(range(start, end))
    return [message for index, message in enumerate(messages) if index not in delete_indices], sorted(delete_indices)


def deleted_message_records(
    messages: list[Any],
    original_indices: list[int],
    deleted_indices: list[int],
    *,
    char_limit: int,
    span_info_by_index: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    span_info_by_index = span_info_by_index or {}
    for current_index in deleted_indices:
        message = messages[current_index]
        record: dict[str, Any] = {
            "current_index": current_index,
            "original_index": original_indices[current_index]
            if current_index < len(original_indices)
            else current_index,
            "summary": message_summary(current_index, message, char_limit=char_limit),
            "message": copy.deepcopy(message),
        }
        info = span_info_by_index.get(current_index)
        if info:
            record["span_reason"] = info.get("reason", "")
            record["span_confidence"] = info.get("confidence", 0.0)
            record["span"] = info.get("span", "")
        if isinstance(message, dict):
            record["role"] = message.get("role")
            if message.get("agent"):
                record["agent"] = message.get("agent")
            if message.get("tool_call_id") is not None:
                record["tool_call_id"] = message.get("tool_call_id")
            tool_call_ids = [
                call.get("id")
                for call in message.get("tool_calls") or []
                if isinstance(call, dict) and call.get("id")
            ]
            if tool_call_ids:
                record["tool_call_ids"] = tool_call_ids
        records.append(record)
    return records


def validate_tool_threading(messages: list[Any]) -> list[tuple[int, str]]:
    """Return orphan tool results as (next_index, tool_call_id) pairs.

    The return type changed from plain strings to structured tuples so callers
    can map the orphan index back to the original message list and decide which
    deletion span(s) to drop instead of rolling back the whole pass.
    """
    called: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                called.add(str(call["id"]))

    problems: list[tuple[int, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        if call_id is not None and str(call_id) not in called:
            problems.append((index, str(call_id)))
    return problems


def _filter_spans_avoiding_orphans(
    messages: list[Any],
    spans: list[tuple[int, int]],
) -> tuple[list[tuple[int, int]], list[str]]:
    """Filter deletion spans so that no orphan tool results are created.

    If deleting all ``spans`` creates orphan tool results, identify the spans
    that delete assistant calls whose tool results survive, and drop only those
    spans. The process repeats until the remaining spans are thread-safe or no
    more problematic spans can be identified.

    Returns the filtered span list and a list of human-readable notes describing
    any dropped spans or residual orphan problems that cannot be fixed by
    dropping spans (e.g. pre-existing orphans in the input).
    """
    # Map each tool_call_id to the index of the assistant call that issued it.
    call_id_to_assistant_idx: dict[str, int] = {}
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                call_id_to_assistant_idx[str(call["id"])] = idx

    remaining_spans = list(spans)
    notes: list[str] = []

    while remaining_spans:
        next_messages, deleted_indices = delete_spans(messages, remaining_spans)
        current_problems = validate_tool_threading(next_messages)
        if not current_problems:
            return remaining_spans, notes

        # Map indices in the post-deletion message list back to original indices.
        deleted_set = set(deleted_indices)
        next_to_working: list[int] = []
        for idx in range(len(messages)):
            if idx not in deleted_set:
                next_to_working.append(idx)

        # Find spans whose deletion caused each orphan tool result.
        bad_starts: set[int] = set()
        for next_idx, call_id in current_problems:
            if next_idx >= len(next_to_working):
                continue
            assistant_idx = call_id_to_assistant_idx.get(call_id)
            if assistant_idx is None:
                # Pre-existing orphan: the assistant call is missing in the
                # original messages, so dropping spans cannot fix it.
                continue
            for start, end in remaining_spans:
                if start <= assistant_idx < end:
                    bad_starts.add(start)
                    break

        if not bad_starts:
            for next_idx, call_id in current_problems:
                notes.append(f"orphan tool result at {next_idx}: {call_id}")
            return remaining_spans, notes

        new_remaining: list[tuple[int, int]] = []
        for start, end in remaining_spans:
            if start in bad_starts:
                notes.append(
                    f"dropped span [{start}, {end}) to avoid orphan tool result(s)"
                )
            else:
                new_remaining.append((start, end))

        if len(new_remaining) == len(remaining_spans):
            # No progress; avoid an infinite loop.
            for next_idx, call_id in current_problems:
                notes.append(f"orphan tool result at {next_idx}: {call_id}")
            return remaining_spans, notes

        remaining_spans = new_remaining

    return remaining_spans, notes


USER_CLEAR_RE = re.compile(r"^(?:/?clear|cls|clear\s+context|reset\s+context)$", re.I)
USER_TINY_NOISE = {".", "..", "...", "?", "??", "ok", "okay"}
ASSISTANT_CLEAR_RE = re.compile(
    r"^(?:context|conversation|chat)?\s*cleared\.?\s*(?:how can i help(?: you)?\??)?$",
    re.I,
)
ASSISTANT_GENERIC_RE = re.compile(
    r"^(?:ok(?:ay)?\.?|done\.?|sure\.?|how can i help(?: you)?\??)$",
    re.I,
)
USER_TASK_NOTIFICATION_RE = re.compile(r"<task-notification>")
ASSISTANT_ALREADY_REPORTED_RE = re.compile(
    r"^(?:already\s+(?:reported|mentioned|covered|answered|addressed)|"
    r"as\s+(?:reported|mentioned|covered|answered|addressed|noted|stated)\s+(?:above|earlier|previously))",
    re.I,
)


def heuristic_candidate(index: int, message: dict[str, Any]) -> CandidateDecision:
    role = str(message.get("role", ""))
    text = compact_text(normalize_content(message.get("content")).lower(), 300)
    reasoning = compact_text(normalize_content(message.get("reasoning_content")).lower(), 300)
    has_tool_calls = bool(message.get("tool_calls"))

    if role == "user" and USER_CLEAR_RE.match(text):
        return CandidateDecision(index, role, True, 0.98, "clear_command", "local clear command", "heuristic")
    if role == "user" and USER_TASK_NOTIFICATION_RE.search(text):
        return CandidateDecision(index, role, True, 0.95, "task_notification", "system task notification, not a real user intent", "heuristic")
    if role == "user" and text in USER_TINY_NOISE:
        return CandidateDecision(index, role, True, 0.62, "tiny_noise", "tiny no-op-like user text", "heuristic")
    if role == "assistant" and not has_tool_calls and ASSISTANT_CLEAR_RE.match(text):
        return CandidateDecision(index, role, True, 0.98, "context_cleared", "local context-cleared reply", "heuristic")
    if role == "assistant" and not has_tool_calls and "context cleared" in reasoning and ASSISTANT_GENERIC_RE.match(text):
        return CandidateDecision(index, role, True, 0.9, "context_cleared", "reasoning says context cleared", "heuristic")
    if role == "assistant" and not has_tool_calls and text in {"", "ok", "okay", "done"}:
        return CandidateDecision(index, role, True, 0.55, "generic_ack", "generic acknowledgement", "heuristic")
    if role == "assistant" and not has_tool_calls and ASSISTANT_ALREADY_REPORTED_RE.match(text):
        return CandidateDecision(index, role, True, 0.9, "redundant_confirmation", "already reported / redundant confirmation", "heuristic")
    return CandidateDecision(index, role, False, 0.0, "", "", "heuristic")


def heuristic_confirm_span(
    messages: list[Any],
    start: int,
    end: int,
    candidate_indices: list[int],
) -> SpanDecision:
    span = [m for m in messages[start:end] if isinstance(m, dict)]
    has_tool_calls = any(m.get("tool_calls") for m in span if m.get("role") == "assistant")
    has_tool_results = any(m.get("role") == "tool" for m in span)
    user_texts = [
        compact_text(normalize_content(m.get("content")).lower(), 300)
        for m in span
        if m.get("role") == "user"
    ]
    assistant_texts = [
        compact_text(normalize_content(m.get("content")).lower(), 300)
        for m in span
        if m.get("role") == "assistant"
    ]

    clear_user = bool(user_texts) and all(USER_CLEAR_RE.match(text) for text in user_texts)
    clear_assistant = all((not text) or ASSISTANT_CLEAR_RE.match(text) or ASSISTANT_GENERIC_RE.match(text) for text in assistant_texts)
    tiny_user = bool(user_texts) and all(text in USER_TINY_NOISE for text in user_texts)
    generic_assistant = all((not text) or ASSISTANT_GENERIC_RE.match(text) for text in assistant_texts)
    task_notification_user = bool(user_texts) and any(USER_TASK_NOTIFICATION_RE.search(t) for t in user_texts)
    already_reported_assistant = bool(assistant_texts) and all(
        (not t) or ASSISTANT_ALREADY_REPORTED_RE.match(t) for t in assistant_texts
    )

    delete = False
    confidence = 0.0
    reason = "not a local standalone meaningless pattern"
    if not has_tool_calls and not has_tool_results and clear_user and clear_assistant:
        delete = True
        confidence = 0.98
        reason = "standalone clear/context-cleared turn"
    elif not has_tool_calls and not has_tool_results and tiny_user and generic_assistant:
        delete = True
        confidence = 0.75
        reason = "tiny no-op user text with generic assistant acknowledgement"
    elif not has_tool_calls and not has_tool_results and task_notification_user and already_reported_assistant:
        delete = True
        confidence = 0.95
        reason = "task notification with redundant already-reported confirmation"

    return SpanDecision(
        start=start,
        end=end,
        candidate_indices=candidate_indices,
        delete=delete,
        confidence=confidence,
        reason=reason,
        source="heuristic",
    )


def float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def model_candidate_decision(
    client: ModelClient,
    messages: list[Any],
    index: int,
    *,
    char_limit: int,
) -> CandidateDecision:
    message = messages[index]
    assert isinstance(message, dict)
    try:
        raw = client.call_json(
            SYSTEM_CANDIDATE_PROMPT,
            candidate_prompt(index, message, char_limit=char_limit),
        )
    except Exception as exc:
        raise RuntimeError(f"candidate classification failed at message {index}: {exc}") from exc
    return CandidateDecision(
        index=index,
        role=str(message.get("role", "")),
        candidate=bool(raw.get("candidate")),
        confidence=float_value(raw.get("confidence")),
        category=str(raw.get("category") or ""),
        reason=str(raw.get("reason") or ""),
        source="model",
        raw=raw,
    )


def model_span_decision(
    client: ModelClient,
    messages: list[Any],
    start: int,
    end: int,
    candidate_indices: list[int],
    *,
    context_radius: int,
    char_limit: int,
) -> SpanDecision:
    try:
        raw = client.call_json(
            SYSTEM_CONFIRM_PROMPT,
            confirmation_prompt(
                messages,
                start,
                end,
                candidate_indices,
                context_radius=context_radius,
                char_limit=char_limit,
            ),
        )
    except Exception as exc:
        raise RuntimeError(
            f"span confirmation failed for [{start}, {end}) candidates={candidate_indices}: {exc}"
        ) from exc
    return SpanDecision(
        start=start,
        end=end,
        candidate_indices=candidate_indices,
        delete=bool(raw.get("delete")),
        confidence=float_value(raw.get("confidence")),
        reason=str(raw.get("reason") or ""),
        meaningful_context=str(raw.get("meaningful_context") or ""),
        source="model",
        raw=raw,
    )


def _run_model_calls(
    keys: list[Any],
    fn: Callable[[Any], Any],
    *,
    workers: int,
    progress: Callable[[str], None] | None,
    label: str,
) -> dict[Any, Any]:
    """并发跑 fn(key)（workers>1 时用线程池，否则串行），返回 {key: result}。

    进度只在 ~每 10% 或全部完成时打一行（一遍最多 ~11 行），不再逐条刷屏。
    单个调用失败会像串行一样抛出异常（由 process_file 兜住记为 error）。
    """
    total = len(keys)
    results: dict[Any, Any] = {}
    if total == 0:
        return results
    done = 0

    def tick() -> None:
        nonlocal done
        done += 1
        if progress is not None and (
            done == total or (done * 10) // total != ((done - 1) * 10) // total
        ):
            progress(f"{label} {done}/{total}")

    if workers <= 1 or total == 1:
        for key in keys:
            results[key] = fn(key)
            tick()
        return results

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fn, key): key for key in keys}
        for future in as_completed(futures):
            key = futures[future]
            results[key] = future.result()  # 出错即抛出，语义同串行
            tick()
    return results


def plan_one_pass(
    messages: list[Any],
    *,
    client: ModelClient | None,
    heuristic_only: bool,
    heuristic_prefilter: bool,
    candidate_threshold: float,
    confirm_threshold: float,
    context_radius: int,
    char_limit: int,
    workers: int = 1,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    detectable = detectable_indices(messages)

    # ---- 候选分类（瓶颈：每条 user/assistant 一次 LLM 调用；并发跑）----
    if heuristic_only:
        candidates: list[CandidateDecision] = []
        for index in detectable:
            decision = heuristic_candidate(index, messages[index])
            if decision.candidate and decision.confidence >= candidate_threshold:
                candidates.append(decision)
    else:
        assert client is not None
        to_classify = [
            index
            for index in detectable
            if not (heuristic_prefilter and not heuristic_candidate(index, messages[index]).candidate)
        ]
        decisions = _run_model_calls(
            to_classify,
            lambda idx: model_candidate_decision(client, messages, idx, char_limit=char_limit),
            workers=workers,
            progress=progress,
            label="classified",
        )
        candidates = [
            decisions[idx]
            for idx in to_classify
            if decisions[idx].candidate and decisions[idx].confidence >= candidate_threshold
        ]

    # ---- 组装候选跨度 ----
    span_candidates: dict[tuple[int, int], list[int]] = {}
    for decision in candidates:
        span = turn_span_for_candidate(messages, decision.index)
        if span is None:
            continue
        span_candidates.setdefault(span, []).append(decision.index)
    ordered_spans = sorted(span_candidates.items())

    # ---- 跨度确认（并发跑）----
    if heuristic_only:
        span_decisions = [
            heuristic_confirm_span(messages, start, end, sorted(candidate_indices))
            for (start, end), candidate_indices in ordered_spans
        ]
    else:
        assert client is not None

        def confirm_span(position: int) -> SpanDecision:
            (start, end), candidate_indices = ordered_spans[position]
            return model_span_decision(
                client,
                messages,
                start,
                end,
                sorted(candidate_indices),
                context_radius=context_radius,
                char_limit=char_limit,
            )

        span_map = _run_model_calls(
            list(range(len(ordered_spans))),
            confirm_span,
            workers=workers,
            progress=progress,
            label="confirmed",
        )
        span_decisions = [span_map[position] for position in range(len(ordered_spans))]

    for span_decision in span_decisions:
        if span_decision.delete and span_decision.confidence < confirm_threshold:
            span_decision.delete = False
            span_decision.reason = (
                f"below confirmation threshold {confirm_threshold}: {span_decision.reason}"
            )

    delete_spans_planned = merge_spans(
        [(decision.start, decision.end) for decision in span_decisions if decision.delete]
    )
    return {
        "candidates": [asdict(item) for item in candidates],
        "span_decisions": [asdict(item) for item in span_decisions],
        "delete_spans": [{"start": start, "end": end} for start, end in delete_spans_planned],
    }


def prune_document(
    document: dict[str, Any],
    *,
    client: ModelClient | None,
    heuristic_only: bool,
    heuristic_prefilter: bool,
    passes: int,
    candidate_threshold: float,
    confirm_threshold: float,
    context_radius: int,
    char_limit: int,
    workers: int = 1,
    progress: "ProgressReporter | None" = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    messages = document.get("messages")
    if not isinstance(messages, list):
        raise ValueError("session JSON must be an object with a messages array")

    working_messages = copy.deepcopy(messages)
    working_original_indices = list(range(len(messages)))
    pass_reports: list[dict[str, Any]] = []
    removed_count = 0

    for pass_number in range(1, max(1, passes) + 1):
        pass_progress: Callable[[str], None] | None = None
        if progress is not None:
            def pass_progress(message: str, _pass: int = pass_number, _reporter: "ProgressReporter" = progress) -> None:
                _reporter.update(f"pass {_pass}: {message}", force=True)
        try:
            plan = plan_one_pass(
                working_messages,
                client=client,
                heuristic_only=heuristic_only,
                heuristic_prefilter=heuristic_prefilter,
                candidate_threshold=candidate_threshold,
                confirm_threshold=confirm_threshold,
                context_radius=context_radius,
                char_limit=char_limit,
                workers=workers,
                progress=pass_progress,
            )
            spans = [(item["start"], item["end"]) for item in plan["delete_spans"]]

            # ---- 只剔除会导致线程问题的 span，其余照常删除 ----
            valid_spans, threading_notes = _filter_spans_avoiding_orphans(
                working_messages, spans
            )
            if threading_notes:
                plan["threading_notes"] = threading_notes
            if valid_spans != spans:
                plan["original_delete_spans"] = [
                    {"start": start, "end": end} for start, end in spans
                ]
                plan["delete_spans"] = [
                    {"start": start, "end": end} for start, end in valid_spans
                ]

            next_messages, deleted_indices = delete_spans(working_messages, valid_spans)
            deleted_index_set = set(deleted_indices)
            next_original_indices = [
                original_index
                for current_index, original_index in enumerate(working_original_indices)
                if current_index not in deleted_index_set
            ]
            # 把每条被删消息(current_index)关联到它所属删除 span 的判定理由/置信度，
            # 供标准报告的 problem/addition 字段使用。span_decisions 已是 asdict 后的字典。
            span_info_by_index: dict[int, dict[str, Any]] = {}
            for sd in plan.get("span_decisions", []):
                if not sd.get("delete"):
                    continue
                start, end = sd.get("start"), sd.get("end")
                info = {
                    "reason": sd.get("reason", ""),
                    "confidence": sd.get("confidence", 0.0),
                    "span": f"[{start},{end})",
                }
                for idx in range(start, end):
                    if idx in deleted_index_set:
                        span_info_by_index[idx] = info
            deleted_records = deleted_message_records(
                working_messages,
                working_original_indices,
                deleted_indices,
                char_limit=char_limit,
                span_info_by_index=span_info_by_index,
            )
            threading_problems = validate_tool_threading(next_messages)
            if threading_problems:
                plan["threading_problems_after_delete"] = [
                    f"orphan tool result at {idx}: {call_id}"
                    for idx, call_id in threading_problems
                ]
            plan["pass"] = pass_number
            plan["deleted_indices"] = deleted_indices
            plan["deleted_original_indices"] = [
                item["original_index"] for item in deleted_records
            ]
            plan["deleted_messages"] = deleted_records
            plan["deleted_count"] = len(deleted_indices)
            pass_reports.append(plan)
            if progress is not None:
                note = ""
                if threading_notes:
                    note = f" (threading adjustments: {len(threading_notes)})"
                progress.update(
                    f"pass {pass_number}: removed {len(deleted_indices)} this pass{note}",
                    force=True,
                )
        except Exception as exc:
            # 单个 pass 失败时保留之前 pass 的结果，不再继续后续 pass。
            pass_reports.append(
                {
                    "pass": pass_number,
                    "error": f"{exc}",
                    "status": "aborted",
                }
            )
            if progress is not None:
                progress.update(
                    f"pass {pass_number}: aborted due to {exc}", force=True
                )
            break
        if not deleted_indices:
            break
        removed_count += len(deleted_indices)
        working_messages = next_messages
        working_original_indices = next_original_indices

    new_document = copy.deepcopy(document)
    new_document["messages"] = working_messages
    return new_document, pass_reports, removed_count


def session_json_files_for_dir(path: Path) -> list[Path] | None:
    if path.name == "batch_cleaning_tasks":
        return sorted(
            item.resolve()
            for item in path.glob("TASK-*/sessions/*.json")
            if item.is_file()
        )
    if path.name.startswith("TASK-"):
        return sorted(
            item.resolve()
            for item in path.glob("sessions/*.json")
            if item.is_file()
        )
    if path.name == "sessions":
        return sorted(
            item.resolve()
            for item in path.glob("*.json")
            if item.is_file()
        )
    return None


def discover_json_files(paths: list[Path], *, recursive: bool, limit: int | None) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if path.is_file():
            if path.suffix.lower() == ".json":
                files.append(path)
            continue
        if not path.is_dir():
            continue
        session_files = session_json_files_for_dir(path)
        if session_files is not None:
            files.extend(session_files)
            continue
        iterator = path.rglob("*.json") if recursive else path.glob("*.json")
        files.extend(sorted(item.resolve() for item in iterator if item.is_file()))
    deduped = sorted(dict.fromkeys(files))
    if limit is not None and limit > 0:
        return deduped[:limit]
    return deduped


def relative_to_any(path: Path, roots: list[Path]) -> Path:
    for root in roots:
        root = root.expanduser().resolve()
        if root.is_file():
            root = root.parent
        try:
            return path.resolve().relative_to(root)
        except ValueError:
            continue
    return Path(path.name)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def output_path_for(source: Path, output_dir: Path | None, roots: list[Path]) -> Path:
    if output_dir is None:
        return source
    return output_dir.expanduser().resolve() / relative_to_any(source, roots)


def process_file(
    path: Path,
    *,
    roots: list[Path],
    client: ModelClient | None,
    args: argparse.Namespace,
) -> FileResult:
    result = FileResult(path=str(path), status="pending")
    label = f"{path.parent.parent.name}/{path.name}"
    reporter = ProgressReporter(label, enabled=not getattr(args, "no_progress", False))
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
            result.status = "skipped_no_messages"
            return result
        messages = document["messages"]
        result.original_message_count = len(messages)
        result.final_message_count = result.original_message_count
        if not args.heuristic_only:
            reporter.update(
                f"{len(messages)} msgs, {len(detectable_indices(messages))} detectable, "
                f"passes={args.passes}, workers={getattr(args, 'workers', 1)}",
                force=True,
            )
        new_document, pass_reports, removed_count = prune_document(
            document,
            client=client,
            heuristic_only=args.heuristic_only,
            heuristic_prefilter=args.heuristic_prefilter,
            passes=args.passes,
            candidate_threshold=args.candidate_threshold,
            confirm_threshold=args.confirm_threshold,
            context_radius=args.context_radius,
            char_limit=args.message_char_limit,
            workers=getattr(args, "workers", 1),
            progress=reporter,
        )
        result.passes = pass_reports
        result.removed_message_count = removed_count
        result.final_message_count = len(new_document["messages"])

        # 在写盘（可能原地改写）之前，基于原始文件定位每条被删消息的行号并固化到 record，
        # 供后续建报告用；否则 apply 模式改写后按原始 index 再定位会错位/落空。
        for pass_report in pass_reports:
            for record in pass_report.get("deleted_messages", []):
                orig_idx = record.get("original_index")
                if orig_idx is None:
                    continue
                line_range = _find_message_line_range(path, orig_idx)
                if line_range:
                    s, e = line_range["start"], line_range["end"]
                    record["line"] = s if s == e else f"{s}-{e}"

        if removed_count == 0:
            result.status = "unchanged"
            return result

        destination = output_path_for(path, args.output_dir, roots)
        result.output_path = str(destination)
        if args.apply:
            write_json(destination, new_document)
            result.status = "updated"
        else:
            result.status = "dry_run"
        return result
    except Exception as exc:  # noqa: BLE001 - keep processing other files.
        result.status = "error"
        result.error = str(exc)
        return result


def model_error_hint(error: str) -> str:
    lowered = error.lower()
    if "model request failed" not in lowered and "classification failed" not in lowered and "confirmation failed" not in lowered:
        return ""
    if "timed out" in lowered or "timeout" in lowered:
        return (
            "model classification calls the model API; check MODEL_BASE_URL/network, "
            "increase --timeout/--retries, or use --heuristic-only for local clear/context-cleared cleanup"
        )
    return (
        "model classification calls the model API; check MODEL_NAME/MODEL_BASE_URL/MODEL_API_KEY, "
        "or use --heuristic-only for local clear/context-cleared cleanup"
    )


def _extract_session_timestamp(session_path: Path) -> str:
    """从 session 文件第一条消息的 _metadata.timestamp 提取轨迹生成时间。"""
    try:
        with session_path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        messages = data.get("messages", [])
        if messages and isinstance(messages[0], dict):
            meta = messages[0].get("_metadata", {})
            if isinstance(meta, dict) and meta.get("timestamp"):
                return meta["timestamp"]
    except Exception:
        pass
    return ""


def _find_message_line_range(file_path: Path, msg_index: int) -> dict[str, int] | None:
    """从原始 JSON 文件中查找第 msg_index 条消息的行号范围。"""
    try:
        lines = file_path.read_text(encoding="utf-8-sig").splitlines()
    except Exception:
        return None
    role_count = 0
    start_line = None
    end_line = None
    for i, line in enumerate(lines):
        if '"role"' in line:
            if role_count == msg_index:
                start_line = i + 1
            elif role_count == msg_index + 1 and start_line is not None:
                end_line = i
                break
            role_count += 1
    if start_line is not None:
        if end_line is None:
            end_line = len(lines)
        return {"start": start_line, "end": end_line}
    return None


def task_dir_of(session_path: Path) -> Path:
    """从 session 文件路径回推它所属的 TASK 目录（标准报告按 TASK 分组用）。

    与 ua_detect_and_repair.task_dir_of 口径一致：
    <task>/sessions/x.json → <task>；<task>/x.json → <task>。
    子 agent 轨迹 <task>/sessions/x/subagents/y.json 会向上取到 sessions 的父目录。
    """
    parent = session_path.parent
    while parent != parent.parent:
        if parent.name == "sessions":
            return parent.parent
        parent = parent.parent
    return session_path.parent


def _session_id_for(session_path: Path) -> str:
    """由文件路径推断 session_id：取 sessions/ 之后的相对路径（去 .json）。

    主轨迹 sessions/3-test-xxx.json -> "3-test-xxx"；无 sessions/ 时回退文件名。
    """
    parts = session_path.resolve().parts
    if "sessions" in parts:
        idx = len(parts) - 1 - parts[::-1].index("sessions")
        rel = Path(*parts[idx + 1:]).as_posix()
    else:
        rel = session_path.name
    if rel.endswith(".json"):
        rel = rel[:-5]
    return rel


def _sub_from_deleted_record(record: dict[str, Any], session_path: Path, model: str) -> dict[str, Any]:
    """把一条 deleted_message 记录转成标准报告的 sub 条目。"""
    message = record.get("message") or {}
    before_content = normalize_content(message.get("content")) if isinstance(message, dict) else ""
    role = message.get("role", "") if isinstance(message, dict) else ""
    orig_idx = record.get("original_index")

    line_info: Any = record.get("line")
    if line_info is None and orig_idx is not None:
        line_range = _find_message_line_range(session_path, orig_idx)
        if line_range:
            s, e = line_range["start"], line_range["end"]
            line_info = s if s == e else f"{s}-{e}"

    reason = record.get("span_reason", "")
    problem = reason or "standalone meaningless dialogue turn"

    addition = {
        "message_index": orig_idx,
        "role": role,
        "span": record.get("span", ""),
        "confidence": record.get("span_confidence", 0.0),
        "model": model,
        "session_timestamp": _extract_session_timestamp(session_path),
    }
    return {
        "function": FUNCTION_LABEL,
        "action_type": ACTION_TYPE,
        "before_content": before_content,
        "after_content": "",
        "problem": problem,
        "line": line_info,
        "addition": addition,
    }


def build_task_report(
    task_id: str,
    task_path: str,
    task_results: list[FileResult],
    model: str,
) -> dict[str, Any]:
    """把某个 TASK 下所有 session 的删除结果汇成一份标准 semantic_report。"""
    sessions: list[dict[str, Any]] = []
    hit_trajectory_count = 0
    total_issues = 0
    for result in sorted(task_results, key=lambda r: Path(r.path).name):
        session_path = Path(result.path)
        sub: list[dict[str, Any]] = []
        for pass_report in result.passes:
            for record in pass_report.get("deleted_messages", []):
                sub.append(_sub_from_deleted_record(record, session_path, model))
        if not sub:
            continue
        hit_trajectory_count += 1
        total_issues += len(sub)
        sessions.append({"session_id": _session_id_for(session_path), "sub": sub})

    return {
        "report_type": REPORT_TYPE,
        "task_id": task_id,
        "task_path": task_path,
        "create": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total_trajectory_count": len(task_results),
            "hit_trajectory_count": hit_trajectory_count,
            "total_issues": total_issues,
        },
        "sessions": sessions,
    }


def write_task_reports(
    results: list[FileResult],
    report_override: Path | None,
    model: str,
) -> list[Path]:
    """按 TASK 分组，每个有删除的 TASK 写一份报告，文件名固定 <脚本名>.json。

    缺省（report_override=None）：写到该 TASK 自己的 <task_dir>/report/ 目录；
    指定 --report <dir>：写到 <dir>（单 TASK 直接落，多 TASK 各按 <dir>/<task_id>/ 分开防重名）。
    无删除的 TASK 不写文件（与 ua/dedup 一致）。
    """
    by_task: dict[Path, list[FileResult]] = {}
    for result in results:
        tdir = task_dir_of(Path(result.path))
        by_task.setdefault(tdir, []).append(result)

    multi = len(by_task) > 1
    written: list[Path] = []
    for tdir, task_results in sorted(by_task.items(), key=lambda kv: kv[0].name):
        report = build_task_report(tdir.name, str(tdir), task_results, model)
        if not report["sessions"]:
            continue
        if report_override is None:
            dest_dir = tdir / "report"
        else:
            dest_dir = report_override / tdir.name if multi else report_override
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{SCRIPT_STEM}.json"
        dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(dest)
    return written


def print_run_summary(results: list[FileResult]) -> dict[str, Any]:
    """汇总各文件状态，供 main() 打印到 stdout（不落盘）。"""
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return {
        "files_total": len(results),
        "by_status": counts,
        "messages_removed": sum(result.removed_message_count for result in results),
        "files_with_removals": sum(1 for result in results if result.removed_message_count > 0),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect and remove standalone meaningless dialogue turns from trajectory JSON files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[DEFAULT_INPUT_DIR],
        help="JSON file(s) or directories to scan. By default scans batch_cleaning_tasks/TASK-*/sessions/*.json.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--apply", dest="apply", action="store_true", help="Write changes (default).")
    mode_group.add_argument("--dry-run", dest="apply", action="store_false", help="Preview changes without writing.")
    parser.set_defaults(apply=True)
    parser.add_argument("--output-dir", type=Path, default=None, help="Write changed JSON files under this directory.")
    parser.add_argument("--no-recursive", action="store_true", help="Do not recursively scan directories.")
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N JSON files; 0 means no limit.")
    parser.add_argument("--report", type=Path, default=None, help="报告落盘根目录（覆盖默认的 <TASK>/report/）；多 TASK 时自动加 <report>/<task_id>/ 子层。")
    parser.add_argument("--passes", type=int, default=2, help="Repeat detection/deletion on the in-memory result.")
    parser.add_argument("--candidate-threshold", type=float, default=0.5, help="Minimum model confidence for candidate nodes.")
    parser.add_argument("--confirm-threshold", type=float, default=0.75, help="Minimum model confidence for confirmed deletion.")
    parser.add_argument("--context-radius", type=int, default=6, help="Messages before/after the target turn sent to the model.")
    parser.add_argument("--message-char-limit", type=int, default=1200, help="Max characters per message summary in prompts.")
    parser.add_argument("--heuristic-prefilter", action="store_true", help="Only send local no-op-looking messages to the model.")
    parser.add_argument("--heuristic-only", action="store_true", help="Do not call the model; only remove obvious clear/context-cleared turns.")
    parser.add_argument("--no-progress", action="store_true", help="Do not print per-message progress to stderr (progress is on by default).")
    parser.add_argument("--workers", type=int, default=40, help="Concurrent model calls per file (default 40; 1=sequential).")

    parser.add_argument("--temperature", type=float, default=0.0, help="Model temperature.")
    parser.add_argument("--max-tokens", type=int, default=1000, help="Max tokens per model response.")
    parser.add_argument("--timeout", type=int, default=900, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Retries after the first request.")
    parser.add_argument("--no-response-format", action="store_true", help="Do not send response_format=json_object.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    roots = [path.expanduser().resolve() for path in args.paths]
    report_override = args.report.expanduser().resolve() if args.report else None
    files = discover_json_files(roots, recursive=not args.no_recursive, limit=args.limit or None)
    if not files:
        print("no JSON files matched", file=sys.stderr)
        return 2

    model_config: ModelConfig | None = None
    client: ModelClient | None = None
    if not args.heuristic_only:
        try:
            model_config = resolve_model_config(args)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        client = ModelClient(model_config)

    print(f"matched JSON files: {len(files)}")
    if not args.apply:
        print("dry-run mode; matched JSON files will not be changed")
    else:
        print("apply mode; pass --dry-run to preview without writing")
    if args.heuristic_only:
        print("heuristic-only mode; model API will not be called")
    else:
        print(f"model mode; classification calls the model API (timeout={args.timeout}s, retries={args.retries})")

    results: list[FileResult] = []
    printed_model_error_hint = False
    for index, path in enumerate(files, start=1):
        print(f"[{index}/{len(files)}] {path}")
        result = process_file(path, roots=roots, client=client, args=args)
        results.append(result)
        print(
            f"  {result.status}: removed={result.removed_message_count} "
            f"messages {result.original_message_count}->{result.final_message_count}"
        )
        if result.error:
            print(f"  error: {result.error}", file=sys.stderr)
            hint = model_error_hint(result.error)
            if hint and not printed_model_error_hint:
                print(f"  hint: {hint}", file=sys.stderr)
                printed_model_error_hint = True

    model_name = model_config.model if model_config else ""
    written = write_task_reports(results, report_override, model_name)
    for dest in written:
        print(f"report: {dest}")
    if not written:
        print("no removals; no report written")
    print(json.dumps(print_run_summary(results), ensure_ascii=False, indent=2))
    return 1 if any(result.status == "error" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

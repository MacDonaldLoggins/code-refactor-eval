#!/usr/bin/env python3
"""
调用 LLM 精简 assistant 消息的 content。

参数：
  -r, --root       递归查找的根目录
  -i, --input      指定输入文件（可选，可多个；不指定则从 -r 递归发现）
  -o, --output     输出目录（可选，不指定则原地覆盖源文件）
  -report          报告输出路径，目录或文件
                   （可选，默认 {root}/report/modify_assistant_content_{ts}.json，同时产出同名 .md）
  -w, --workers    并发线程数（默认 42）

示例：
  python modify_assistant_content.py -r TASK-12
  python modify_assistant_content.py -r TASK-12 -i sessions/1-xxx.json
  python modify_assistant_content.py -r TASK-12 -o output/ -report report/

环境变量（从仓库根目录 .env 读取）：
  MODEL_API_KEY / ANTHROPIC_API_KEY
  MODEL_BASE_URL / ANTHROPIC_BASE_URL
  ANTHROPIC_VERSION / MODEL_ANTHROPIC_VERSION
  MODEL_NAME / MODIFY_ASSISTANT_MODEL / CLEAN_LLM_MODEL
  MODIFY_ASSISTANT_MIN_CHARS / CLEAN_LLM_MIN_CHARS
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import difflib
import json
from pathlib import Path
from typing import Any

import requests


SCRIPT_NAME = Path(__file__).stem
PROJECT_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_MIN_CHARS = 80
DEFAULT_WORKERS = 42


@dataclass(frozen=True)
class RuntimeConfig:
    api_key: str
    base_url: str
    anthropic_version: str
    model: str
    min_chars: int


SYSTEM_PROMPT = """You are a strict assistant-result cleaner.

You will see one assistant message's reasoning_content and content.
Your task is NOT to summarize reasoning_content, NOT to continue writing, but
to ONLY rewrite the content.

Rewriting requirements:
1. Keep ONLY the conclusion-style content that truly belongs in the final
   assistant text.
2. Delete content of the following kinds:
   - Meta-narration such as "same as every round", "already said in previous
     rounds", "this is an environment limitation".
   - Self-judgment such as "no more code to change", "no further modification
     needed".
   - Defenses about the environment, infrastructure, or network restrictions.
   - Defensive inferences such as "it will succeed on a machine with network",
     "this is not a code defect".
   - Packaging / delivery-layer statements about release bundles or artifact
     contents, e.g. "vendor/ must not ship in the delivery package".
   - Sentences that only describe how the test script detects failure, how it
     exits with a non-zero code, or how it correctly reports errors, e.g.
     "run_tests.sh correctly detects the build failure and exits with code 1".
   - Any sentence whose core message is "the run failed / the build failed /
     the tests failed / the command returned a non-zero exit code"; such
     execution-failure descriptions themselves are NOT preserved.
   - Any QA / verification parenthetical or endorsement phrase, e.g.
     "(confirmed passing by QA)", "verified by QA", "QA confirmed".
   - Next-step suggestions, extra requests, reservations, supplementary
     notes, defensive wording.
   - Anything that is not the final conclusion itself but is an explanation,
     wrapping, concession, or pivot around the conclusion.
3. Delete directly. Do NOT rewrite into another defensive form, and do NOT
   preserve explanations introduced by "but/however/therefore/so".
4. Only direct conclusions such as "what state the code is currently in" and
   "what the final test conclusion is after successful verification" may be
   kept. Packaging requirements, process explanations, script-behavior
   descriptions, QA endorsements, and execution-failure descriptions are
   NOT kept.
5. If a paragraph or list item contains both a valid conclusion and content
   that should be deleted, keep only the valid conclusion. If they cannot be
   cleanly split, delete the whole paragraph or list item.
6. If only part of the content is a valid conclusion, keep only that part and
   delete the rest.
7. Concrete details such as percentages, counts, route/test numbers, file
   names, and command names must use the wording already present in the
   original text. Do NOT reuse fixed numbers from examples, and do NOT move
   numbers from elsewhere into the current text.
8. Do NOT introduce new facts that were not in the original text.
9. Preserve the language of the original text: if the original is English,
   output English; if the original is Chinese, output Chinese.
10. The output MUST be the "revised content body" itself. Do NOT output
    explanations, titles, code blocks, or JSON.
11. If the entire content should not be kept, output an empty string.

Example:
Input content:
This is the same environment limitation reported in every round. There are no remaining code changes to make:

- **Dockerfile** is correct — `composer install` pulls from upstream at build time.
- `vendor/` must not ship in the delivery package.
- **run_tests.sh** correctly detects the build failure and exits with code 1 (confirmed passing by QA).
- **API test coverage** is complete, no weakly-tested endpoints flagged.

The Docker build fails because this environment cannot reach packagist.org over TLS. That is a network/infrastructure constraint, not a code defect. On any host with outbound internet access, `docker compose up --build` will complete successfully.

Output content:
- **Dockerfile** is correct — `composer install` pulls from upstream at build time.
- **API test coverage** is complete, no weakly-tested endpoints flagged.

You may consult reasoning_content when judging, but NEVER rewrite reasoning_content into the output. The output targets only the content.
"""


# ── 工具函数 ──────────────────────────────────────────────────────────

def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def first_non_empty(*values: str | None) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def first_positive_int(default: int, *values: str | None) -> int:
    text = first_non_empty(*values)
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    if value < 0:
        return default
    return value


def anthropic_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
                elif item.get("text"):
                    parts.append(str(item["text"]))
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if value.get("text"):
            return str(value["text"])
    return str(value)


def strip_wrapping(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    if cleaned.startswith('"""') and cleaned.endswith('"""') and len(cleaned) >= 6:
        cleaned = cleaned[3:-3].strip()
    return cleaned


def compute_diff(before: str, after: str) -> dict[str, list[str]]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added: list[str] = []
    deleted: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, before_lines, after_lines
    ).get_opcodes():
        if tag == "delete":
            deleted.extend(before_lines[i1:i2])
        elif tag == "insert":
            added.extend(after_lines[j1:j2])
        elif tag == "replace":
            deleted.extend(before_lines[i1:i2])
            added.extend(after_lines[j1:j2])
    return {"added": added, "deleted": deleted}


# ── 配置 ──────────────────────────────────────────────────────────────

def resolve_runtime_config() -> RuntimeConfig:
    env_file = load_env(PROJECT_ENV_PATH)
    api_key = first_non_empty(
        env_file.get("MODEL_API_KEY"),
        env_file.get("ANTHROPIC_API_KEY"),
    )
    base_url = first_non_empty(
        env_file.get("MODEL_BASE_URL"),
        env_file.get("ANTHROPIC_BASE_URL"),
        env_file.get("DEFAULT_BASE_URL"),
    )
    anthropic_version = first_non_empty(
        env_file.get("ANTHROPIC_VERSION"),
        env_file.get("MODEL_ANTHROPIC_VERSION"),
    )
    model = first_non_empty(
        env_file.get("MODEL_NAME"),
        env_file.get("MODIFY_ASSISTANT_MODEL"),
        env_file.get("CLEAN_LLM_MODEL"),
        env_file.get("DEFAULT_MODEL"),
    )
    min_chars = first_positive_int(
        DEFAULT_MIN_CHARS,
        env_file.get("MODIFY_ASSISTANT_MIN_CHARS"),
        env_file.get("CLEAN_LLM_MIN_CHARS"),
        env_file.get("DEFAULT_MIN_CHARS"),
    )
    return RuntimeConfig(
        api_key=api_key,
        base_url=base_url,
        anthropic_version=anthropic_version,
        model=model,
        min_chars=min_chars,
    )


# ── LLM 调用 ─────────────────────────────────────────────────────────

def call_llm(
    *,
    api_key: str,
    base_url: str,
    anthropic_version: str,
    model: str,
    reasoning_content: str,
    content: str,
    timeout: int,
) -> str:
    user_prompt = (
        "Please return ONLY the revised content.\n\n"
        "[reasoning_content]\n"
        f"{reasoning_content or '(empty)'}\n\n"
        "[content]\n"
        f"{content}\n"
    )
    # 本地 patch B: 动态 max_tokens。清理任务需要照抄大部分原文，输出量 ≈ 输入量。
    # 按 1 token ≈ 3 chars 估算 input tokens，×1.3 留 buffer，最少 4096 最多 16384。
    est_input_tokens = max(len(content), len(reasoning_content)) // 3
    dyn_max_tokens = min(max(int(est_input_tokens * 1.3) + 500, 4096), 16384)
    payload = {
        "model": model,
        "max_tokens": dyn_max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": anthropic_version,
        "content-type": "application/json",
    }
    response = requests.post(
        anthropic_messages_url(base_url),
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"LLM API 错误 {response.status_code}: {response.text[:500]}")
    data = response.json()
    # 本地 patch: 撞到 max_tokens 上限时 LLM 会在句中硬切，输出即"损坏数据"。
    # 直接抛异常，上层归为 error 而不是 modified，避免把截尾写回文件。
    stop_reason = data.get("stop_reason")
    if stop_reason == "max_tokens":
        raise RuntimeError(
            f"LLM 输出被 max_tokens 截断 (stop_reason=max_tokens)；"
            f"input content 长度 {len(content)} chars, 动态 max_tokens={dyn_max_tokens}，"
            f"本条 assistant 需人工处理"
        )
    blocks = data.get("content", [])
    text = "\n".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    cleaned_text = strip_wrapping(text)
    # 本地 patch: 短路 —— LLM 判定"无可清理"时会原样返回。此时应视为无变化，
    # 跳过所有异常检测（否则 refusal/骤降关键词会误伤"原文本来就长这样"的样本）。
    if cleaned_text.strip() == content.strip():
        return cleaned_text
    # 本地 patch: 检测 LLM 拒绝执行任务的元回复（"I don't have context", "Could you clarify",
    # "Please provide", "I don't have access"）—— 这些短语在真实的技术总结里几乎不会出现，
    # 但 LLM 判定输入"没有可清理内容"时会突然吐这类澄清语，把 assistant content 整个替换掉。
    _refusal_markers = (
        "I don't have context",
        "I don't have access to",
        "Could you clarify what",
        "Please provide the",
        "I need more context",
        "What content would you like",
        # 补：应对"我没有原文/只拿到摘要"变体（50-TASK 自测中 F702AF/1-develop 漏网案例）
        "I don't have the revised",
        "only the summary you pasted",
        "To return the revised",
        "I need the actual file",
    )
    if any(m in cleaned_text for m in _refusal_markers):
        raise RuntimeError(
            f"LLM 输出疑似元回复/拒绝执行；命中标记，原文长度 {len(content)}，"
            f"输出长度 {len(cleaned_text)}；已拦截避免覆盖真实内容"
        )
    # 本地 patch: 长度骤降 —— 输出小于原文 15% 且原文 >200 字符时，视为可疑清理（可能是 LLM
    # 误把大段内容当"多余"整段砍掉，或输出没跟原意对齐）。抛异常保 before。
    # 阈值 15%：防御性文字压缩到 20~30% 是合规清理（见 F9FDFB/1，27% 是正确 3 条要点），
    # 只有跌破 15% 才可能是真整段丢失。
    if len(content) > 200 and len(cleaned_text) < len(content) * 0.15:
        raise RuntimeError(
            f"LLM 输出长度骤降（{len(content)} -> {len(cleaned_text)}，剩 {len(cleaned_text)/max(1,len(content)):.0%}）；"
            f"疑似过度删除，已拦截"
        )
    # 本地 patch: 子串完整性校验 —— SYSTEM_PROMPT 要求"直接删除，不改写"，所以合规输出的
    # 每一非空行都应是原文子串。只在输出接近或超过原文长度时启用（0.85+），大幅清理时跳过
    # （删除会打断句子子串关系）。用于拦截：
    #   - 元前缀污染（如 "The content you provided appears to be... returned as-is:"）
    #   - 字符级换字（em dash `—` 换 ASCII `-`）
    #   - 任何 LLM"轻微润色"造成的字改
    if len(cleaned_text) > len(content) * 0.85:
        for line in cleaned_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped not in content:
                raise RuntimeError(
                    f"LLM 输出含非原文行（疑似加字/换字/元前缀污染）：{stripped[:100]!r}；"
                    f"原文长度 {len(content)}，输出长度 {len(cleaned_text)}；已拦截"
                )
    return cleaned_text


# 本地 patch A: 可重试错误关键词。max_tokens 截尾即使重试也会撞（动态 max_tokens 已拉高上限，
# 再撞说明输入本身超长，重试无意义）；refusal 和长度骤降是 LLM 单次抽风，重试通常能好。
_RETRYABLE_MARKERS = (
    "LLM 输出疑似元回复",
    "LLM 输出长度骤降",
    "LLM 输出含非原文行",
)


def call_llm_with_retry(
    *,
    api_key: str,
    base_url: str,
    anthropic_version: str,
    model: str,
    reasoning_content: str,
    content: str,
    timeout: int,
    max_attempts: int = 3,
) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return call_llm(
                api_key=api_key,
                base_url=base_url,
                anthropic_version=anthropic_version,
                model=model,
                reasoning_content=reasoning_content,
                content=content,
                timeout=timeout,
            )
        except RuntimeError as exc:
            last_exc = exc
            msg = str(exc)
            if not any(m in msg for m in _RETRYABLE_MARKERS):
                raise
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"重试 {max_attempts} 次仍失败：{msg}"
                )
            print(f"    ↻ 第 {attempt} 次输出异常，重试中：{msg[:80]}")
    assert last_exc is not None
    raise last_exc


# ── 文件发现与消息解析 ────────────────────────────────────────────────

def discover_session_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("sessions/*.json")):
        if path.parent.name != "sessions":
            continue
        if not any(parent.name.startswith("TASK-") for parent in path.parents):
            continue
        files.append(path)
    return files


def find_message_list(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    for key in ("messages", "chat_messages", "conversation", "history"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return None


def find_target_assistants(messages: list[dict[str, Any]]) -> list[tuple[int, str, str]]:
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if message.get("role") != "assistant":
            continue
        if message.get("tool_calls"):
            return []
        content_text = normalize_text(message.get("content")).strip()
        if not content_text:
            return []
        reasoning_text = normalize_text(message.get("reasoning_content")).strip()
        return [(idx, reasoning_text, content_text)]
    return []


def update_content(message: dict[str, Any], new_text: str) -> None:
    old_content = message.get("content")
    if isinstance(old_content, list):
        message["content"] = [{"type": "text", "text": new_text}]
    else:
        message["content"] = new_text


def clear_reasoning_content(message: dict[str, Any]) -> None:
    if "reasoning_content" not in message:
        return
    old_reasoning = message.get("reasoning_content")
    if isinstance(old_reasoning, list):
        message["reasoning_content"] = []
    else:
        message["reasoning_content"] = ""


# ── 报告路径解析 ──────────────────────────────────────────────────────

def resolve_report_paths(report_arg: str | None, root: Path, timestamp: str) -> tuple[Path, Path]:
    """返回 (json_path, md_path)。

    - 未指定 -report：默认 {root}/report/{SCRIPT_NAME}_{ts}.json/.md
    - -report 传目录：{dir}/{SCRIPT_NAME}_{ts}.json/.md
    - -report 传 .json/.md 文件：沿用其 stem，若未带时间戳则附加 _{ts}
    """
    if report_arg is None:
        base = root / "report" / f"{SCRIPT_NAME}_{timestamp}.json"
    else:
        p = Path(report_arg).expanduser().resolve()
        if p.suffix in (".json", ".md"):
            base = p.with_suffix(".json")
        else:
            base = p / f"{SCRIPT_NAME}_{timestamp}.json"

    if base.stem.endswith(f"_{timestamp}"):
        md = base.with_suffix(".md")
    else:
        md = base.with_name(f"{base.stem}_{timestamp}").with_suffix(".md")
        base = base.with_name(f"{base.stem}_{timestamp}").with_suffix(".json")
    return base, md


# ── Markdown 报告 ─────────────────────────────────────────────────────

def _fence(text: str) -> tuple[str, str]:
    """选一条不会与文本冲突的围栏。默认 ```，若正文含 ``` 则用 ````。"""
    fence = "```"
    while fence in text:
        fence += "`"
    return fence, fence


def write_markdown_report(
    md_path: Path,
    modifications: list[dict[str, Any]],
    summary: dict[str, int],
) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# assistant 消息 content 精简 — 修改前 / 修改后 / 新增 / 删除",
        "",
        f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 扫描文件数: {summary['files_scanned']}",
        f"- 修改文件数: {summary['files_modified']}",
        f"- 修改消息数: {summary['messages_modified']}",
        "",
    ]
    for i, m in enumerate(modifications, 1):
        before = m.get("before") or ""
        after = m.get("after") or ""
        added = m.get("added") or []
        deleted = m.get("deleted") or []
        fence_open, fence_close = _fence(before + "\n" + after)
        lines.extend([
            "---",
            "",
            f"## #{i} `{m.get('file')}` · msg_idx {m.get('message_index')}",
            "",
            "### 修改前",
            fence_open,
            before,
            fence_close,
            "",
            "### 修改后",
            fence_open,
            after,
            fence_close,
            "",
            f"### 新增（{len(added)} 行）",
        ])
        if added:
            lines.extend(f"- {line}" for line in added)
        else:
            lines.append("_（无）_")
        lines.append("")
        lines.append(f"### 删除（{len(deleted)} 行）")
        if deleted:
            lines.extend(f"- {line}" for line in deleted)
        else:
            lines.append("_（无）_")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")


def _describe_problem(deleted: list[str]) -> str:
    """如实转述实际删除的内容作为问题定性，不靠关键词预判分类。

    旧实现按关键词（docker/qa/vendor 等）映射到固定标签，会贴错——例如某条
    实际删的是"客套 + 免责声明"，却因命中 environment/network 被误标成
    "环境限制描述"。这里改为直接引用真正删掉的行，删了什么就写什么，
    不提前归类。
    """
    non_empty = [line.strip() for line in deleted if line.strip()]
    if not non_empty:
        return "assistant content contains non-conclusive material (no line-level diff)"
    return "removed non-conclusion content: " + " / ".join(non_empty)


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


def _extract_task_id(rel: str, base: Path) -> tuple[str, Path]:
    """从文件相对路径推断 task_id 和 task 根目录。"""
    parts = rel.replace("\\", "/").split("/")
    if parts and parts[0].startswith("TASK-"):
        return parts[0], base / parts[0]
    return base.name, base


def write_task_reports(
    root: Path,
    results: list[dict[str, Any]],
) -> None:
    """为每个有修改的 task 写入独立修复总结 report。"""
    from collections import defaultdict

    task_mods: dict[str, list[dict[str, Any]]] = defaultdict(list)
    task_file_counts: dict[str, int] = defaultdict(int)
    task_total_scanned: dict[str, int] = defaultdict(int)

    for r in results:
        rel = r.get("file", "")
        task_id, _ = _extract_task_id(rel, root)
        task_total_scanned[task_id] += 1
        if r.get("status") != "modified":
            continue
        task_mods[task_id].extend(r.get("modifications", []))
        task_file_counts[task_id] += 1

    for task_id, mods in sorted(task_mods.items()):
        _, task_path = _extract_task_id("", root)
        if task_id != root.name:
            task_path = root / task_id
        report_dir = task_path / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / "modify_assistant_content_report.json"

        file_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in mods:
            file_groups[m.get("file", "")].append(m)

        sessions: list[dict[str, Any]] = []
        hit_count = 0
        for file_rel, file_mods in sorted(file_groups.items()):
            session_id = Path(file_rel).stem
            hit_count += 1
            sub: list[dict[str, Any]] = []
            for m in file_mods:
                # before/after 存全文，不截断——一条 assistant 消息通常也就几百字，
                # 全存进报告成本可忽略，换来"删前 vs 删后"的完整对照，也便于核对删得准不准。
                before_text = m.get("before") or ""
                after_text = m.get("after") or ""
                deleted_lines = [line for line in (m.get("deleted") or []) if line.strip()]
                session_timestamp = ""
                msg_idx = m.get("message_index")
                line_info = None
                if msg_idx is not None:
                    abs_path = root / file_rel
                    line_range = _find_message_line_range(abs_path, msg_idx)
                    if line_range:
                        s, e = line_range["start"], line_range["end"]
                        line_info = s if s == e else f"{s}-{e}"
                    session_timestamp = _extract_session_timestamp(abs_path)
                sub.append({
                    "function": "modify_assistant_content",
                    "action_type": "update",
                    "before_content": before_text,
                    "after_content": after_text,
                    "deleted_content": deleted_lines,
                    "problem": _describe_problem(m.get("deleted", [])),
                    "line": line_info,
                    "addition": [
                        {
                            "original_chars": len(m.get("before", "")),
                            "cleaned_chars": len(m.get("after", "")),
                            "delta_chars": len(m.get("before", "")) - len(m.get("after", "")),
                            "session_timestamp": session_timestamp,
                        }
                    ],
                })
            sessions.append({"session_id": session_id, "sub": sub})

        task_report: dict[str, Any] = {
            "report_type": "modify_assistant_content_report",
            "task_id": task_id,
            "task_path": str(task_path),
            "create": datetime.now().isoformat(timespec="seconds"),
            "summary": {
                "total_trajectory_count": task_total_scanned[task_id],
                "hit_trajectory_count": hit_count,
                "total_issues": len(mods),
            },
            "sessions": sessions,
        }
        report_file.write_text(
            json.dumps(task_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[task-report] {report_file}")


# ── 单文件处理（无共享可变状态，线程安全） ─────────────────────────────

def process_one_file(
    *,
    path: Path,
    root: Path,
    config: RuntimeConfig,
    output_dir: Path | None,
) -> dict[str, Any]:
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = path.name

    result: dict[str, Any] = {
        "file": rel,
        "status": "pending",
        "modifications": [],
    }

    print(f"\n▶ 处理: {rel}")

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    messages = find_message_list(data)
    if messages is None:
        print(f"  → 无消息列表，跳过: {rel}")
        result["status"] = "skipped"
        return result

    targets = find_target_assistants(messages)
    if not targets:
        print(f"  → 无符合条件的 assistant 消息，跳过: {rel}")
        result["status"] = "skipped"
        return result

    file_modified = False

    for idx, reasoning_text, content_text in targets:
        if len(content_text) < config.min_chars:
            print(f"  [{idx}] content 过短({len(content_text)}字符)，跳过: {rel}")
            result["status"] = "skipped"
            continue

        try:
            cleaned = call_llm_with_retry(
                api_key=config.api_key,
                base_url=config.base_url,
                anthropic_version=config.anthropic_version,
                model=config.model,
                reasoning_content=reasoning_text,
                content=content_text,
                timeout=120,
            )
        except Exception as exc:
            print(f"  [{idx}] ❌ LLM 错误: {exc}: {rel}")
            result["status"] = "error"
            result["error"] = str(exc)
            continue

        if cleaned.strip() == content_text.strip():
            print(f"  [{idx}] ○ 无变化: {rel}")
            result["status"] = "unchanged"
            continue

        diff = compute_diff(content_text, cleaned)
        removed_chars = len(content_text) - len(cleaned)
        print(f"  [{idx}] ✓ content 变更，字符变化 {removed_chars}: {rel}")

        result["modifications"].append({
            "file": rel,
            "message_index": idx,
            "before": content_text,
            "after": cleaned,
            "added": diff["added"],
            "deleted": diff["deleted"],
        })

        update_content(messages[idx], cleaned)
        clear_reasoning_content(messages[idx])
        file_modified = True

    if not file_modified:
        return result

    if output_dir:
        out_path = output_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_path = path

    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)

    result["status"] = "modified"
    return result


# ── CLI 参数 ──────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="调用 LLM 精简 TASK*/sessions/*.json 中 assistant 消息的 content"
    )
    parser.add_argument(
        "-r",
        "--root",
        required=True,
        help="递归查找的根目录，例如 TASK-12 或 /path/to/root",
    )
    parser.add_argument(
        "-i",
        "--input",
        nargs="+",
        help="指定输入文件（可选，可多个；不指定则从 -r 递归发现）",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="输出目录（可选，不指定则原地覆盖源文件）",
    )
    parser.add_argument(
        "-report",
        help=(
            "报告输出路径，目录或文件"
            f"（可选，默认 {{root}}/report/{SCRIPT_NAME}_<ts>.json，同时产出同名 .md）"
        ),
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"并发线程数（默认 {DEFAULT_WORKERS}）",
    )
    return parser


# ── 主流程 ────────────────────────────────────────────────────────────

def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"错误: root 路径不存在: {root}")
        return 1

    config = resolve_runtime_config()
    missing_config = [
        name
        for name, value in (
            ("MODEL_API_KEY", config.api_key),
            ("MODEL_BASE_URL", config.base_url),
            ("ANTHROPIC_VERSION", config.anthropic_version),
            ("MODEL_NAME", config.model),
        )
        if not value
    ]
    if missing_config:
        print(
            f"错误: {PROJECT_ENV_PATH} 缺少模型配置: "
            + ", ".join(missing_config)
        )
        return 1

    # ── 确定待处理文件列表 ──
    if args.input:
        session_files = [Path(f).expanduser().resolve() for f in args.input]
        missing = [f for f in session_files if not f.exists()]
        if missing:
            for f in missing:
                print(f"错误: 输入文件不存在: {f}")
            return 1
    else:
        session_files = discover_session_files(root)

    if not session_files:
        print(f"在 {root} 下未找到符合条件的文件")
        return 0

    output_dir = Path(args.output).expanduser().resolve() if args.output else None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path, md_path = resolve_report_paths(args.report, root, timestamp)
    workers = max(1, args.workers)

    print(f"找到 {len(session_files)} 个文件，并发线程数: {workers}")
    if output_dir:
        print(f"输出目录: {output_dir}")
    print(f"报告(JSON): {json_path}")
    print(f"报告(MD):   {md_path}")

    # ── 多线程处理 ──
    results: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                process_one_file,
                path=path,
                root=root,
                config=config,
                output_dir=output_dir,
            ): path
            for path in session_files
        }
        for future in as_completed(futures):
            fpath = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                try:
                    rel = str(fpath.relative_to(root))
                except ValueError:
                    rel = fpath.name
                print(f"\n❌ 文件处理异常: {rel}: {exc}")
                results.append({
                    "file": rel,
                    "status": "error",
                    "error": str(exc),
                    "modifications": [],
                })

    # ── 汇总统计 ──
    all_modifications: list[dict[str, Any]] = []
    files_modified = 0
    messages_modified = 0
    unchanged = 0
    skipped = 0
    errors = 0

    for r in results:
        status = r["status"]
        mods = r.get("modifications", [])
        if status == "modified":
            files_modified += 1
            messages_modified += len(mods)
            all_modifications.extend(mods)
        elif status == "unchanged":
            unchanged += 1
        elif status == "skipped":
            skipped += 1
        elif status == "error":
            errors += 1

    # ── 写入报告 ──
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "files_scanned": len(session_files),
            "files_modified": files_modified,
            "messages_modified": messages_modified,
        },
        "modifications": all_modifications,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    write_markdown_report(md_path, all_modifications, report["summary"])

    write_task_reports(root, results)

    # ── 终端摘要 ──
    print("\n" + "=" * 50)
    print("处理完成:")
    print(f"  ✓ 修改文件数:    {files_modified}")
    print(f"  ✓ 修改消息数:    {messages_modified}")
    print(f"  ○ 无变化:        {unchanged}")
    print(f"  → 跳过:         {skipped}")
    print(f"  ✗ 错误:         {errors}")
    print(f"  共扫描文件:      {len(session_files)}")
    print(f"  报告(JSON): {json_path}")
    print(f"  报告(MD):   {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

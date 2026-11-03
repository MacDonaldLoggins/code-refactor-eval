#!/usr/bin/env python3
"""修复 final sessions 中主 session 与 compact subagent 的对齐关系。

支持参数：
  -r       包含各个 TASK 目录的根目录。
  -i       可选，只处理指定的单个主 session JSON 文件。
  -o       可选，输出文件或输出目录；不传则原地修改。
  -report  可选，报告根目录；不传则写入各 TASK 内的 report/ 目录。
           默认落 <task_dir>/report/fix_compact_alignment.json；
           指定时落 <report 根>/<task_id>/report/fix_compact_alignment.json。
  -workers 可选，指定并发线程数；不传则自动使用 min(32, CPU 核数)。

只处理 agent-acompact-*.json，其它 subagent 文件（agent-general-*, agent-Explore- 等）
一律不动；主 session 从不修改。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import requests  # 仅在 --enable-llm 时用；未装也不影响主流程
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


PRINT_LOCK = threading.Lock()


def log_line(message: str) -> None:
    with PRINT_LOCK:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()


SCRIPT_NAME = Path(__file__).stem
COMPACT_ID_RE = re.compile(r"acompact-?([0-9a-f]+)", re.IGNORECASE)
SESSION_NAME_RE = re.compile(r"^(\d+)-(develop|bugfix|test)-(.+)\.json$")
COMPACT_PROMPT_PREFIX = "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools."
ADDITIONAL_INSTRUCTIONS_SEP = "\n\nAdditional Instructions:"
BACKUP_SUFFIX_RE = re.compile(r"\.CORRUPTED_[^.]+_BACKUP$")


# --------------------------------------------------------------------------
# LLM 辅助基础设施（对齐 fix_missing_summary_nodes_v2 的调用范式）
#
# 只在 --enable-llm 打开时启用；关闭时脚本行为与老版本完全一致。
# 落地点：
#   - #1 backup-repaired：容错解析出 JSON 后，让 LLM 判 usable/partial/unrecoverable
#   - #12 misfiled-referenced-skipped：让 LLM 分类 wrong_agent_id / wrong_file_content /
#         compact_missing_critical / uncertain，只写报告，不改文件
# --------------------------------------------------------------------------

# 与 fix_missing 一致：从仓库根 .env 读凭据；本脚本位于 change_session/
# → parents[1] 是 d:/vscode/code/
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_LLM_BASE_URL = "https://ai.mindflow.com.cn"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_LLM_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True)
class LLMConfig:
    """LLM 运行时配置。api_key 为空视为未启用。"""
    api_key: str
    base_url: str
    anthropic_version: str
    model: str
    enable_misfiled: bool = False
    enable_backup: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and (self.enable_misfiled or self.enable_backup)


FIX_TYPES: dict[int, str] = {
    1: "backup-repaired",
    2: "orphan-deleted",
    3: "misfiled-orphan-deleted",
    4: "sub-role-fixed",
    5: "trigger-user-synthesized",
    6: "tool-call-args-overwritten",
    7: "tail-echo-flattened",
    8: "system-synced",
    9: "trigger-content-synced",
    10: "prefix-synced",
    11: "summary-synced",
    12: "misfiled-referenced-skipped",
}

FIX_TYPE_CN: dict[str, str] = {
    "backup-repaired": "修补被截断/含 null 的 CORRUPTED_BACKUP JSON 为可用 compact 文件",
    "orphan-deleted": "删除主 session 无对应 tool_call 的孤儿 compact 文件",
    "misfiled-orphan-deleted": "删除结构不像 compact 的孤儿 agent-acompact-*.json",
    "sub-role-fixed": "把 compact sub 首条消息的 role 恢复为 system",
    "trigger-user-synthesized": "在最终 summary 前补齐缺失的 compact trigger user 消息",
    "tool-call-args-overwritten": "用主 session tool_call 的 arguments 覆盖 sub 中损坏的 arguments",
    "tail-echo-flattened": "移除 compact sub 尾部对主 session 的多余回显",
    "system-synced": "把 compact sub 的 system prompt 同步为主 session 的 compact prompt",
    "trigger-content-synced": "把 compact sub 的 trigger user content 同步为主 session 的 compact prompt",
    "prefix-synced": "用主 session 的活跃上下文覆盖 compact sub 的 prefix 段",
    "summary-synced": "把 compact sub 的最终 summary 内容同步为主 session 的 tool_result",
    "misfiled-referenced-skipped": "命名 agent-acompact-* 但内容不是 compact（除 idx0 外找不到 CRITICAL prompt）—— 主 session 有引用所以不删，跳过所有修复动作等人工核实",
}

# function 字段：按 fix_type 归类的"具体操作类型"（比检查描述更短，供报告分组/阅读）
FIX_TYPE_FUNCTION: dict[str, str] = {
    "backup-repaired": "compact 文件损坏修复",
    "orphan-deleted": "孤儿 compact 文件删除",
    "misfiled-orphan-deleted": "孤儿 compact 文件删除",
    "sub-role-fixed": "compact 节点角色修复",
    "trigger-user-synthesized": "trigger user 节点补齐",
    "tool-call-args-overwritten": "工具调用参数修复",
    "tail-echo-flattened": "尾部回显清理",
    "system-synced": "system prompt 同步",
    "trigger-content-synced": "trigger user 内容同步",
    "prefix-synced": "prefix 上下文同步",
    "summary-synced": "assistant 总结内容同步",
    "misfiled-referenced-skipped": "疑似误命名 compact 文件标注",
}

# Verify 阶段可能出现的检测项（仅统计，不一定对应处理动作）
VERIFY_TYPE_CN: dict[str, str] = {
    **FIX_TYPE_CN,
    "ok": "已对齐",
    "missing-compact-subagent-file": "缺少 compact subagent 文件",
    "missing-subagent-summary-assistant": "compact subagent 缺少最终 summary assistant",
    "invalid-main-messages": "主 session messages 无效",
    "invalid-subagent-messages": "compact subagent messages 无效",
    "fake-summary-cloned": "主 session tool_result 与已有 assistant 消息一致（不算真 summary，跳过）",
    "orphan-compact-tool-call": "主 session 有 compact tool_call 但缺少对应 tool_result（不修）",
    "orphan-continuation-user": "主 session 中出现孤悬的 continuation user（不修）",
    "session-begins-with-continuation": "主 session 首条即为 continuation user（不修）",
    "additional-instructions-divergence": "trigger user 尾部带 Additional Instructions（保留，不修）",
    "mapping-regex-divergence": "mapping 表和 tool_call_id 正则得出的 agent_id 不一致（以 mapping 为准，仅告警）",
}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def preview(text: str, limit: int = 300) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


# --------------------------------------------------------------------------
# 行号定位：把「被改字段」映射到写出后 subagent JSON 文件里的行号。
#
# top_level_element_lines / messages_element_lines 移植自
# structure_check_script/check_session_node_order.py（纯字符扫描、缩进无关，
# 已在仓库验证），用来拿到 messages[] 里每条 message 对象的起始行；再用
# _field_offset_in_message 在对象内定位顶层字段（role/content）行偏移。
# --------------------------------------------------------------------------

def top_level_element_lines(text: str) -> list[int]:
    """扫描一个 JSON 数组文本，返回其每个顶层元素的起始行号（1-based，相对 text）。"""
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


def messages_element_lines(path: Path) -> list[int]:
    """返回 path 文件里 messages[] 每条消息对象的起始行号（1-based）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
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


def _field_offset_in_message(message: dict[str, Any], field: str) -> int | None:
    """在 json.dumps(message, indent=2) 里，顶层 field 键相对对象首行 `{` 的行偏移（0-based）。

    由于 json.dumps 行数与缩进深度无关，这个偏移可直接叠加到文件里该消息对象的起始行上。
    字符串值里的换行会被转义成字面 \\n（不产生真实换行），故内层不会伪装成顶层键。
    """
    try:
        text = json.dumps(message, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return None
    key_prefix = f'  "{field}":'
    for i, ln in enumerate(text.splitlines()):
        if ln.startswith(key_prefix):
            return i
    return None


def _find_tool_call_message_index(messages: list[dict[str, Any]], tool_call_id: str | None) -> int | None:
    if not tool_call_id:
        return None
    for i, msg in enumerate(messages):
        for call in (msg.get("tool_calls") or []):
            if isinstance(call, dict) and call.get("id") == tool_call_id:
                return i
    return None


def _message_timestamp(message: dict[str, Any] | None) -> str:
    """取消息 _metadata.timestamp；缺失返回空串。"""
    if not isinstance(message, dict):
        return ""
    metadata = message.get("_metadata")
    if isinstance(metadata, dict):
        ts = metadata.get("timestamp")
        if isinstance(ts, str):
            return ts
    return ""


def assign_change_lines(
    effective_read: Path,
    sub_messages: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    *,
    summary_index: int | None = None,
    prompt_user_index: int | None = None,
) -> None:
    """给每条带 locator 的 change 填 change['line']（写出后 subagent 文件里的行号）。

    索引解析基于**最终** sub_messages：summary/trigger 用调用方传入的最终下标，
    prefix 取首条被替换消息（idx=1），tool_args 按 tool_call_id 现查。无法定位则置 None。
    """
    element_lines: list[int] | None = None

    def resolve_elem_line(idx: int | None) -> int | None:
        nonlocal element_lines
        if idx is None or idx < 0:
            return None
        if element_lines is None:
            element_lines = messages_element_lines(effective_read)
        return element_lines[idx] if idx < len(element_lines) else None

    for change in changes:
        locator = change.get("locator")
        if not isinstance(locator, dict):
            continue
        kind = locator.get("kind")
        idx: int | None = None
        field: str | None = None
        if kind == "msg_field":
            idx = locator.get("index")
            field = locator.get("field")
        elif kind == "summary":
            idx = summary_index
            field = "content"
        elif kind == "trigger":
            idx = prompt_user_index
            field = "content"
        elif kind == "prefix":
            idx = 1
        elif kind == "tool_args":
            idx = _find_tool_call_message_index(sub_messages, locator.get("tool_call_id"))

        if idx is not None and 0 <= idx < len(sub_messages):
            change["timestamp"] = _message_timestamp(sub_messages[idx])

        base_line = resolve_elem_line(idx)
        if base_line is None:
            change["line"] = None
            continue
        if field is None or idx is None or idx >= len(sub_messages):
            change["line"] = base_line
            continue
        offset = _field_offset_in_message(sub_messages[idx], field)
        change["line"] = base_line + offset if offset is not None else base_line


# --------------------------------------------------------------------------
# hallucination_free 退化对策：#11 summary-synced / #10 prefix-synced 加护栏
#
# 背景：A/B judge 结果显示 overall 通过率 76.6% → 76.4%，其他 6 维基本持平，
# 只有 hallucination_free -2.23pp。锋利单维退化 → 定位到"在 sub 的 assistant
# 消息里注入了主 session 视角的新文本"，即 #11（summary-synced）和 #10
# （prefix-synced）。#11 是主嫌疑：把主 session 的 tool_result（Claude 自己
# 拼的 summary，可能引用 sub 上下文没有的信息）直接盖到 sub 尾部 assistant.content
# 上，judge 判"claim 无观测出处"。
#
# 对策：只在 sub 端 summary 真的缺/残缺时才 sync；sub 端已成型的 summary 保留。
# --------------------------------------------------------------------------

def is_sub_summary_intact(text: str) -> bool:
    """判定 sub 端 summary 是否已经"成型"—— 只在缺/残缺时才让主视角覆盖。

    退化画像：sub 端 summary 通常本身完整，只是与主 session 视角不同；
    盖过去让 sub 出现"没对应 tool_output 的 claim"。反过来，只有 sub 真正
    缺 summary（空、极短、错误占位）时，用主 session 的 tool_result 回填
    才是净收益。
    """
    if not isinstance(text, str):
        return False
    s = text.strip()
    if len(s) < 120:
        return False
    low = s.lower()
    if low.startswith(("[error", "error:", "<error", "(error")):
        return False
    if s in ("(no output)", "(empty)", "...", "…"):
        return False
    return True


def prefix_similarity(main_prefix: list[dict[str, Any]], sub_prefix: list[dict[str, Any]]) -> float:
    """按 (role, content 前 200 字的 sha256) 集合交集估相似度。

    用于 #10 prefix-synced 的护栏：sub prefix 已经与 main prefix 大致对齐时
    不动，避免把 sub 独有的 tool_result 覆盖成主视角。
    """
    if not main_prefix or not sub_prefix:
        return 0.0

    def keys(msgs: list[dict[str, Any]]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", ""))
            content = m.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            elif not isinstance(content, str):
                content = str(content)
            out.append((role, sha256_text(content[:200])))
        return out

    ka, kb = keys(main_prefix), keys(sub_prefix)
    if not ka or not kb:
        return 0.0
    common = len(set(ka) & set(kb))
    return common / max(len(ka), len(kb))


# --------------------------------------------------------------------------
# LLM 环境读取 & Anthropic Messages 调用（与 fix_missing_summary_nodes_v2 对齐）
# --------------------------------------------------------------------------


def load_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析器，与 fix_missing 一致。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def anthropic_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def resolve_llm_config(*, enable_misfiled: bool, enable_backup: bool) -> LLMConfig:
    env = load_env_file(DEFAULT_ENV_PATH)
    api_key = _first_non_empty(
        env.get("MODEL_API_KEY"),
        env.get("ANTHROPIC_API_KEY"),
        os.environ.get("MODEL_API_KEY"),
        os.environ.get("ANTHROPIC_API_KEY"),
    )
    base_url = _first_non_empty(
        env.get("MODEL_BASE_URL"),
        env.get("ANTHROPIC_BASE_URL"),
        env.get("DEFAULT_BASE_URL"),
        os.environ.get("MODEL_BASE_URL"),
        DEFAULT_LLM_BASE_URL,
    )
    anthropic_version = _first_non_empty(
        env.get("ANTHROPIC_VERSION"),
        env.get("MODEL_ANTHROPIC_VERSION"),
        os.environ.get("ANTHROPIC_VERSION"),
        DEFAULT_ANTHROPIC_VERSION,
    )
    model = _first_non_empty(
        env.get("MODEL_NAME"),
        env.get("FIX_COMPACT_LLM_MODEL"),
        env.get("CLEAN_LLM_MODEL"),
        env.get("DEFAULT_MODEL"),
        os.environ.get("MODEL_NAME"),
        DEFAULT_LLM_MODEL,
    )
    return LLMConfig(
        api_key=api_key,
        base_url=base_url,
        anthropic_version=anthropic_version,
        model=model,
        enable_misfiled=enable_misfiled,
        enable_backup=enable_backup,
    )


def _strip_wrapping(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _parse_llm_json(text: str) -> dict[str, Any]:
    """尽最大努力把 LLM 输出解析为 dict。"""
    cleaned = _strip_wrapping(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        s, e = cleaned.find("{"), cleaned.rfind("}")
        if s == -1 or e <= s:
            raise RuntimeError(f"LLM 返回不可解析: {cleaned[:400]!r}")
        parsed = json.loads(cleaned[s:e + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError(f"LLM 返回不是 JSON 对象: {cleaned[:400]!r}")
    return parsed


def _call_anthropic(
    *,
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    timeout: int,
) -> str:
    """内部通用调用；只返回 text，不做 JSON 解析。"""
    if requests is None:
        raise RuntimeError("requests 未安装，无法调用 LLM；请 pip install requests")
    if not config.api_key:
        raise RuntimeError("LLM 未配置 api_key（.env 里应含 MODEL_API_KEY）")
    payload = {
        "model": config.model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": config.api_key,
        "anthropic-version": config.anthropic_version,
        "content-type": "application/json",
    }
    # 简单指数退避 retry；只对网络异常 / 429 / 5xx 生效，永不吞掉最终失败。
    last_err: Exception | None = None
    for attempt in range(3):  # 1 次原始 + 最多 2 次重试
        try:
            response = requests.post(
                anthropic_messages_url(config.base_url),
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:  # 连接/超时/DNS 之类
            last_err = exc
            if attempt == 2:
                raise RuntimeError(f"LLM 网络错误(重试 {attempt} 次仍失败): {exc}") from exc
            time.sleep(0.7 * (2 ** attempt))
            continue

        if response.status_code == 200:
            data = response.json()
            blocks = data.get("content", []) or []
            text = "\n".join(
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
            return text

        # 429 或 5xx 才重试；4xx（除 429）当作明确错误直接抛出
        if response.status_code == 429 or 500 <= response.status_code < 600:
            last_err = RuntimeError(
                f"LLM API {response.status_code}: {response.text[:200]}"
            )
            if attempt == 2:
                raise last_err
            time.sleep(0.7 * (2 ** attempt))
            continue

        raise RuntimeError(
            f"LLM API 错误 {response.status_code}: {response.text[:400]}"
        )

    # 逻辑上不会到达（要么 return 要么 raise），加个兜底
    raise RuntimeError(f"LLM 调用失败: {last_err}")


# ---------------- #12 misfiled-referenced-skipped 分类辅助 -----------------
# 只写报告，不改文件。四分类 + 置信度。
MISFILED_CLASSIFY_SYSTEM_PROMPT = """You are a strict data-integrity classifier
for compact-subagent files in an AI coding assistant's session corpus.

Background:
A valid "compact subagent" file MUST satisfy:
  - filename matches agent-acompact-<hex>.json
  - first message: role=system, content starts with "CRITICAL:" and tells the
    subagent to review the trajectory and write a compact summary
  - the file is referenced by exactly one tool_call in the main session, whose
    arguments carry agent_id = "acompact-<hex>" matching the filename.

You will be shown:
  1) The main session's compact tool_call (id, name, arguments preview)
  2) A preview of the first few messages of the subagent file
  3) The filename of the subagent file

The current file has been flagged as "misfiled but referenced" — its filename
says acompact- but the content is NOT a valid compact snapshot (no CRITICAL
prompt anywhere, or the structure looks like a normal subagent). The main
session still references it, so it MUST NOT be deleted.

Your only task: pick exactly ONE of four labels explaining the most likely
cause.

Labels:
  - "wrong_agent_id":            the tool_call's agent_id points at the wrong
                                 file. The referenced content is a legitimate
                                 subagent, but of a different task / agent
                                 (e.g. a normal Explore or general subagent).
  - "wrong_file_content":        the filename & tool_call agree, but the file
                                 content itself has been overwritten with an
                                 unrelated conversation (upstream data damage:
                                 wrong write / disk corruption / stale replay).
  - "compact_missing_critical":  the content DOES look like a real compact
                                 subagent (trajectory-review style content),
                                 only the CRITICAL system prompt at idx 0 is
                                 missing or replaced by something else.
  - "uncertain":                 evidence is insufficient — none of the above
                                 fits confidently.

Judgment rules:
  - Do NOT propose a rewrite. You are ONLY classifying.
  - If unsure, prefer "uncertain" over guessing.
  - "wrong_agent_id" vs "wrong_file_content": if the file preview looks like
    a coherent normal-subagent trajectory (has its own user prompt about a
    different task), lean "wrong_agent_id". If the preview shows random /
    unrelated / mid-sentence content with no clear task framing, lean
    "wrong_file_content".

Output ONLY this JSON, no prose, no markdown fence:
{"label": "wrong_agent_id" | "wrong_file_content" | "compact_missing_critical" | "uncertain",
 "confidence": "high" | "medium" | "low",
 "reason": "one short sentence citing the concrete evidence"}
"""


def call_misfiled_classify_llm(
    *,
    config: LLMConfig,
    tool_call_snippet: str,
    file_preview: str,
    filename: str,
    timeout: int = 60,
) -> dict[str, Any]:
    """给 #12 分支的文件做三类归因。返回 {label, confidence, reason}。"""
    user_prompt = (
        "Classify the following misfiled-but-referenced compact subagent file, "
        "and output the JSON exactly as SYSTEM requires.\n\n"
        f"[Filename]\n{filename}\n\n"
        f"[Main session tool_call snippet]\n{tool_call_snippet}\n\n"
        f"[Subagent file preview]\n{file_preview}\n"
    )
    text = _call_anthropic(
        config=config,
        system_prompt=MISFILED_CLASSIFY_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=256,
        timeout=timeout,
    )
    parsed = _parse_llm_json(text)
    valid_labels = {
        "wrong_agent_id",
        "wrong_file_content",
        "compact_missing_critical",
        "uncertain",
    }
    label = str(parsed.get("label") or "").strip()
    if label not in valid_labels:
        label = "uncertain"
    conf = str(parsed.get("confidence") or "low").strip().lower()
    if conf not in {"high", "medium", "low"}:
        conf = "low"
    return {
        "label": label,
        "confidence": conf,
        "reason": str(parsed.get("reason") or "").strip(),
    }


# ---------------- #1 backup-repaired 判定辅助 -----------------
# 只判"复原后的文件是否可用"，绝不让 LLM 续写消息内容。
BACKUP_ASSESS_SYSTEM_PROMPT = """You are a strict data-integrity assessor for
partially-recovered compact-subagent JSON files.

Background:
A compact subagent file was corrupted (truncated / had null bytes / broken JSON)
and has been mechanically repaired into parseable JSON. Your task is to judge
whether the repaired content is safe to use downstream.

You will be shown:
  1) The number of messages after repair
  2) A structural summary: role sequence of the first ~6 and last ~6 messages
  3) The content preview of the LAST assistant message (if any)
  4) A short note from the repairer about what had to be patched

Judgment principles:
  - Do NOT try to guess what the missing content should have been.
  - Do NOT propose a rewrite.
  - Only decide: is what remains STRUCTURALLY intact enough that a downstream
    fixer can safely operate on it?

Labels:
  - "usable":         structure looks complete, final assistant has a real
                      summary body, tool batches look matched. Downstream
                      #4/#5/#6/#7/#8/#9/#10/#11 can proceed normally.
  - "partial":        structure is present but the final assistant summary is
                      clearly truncated OR the last tool batch is unmatched.
                      Downstream MUST NOT overwrite the tail from main-session
                      view (would inject unverifiable claims). Downstream #10
                      prefix-synced and #11 summary-synced should be skipped.
  - "unrecoverable":  repair produced obviously nonsense structure
                      (mid-sentence roles, missing entire summary, contradictory
                      role sequence). File should be quarantined for manual
                      review; do NOT promote it to a usable compact.

Output ONLY this JSON, no prose, no markdown fence:
{"label": "usable" | "partial" | "unrecoverable",
 "reason": "one short sentence citing the concrete evidence"}
"""


def call_backup_assess_llm(
    *,
    config: LLMConfig,
    n_messages: int,
    role_head: list[str],
    role_tail: list[str],
    last_asst_preview: str,
    repair_note: str,
    timeout: int = 60,
) -> dict[str, Any]:
    """给 #1 分支复原后的文件做安全性判定。返回 {label, reason}。"""
    user_prompt = (
        "Assess whether the mechanically-repaired compact subagent JSON is safe "
        "to use downstream, and output the JSON exactly as SYSTEM requires.\n\n"
        f"[Message count]\n{n_messages}\n\n"
        f"[First 6 roles]\n{role_head}\n\n"
        f"[Last 6 roles]\n{role_tail}\n\n"
        f"[Last assistant content preview (up to 800 chars)]\n"
        f"{last_asst_preview[:800]}\n\n"
        f"[Repair note]\n{repair_note}\n"
    )
    text = _call_anthropic(
        config=config,
        system_prompt=BACKUP_ASSESS_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=256,
        timeout=timeout,
    )
    parsed = _parse_llm_json(text)
    valid = {"usable", "partial", "unrecoverable"}
    label = str(parsed.get("label") or "").strip()
    if label not in valid:
        # 未知标签保守降级为 partial：不删文件，但抑制下游高风险 fix
        label = "partial"
    return {
        "label": label,
        "reason": str(parsed.get("reason") or "").strip(),
    }


def compact_agent_id_from_text(text: str) -> str | None:
    match = COMPACT_ID_RE.search(text or "")
    if not match:
        return None
    return f"acompact-{match.group(1).lower()}"


def parse_session_name(path: Path) -> tuple[str, str, str] | None:
    match = SESSION_NAME_RE.match(path.name)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def normalized_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") or {}
    return {
        "id": call.get("id"),
        "type": call.get("type"),
        "function": {
            "name": function.get("name"),
            "arguments": function.get("arguments"),
        },
    }


def normalized_message(msg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "role": msg.get("role"),
        "content": msg.get("content", ""),
    }
    if "reasoning_content" in msg:
        out["reasoning_content"] = msg.get("reasoning_content") or ""
    if "tool_call_id" in msg:
        out["tool_call_id"] = msg.get("tool_call_id")
    if msg.get("tool_calls") is not None:
        out["tool_calls"] = [normalized_tool_call(c) for c in msg.get("tool_calls") or []]
    return out


def digest_messages(messages: list[dict[str, Any]]) -> str:
    # 过滤 None/非 dict 项 —— 原始数据中偶见 CORRUPTED backup 里残留 None
    normalized = [normalized_message(m) for m in messages if isinstance(m, dict)]
    data = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def normalize_messages_list(messages: Any) -> list[dict[str, Any]]:
    """从 sub_data.get('messages') 得到干净的 list[dict]：
    - 非 list 返回 []
    - list 里的 None/非 dict 全部剔除
    原始 subagent JSON 里偶见 null 消息（来自不完整写入），后续代码假定每条是 dict。
    """
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict)]


def extract_prompt_from_tool_call(call: dict[str, Any]) -> str:
    function = call.get("function") or {}
    raw_args = function.get("arguments") or ""
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return ""
    prompt = args.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def find_following_tool_result(
    messages: list[dict[str, Any]], start: int, tool_call_id: str
) -> tuple[int | None, dict[str, Any] | None]:
    for i in range(start + 1, len(messages)):
        msg = messages[i]
        if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
            return i, msg
    return None, None


def find_summary_message(messages: list[dict[str, Any]]) -> tuple[int | None, dict[str, Any] | None]:
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), str) and msg["content"]:
            return i, msg
    return None, None


def normalize_trigger_user_prompt(content: str) -> str:
    if not isinstance(content, str):
        return ""
    idx = content.find(ADDITIONAL_INSTRUCTIONS_SEP)
    if idx == -1:
        return content
    return content[:idx]


def is_compact_prompt_user(msg: dict[str, Any], prompt: str) -> bool:
    content = msg.get("content")
    if msg.get("role") != "user" or not isinstance(content, str):
        return False
    normalized_content = normalize_trigger_user_prompt(content)
    if prompt and normalized_content == prompt:
        return True
    return normalized_content.startswith(COMPACT_PROMPT_PREFIX)


def find_prompt_user(
    messages: list[dict[str, Any]],
    prompt: str,
    summary_index: int | None,
    min_index: int = 1,
) -> int | None:
    if summary_index is None:
        return None
    for i in range(summary_index - 1, min_index - 1, -1):
        if is_compact_prompt_user(messages[i], prompt):
            return i
    return None


def is_continuation_user_message(msg: dict[str, Any]) -> bool:
    content = msg.get("content")
    return (
        msg.get("role") == "user"
        and isinstance(content, str)
        and content.startswith("This session is being continued from a previous conversation")
    )


def active_context_start(messages: list[dict[str, Any]], before_index: int) -> int:
    for i in range(before_index - 1, -1, -1):
        if is_continuation_user_message(messages[i]):
            return i
    return 1 if messages and messages[0].get("role") == "system" else 0


def rewrite_agent(messages: list[dict[str, Any]], agent_id: str) -> list[dict[str, Any]]:
    rewritten = copy.deepcopy(messages)
    for msg in rewritten:
        if msg.get("role") != "system":
            msg["agent"] = agent_id
    return rewritten


def make_compact_prompt_user(prompt: str, agent_id: str, summary_msg: dict[str, Any]) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "user",
        "agent": agent_id,
        "content": prompt,
    }
    metadata = summary_msg.get("_metadata")
    if isinstance(metadata, dict) and "timestamp" in metadata:
        msg["_metadata"] = {"timestamp": metadata["timestamp"]}
    return msg


def is_sub_compact_structured(sub_messages: list[dict[str, Any]]) -> bool:
    if not isinstance(sub_messages, list) or len(sub_messages) < 3:
        return False
    for msg in sub_messages:
        content = msg.get("content")
        if isinstance(content, str) and normalize_trigger_user_prompt(content).startswith(COMPACT_PROMPT_PREFIX):
            return True
    return False


def is_sub_compact_structured_pre_mutation(sub_messages: list[dict[str, Any]]) -> bool:
    """在 repair_subagent 做任何 mutation *之前* 用来判断
    "这个 sub 原本是不是 compact 快照"。

    与直接调 is_sub_compact_structured 的差别只在 **时机**：
    - 若在 #8 system-synced 写入 sub[0] 之后调用，返回值会被 mutation 污染
    - 只要在 mutation 之前 snapshot，判断结果就反映原始状态

    判断标准（与 is_sub_compact_structured 保持一致）：
      任何一条消息的 content 以 CRITICAL prompt 开头即为 compact。
      因为除了 compact sub，没有别的 sub 类型会在任何位置放 "CRITICAL: Respond with TEXT ONLY"。

    注意：不能"忽略 idx0"—— 那样会把"msg[0]=CRITICAL 但 msg[N-1] 尚未到 trigger user"
    这类中途截断的 compact 快照误判为 misfiled，而它们本应被 #5 补上 trigger user。
    """
    return is_sub_compact_structured(sub_messages)


def is_fake_summary(main_messages: list[dict[str, Any]], tool_call_index: int, main_summary: str) -> bool:
    if not main_summary:
        return False
    target_hash = sha256_text(main_summary)
    for i in range(tool_call_index - 1, -1, -1):
        msg = main_messages[i]
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content and sha256_text(content) == target_hash:
            return True
    return False


def load_agent_mappings(task_dir: Path, mapping_root: Path | None = None) -> tuple[dict[str, str], set[str], str, str]:
    """加载 tool_call_id → agent_id 映射。

    候选路径按顺序尝试：
      1. task_dir/convert-sessions/agent_mappings.json（原地）
      2. mapping_root/<task_name>/convert-sessions/agent_mappings.json（回退，
         一般对应 original_task/ 的镜像）
    返回 (mappings, supplement_agents, status, source)：
      supplement_agents 是 mapped_by=supplementary_node 的 acompact- agent_id 集合，
      供孤儿判定放行上游转换器补齐但主 session tool_call 里没引用的合法节点。
      status ∈ {"loaded", "missing", "invalid-json"}
      source 是实际读到的文件路径（或 ""）
    """
    candidates: list[Path] = [task_dir / "convert-sessions" / "agent_mappings.json"]
    if mapping_root is not None:
        candidates.append(mapping_root / task_dir.name / "convert-sessions" / "agent_mappings.json")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = load_json(path)
        except (json.JSONDecodeError, OSError):
            return {}, set(), "invalid-json", str(path)
        out: dict[str, str] = {}
        supplement_agents: set[str] = set()
        for session in (data.get("sessions") or {}).values():
            for tool_call_id, item in (session.get("mappings") or {}).items():
                agent_id = item.get("agent_id")
                if isinstance(agent_id, str):
                    out[tool_call_id] = agent_id
                    if item.get("mapped_by") == "supplementary_node" and agent_id.startswith("acompact-"):
                        supplement_agents.add(agent_id)
        return out, supplement_agents, "loaded", str(path)

    return {}, set(), "missing", ""


def resolve_mapping_root(root: Path, override: Path | None) -> Path | None:
    """自动定位 convert-sessions 的所在目录。

    优先级：
      1. 显式 --mapping-root
      2. root 同级 original_task/
      3. root 同级 original_tasks/
      4. 找不到时返回 None（脚本继续走正则回退）
    """
    if override is not None:
        return override
    root_anchor = root.parent if root.name.startswith("TASK-") else root
    parent = root_anchor.resolve().parent
    for name in ("original_task", "original_tasks"):
        candidate = parent / name
        if candidate.is_dir():
            return candidate
    return None


def effective_path(source: Path, root: Path, output: Path | None, single_output_file: bool) -> Path:
    if output is None:
        return source
    if single_output_file:
        return output
    try:
        rel = source.resolve().relative_to(root.resolve())
    except ValueError:
        rel = Path(source.name)
    return output / rel


def resolve_output_path(root: Path, source_path: Path, output: Path | None, single_output_file: bool) -> Path:
    return effective_path(source_path, root, output, single_output_file)


def make_change(
    fix_type: str,
    before: Any = None,
    after: Any = None,
    added: list[str] | None = None,
    deleted: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    content_before: str = "",
    content_after: str = "",
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """记录一处改动。

    - before/after：结构化摘要（sha256/preview/len 等），供报告 addition 使用。
    - content_before/content_after：改动前原文/改动后新文的**完整**字符串，直接进模板
      report 的 before_content/after_content。整文件级动作（backup 重建、orphan 删除、
      misfiled 跳过）留空。
    - locator：定位被改字段在写出后 subagent 文件里的行号，形如
      {"kind": "msg_field", "index": 0, "field": "role"} / {"kind": "summary"} /
      {"kind": "trigger"} / {"kind": "prefix"} / {"kind": "tool_args", "tool_call_id": ...}。
      整文件级动作不带 locator（line 记为 null）。
    """
    change: dict[str, Any] = {
        "type": fix_type,
        "before": before,
        "after": after,
        "added": added or [],
        "deleted": deleted or [],
        "content_before": content_before,
        "content_after": content_after,
        "locator": locator,
        "line": None,
        "timestamp": "",
    }
    if extra:
        change.update(extra)
    return change


def try_repair_backup(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"read-failed:{exc}"

    data: Any
    try:
        data = json.loads(raw)
        note = "loaded-directly"
    except json.JSONDecodeError as exc:
        candidate_end = exc.pos if exc.pos else len(raw)
        data = None
        note = "json-decode-failed"
        for cut in range(candidate_end, 0, -1):
            candidate = raw[:cut]
            try:
                data = json.loads(candidate)
                note = f"trimmed-at:{cut}"
                break
            except json.JSONDecodeError:
                continue
        if data is None:
            return None, f"json-decode-failed:{exc}"

    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list):
            data["messages"] = [m for m in messages if isinstance(m, dict)]
    return data, note


def scan_and_repair_files(
    root: Path,
    task_dir: Path,
    main_path: Path,
    main_messages: list[dict[str, Any]],
    mappings: dict[str, str],
    output: Path | None,
    single_output_file: bool,
    llm_config: LLMConfig | None = None,
    supplement_agents: set[str] | None = None,
) -> list[dict[str, Any]]:
    """在正式 repair 之前，先扫描 compact subagent 目录：
      - 修补 .CORRUPTED_*_BACKUP 到镜像目录里的 agent-acompact-*.json
      - 删除主 session 无引用的 orphan agent-acompact-*.json
      - 删除结构不像 compact 的 misfiled agent-acompact-*.json
    只对 agent-acompact-*.json 生效，其它 sub 文件一律不看。
    """
    subagent_dir = main_path.parent / main_path.stem / "subagents"
    if not subagent_dir.is_dir():
        return []

    reports: list[dict[str, Any]] = []
    supplement_agents = supplement_agents or set()
    referenced_agents: set[str] = {p["agent_id"] for p in compact_points(main_messages, mappings)}

    # 收集源目录里的 backup 与 acompact 文件（源目录只读；改动在镜像里进行）
    backup_files = sorted(subagent_dir.glob("agent-acompact-*.json.CORRUPTED_*_BACKUP"))
    sub_files = sorted(subagent_dir.glob("agent-acompact-*.json"))

    # 1. backup 修补
    for backup_path in backup_files:
        stem = backup_path.name.split(".CORRUPTED_", 1)[0]  # e.g. agent-acompact-xxx.json
        agent_id = stem.removeprefix("agent-").removesuffix(".json")

        # ★ 前置校验：主 session 若无对应 tool_call 引用，即使修好了也会立刻被 orphan-deleted 逻辑清理。
        # 更重要的是，如果我们把它写进 mirror，后面的 orphan 清理循环用的是 *源目录* pre-loop 采集
        # 的 sub_files 列表 —— 这个新写入的 mirror-only 文件永远不会被复扫，会残留成孤儿。
        # 所以：不在 referenced_agents 里的直接跳过，别写。
        if agent_id not in referenced_agents and agent_id not in supplement_agents:
            continue

        target_source = subagent_dir / stem
        target_output = effective_path(target_source, root, output, single_output_file)

        needs_repair = True
        if target_output.exists():
            try:
                load_json(target_output)
                needs_repair = False
            except (json.JSONDecodeError, OSError):
                needs_repair = True

        if not needs_repair:
            continue

        data, note = try_repair_backup(backup_path)
        report: dict[str, Any] = {
            "main_session": str(main_path),
            "subagent_file": str(target_source),
            "output_file": "",
            "agent_id": agent_id,
            "tool_call_id": "",
            "changes": [],
            "skipped": [],
        }
        if data is None:
            report["skipped"].append({"reason": f"backup-repair-failed:{note}"})
            reports.append(report)
            continue

        # === LLM 后置判定：只判"复原后的文件是否可用"，不改内容 ===
        # 失败/未启用时行为等价于 usable，绝不阻塞主流程。
        backup_llm: dict[str, Any] | None = None
        if llm_config is not None and llm_config.enable_backup and llm_config.enabled:
            try:
                msgs = data.get("messages") or []
                roles = [
                    (m.get("role") if isinstance(m, dict) else None) or "?"
                    for m in msgs
                ]
                head = roles[:6]
                tail = roles[-6:] if len(roles) > 6 else roles
                last_asst_preview = ""
                for m in reversed(msgs):
                    if isinstance(m, dict) and m.get("role") == "assistant":
                        c = m.get("content")
                        if isinstance(c, str):
                            last_asst_preview = c
                        elif isinstance(c, list):
                            parts: list[str] = []
                            for blk in c:
                                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                                    parts.append(blk["text"])
                            last_asst_preview = "\n".join(parts)
                        break
                backup_llm = call_backup_assess_llm(
                    config=llm_config,
                    n_messages=len(msgs),
                    role_head=head,
                    role_tail=tail,
                    last_asst_preview=last_asst_preview,
                    repair_note=note,
                )
            except Exception as exc:
                backup_llm = {"label": "llm_error", "reason": str(exc)[:200]}

        # 三档处理
        # 保守默认：缺 label 或 llm_error 时走 partial（禁用 #10/#11/#7 的下游合成），
        # 避免网络抖动让明显崩坏的 backup 被当成 usable 落地。
        llm_label = (backup_llm or {}).get("label", "partial")
        if llm_label == "llm_error":
            llm_label = "partial"
        if llm_label == "unrecoverable":
            # 不写文件，进 skipped，防止把明显崩坏的 compact 假装可用
            report["skipped"].append({
                "reason": "backup-llm-unrecoverable",
                "backup_llm": backup_llm,
                "repair_note": note,
            })
            reports.append(report)
            continue

        write_json(target_output, data)
        report["output_file"] = str(target_output)
        change = make_change(
            fix_type="backup-repaired",
            before={"source": str(backup_path), "note": note},
            after={"path": str(target_output), "message_count": len(data.get("messages") or [])},
            added=[f"用 backup 内容重建 {target_output.name}"],
            deleted=[],
            extra={"backup_llm": backup_llm} if backup_llm is not None else None,
        )
        report["changes"].append(change)
        reports.append(report)

    # 2. orphan / misfiled 删除
    for sub_file in sub_files:
        agent_id = sub_file.stem.removeprefix("agent-")
        if not agent_id.startswith("acompact-"):
            continue
        if agent_id in referenced_agents or agent_id in supplement_agents:
            continue

        target_output = effective_path(sub_file, root, output, single_output_file)
        report: dict[str, Any] = {
            "main_session": str(main_path),
            "subagent_file": str(sub_file),
            "output_file": str(target_output),
            "agent_id": agent_id,
            "tool_call_id": "",
            "changes": [],
            "skipped": [],
        }

        fix_type = "orphan-deleted"
        try:
            read_from = target_output if target_output.exists() else sub_file
            sub_data = load_json(read_from)
            sub_messages = sub_data.get("messages") if isinstance(sub_data, dict) else None
            if isinstance(sub_messages, list) and not is_sub_compact_structured(sub_messages):
                fix_type = "misfiled-orphan-deleted"
        except (json.JSONDecodeError, OSError):
            # 无法读也删（是 orphan 且不可用）
            pass

        if target_output.exists():
            try:
                target_output.unlink()
            except OSError as exc:
                report["skipped"].append({"reason": f"delete-failed:{exc}"})
                reports.append(report)
                continue
        else:
            # 只在源目录存在的孤儿：记录动作但没有实际文件可删（镜像里没有）
            report["skipped"].append({"reason": "orphan-in-source-only"})
            reports.append(report)
            continue

        report["changes"].append(make_change(
            fix_type=fix_type,
            before={"path": str(target_output)},
            after=None,
            added=[],
            deleted=[f"删除 orphan 文件 {target_output.name}"],
        ))
        reports.append(report)

    return reports


def compact_points(main_messages: list[dict[str, Any]], mappings: dict[str, str]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for i, msg in enumerate(main_messages):
        tool_calls = msg.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            tool_call_id = call.get("id")
            if not isinstance(tool_call_id, str):
                continue
            regex_agent_id = compact_agent_id_from_text(tool_call_id)
            mapped = mappings.get(tool_call_id)
            mapping_agent_id = mapped if (isinstance(mapped, str) and mapped.startswith("acompact-")) else None
            if mapping_agent_id:
                agent_id = mapping_agent_id
                mapping_regex_divergence = bool(regex_agent_id and regex_agent_id != mapping_agent_id)
                agent_source = "mapping"
            elif regex_agent_id:
                agent_id = regex_agent_id
                mapping_regex_divergence = False
                agent_source = "regex"
            else:
                continue
            result_index, result_msg = find_following_tool_result(main_messages, i, tool_call_id)
            if result_msg is None:
                continue
            prompt = extract_prompt_from_tool_call(call)
            points.append({
                "agent_id": agent_id,
                "tool_call_id": tool_call_id,
                "tool_call_index": i,
                "tool_result_index": result_index,
                "prompt": prompt,
                "summary": result_msg.get("content") if isinstance(result_msg.get("content"), str) else "",
                "tool_call": call,
                "agent_source": agent_source,
                "mapping_regex_divergence": mapping_regex_divergence,
                "regex_agent_id": regex_agent_id,
                "mapping_agent_id": mapping_agent_id,
            })
    return points


def repair_subagent(
    root: Path,
    task_dir: Path,
    main_path: Path,
    subagent_path: Path,
    main_messages: list[dict[str, Any]],
    point: dict[str, Any],
    output: Path | None,
    single_output_file: bool,
    disabled_fixes: set[str] | None = None,
    llm_config: LLMConfig | None = None,
    skip_summary_and_prefix_sync: bool = False,
) -> dict[str, Any]:
    disabled_fixes = set(disabled_fixes or set())
    # backup-llm 判定 partial 的 sub：主视角回填风险不可控，强制关掉 #10/#11
    if skip_summary_and_prefix_sync:
        disabled_fixes = disabled_fixes | {"prefix-synced", "summary-synced", "tail-echo-flattened"}
    agent_id = point["agent_id"]
    effective_read = effective_path(subagent_path, root, output, single_output_file)
    file_report: dict[str, Any] = {
        "main_session": str(main_path),
        "subagent_file": str(subagent_path),
        "output_file": "",
        "agent_id": agent_id,
        "tool_call_id": point["tool_call_id"],
        "changes": [],
        "skipped": [],
    }
    if not effective_read.exists():
        file_report["skipped"].append({"reason": "missing-compact-subagent-file"})
        return file_report

    try:
        sub_data = load_json(effective_read)
    except json.JSONDecodeError as exc:
        file_report["skipped"].append({"reason": f"invalid-json:{exc}"})
        return file_report
    sub_messages = normalize_messages_list(sub_data.get("messages"))
    if not sub_messages:
        file_report["skipped"].append({"reason": "invalid-subagent-messages"})
        return file_report

    # ★ 判定这个 sub 是否为 compact 的权威依据：
    #   1) 文件名匹配 agent-acompact-<hex>.json
    #   2) 主 session 里存在 tool_call 指向该 tool_call_id（即 referenced_agents 命中）
    # 通常这两条就够 —— 走到这里的必定是主 session 声明的 compact，尽力修复。
    #
    # ★ 但存在极端情况：主 session tool_call 指向的文件里 **所有消息** 都不含
    #   CRITICAL prompt（连 msg[0] 都不是 system CRITICAL）。此时该文件绝无可能
    #   是真 compact 快照（真 compact 的 msg[0] 一定是 CRITICAL system prompt，
    #   这是 subagent 一启动就收到的 prompt，从未看见过例外）。可能原因：主 session
    #   tool_call 引用错、或该文件是别类 subagent 被误命名。
    #   此时若强行走 #4/#5/#8/#10/#11 会把非 compact 内容伪造成 compact —— 严禁。
    #   pre-mutation snapshot 判断（原版 is_sub_compact_structured：全消息扫描，
    #   不忽略 idx0），能同时正确处理"中途截断的真 compact"（msg[0]=CRITICAL）。
    if not is_sub_compact_structured_pre_mutation(sub_messages):
        head_preview = []
        for m in sub_messages[:6]:
            if isinstance(m, dict):
                role = m.get("role", "?")
                c = m.get("content", "")
                if isinstance(c, list):
                    snippet = json.dumps(c)[:60]
                else:
                    snippet = str(c)[:60]
                head_preview.append({"role": role, "snippet": snippet})
        file_report["skipped"].append({"reason": "misfiled-referenced-skipped"})

        # -------- LLM 分类辅助（仅当 --enable-llm 打开时）--------
        # 只写报告字段，不改文件、不影响 fix 决策链 —— #12 的核心承诺不变。
        llm_result: dict[str, Any] = {}
        if llm_config is not None and llm_config.enable_misfiled and llm_config.api_key:
            try:
                # 取主 session 里对应 tool_call 的最小 snippet（防止 prompt 过长）
                tc_id = point.get("tool_call_id", "")
                tc_agent = point.get("agent_id", "")
                # 找主 session 中对应的 tool_call
                tc_arg_preview = ""
                for m in main_messages or []:
                    if not isinstance(m, dict):
                        continue
                    for tc in m.get("tool_calls") or []:
                        if isinstance(tc, dict) and tc.get("id") == tc_id:
                            args_raw = tc.get("arguments") or tc.get("function", {}).get("arguments") or ""
                            tc_arg_preview = str(args_raw)[:400]
                            break
                    if tc_arg_preview:
                        break
                tool_call_snippet = json.dumps(
                    {
                        "id": tc_id,
                        "agent_id": tc_agent,
                        "arguments_preview": tc_arg_preview,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                # 前 6 条 role + content 前 200 字
                file_preview_lines = []
                for i, hp in enumerate(head_preview):
                    file_preview_lines.append(
                        f"[{i}] role={hp.get('role')!r} snippet={hp.get('snippet')!r}"
                    )
                file_preview = "\n".join(file_preview_lines) or "(empty)"
                verdict = call_misfiled_classify_llm(
                    config=llm_config,
                    tool_call_snippet=tool_call_snippet,
                    file_preview=file_preview,
                    filename=Path(subagent_path).name,
                )
                llm_result = verdict
            except Exception as exc:  # noqa: BLE001
                llm_result = {"label": "llm_error", "reason": f"llm error: {exc}"}

        file_report["changes"].append(make_change(
            fix_type="misfiled-referenced-skipped",
            before={
                "message_count": len(sub_messages),
                "head_roles": [m.get("role") for m in sub_messages[:6] if isinstance(m, dict)],
                "head_preview": head_preview,
            },
            after=None,
            added=[],
            deleted=["跳过所有 #4/#5/#6/#7/#8/#9/#10/#11 修复动作 —— 需人工核实"],
            extra={"misfiled_llm": llm_result} if llm_result else None,
        ))
        return file_report

    prompt = point["prompt"]
    main_summary = point["summary"]
    changed = False

    # #4 sub-role-fixed —— sub[0].role 应当是 system
    if sub_messages and sub_messages[0].get("role") != "system" and "sub-role-fixed" not in disabled_fixes:
        before_role = sub_messages[0].get("role")
        sub_messages[0]["role"] = "system"
        changed = True
        file_report["changes"].append(make_change(
            fix_type="sub-role-fixed",
            before={"role": before_role},
            after={"role": "system"},
            added=[],
            deleted=[f"原 role={before_role!r}"],
            content_before="" if before_role is None else str(before_role),
            content_after="system",
            locator={"kind": "msg_field", "index": 0, "field": "role"},
        ))

    # #8 system-synced
    if sub_messages and prompt and "system-synced" not in disabled_fixes:
        system_content = sub_messages[0].get("content")
        if system_content != prompt:
            before = system_content if isinstance(system_content, str) else ""
            sub_messages[0]["content"] = prompt
            changed = True
            file_report["changes"].append(make_change(
                fix_type="system-synced",
                before={"len": len(before), "sha256": sha256_text(before), "preview": preview(before)},
                after={"len": len(prompt), "sha256": sha256_text(prompt), "preview": preview(prompt)},
                added=[],
                deleted=[],
                content_before=before,
                content_after=prompt,
                locator={"kind": "msg_field", "index": 0, "field": "content"},
            ))

    # #6 tool-call-args-overwritten —— 只在 sub 端 arguments 无效时用主 session 覆盖
    main_call = point.get("tool_call") or {}
    main_call_args = ((main_call.get("function") or {}).get("arguments")) or ""
    if "tool-call-args-overwritten" not in disabled_fixes:
        for msg in sub_messages:
            tool_calls = msg.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                if call.get("id") != point["tool_call_id"]:
                    continue
                fn = call.get("function") or {}
                sub_args = fn.get("arguments") or ""
                if sub_args == main_call_args:
                    continue
                sub_args_valid = True
                try:
                    if isinstance(sub_args, str):
                        json.loads(sub_args)
                    else:
                        sub_args_valid = False
                except (json.JSONDecodeError, TypeError):
                    sub_args_valid = False
                if sub_args_valid:
                    continue
                call.setdefault("function", {})
                call["function"]["arguments"] = main_call_args
                changed = True
                file_report["changes"].append(make_change(
                    fix_type="tool-call-args-overwritten",
                    before={"sha256": sha256_text(str(sub_args)), "preview": preview(str(sub_args))},
                    after={"sha256": sha256_text(main_call_args), "preview": preview(main_call_args)},
                    added=[],
                    deleted=[],
                    content_before=str(sub_args),
                    content_after=main_call_args,
                    locator={"kind": "tool_args", "tool_call_id": point["tool_call_id"]},
                ))

    summary_index, summary_msg = find_summary_message(sub_messages)
    if summary_msg is None or summary_index is None:
        file_report["skipped"].append({"reason": "missing-subagent-summary-assistant"})
        if changed:
            sub_data["messages"] = sub_messages
            write_json(effective_read, sub_data)
            file_report["output_file"] = str(effective_read)
            assign_change_lines(effective_read, sub_messages, file_report["changes"])
        return file_report

    # #5 / #9 trigger user
    prompt_user_index = find_prompt_user(sub_messages, prompt, summary_index, min_index=1)
    if prompt and prompt_user_index is None and "trigger-user-synthesized" not in disabled_fixes:
        # 走到这里说明主 session 有 tool_call 引用这个 sub —— 它就是 compact。
        # 缺 trigger user 则补齐（可能是中途截断的快照）。
        prompt_user = make_compact_prompt_user(prompt, agent_id, summary_msg)
        sub_messages.insert(summary_index, prompt_user)
        prompt_user_index = summary_index
        summary_index += 1
        summary_msg = sub_messages[summary_index]
        changed = True
        file_report["changes"].append(make_change(
            fix_type="trigger-user-synthesized",
            before=None,
            after={"role": "user", "agent": agent_id, "content_len": len(prompt), "content_preview": preview(prompt)},
            added=["在最终 summary 前补入 compact trigger user 消息"],
            deleted=[],
            content_before="",
            content_after=prompt,
            locator={"kind": "trigger"},
        ))
    elif prompt_user_index is not None and prompt and "trigger-content-synced" not in disabled_fixes:
        current = sub_messages[prompt_user_index].get("content")
        current_str = current if isinstance(current, str) else ""
        current_norm = normalize_trigger_user_prompt(current_str)
        if current_norm != prompt:
            sub_messages[prompt_user_index]["content"] = prompt
            changed = True
            file_report["changes"].append(make_change(
                fix_type="trigger-content-synced",
                before={"len": len(current_str), "sha256": sha256_text(current_str), "preview": preview(current_str)},
                after={"len": len(prompt), "sha256": sha256_text(prompt), "preview": preview(prompt)},
                added=[],
                deleted=[],
                content_before=current_str,
                content_after=prompt,
                locator={"kind": "trigger"},
            ))
        # 若 current_norm == prompt 但 current_str != prompt，说明保留了 Additional Instructions 尾巴，
        # 按约定不改，只在 verify 里记为 additional-instructions-divergence。

    # #10 prefix-synced（含 #7 tail-echo-flattened 检测）
    if prompt_user_index is not None and "prefix-synced" not in disabled_fixes:
        start_index = active_context_start(main_messages, point["tool_call_index"])
        main_prefix = rewrite_agent(main_messages[start_index:point["tool_call_index"]], agent_id)
        sub_prefix = sub_messages[1:prompt_user_index]
        main_hash = digest_messages(main_prefix)
        sub_hash = digest_messages(sub_prefix)
        if main_hash != sub_hash:
            # 护栏：sub prefix 与 main prefix 相似度已经足够高时不动 —— 避免把 sub
            # 独有的 tool_result 覆盖成主视角，是 hallucination_free 退化的次因。
            similarity = prefix_similarity(main_prefix, sub_prefix)
            if similarity >= 0.85:
                file_report["skipped"].append({"reason": "prefix-similar-enough", "similarity": round(similarity, 3)})
            else:
                tail_echo = 0
                if len(sub_prefix) > len(main_prefix) and main_prefix:
                    extra = len(sub_prefix) - len(main_prefix)
                    max_k = min(len(main_prefix), extra)
                    for k in range(max_k, 0, -1):
                        if digest_messages(sub_prefix[len(main_prefix):len(main_prefix) + k]) == digest_messages(main_prefix[:k]):
                            tail_echo = k
                            break
                old_count = len(sub_prefix)
                new_count = len(main_prefix)
                sub_messages[1:prompt_user_index] = main_prefix
                summary_index = summary_index - old_count + new_count
                prompt_user_index = 1 + new_count
                summary_msg = sub_messages[summary_index]
                changed = True
                file_report["changes"].append(make_change(
                    fix_type="prefix-synced",
                    before={"message_count": old_count, "sha256": sub_hash, "similarity": round(similarity, 3)},
                    after={"message_count": new_count, "sha256": main_hash},
                    added=[f"来自主 session 的 {new_count} 条活跃上下文消息"],
                    deleted=[f"compact subagent 中 {old_count} 条旧 prefix 消息"],
                    content_before=json.dumps(sub_prefix, ensure_ascii=False, indent=2),
                    content_after=json.dumps(main_prefix, ensure_ascii=False, indent=2),
                    locator={"kind": "prefix"},
                ))
                if tail_echo > 0 and "tail-echo-flattened" not in disabled_fixes:
                    file_report["changes"].append(make_change(
                        fix_type="tail-echo-flattened",
                        before={"tail_echo_messages": tail_echo},
                        after=None,
                        added=[],
                        deleted=[f"抹平尾部对主 session 前 {tail_echo} 条的多余回显"],
                        content_before=json.dumps(
                            sub_prefix[len(main_prefix):len(main_prefix) + tail_echo],
                            ensure_ascii=False,
                            indent=2,
                        ),
                        content_after="",
                    ))

    # #11 summary-synced（含 fake-summary 跳过）
    sub_summary = summary_msg.get("content") if isinstance(summary_msg.get("content"), str) else ""
    if main_summary != sub_summary and "summary-synced" not in disabled_fixes:
        if is_fake_summary(main_messages, point["tool_call_index"], main_summary):
            file_report["skipped"].append({"reason": "fake-summary-cloned"})
        elif is_sub_summary_intact(sub_summary):
            # 护栏：sub 端已有像样的 summary → 保留 sub 视角，不用主视角覆盖。
            # 这是 A/B 测得 hallucination_free -2.23pp 的根源：主 session 的
            # tool_result 里可能带 sub 上下文没有的 claim，盖过去就变幻觉。
            # 只有 sub summary 真正缺/残缺时才让主视角回填，见 is_sub_summary_intact。
            file_report["skipped"].append({
                "reason": "sub-summary-preserved",
                "sub_summary_len": len(sub_summary),
                "main_summary_len": len(main_summary),
            })
        else:
            summary_msg["content"] = main_summary
            changed = True
            file_report["changes"].append(make_change(
                fix_type="summary-synced",
                before={"len": len(sub_summary), "sha256": sha256_text(sub_summary), "preview": preview(sub_summary)},
                after={"len": len(main_summary), "sha256": sha256_text(main_summary), "preview": preview(main_summary)},
                added=["主 session 中的 compact summary 内容"],
                deleted=["compact subagent 中旧的最终 summary 内容"],
                content_before=sub_summary,
                content_after=main_summary,
                locator={"kind": "summary"},
            ))

    if changed:
        sub_data["messages"] = sub_messages
        write_json(effective_read, sub_data)
        file_report["output_file"] = str(effective_read)
        assign_change_lines(
            effective_read,
            sub_messages,
            file_report["changes"],
            summary_index=summary_index,
            prompt_user_index=prompt_user_index,
        )
    else:
        file_report["skipped"].append({"reason": "already-aligned"})

    return file_report


def existing_compact_target_count(main_path: Path, mapping_root: Path | None = None) -> int:
    task_dir = main_path.parent.parent
    mappings, _, _, _ = load_agent_mappings(task_dir, mapping_root)
    try:
        main_data = load_json(main_path)
    except json.JSONDecodeError:
        return 0
    main_messages = main_data.get("messages") or []
    if not isinstance(main_messages, list):
        return 0
    subagent_dir = main_path.parent / main_path.stem / "subagents"
    count = 0
    for point in compact_points(main_messages, mappings):
        subagent_path = subagent_dir / f"agent-{point['agent_id']}.json"
        if subagent_path.exists():
            count += 1
    return count


def process_main_session(
    root: Path,
    main_path: Path,
    output: Path | None,
    single_output_file: bool,
    mapping_root: Path | None = None,
    task_index: int | None = None,
    total_tasks: int | None = None,
    verbose: bool = True,
    disabled_fixes: set[str] | None = None,
    llm_config: LLMConfig | None = None,
) -> list[dict[str, Any]]:
    disabled_fixes = disabled_fixes or set()
    task_dir = main_path.parent.parent
    mappings, supplement_agents, mapping_status, mapping_source = load_agent_mappings(task_dir, mapping_root)
    try:
        main_data = load_json(main_path)
    except json.JSONDecodeError as exc:
        if verbose:
            log_line(f"[{task_index}/{total_tasks}] {main_path.name}: invalid-main-json ({exc})")
        return [{
            "main_session": str(main_path),
            "subagent_file": "",
            "output_file": "",
            "changes": [],
            "skipped": [{"reason": f"invalid-main-json:{exc}"}],
            "mapping_status": mapping_status,
            "mapping_source": mapping_source,
        }]
    main_messages = main_data.get("messages") or []
    if not isinstance(main_messages, list):
        if verbose:
            log_line(f"[{task_index}/{total_tasks}] {main_path.name}: invalid-main-messages")
        return [{
            "main_session": str(main_path),
            "subagent_file": "",
            "output_file": "",
            "changes": [],
            "skipped": [{"reason": "invalid-main-messages"}],
            "mapping_status": mapping_status,
            "mapping_source": mapping_source,
        }]

    subagent_dir = main_path.parent / main_path.stem / "subagents"
    reports: list[dict[str, Any]] = []

    # 单文件输出模式（-o xxx.json）只针对一个 compact 目标，不做批量的 backup/orphan 扫描
    if not single_output_file:
        reports.extend(scan_and_repair_files(
            root, task_dir, main_path, main_messages, mappings, output, single_output_file,
            llm_config=llm_config,
            supplement_agents=supplement_agents,
        ))

    # 从上一步 reports 里提取 backup-llm 判定 partial 的 agent_id：下游 #10/#11 强制跳过
    backup_partial_agents: set[str] = set()
    for r in reports:
        for c in r.get("changes", []):
            if c.get("type") == "backup-repaired":
                llm_info = c.get("backup_llm") or {}
                if llm_info.get("label") == "partial":
                    aid = r.get("agent_id")
                    if aid:
                        backup_partial_agents.add(aid)

    points = compact_points(main_messages, mappings)
    divergence_count = sum(1 for p in points if p.get("mapping_regex_divergence"))
    for point in points:
        subagent_path = subagent_dir / f"agent-{point['agent_id']}.json"
        effective_sub = effective_path(subagent_path, root, output, single_output_file)
        if not effective_sub.exists():
            reports.append({
                "main_session": str(main_path),
                "subagent_file": str(subagent_path),
                "output_file": "",
                "agent_id": point["agent_id"],
                "tool_call_id": point["tool_call_id"],
                "changes": [],
                "skipped": [{"reason": "missing-compact-subagent-file"}],
                "mapping_status": mapping_status,
                "mapping_source": mapping_source,
                "agent_source": point.get("agent_source"),
                "mapping_regex_divergence": point.get("mapping_regex_divergence"),
            })
            continue
        rep = repair_subagent(
            root=root,
            task_dir=task_dir,
            main_path=main_path,
            subagent_path=subagent_path,
            main_messages=main_messages,
            point=point,
            output=output,
            single_output_file=single_output_file,
            disabled_fixes=disabled_fixes,
            llm_config=llm_config,
            skip_summary_and_prefix_sync=point["agent_id"] in backup_partial_agents,
        )
        rep["mapping_status"] = mapping_status
        rep["mapping_source"] = mapping_source
        rep["agent_source"] = point.get("agent_source")
        rep["mapping_regex_divergence"] = point.get("mapping_regex_divergence")
        reports.append(rep)

    if verbose:
        changed_here = sum(1 for r in reports if r.get("changes"))
        deleted_here = sum(
            1 for r in reports
            if any(c.get("type") in ("orphan-deleted", "misfiled-orphan-deleted") for c in r.get("changes", []))
        )
        repaired_here = sum(
            1 for r in reports
            if any(c.get("type") == "backup-repaired" for c in r.get("changes", []))
        )
        modified_here = changed_here - deleted_here - repaired_here
        log_line(
            f"[{task_index or '-'}/{total_tasks or '-'}] {task_dir.name}/{main_path.stem} "
            f"mapping={mapping_status}({len(mappings)}) points={len(points)} "
            f"mod={modified_here} del={deleted_here} bkup={repaired_here} "
            f"diverge={divergence_count}"
        )
    return reports


def iter_main_sessions(root: Path, input_file: Path | None) -> list[Path]:
    if input_file is not None:
        return [input_file]
    paths: list[Path] = []
    task_dirs = (
        [root]
        if root.name.startswith("TASK-") and (root / "sessions").is_dir()
        else sorted(p for p in root.iterdir() if p.is_dir() and p.name != "__MACOSX")
    )
    for task_dir in task_dirs:
        sessions_dir = task_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        for path in sorted(sessions_dir.glob("*.json")):
            if parse_session_name(path):
                paths.append(path)
    return paths


def path_key(path: Path) -> str:
    return str(path.resolve()).lower()


def add_issues(summary: dict[str, int], issues: list[str]) -> None:
    for issue in issues:
        summary[issue] = summary.get(issue, 0) + 1


def verify_main_session(
    root: Path,
    main_path: Path,
    effective_output: Path | None,
    mapping_root: Path | None = None,
) -> tuple[int, dict[str, int]]:
    task_dir = main_path.parent.parent
    mappings, supplement_agents, _, _ = load_agent_mappings(task_dir, mapping_root)
    try:
        main_data = load_json(main_path)
    except json.JSONDecodeError:
        return 1, {"invalid-main-messages": 1}
    main_messages = main_data.get("messages") or []
    summary: dict[str, int] = {}
    records = 0

    if not isinstance(main_messages, list):
        return 1, {"invalid-main-messages": 1}

    # 主 session 层面的检测
    if main_messages and is_continuation_user_message(main_messages[0]):
        records += 1
        add_issues(summary, ["session-begins-with-continuation"])
    for i, msg in enumerate(main_messages):
        if i == 0:
            continue
        if is_continuation_user_message(msg):
            prev = main_messages[i - 1]
            if prev.get("role") != "tool":
                records += 1
                add_issues(summary, ["orphan-continuation-user"])

    # 主 session 里 compact tool_call 缺 tool_result 的情况
    for i, msg in enumerate(main_messages):
        for call in (msg.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            tool_call_id = call.get("id")
            if not isinstance(tool_call_id, str):
                continue
            mapped = mappings.get(tool_call_id)
            agent_id = mapped if isinstance(mapped, str) and mapped.startswith("acompact-") else compact_agent_id_from_text(tool_call_id)
            if not agent_id:
                continue
            _, result_msg = find_following_tool_result(main_messages, i, tool_call_id)
            if result_msg is None:
                records += 1
                add_issues(summary, ["orphan-compact-tool-call"])

    subagent_dir = main_path.parent / main_path.stem / "subagents"
    seen_agents: set[str] = set()

    for point in compact_points(main_messages, mappings):
        records += 1
        agent_id = point["agent_id"]
        seen_agents.add(agent_id)
        if point.get("mapping_regex_divergence"):
            add_issues(summary, ["mapping-regex-divergence"])
        subagent_path = subagent_dir / f"agent-{agent_id}.json"
        if effective_output is not None:
            try:
                rel = subagent_path.resolve().relative_to(root.resolve())
                effective_subagent_path = effective_output / rel
            except ValueError:
                effective_subagent_path = subagent_path
        else:
            effective_subagent_path = subagent_path
        issues: list[str] = []

        if not effective_subagent_path.exists():
            add_issues(summary, ["missing-compact-subagent-file"])
            continue

        try:
            sub_data = load_json(effective_subagent_path)
        except json.JSONDecodeError:
            add_issues(summary, ["invalid-subagent-messages"])
            continue
        sub_messages = normalize_messages_list(sub_data.get("messages"))
        if not sub_messages:
            add_issues(summary, ["invalid-subagent-messages"])
            continue

        prompt = point["prompt"]
        if sub_messages and sub_messages[0].get("role") != "system":
            issues.append("sub-role-fixed")
        if sub_messages and prompt and sub_messages[0].get("content") != prompt:
            issues.append("system-synced")

        summary_index, summary_msg = find_summary_message(sub_messages)
        if summary_msg is None or summary_index is None:
            add_issues(summary, issues + ["missing-subagent-summary-assistant"])
            continue

        main_summary = point["summary"]
        sub_summary = summary_msg.get("content") if isinstance(summary_msg.get("content"), str) else ""
        if main_summary != sub_summary:
            if is_fake_summary(main_messages, point["tool_call_index"], main_summary):
                issues.append("fake-summary-cloned")
            else:
                issues.append("summary-synced")

        prompt_user_index = find_prompt_user(sub_messages, prompt, summary_index, min_index=1)
        if prompt and prompt_user_index is None:
            issues.append("trigger-user-synthesized")
        elif prompt_user_index is not None and prompt:
            current = sub_messages[prompt_user_index].get("content")
            current_str = current if isinstance(current, str) else ""
            current_norm = normalize_trigger_user_prompt(current_str)
            if current_norm != prompt:
                issues.append("trigger-content-synced")
            elif current_str != prompt:
                issues.append("additional-instructions-divergence")

        if prompt_user_index is not None:
            start_index = active_context_start(main_messages, point["tool_call_index"])
            main_prefix = rewrite_agent(main_messages[start_index:point["tool_call_index"]], agent_id)
            sub_prefix = sub_messages[1:prompt_user_index]
            if digest_messages(main_prefix) != digest_messages(sub_prefix):
                issues.append("prefix-synced")

        add_issues(summary, issues or ["ok"])

    # 孤儿 compact sub 文件
    scan_dir = subagent_dir
    if effective_output is not None:
        try:
            rel = subagent_dir.resolve().relative_to(root.resolve())
            scan_dir = effective_output / rel
        except ValueError:
            pass
    if scan_dir.exists():
        for sub_file in sorted(scan_dir.glob("agent-acompact-*.json")):
            agent_id = sub_file.stem.removeprefix("agent-")
            if agent_id not in seen_agents and agent_id not in supplement_agents:
                records += 1
                add_issues(summary, ["orphan-deleted"])

    return records, summary


def verify_compact_alignment(
    root: Path,
    main_sessions: list[Path],
    effective_output: Path | None = None,
    mapping_root: Path | None = None,
) -> dict[str, Any]:
    total_records = 0
    total_summary: dict[str, int] = {}

    for main_path in main_sessions:
        records, summary = verify_main_session(root, main_path, effective_output, mapping_root)
        total_records += records
        for issue, count in summary.items():
            total_summary[issue] = total_summary.get(issue, 0) + count

    return {
        "records": total_records,
        "summary": dict(sorted(total_summary.items())),
    }


def fix_type_number(fix_type: str) -> int:
    for number, name in FIX_TYPES.items():
        if name == fix_type:
            return number
    return 0


def _build_addition(item: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """给一条 change 拼出模板 addition 对象（承载结构化额外字段，供串联脚本消费）。"""
    fix_type = change.get("type", "")
    addition: dict[str, Any] = {
        "fix编号": fix_type_number(fix_type),
        "fix_type": fix_type,
        "timestamp": change.get("timestamp", ""),
        "agent_id": item.get("agent_id", ""),
        "tool_call_id": item.get("tool_call_id", ""),
        "agent_source": item.get("agent_source", "") or "",
        "subagent_file": item.get("subagent_file", ""),
        "output_file": item.get("output_file", ""),
        "before": change.get("before"),
        "after": change.get("after"),
        "added": change.get("added", []),
        "deleted": change.get("deleted", []),
    }
    if change.get("misfiled_llm") is not None:
        addition["misfiled_llm判定"] = change.get("misfiled_llm")
    if change.get("backup_llm") is not None:
        addition["backup_llm判定"] = change.get("backup_llm")
    return addition


def build_structure_report(
    task_id: str,
    task_path: str,
    task_files: list[dict[str, Any]],
    total_trajectory_count: int,
) -> dict[str, Any]:
    """按 structure.json 模板构建单个 TASK 的报告。

    - 只把有 change 的 file_report 计入 sessions；纯跳过（already-aligned 等）不进。
    - 按主 session stem 分组 → sessions[].sub[]，每条 change 一个 sub。
    - summary 三个计数自洽：total_issues == 所有 sub 条数；hit == 有 sub 的 session 数。
    """
    sessions_by_stem: dict[str, list[dict[str, Any]]] = {}
    session_order: list[str] = []
    for item in task_files:
        changes = item.get("changes") or []
        if not changes:
            continue
        stem = Path(item.get("main_session", "")).stem
        if stem not in sessions_by_stem:
            sessions_by_stem[stem] = []
            session_order.append(stem)
        for change in changes:
            fix_type = change.get("type", "")
            sessions_by_stem[stem].append({
                "function": FIX_TYPE_FUNCTION.get(fix_type, fix_type),
                "action_type": "fix",
                "before_content": change.get("content_before", ""),
                "after_content": change.get("content_after", ""),
                "problem": FIX_TYPE_CN.get(fix_type, fix_type),
                "line": change.get("line"),
                "addition": _build_addition(item, change),
            })

    sessions = [
        {"session_id": stem, "sub": sessions_by_stem[stem]}
        for stem in session_order
    ]
    total_issues = sum(len(s["sub"]) for s in sessions)

    return {
        "report_type": "structure_report",
        "task_id": task_id,
        "task_path": task_path,
        "create": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total_trajectory_count": total_trajectory_count,
            "hit_trajectory_count": len(sessions),
            "total_issues": total_issues,
        },
        "sessions": sessions,
    }


def write_structure_report(
    task_dir: Path,
    reports_root_override: Path | None,
    report_dict: dict[str, Any],
) -> Path:
    """写出单个 TASK 的结构报告，文件名固定为 <SCRIPT_NAME>.json。

    - 默认（reports_root_override 为 None）：落到该 TASK 内的 <task_dir>/report/<SCRIPT_NAME>.json。
    - 指定 -report 时：落到 <report 根>/<task_id>/report/<SCRIPT_NAME>.json，保持按 TASK 分隔。
    """
    if reports_root_override is not None:
        report_path = reports_root_override / task_dir.name / "report" / f"{SCRIPT_NAME}.json"
    else:
        report_path = task_dir / "report" / f"{SCRIPT_NAME}.json"
    write_json(report_path, report_dict)
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="修复主 session 与 compact subagent 的对齐关系")
    parser.add_argument("-r", required=True, help="包含 TASK 目录的根目录")
    parser.add_argument("-i", help="可选，只处理指定的单个主 session JSON 文件")
    parser.add_argument("-o", help="可选，输出文件或输出目录；不传则原地修改")
    parser.add_argument("-report", help="可选，报告根目录；不传则写入各 TASK 内的 report/ 目录（<task_dir>/report/fix_compact_alignment.json）。指定时落 <report 根>/<task_id>/report/fix_compact_alignment.json")
    parser.add_argument("-workers", type=int, help="可选，指定并发线程数；不传则自动使用 min(32, CPU 核数)")
    parser.add_argument(
        "--mapping-root",
        help="可选，convert-sessions 所在根目录；默认自动尝试 <-r 的同级 original_task/。",
    )
    parser.add_argument("--quiet", action="store_true", help="关闭逐 session 的进度打印")
    parser.add_argument(
        "--enable-llm",
        default="off",
        choices=["off", "misfiled", "backup", "both"],
        help=(
            "启用 LLM 辅助判定（默认 off）。"
            "misfiled：仅 #12 misfiled-referenced-skipped 做四分类；"
            "backup：仅 #1 backup-repaired 做可用性评估（unrecoverable 会被隔离不写）；"
            "both：两者都启用。LLM 只做判定/分类，不用于内容生成；"
            "任何 LLM 调用失败都会被降级为保守选择，不会阻塞主流程。"
        ),
    )
    args = parser.parse_args()

    disabled_fixes: set[str] = set()

    enable_misfiled = args.enable_llm in ("misfiled", "both")
    enable_backup = args.enable_llm in ("backup", "both")
    llm_config: LLMConfig | None = None
    if enable_misfiled or enable_backup:
        try:
            llm_config = resolve_llm_config(
                enable_misfiled=enable_misfiled,
                enable_backup=enable_backup,
            )
        except Exception as exc:
            parser.error(f"resolve_llm_config 失败：{exc}")
        if not llm_config.enabled:
            log_line(
                "[warn] --enable-llm 已开启但缺少 API key（MODEL_API_KEY / ANTHROPIC_API_KEY），"
                "LLM 判定会被无声跳过，主流程照旧运行。"
            )

    root = Path(args.r)
    input_file = Path(args.i) if args.i else None
    if input_file is not None and not input_file.exists():
        root_relative = root / input_file
        if root_relative.exists():
            input_file = root_relative
    output = Path(args.o) if args.o else None
    if not root.is_dir():
        parser.error(f"根目录不存在或不是目录: {root}")
    if input_file is not None and not input_file.is_file():
        parser.error(f"输入文件不存在或不是文件: {input_file}")

    mapping_root_arg = Path(args.mapping_root) if args.mapping_root else None
    mapping_root = resolve_mapping_root(root, mapping_root_arg)
    verbose = not args.quiet

    main_sessions = iter_main_sessions(root, input_file)
    single_output_file = output is not None and output.suffix.lower() == ".json"
    if single_output_file:
        if input_file is None:
            parser.error("-o 如果以 .json 结尾，必须配合 -i 且只能有一个 compact 目标；批量输出请使用目录")
        target_count = existing_compact_target_count(input_file, mapping_root)
        if target_count != 1:
            parser.error(
                "-o 如果以 .json 结尾，-i 对应的主 session 必须恰好有一个已存在的 compact 目标；"
                f"当前找到 {target_count} 个。请改用输出目录。"
            )

    if verbose:
        log_line("=" * 72)
        log_line(f"[fix_compact_alignment]")
        log_line(f"  root         = {root}")
        log_line(f"  output       = {output if output else 'in-place'}")
        log_line(f"  mapping-root = {mapping_root if mapping_root else '(none, regex-only)'}")
        log_line(f"  main sessions found = {len(main_sessions)}")
        if llm_config is not None:
            log_line(
                f"  LLM aux      = enable_misfiled={llm_config.enable_misfiled} "
                f"enable_backup={llm_config.enable_backup} model={llm_config.model}"
            )
        else:
            log_line(f"  LLM aux      = off")

    # 提前统计每个 task 的 mapping 状态，让用户一眼看到覆盖情况
    mapping_stats = {"loaded": 0, "missing": 0, "invalid-json": 0}
    task_dirs_seen: set[Path] = set()
    for main_path in main_sessions:
        task_dir = main_path.parent.parent
        if task_dir in task_dirs_seen:
            continue
        task_dirs_seen.add(task_dir)
        _, _, status, _ = load_agent_mappings(task_dir, mapping_root)
        mapping_stats[status] = mapping_stats.get(status, 0) + 1
    if verbose:
        log_line(
            f"  mapping status across {len(task_dirs_seen)} tasks: "
            f"loaded={mapping_stats['loaded']} missing={mapping_stats['missing']} "
            f"invalid={mapping_stats['invalid-json']}"
        )

    if output is not None and not single_output_file and output.resolve() != root.resolve():
        root_resolved = root.resolve()
        task_dirs = {path.parent.parent for path in main_sessions}
        if verbose:
            log_line(f"  mirroring {len(task_dirs)} task dirs into output...")
        for task_dir in sorted(task_dirs):
            try:
                rel = task_dir.resolve().relative_to(root_resolved)
            except ValueError:
                rel = Path(task_dir.name)
            dest = output / rel
            shutil.copytree(task_dir, dest, dirs_exist_ok=True)
        if verbose:
            log_line(f"  mirror done.")

    default_workers = max(1, min(32, (os.cpu_count() or 4)))
    workers = args.workers if args.workers is not None else default_workers
    if workers < 1:
        parser.error("-workers 必须是正整数")

    if verbose:
        log_line(f"  workers      = {workers}")
        log_line("-" * 72)
        log_line("[stage 1/3] before-check verifying against SOURCE...")
    before_check = verify_compact_alignment(root, main_sessions, effective_output=None, mapping_root=mapping_root)
    if verbose:
        log_line(f"  before: records={before_check['records']} summary={before_check['summary']}")
        log_line("-" * 72)
        log_line(f"[stage 2/3] repairing {len(main_sessions)} main sessions with {workers} workers...")

    files: list[dict[str, Any]] = []
    total = len(main_sessions)
    done_counter = {"n": 0}
    counter_lock = threading.Lock()

    def _work(idx: int, path: Path) -> list[dict[str, Any]]:
        result = process_main_session(
            root, path, output, single_output_file,
            mapping_root=mapping_root,
            task_index=idx,
            total_tasks=total,
            verbose=verbose,
            disabled_fixes=disabled_fixes,
            llm_config=llm_config,
        )
        with counter_lock:
            done_counter["n"] += 1
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_work, idx + 1, path)
            for idx, path in enumerate(main_sessions)
        ]
        for future in as_completed(futures):
            files.extend(future.result())

    modified = [item for item in files if item.get("changes")]
    unchanged = [
        item for item in files
        if item.get("skipped") and not item.get("changes")
        and any(skip.get("reason") == "already-aligned" for skip in item.get("skipped", []))
    ]
    skipped = [
        item for item in files
        if item.get("skipped") and not item.get("changes")
        and not any(skip.get("reason") == "already-aligned" for skip in item.get("skipped", []))
    ]

    if verbose:
        log_line("-" * 72)
        log_line("[stage 3/3] after-check verifying against OUTPUT...")
    effective_after = output if (output is not None and not single_output_file) else None
    after_check = verify_compact_alignment(
        root, main_sessions,
        effective_output=effective_after,
        mapping_root=mapping_root,
    )
    if verbose:
        log_line(f"  after:  records={after_check['records']} summary={after_check['summary']}")
        log_line("=" * 72)

    report_files = sorted(
        modified + skipped,
        key=lambda item: (item.get("main_session", ""), item.get("subagent_file", "")),
    )

    delete_types = {"orphan-deleted", "misfiled-orphan-deleted"}
    non_mutating_types = {"misfiled-referenced-skipped"}  # 只是标注，不写文件
    files_deleted = sum(
        1 for item in modified
        if any(c.get("type") in delete_types for c in item.get("changes", []))
    )
    files_backup_repaired = sum(
        1 for item in modified
        if any(c.get("type") == "backup-repaired" for c in item.get("changes", []))
    )
    files_modified = sum(
        1 for item in modified
        if any(
            c.get("type") not in delete_types
            and c.get("type") != "backup-repaired"
            and c.get("type") not in non_mutating_types
            for c in item.get("changes", [])
        )
    )
    files_misfiled_referenced_skipped = sum(
        1 for item in modified
        if any(c.get("type") == "misfiled-referenced-skipped" for c in item.get("changes", []))
    )

    # LLM 辅助判定分布：只统计有 label 的记录
    misfiled_llm_dist: dict[str, int] = {}
    backup_llm_dist: dict[str, int] = {}
    for item in files:
        for c in item.get("changes", []):
            if c.get("type") == "misfiled-referenced-skipped":
                llm = c.get("misfiled_llm") or {}
                lbl = llm.get("label")
                if lbl:
                    misfiled_llm_dist[lbl] = misfiled_llm_dist.get(lbl, 0) + 1
            elif c.get("type") == "backup-repaired":
                llm = c.get("backup_llm") or {}
                lbl = llm.get("label")
                if lbl:
                    backup_llm_dist[lbl] = backup_llm_dist.get(lbl, 0) + 1
    # 被 backup-llm 判为 unrecoverable 而未写入的文件：来自 skipped 里的 backup-llm-unrecoverable
    backup_llm_unrecoverable_quarantined = sum(
        1 for item in files
        for skip in item.get("skipped", [])
        if skip.get("reason") == "backup-llm-unrecoverable"
    )
    if backup_llm_unrecoverable_quarantined:
        backup_llm_dist["unrecoverable_quarantined"] = backup_llm_unrecoverable_quarantined

    # mapping 覆盖统计（相对本轮 compact points）
    total_points_seen = sum(
        1 for item in files
        if item.get("agent_source") in ("mapping", "regex")
    )
    points_by_mapping = sum(1 for item in files if item.get("agent_source") == "mapping")
    points_by_regex = sum(1 for item in files if item.get("agent_source") == "regex")
    points_divergence = sum(1 for item in files if item.get("mapping_regex_divergence"))

    report = {
        "script": SCRIPT_NAME,
        "root": str(root),
        "input_file": str(input_file) if input_file else "",
        "output": str(output) if output else "in-place",
        "mapping_root": str(mapping_root) if mapping_root else "",
        "summary": {
            "scanned_main_sessions": len(main_sessions),
            "changed_records": len(modified),
            "files_modified": files_modified,
            "files_deleted": files_deleted,
            "files_backup_repaired": files_backup_repaired,
            "files_misfiled_referenced_skipped": files_misfiled_referenced_skipped,
            "unchanged_records": len(unchanged),
            "skipped_records": len(skipped),
            "workers": workers,
            "mappings_loaded_tasks": mapping_stats.get("loaded", 0),
            "mappings_missing_tasks": mapping_stats.get("missing", 0),
            "mappings_invalid_tasks": mapping_stats.get("invalid-json", 0),
            "compact_points_total": total_points_seen,
            "compact_points_by_mapping": points_by_mapping,
            "compact_points_by_regex": points_by_regex,
            "compact_points_mapping_regex_divergence": points_divergence,
            "llm_aux": {
                "misfiled_llm_dist": misfiled_llm_dist,
                "backup_llm_dist": backup_llm_dist,
                "enabled": (
                    None if llm_config is None
                    else {
                        "misfiled": llm_config.enable_misfiled,
                        "backup": llm_config.enable_backup,
                        "model": llm_config.model,
                    }
                ),
            },
        },
        "checks": {
            "before": before_check,
            "after": after_check,
        },
        "files": report_files,
    }

    # 按 TASK 维度产出 structure 模板报告（每 task 一份）。
    reports_root_override = Path(args.report) if args.report else None

    # 每个 task 的主 session 总数（含无改动）
    traj_count_by_task: dict[Path, int] = {}
    for main_path in main_sessions:
        task_dir = main_path.parent.parent
        traj_count_by_task[task_dir] = traj_count_by_task.get(task_dir, 0) + 1

    # 按 task 分组 file_reports
    files_by_task: dict[Path, list[dict[str, Any]]] = {}
    for item in files:
        main_session = item.get("main_session", "")
        if not main_session:
            continue
        task_dir = Path(main_session).parent.parent
        files_by_task.setdefault(task_dir, []).append(item)

    written_reports: list[Path] = []
    for task_dir in sorted(files_by_task, key=lambda p: p.name):
        task_report = build_structure_report(
            task_id=task_dir.name,
            task_path=str(task_dir),
            task_files=files_by_task[task_dir],
            total_trajectory_count=traj_count_by_task.get(task_dir, 0),
        )
        written_reports.append(write_structure_report(task_dir, reports_root_override, task_report))

    print(json.dumps({
        "summary": report["summary"],
        "checks": report["checks"],
    }, ensure_ascii=False, indent=2))
    for report_path in written_reports:
        print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

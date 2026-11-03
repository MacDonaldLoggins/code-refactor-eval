#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_last_assistant.py

读取 TASK-*/sessions/ 下所有 JSON 文件（含 subagents/ 子目录中的子 agent 轨迹），
只处理每个 session 中最后一条符合结构条件的 assistant 消息。脚本调用 LLM 进行语义判断：
只保留“已完成总结/最终结论/已验证结果”，删除所有非总结性语句；如果当前回复没有
总结性语句，则根据前面几条消息生成简短总结。

默认行为：
  - 不创建 .bak 文件
  - 写出机器可读 report
  - 不使用正则/关键词规则决定是否清理

用法:
  python action_semantics_fix_script/clean_last_assistant.py
  python action_semantics_fix_script/clean_last_assistant.py --base-dir source-issues-tasks
  python action_semantics_fix_script/clean_last_assistant.py --base-dir TASK-xxx -i TASK-xxx/sessions/1.json
  python action_semantics_fix_script/clean_last_assistant.py --filter CC7ED9
  python action_semantics_fix_script/clean_last_assistant.py --dry-run
  python action_semantics_fix_script/clean_last_assistant.py --max-files 2
  python action_semantics_fix_script/clean_last_assistant.py --report report/clean_last_assistant_report.json

项目根目录 .env 配置:
  MODEL_API_KEY / ANTHROPIC_API_KEY
  MODEL_BASE_URL / ANTHROPIC_BASE_URL
  ANTHROPIC_VERSION / MODEL_ANTHROPIC_VERSION
  MODEL_NAME / CLEAN_LLM_MODEL
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests


def configure_console_encoding() -> None:
    """避免 Windows 非 UTF-8 控制台因进度符号或中文日志中断脚本。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


configure_console_encoding()


def find_project_root(start: Path | None = None) -> Path:
    """从脚本位置向上查找仓库根目录，保证脚本移动后仍能读取根目录 .env。"""
    here = (start or Path(__file__).resolve()).parent
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    for candidate in (here, *here.parents):
        if (candidate / ".env").is_file():
            return candidate
    return here


PROJECT_ROOT = find_project_root()
DOTENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_INPUT_DIR = PROJECT_ROOT / "batch_cleaning_tasks"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "report" / "clean_last_assistant_report.json"
DEFAULT_WORKERS = 4

LLM_TIMEOUT_SECONDS = 240
LLM_MAX_ATTEMPTS = 3
TRANSIENT_HTTP_STATUS = {408, 429, 500, 502, 503, 504}

# 终态 summary 必须为英文。结果中中文占比超过该阈值时拒绝写入，避免中文末条遗留。
ENGLISH_OUTPUT_CHINESE_RATIO_MAX = 0.05


def chinese_ratio(text: str) -> float:
    """CJK 统一表意文字占非空白字符的比例，用于粗判文本是否为中文。"""
    if not text:
        return 0.0
    non_space = [c for c in text if not c.isspace()]
    if not non_space:
        return 0.0
    zh = sum(1 for c in non_space if "一" <= c <= "鿿")
    return zh / len(non_space)


def contains_excessive_chinese(text: str) -> bool:
    """判断终态总结是否仍含超阈值中文，终态输出统一要求英文。"""
    return bool(text.strip()) and chinese_ratio(text) > ENGLISH_OUTPUT_CHINESE_RATIO_MAX
SYSTEM_PROMPT = """You are a strict terminal-response cleaner. The LAST assistant message in every trajectory MUST be a purely summary/conclusive statement — no plans, no transitions, no analysis, no politeness, no file dumps. Your job is to clean it into a TRUTHFUL terminal summary by stripping all non-summary language, or generating one from context if none exists.

CRITICAL — outcome policy (mentor rule):
- ONLY `success` is a legal terminal outcome for this pipeline. The trajectory must show that the requested task was actually completed.
- If the outcome is anything other than success — `partial`, `failed`, `blocked`, or `indeterminate` — you MUST return `action=reject`. Do NOT clean it. Do NOT rewrite it. Do NOT fabricate a success. Simply reject and let the trajectory be filtered out downstream.
- Under NO circumstances may you convert a partial/failed/blocked/indeterminate trajectory into a successful-looking summary. If evidence supports success, keep and clean; otherwise reject.

Outcome determination rules (CRITICAL — read in order):
1. FIRST, check whether the reply contains a COMPLETION statement (e.g. "Updated X", "Fixed Y", "Implemented Z", "Added W"). A completion statement describes work that was actually finished and is a concrete deliverable.
2. If a completion statement exists AND is followed by disclaimer/defensive language (e.g. "I did not run tests...", "Docker/build failed...", "so the report leaves those as remaining acceptance conditions"), the outcome is `success` — the task WAS completed. Set action=clean and strip ONLY the disclaimer part; do NOT reject the entire reply.
3. Specifically, the following patterns MUST be treated as `success` with a `clean` action:
   - "Updated/Modified/Added/Deleted/Implemented/Fixed [file/feature] ... [then disclaimer about what was NOT verified]"
   - Any sentence where a concrete deliverable is stated as completed, followed by a separate sentence admitting unverified items or remaining conditions.
4. Only reject (outcome=partial/failed/blocked/indeterminate) when there is NO completion statement at all and the reply describes only incomplete work, failures, or blockers.
5. When a disclaimer follows a completion statement, the disclaimer is NOT the terminal outcome — it is an impurity that must be stripped. The completion statement IS the terminal outcome.

Cleanup rules:
1. Keep ONLY truthful terminal summary content. The summary must reflect the REAL outcome — not an idealized or "cleaned up" version.
2. The following MUST be deleted (non-exhaustive):
   - Plans, intentions, next steps, what will be done (but NOT the task outcome itself).
   - Politeness tails: "Ready for next round", "Let me know if you need...", "Feel free to...", "Thank you!", etc.
   - Transitional/execution opening language: "Perfect! Let me compile...", "I now have the complete picture... let me build...", "Now I understand. Let me...", etc.
   - Irrelevant suggestions, tangential recommendations, future redesign ideas.
3. SUMMARY-ONLY TRUNCATION RULE (CRITICAL): When the reply contains a completion/task-finished sentence followed by additional details, descriptions, or breakdowns, ONLY the completion sentence is the terminal summary — everything after it must be DELETED. Examples:
   - "The design review is complete. The report has been written to ... with the review grouped under checklist categories 1-7. The written report records 20 issues total..." => keep ONLY "The design review is complete. The report has been written to ..."
   - "The fix is done. Here are the specific changes made: 1) updated X, 2) modified Y, 3) added Z..." => keep ONLY "The fix is done."
   - "I've finished the analysis. The key findings are: A shows ..., B indicates ..., C reveals..." => keep ONLY "I've finished the analysis."
   The completion/finished sentence IS the terminal summary. Any details, enumerations, or elaborations after it are impurities that must be stripped.
3. The following MUST BE RETAINED when they determine the task outcome:
   - Failure reasons and root causes (e.g. "build failed because the Docker environment could not reach packagist.org").
   - Blockers and missing prerequisites (e.g. "cannot continue without valid API credentials").
   - The scope of partial completion (e.g. "X and Y were implemented, but Z remains incomplete").
   - Failed verifications that affect the outcome (e.g. "620 tests pass, but the E2E tests could not be run").
4. Gap analysis — distinguish two cases:
   - DELETE: intermediate exploration gaps that do not define the final outcome (e.g. mid-conversation "However, nothing in the current code does X" when the task was later completed).
   - RETAIN: gaps that define a partial or failed outcome (e.g. "The worker was implemented but drag-preview integration remains incomplete — the task is partially complete").
   Decision rule: if removing the gap statement would change whether the user perceives the task as completed, it must be kept.
5. If the reply contains BOTH terminal summary and non-summary content, keep the truthful terminal content, remove only plans, transitions, irrelevant dumps, repeated details, and pleasantries. Only do this when outcome=success; otherwise reject.
6. If the reply has NO terminal summary content at all, and preceding context supports outcome=success, generate a truthful success summary. Do NOT fabricate a success if evidence is missing or mixed — reject instead.
7. If context is insufficient to determine that the outcome is success (i.e. outcome would be partial / failed / blocked / indeterminate), return action=reject. Do NOT generate "Completed the changes described above" or any equivalent fabrication.
8. Task-type awareness:
   - Implementation tasks: pure architecture/status descriptions are NOT terminal summaries. Describe what was actually changed/delivered.
   - Analysis/research/review tasks: findings and conclusions ARE valid deliverables if they directly answer the assigned task and are presented as a completed result.
   - Advisory/QA tasks: the answer, command, or explanation itself may be the deliverable. Do not delete commands that ARE the requested answer.
9. Write the terminal summary in ENGLISH. Translate retained content when needed. English-only terminal normalization is required by this processing stage.

Examples:
- "All tests pass. Let me know if you need anything else." => keep only "All tests pass."
- "Done. To verify, run: ... Ready for pass 29." => keep only "Done."
- "Let me systematically fix all remaining issues." => no summary — generate truthful summary from context.
- "The Docker build fails because this environment cannot reach packagist.org." => KEEP this as the terminal outcome: "Task failed because the Docker environment could not reach packagist.org, so the build could not proceed." Do NOT delete the failure reason and fabricate a success.
- "Implemented the worker with thumbnailBitmap support. However, drag-preview integration remains incomplete." => IF it was implemented this task: KEEP as partial — "Implemented the worker with thumbnailBitmap support, but drag-preview integration remains incomplete. The task is partially complete." IF it only describes pre-existing code: treat as non-summary, do not claim completion.
- User says "good job", assistant replies "Thank you!" => no summary — the reply is a social exchange with zero terminal content; generate truthful summary from preceding context.
- "How do I run the migration?" — assistant replies with the migration command => the command IS the deliverable for this advisory task; do not delete it.

You MUST return a strict JSON object. No markdown code blocks, no extra text.
JSON string escaping rules (violations will cause parse failure):
- cleaned_text is a JSON string value. ALL double quotes inside it MUST be escaped as \"
- ALL backslashes inside MUST be escaped as \\
- ALL literal newlines inside MUST be written as \n
- No unescaped tabs or invisible control characters

JSON output format:
{{
  "action": "<unchanged | clean | rewrite | reject>",
  "outcome": "<success | partial | failed | blocked | indeterminate>",
  "cleaned_text": "<fill when action=clean/rewrite; empty string otherwise>",
  "reason": "<one-sentence explanation in the trajectory language, no quotes or code>"
}}

Action meanings:
- unchanged: the reply is already a pure terminal summary with truthful outcome.
- clean: the reply contains a valid terminal summary mixed with non-summary content — remove only impurities.
- rewrite: the reply has no terminal summary, but context supports generating a truthful one.
- reject: context is insufficient to determine the real outcome. Do NOT fabricate.

Correct JSON example (note escaped double quotes):
{{
  "action": "clean",
  "outcome": "success",
  "cleaned_text": "Fixed the entrypoint wiring. Added \\"managed_department\\" field to ent schema. All 620 tests pass.",
  "reason": "Removed trailing verification command and next-round prompt."
}}

Wrong JSON example (unescaped double quotes — WILL BREAK):
{{
  "action": "clean",
  "cleaned_text": "Fixed the "managed_department" field.",
  "reason": "completed fix"
}}
The unescaped double quotes around managed_department will break the JSON structure."""


@dataclass
class SemanticDecision:
    """LLM 对单条 assistant 消息的语义处理结果。"""

    action: str
    cleaned_text: str
    reason: str = ""
    outcome: str = ""  # success / partial / failed / blocked / indeterminate

    @property
    def changed(self) -> bool:
        return self.action in ("clean", "rewrite")


def load_system_prompt() -> str:
    return SYSTEM_PROMPT


def load_project_env(path: Path | None = None) -> dict[str, str]:
    """读取项目根目录 .env。"""
    if path is None:
        path = DOTENV_PATH
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _anthropic_config() -> tuple[str, str, str, str]:
    file_env = load_project_env()
    api_key = file_env.get("MODEL_API_KEY") or file_env.get("ANTHROPIC_API_KEY", "")
    base_url = file_env.get("MODEL_BASE_URL") or file_env.get("ANTHROPIC_BASE_URL", "")
    anthropic_version = file_env.get("ANTHROPIC_VERSION") or file_env.get(
        "MODEL_ANTHROPIC_VERSION", ""
    )
    model = file_env.get("MODEL_NAME") or file_env.get("CLEAN_LLM_MODEL", "")
    return api_key, base_url, anthropic_version, model


def anthropic_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def extract_first_json_object(text: str) -> str | None:
    """从模型文本输出中提取第一个完整 JSON 对象，兼容 ```json 代码块和少量前后缀说明。"""
    for start in (idx for idx, char in enumerate(text) if char == "{"):
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, len(text)):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:pos + 1]
    return None


def extract_partial_action(text: str) -> str | None:
    """只在输出开头已经明确给出 action 时，用于安全处理被截断的 unchanged 响应。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:]).lstrip() if len(lines) > 1 else ""
    match = re.match(r'^\{\s*"action"\s*:\s*"(clean|unchanged|rewrite|reject)"', stripped)
    return match.group(1) if match else None


def extract_json_string_field(text: str, field_name: str) -> str | None:
    """从近似 JSON 文本中安全提取字符串字段，用于容错 reason 字段未转义引号的情况。"""
    match = re.search(rf'"{re.escape(field_name)}"\s*:\s*', text)
    if not match:
        return None
    value_text = text[match.end():].lstrip()
    if not value_text.startswith('"'):
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(value_text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def parse_llm_decision_text(raw_text: str, original_text: str) -> SemanticDecision:
    """解析模型返回的判定文本；判定语义仍完全来自模型，只容错输出包装格式。"""
    text = raw_text.strip()
    candidates = [text]
    extracted = extract_first_json_object(text)
    if extracted and extracted != text:
        candidates.append(extracted)

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return parse_semantic_decision(json.loads(candidate), original_text)
        except json.JSONDecodeError as exc:
            last_error = exc
        except ValueError:
            raise

    action = extract_json_string_field(text, "action") or extract_partial_action(text)
    outcome = extract_json_string_field(text, "outcome") or ""
    if action == "unchanged":
        return SemanticDecision(
            action="unchanged",
            cleaned_text=original_text,
            reason="模型返回了明确的 unchanged 判定，但 JSON 被截断；已保留原文。",
            outcome=outcome,
        )
    if action == "clean":
        # cleaned_text 是核心输出，正则提取不可靠（可能抠到分析块而非总结），
        # 因此不尝试容错解析，直接报错让上层感知。
        snippet = text[:500]
        raise ValueError(f"LLM 返回 clean 判定但 JSON 不完整，为确保输出质量拒绝容错解析: {snippet}")
    snippet = text[:500]
    raise ValueError(f"LLM 返回不是可解析 JSON: {snippet}") from last_error


def post_json_with_retries(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int = LLM_TIMEOUT_SECONDS,
    max_attempts: int = LLM_MAX_ATTEMPTS,
) -> requests.Response:
    last_error: Exception | None = None
    last_response: requests.Response | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code not in TRANSIENT_HTTP_STATUS:
                raise RuntimeError(f"LLM API 错误 {resp.status_code}: {resp.text[:500]}")
            last_response = resp

        if attempt < max_attempts:
            delay = min(2 ** (attempt - 1), 8)
            print(f"  ! LLM 请求失败，{delay}s 后重试 ({attempt}/{max_attempts})", file=sys.stderr)
            time.sleep(delay)

    if last_response is not None:
        raise RuntimeError(f"LLM API 错误 {last_response.status_code}: {last_response.text[:500]}")
    raise RuntimeError(f"LLM API 请求失败: {last_error}") from last_error


def call_llm(
    message_text: str,
    context_text: str = "",
    is_main: bool = True,
    check_info: dict[str, str] | None = None,
    language_reference: str | None = None,
) -> SemanticDecision:
    """
    向 Anthropic API 发送消息，返回结构化语义判断结果。

    is_main：True=主轨迹(STRICT，分析/清单/现状不算收尾)；False=子 agent(RELAX，研究型
    findings 可作收尾)。与 fix_missing_summary_nodes 的主/子判据保持一致，避免两步打架。

    check_info：来自 fix_missing_summary_nodes 的分类结果，含 content_type 和 outcome，
    作为 LLM 预判参考注入 user_prompt，帮助 LLM 更快做出正确决策。

    language_reference：保留参数兼容既有调用；终态输出统一要求英文，不再按原文语言放行。

    如果 LLM 返回的 JSON 无法解析，会以当前回复作为上下文追加一条纠正消息
    重试（最多 2 次），要求模型修复 JSON 格式。不做正则截断或关键词判定。

    另有英文终态防护：cleaned_text 含超阈值中文时，会追加英文纠正消息要求英文重写；
    重试后仍不合规则报错让上层记录 error，避免写入不合规末条。
    """
    api_key, base_url, anthropic_version, model = _anthropic_config()
    missing = [
        name
        for name, value in (
            ("MODEL_API_KEY", api_key),
            ("MODEL_BASE_URL", base_url),
            ("ANTHROPIC_VERSION", anthropic_version),
            ("MODEL_NAME", model),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "项目根目录 .env 缺少模型配置: " + ", ".join(missing)
        )

    context_section = context_text.strip() or "(no preceding context available)"
    trajectory_note = (
        "This is a MAIN trajectory. Apply STRICT rules: pure analysis / findings / "
        "status descriptions are NOT a terminal summary."
        if is_main else
        "This is a subagent trajectory. RELAX: research / analysis findings are "
        "acceptable terminal content."
    )

    # 来自 fix_missing_summary_nodes 的预分类信息
    preclass_section = ""
    if check_info and check_info.get("content_type"):
        ct = check_info["content_type"]
        oc = check_info.get("outcome", "unknown")
        preclass_map = {
            "pure_summary": "already a pure terminal summary — only remove politeness tails if present",
            "mixed": "contains valid terminal content mixed with non-summary impurities — strip the impurities",
            "non_summary": "no terminal summary present — generate one from context",
            "unrecoverable": "context insufficient to determine real outcome — consider reject",
        }
        hint = preclass_map.get(ct, "")
        preclass_section = (
            f"\n[Pre-classification from structure checker]\n"
            f"content_type={ct}  outcome={oc}\n"
            f"Suggested approach: {hint}\n"
        )

    user_prompt = (
        "Clean the LAST assistant reply below. The result MUST be a purely summary/conclusive "
        "statement — strip ALL non-summary language (plans, transitions, gap analysis that "
        "doesn't define the outcome, commands, politeness, file dumps, social exchanges).\n\n"
        "CRITICAL: the summary must reflect the REAL outcome and be written in ENGLISH. "
        "Translate retained content when necessary. Do NOT fabricate a success "
        "when the evidence shows partial/failed/blocked/indeterminate.\n\n"
        f"{trajectory_note}\n"
        f"{preclass_section}"
        "[Preceding context]\n"
        f"{context_section}\n\n"
        "[Assistant reply to clean]\n"
        "---\n"
        f"{message_text}\n"
        "---\n\n"
        "Return a strict JSON object per the system instructions. Include action, outcome, "
        "cleaned_text, and reason fields.\n"
        "outcome must be one of: success | partial | failed | blocked | indeterminate"
    )

    headers = {
        "x-api-key": api_key,
        "anthropic-version": anthropic_version,
        "content-type": "application/json",
    }
    url = anthropic_messages_url(base_url)

    # 用 messages 列表累积对话，方便重试时追加纠正消息
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    max_tokens = max(4096, min(8192, int(len(message_text) * 0.5) + 2048))

    # 语言漂移基准：优先用调用方指定的"真实原文"，缺省 fallback 到 message_text
    lang_ref = language_reference if language_reference is not None else message_text

    last_raw: str = ""
    json_retries = 0
    drift_retries = 0
    MAX_JSON_RETRIES = 2
    MAX_DRIFT_RETRIES = 1
    # 总步数 = 首发 + 最多 MAX_JSON_RETRIES 次 JSON 重试 + 最多 MAX_DRIFT_RETRIES 次漂移重试
    max_steps = 1 + MAX_JSON_RETRIES + MAX_DRIFT_RETRIES
    for step in range(max_steps):
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": load_system_prompt(),
            "messages": messages,
        }
        resp = post_json_with_retries(url, headers=headers, payload=payload)
        result = resp.json()
        content_blocks = result.get("content", [])
        texts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        raw_text = "\n".join(texts).strip()
        last_raw = raw_text

        try:
            decision = parse_llm_decision_text(raw_text, message_text)
        except ValueError as exc:
            if json_retries >= MAX_JSON_RETRIES:
                raise RuntimeError(
                    f"LLM JSON 解析失败，已重试 {json_retries} 次仍无法修复。"
                    f"最后返回: {raw_text[:300]}"
                ) from exc
            json_retries += 1
            # 把失败的回复和错误信息发给模型，要求修复。
            # 纠错消息必须使用英文——中文纠错消息会让 LLM 认为对话已切换到中文，
            # 从而把 cleaned_text 也用中文输出（即使原始轨迹是纯英文）。
            correction = (
                f"Your previous response could not be parsed as JSON. Error: {exc}\n\n"
                f"Your previous response was:\n---\n{raw_text[:2000]}\n---\n\n"
                "Please fix the JSON formatting and try again. You MUST ensure:\n"
                "- All double quotes are properly escaped as \\\"\n"
                "- cleaned_text contains NO unescaped literal newlines (use \\n)\n"
                "- Output ONLY the raw JSON object — no markdown code fences, no extra prose\n"
                "- cleaned_text must be written in ENGLISH. Translate retained content when needed."
            )
            messages.append({"role": "assistant", "content": raw_text})
            messages.append({"role": "user", "content": correction})
            continue

        # ── 英文终态防护（仅对 clean/rewrite 有输出文本时检查）──
        if decision.action in ("clean", "rewrite") and contains_excessive_chinese(decision.cleaned_text):
            if drift_retries >= MAX_DRIFT_RETRIES:
                res_ratio = chinese_ratio(decision.cleaned_text)
                raise RuntimeError(
                    f"LLM 输出未满足英文终态要求：cleaned_text 中文占比 {res_ratio:.1%}，"
                    f"重试后仍未纠正。cleaned_text 前 200 字: {decision.cleaned_text[:200]}"
                )
            drift_retries += 1
            drift_correction = (
                "ENGLISH OUTPUT REQUIRED. Your cleaned_text contains Chinese characters. "
                "Rewrite your previous JSON response with cleaned_text entirely in ENGLISH. "
                "Keep the same action, outcome, and truthful semantic meaning. Output ONLY "
                "the corrected JSON object, no markdown, no prose."
            )
            messages.append({"role": "assistant", "content": raw_text})
            messages.append({"role": "user", "content": drift_correction})
            continue

        return decision

    raise RuntimeError("LLM 调用失败: 超出重试预算未获得有效结果")


def parse_semantic_decision(raw: Any, original_text: str) -> SemanticDecision:
    if not isinstance(raw, dict):
        raise ValueError("LLM 返回 JSON 必须是对象")

    action = raw.get("action")
    if action not in {"clean", "unchanged", "rewrite", "reject"}:
        raise ValueError("LLM 返回 action 必须是 clean/unchanged/rewrite/reject")

    reason = raw.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)

    outcome = raw.get("outcome", "")
    if outcome not in {"success", "partial", "failed", "blocked", "indeterminate", ""}:
        outcome = ""  # 容错：非法值置空

    # mentor 政策: 只有 outcome=success 才是合法终态。
    # 无论 LLM 给出何种 action, 只要 outcome != success 就强制 reject —
    # 防止 LLM 在 partial/failed/blocked/indeterminate 时仍然尝试 clean/rewrite 而伪造成功。
    if action in ("clean", "rewrite", "unchanged") and outcome not in ("success", ""):
        return SemanticDecision(
            action="reject",
            cleaned_text=original_text,
            reason=(
                f"outcome={outcome} 非 success, 按 mentor 政策强制 reject "
                f"(原 LLM action={action}, 原 reason={reason})"
            ),
            outcome=outcome,
        )

    if action in ("unchanged", "reject"):
        return SemanticDecision(
            action=action, cleaned_text=original_text, reason=reason, outcome=outcome,
        )

    if action in ("clean", "rewrite"):
        cleaned_text = raw.get("cleaned_text")
        if not isinstance(cleaned_text, str):
            raise ValueError(f"LLM 返回 {action} 时 cleaned_text 必须是字符串")
        if not cleaned_text.strip():
            raise ValueError(f"LLM 返回 {action} 时 cleaned_text 不能为空")
        if cleaned_text.strip() == original_text.strip():
            return SemanticDecision(
                action="unchanged", cleaned_text=original_text, reason=reason, outcome=outcome,
            )
        if len(cleaned_text) > max(len(original_text) * 2, 1000):
            raise ValueError(f"LLM 返回 {action} 时 cleaned_text 过长，不像简短总结")
        return SemanticDecision(
            action=action, cleaned_text=cleaned_text, reason=reason, outcome=outcome,
        )


# ---------------------------------------------------------------------------
# JSON 读取/写入
# ---------------------------------------------------------------------------

def content_to_text(content: Any) -> str:
    """把常见 session content 结构转换成待判定文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def compact_context_text(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "\n...[truncated]"


def build_context_text(messages: list[Any], idx: int, *, max_messages: int = 8, max_chars: int = 8000) -> str:
    """提取目标 assistant 前面的少量消息，供无总结时生成保守总结。"""
    blocks: list[str] = []
    total_chars = 0
    start = max(0, idx - max_messages)
    for pos in range(start, idx):
        msg = messages[pos]
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            role = "unknown"
        text = content_to_text(msg.get("content")).strip()
        if not text:
            text = content_to_text(msg.get("reasoning_content")).strip()
        if not text:
            continue
        block = f"[{pos}] {role}:\n{compact_context_text(text, 1200)}"
        if total_chars + len(block) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 200:
                blocks.append(compact_context_text(block, remaining))
            break
        blocks.append(block)
        total_chars += len(block)
    return "\n\n".join(blocks)


def find_last_eligible_assistant(messages: list[Any]) -> tuple[int | None, str, str]:
    """定位末节点收尾目标，并返回结构状态（不再整条跳过）。

    需求：轨迹的最后一个节点必须是总结性 assistant。旧逻辑遇到"末 assistant 带
    tool_calls / content 为空 / 末条根本不是 assistant"时直接 return None 整条跳过，
    导致这些结构异常的 session 永远修不到（空节点、被删后停在 user/tool 上等）。
    现在改为返回状态标记，交由上层分别处理（原位生成 / 末尾追加）。

    先判"整条轨迹最后一条消息是不是 assistant"，再判该 assistant 形态——不能只找
    "最后一个 assistant"，否则末 assistant 后面还挂着 user/tool 时会误判。

    返回 (index, text, status)：
      - "OK"                 末条是 assistant、无 tool_calls、content 非空 → 原位清理/判定
      - "EMPTY_CONTENT"      末条是 assistant、无 tool_calls、content 空 → 原位生成总结
      - "HAS_TOOLCALLS"      末条是 assistant 但带 tool_calls（结构异常）→ 末尾追加总结节点
      - "ENDS_NON_ASSISTANT" 整条末条是 user/tool（末尾无 assistant）→ 末尾追加总结节点
      - "NO_ASSISTANT"       整条无 assistant → 末尾追加（无可用锚点时上层跳过）
    index：OK/EMPTY_CONTENT 时是该 assistant 下标；追加类为取上文的锚点（可为 None）。

    注：正常 pipeline 里 fix_missing_summary_nodes(step 11) 已先处理这些结构异常，
    clean_last(step 12) 的生成/追加分支主要用于"单独运行"或上游漏网时的兜底，
    不会与 fix_missing 重复追加（step 11 补好后，step 12 看到的末节点即 OK）。
    """
    last_real_idx = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], dict)),
        None,
    )
    last_asst_idx = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if isinstance(messages[i], dict) and messages[i].get("role") == "assistant"
        ),
        None,
    )

    # 末条不是 assistant（被删后停在 user/tool 上）→ 追加
    if last_real_idx is not None and messages[last_real_idx].get("role") != "assistant":
        return last_asst_idx, "", "ENDS_NON_ASSISTANT"
    # 整条没有 assistant
    if last_asst_idx is None:
        return None, "", "NO_ASSISTANT"

    msg = messages[last_asst_idx]
    if msg.get("tool_calls"):
        return last_asst_idx, content_to_text(msg.get("content")), "HAS_TOOLCALLS"
    text = content_to_text(msg.get("content"))
    if not text.strip():
        return last_asst_idx, "", "EMPTY_CONTENT"
    return last_asst_idx, text, "OK"


def find_messages(data: Any) -> tuple[str | None, list[Any] | None]:
    if not isinstance(data, dict):
        return None, None
    for key in ("messages", "chat_messages", "conversation", "history"):
        val = data.get(key)
        if isinstance(val, list):
            return key, val
    return None, None


def update_message_content(messages: list[Any], idx: int, new_text: str) -> None:
    """更新 messages[idx] 的 content。"""
    msg = messages[idx]
    old_content = msg.get("content", "")
    if isinstance(old_content, list):
        msg["content"] = [{"type": "text", "text": new_text}]
    else:
        msg["content"] = new_text


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def gather_session_files(base: Path, task_filter: str | None, max_files: int) -> list[Path]:
    """发现集合根或单个 TASK 包根中的 session 轨迹 JSON 文件。"""
    patterns = ["TASK-*/sessions/**/*.json"]
    if (base / "sessions").is_dir():
        patterns.append("sessions/**/*.json")

    files = {
        fp.resolve()
        for pattern in patterns
        for fp in base.glob(pattern)
        if fp.is_file()
    }
    top_files = sorted(files)
    if task_filter:
        top_files = [fp for fp in top_files if task_filter in str(fp)]
    if max_files > 0:
        top_files = top_files[:max_files]
    return top_files


def gather_input_session_files(base: Path, inputs: list[Path], max_files: int) -> list[Path]:
    """Resolve explicit session JSON inputs and ensure they stay under base."""
    resolved_base = base.resolve()
    files: list[Path] = []
    missing: list[str] = []
    outside_base: list[str] = []
    seen: set[Path] = set()

    for raw_path in inputs:
        fp = raw_path.expanduser().resolve()
        if not fp.is_file():
            missing.append(str(fp))
            continue
        try:
            fp.relative_to(resolved_base)
        except ValueError:
            outside_base.append(str(fp))
            continue
        if fp not in seen:
            seen.add(fp)
            files.append(fp)

    if missing:
        raise FileNotFoundError("input session file(s) not found: " + ", ".join(missing))
    if outside_base:
        raise ValueError("input session file(s) must be under --base-dir: " + ", ".join(outside_base))
    if max_files > 0:
        files = files[:max_files]
    return files


def is_problem_data_directory(base: Path) -> bool:
    """判断输入是否位于仓库问题包目录，避免默认原地覆盖原始数据。"""
    data_dir = (PROJECT_ROOT / "data").resolve()
    try:
        base.resolve().relative_to(data_dir)
    except ValueError:
        return False
    return True


def _extract_task_id(rel: str, base: Path) -> tuple[str, Path]:
    """从文件相对路径推断 task_id 和 task 根目录。"""
    parts = rel.split("/")
    if parts and parts[0].startswith("TASK-"):
        task_id = parts[0]
        return task_id, base / task_id
    return base.name, base


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


def write_task_reports(
    base: Path,
    records: list[dict[str, Any]],
    stats: dict[str, int],
) -> None:
    """为每个有修改的 task 写入独立修复总结 report。"""
    _api_key, _base_url, _anthropic_version, model = _anthropic_config()
    modified_statuses = {"cleaned", "generated", "appended"}
    task_records: dict[str, list[dict[str, Any]]] = {}
    task_stats: dict[str, dict[str, int]] = {}

    for rec in records:
        rel = rec.get("file", "")
        if not rel:
            continue
        task_id, task_path = _extract_task_id(rel, base)
        task_records.setdefault(task_id, []).append(rec)
        ts = task_stats.setdefault(task_id, {"files_scanned": 0, "files_modified": 0, "total_issues": 0})
        ts["files_scanned"] += 1
        if rec.get("status") in modified_statuses:
            ts["files_modified"] += 1

    for task_id, t_records in task_records.items():
        t_stats = task_stats[task_id]
        if t_stats["files_modified"] == 0:
            continue
        _, task_path = _extract_task_id(
            next(r["file"] for r in t_records if r.get("file")), base
        )
        report_dir = task_path / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / "clean_last_assistant_report.json"

        hit_count = 0
        sessions: dict[str, dict[str, Any]] = {}
        for rec in t_records:
            if rec.get("status") not in modified_statuses:
                continue
            hit_count += 1
            rel = rec.get("file", "")
            session_id = Path(rel).stem
            # before/after 存全文，不截断——一条 assistant 消息通常也就几百字，
            # 全存进报告成本可忽略，换来"删前 vs 删后"的完整对照。
            before_text = rec.get("original_text_full")
            if before_text is None:
                before_text = rec.get("original_text_preview") or ""
            after_text = rec.get("cleaned_text_full")
            if after_text is None:
                after_text = rec.get("cleaned_preview") or ""
            if not after_text and rec.get("status") == "generated":
                after_text = "(generated summary from context)"

            action_map = {"cleaned": "update", "generated": "add", "appended": "add"}
            action_type = action_map.get(rec.get("status", ""), "problem")
            terminal_status = rec.get("terminal_status", "")
            if terminal_status in ("HAS_TOOLCALLS", "ENDS_NON_ASSISTANT", "NO_ASSISTANT"):
                action_type = "add"

            # problem 优先用 LLM 的 reason——它就是"这条实际删了什么"的一句话说明
            # （如"删除了尾部验证命令和下一轮提示"），比固定的"包含非总结内容"更具体。
            # 结构异常（无有效末条可判）没有 reason，回退到固定描述。
            problem_map = {
                "HAS_TOOLCALLS": "last assistant has tool_calls, no valid terminal summary",
                "ENDS_NON_ASSISTANT": "trajectory ends with non-assistant message",
                "NO_ASSISTANT": "trajectory has no assistant messages",
                "EMPTY_CONTENT": "last assistant content is empty",
            }
            llm_reason = (rec.get("reason") or "").strip()
            if terminal_status in problem_map:
                problem = problem_map[terminal_status]
            elif llm_reason:
                problem = llm_reason
            else:
                problem = "last assistant contains non-summary content"

            # line: 从消息索引推算行号
            msg_idx = rec.get("message_index")
            line_info = None
            if msg_idx is not None:
                abs_path = base / rel
                line_range = _find_message_line_range(abs_path, msg_idx)
                if line_range:
                    s, e = line_range["start"], line_range["end"]
                    line_info = s if s == e else f"{s}-{e}"

            session_timestamp = _extract_session_timestamp(base / rel)

            addition = [
                {
                    "original_chars": rec.get("original_chars", 0),
                    "cleaned_chars": rec.get("cleaned_chars", len(rec.get("cleaned_preview") or "")),
                    "outcome": rec.get("outcome", "success"),
                    "session_timestamp": session_timestamp,
                    # 溯源与定位：message_index 不受排版/行号偏移影响，比 line 更可靠；
                    # model 记录本次判定所用模型，reason 记录判定理由，便于日后复查。
                    "message_index": msg_idx,
                    "model": model,
                    "reason": llm_reason,
                }
            ]

            if session_id not in sessions:
                sessions[session_id] = {"session_id": session_id, "sub": []}
            sessions[session_id]["sub"].append({
                "function": "clean_last_assistant",
                "action_type": action_type,
                "before_content": before_text,
                "after_content": after_text,
                "problem": problem,
                "line": line_info,
                "addition": addition,
            })

        task_report: dict[str, Any] = {
            "report_type": "clean_last_assistant_report",
            "task_id": task_id,
            "task_path": str(task_path),
            "create": datetime.now().isoformat(timespec="seconds"),
            "summary": {
                "total_trajectory_count": t_stats["files_scanned"],
                "hit_trajectory_count": hit_count,
                "total_issues": hit_count,
            },
            "sessions": list(sessions.values()),
        }
        write_json(report_file, task_report)
        print(f"[task-report] {report_file}")


def make_report(
    *,
    base: Path,
    report_path: Path,
    dry_run: bool,
    output_dir: Path | None,
    files_scanned: int,
    stats: dict[str, int],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    _api_key, _base_url, _anthropic_version, model = _anthropic_config()
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_dir": str(base),
        "report_path": str(report_path),
        "dry_run": dry_run,
        "output_dir": str(output_dir) if output_dir else None,
        "model": model,
        "summary": {**stats, "files_scanned": files_scanned},
        "files": records,
    }


def is_main_trajectory(path: Path) -> bool:
    """判断是否为主轨迹（与 fix_missing_summary_nodes.is_main_trajectory 对齐）。

    子 agent 的两种命名：路径含 subagents/ 目录，或文件名以 agent- 开头。
    不能用 "subagents" in str(path) 粗判——会误伤 subagents_backup/ 之类路径。
    """
    parts = path.parts
    if "subagents" in parts:
        return False
    if path.name.startswith("agent-"):
        return False
    return True


def _process_single_file(
    fp: Path,
    base: Path,
    dry_run: bool,
    output_dir: Path | None,
    cleaner: Callable[..., SemanticDecision],
    lock: threading.Lock,
    counter: list[int],
    total: int,
    check_report: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """处理单个 JSON 文件，返回处理记录。"""
    rel = fp.relative_to(base).as_posix()
    record: dict[str, Any] = {"file": rel}

    try:
        for attempt in range(3):
            try:
                with fp.open("r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                break
            except json.JSONDecodeError:
                if attempt >= 2:
                    raise
                time.sleep(0.1)
    except Exception as exc:
        with lock:
            counter[0] += 1
            print(f"[{counter[0]}/{total}] ✗ JSON 读取失败: {rel}")
        record.update({"status": "error", "error": repr(exc)})
        return record

    message_key, messages = find_messages(data)
    record["message_key"] = message_key
    if messages is None:
        with lock:
            counter[0] += 1
            print(f"[{counter[0]}/{total}] → 无消息列表, 跳过: {rel}")
        record["status"] = "skipped_no_messages"
        return record

    idx, orig_text, status = find_last_eligible_assistant(messages)
    record["terminal_status"] = status

    # 极端异常：整条无 assistant 且无消息可作上文 → 跳过（无从生成）
    if status == "NO_ASSISTANT" and not messages:
        with lock:
            counter[0] += 1
            print(f"[{counter[0]}/{total}] → 无 assistant/无上文, 跳过: {rel}")
        record["status"] = "skipped_no_candidate"
        return record

    # 取上文锚点：OK/EMPTY_CONTENT 用该节点位置；追加类用末尾
    anchor = idx if idx is not None else len(messages)
    context_text = build_context_text(messages, anchor)

    # 送 LLM 的文本：OK 用原文（清理夹带尾巴）；结构异常用占位符，
    # 触发 SYSTEM_PROMPT 规则「无总结→据上文生成简短总结」。
    if status == "OK":
        to_judge = orig_text
    elif status == "EMPTY_CONTENT":
        to_judge = "(The terminal assistant content is empty. Generate a short closing summary from the context.)"
    else:  # HAS_TOOLCALLS / ENDS_NON_ASSISTANT / NO_ASSISTANT
        to_judge = "(There is no qualified terminal summary. Generate a short closing summary from the context.)"

    record.update({"message_index": idx, "original_chars": len(orig_text)})
    is_main = is_main_trajectory(fp)
    record["is_main_trajectory"] = is_main

    # 从 fix_missing_summary_nodes 报告中查找预分类信息
    check_info: dict[str, str] | None = None
    if check_report:
        # 归一化路径分隔符（fix 报告可能用 \\，clean 用 /）
        rel_norm = rel.replace("\\", "/")
        check_info = (
            check_report.get(rel_norm)
            or check_report.get(fp.as_posix().replace("\\", "/"))
        )
        if check_info is None:
            # 再尝试按文件名尾部匹配
            for key, val in check_report.items():
                kn = key.replace("\\", "/")
                if kn.endswith(rel_norm) or rel_norm.endswith(kn):
                    check_info = val
                    break
        if check_info is not None:
            record["pre_classification"] = check_info

    # 预分类优化：pure_summary / non_summary / unrecoverable 跳过 LLM
    # non_summary 已被 fix 脚本的 REWRITE LLM 生成过总结，clean 不再重复处理；
    # mixed 是 fix 只识别未清理的，仍需 LLM 来剥离杂质。
    if check_info:
        pre_ct = check_info.get("content_type", "")
        pre_oc = check_info.get("outcome", "")
        # mentor 政策: 无论 content_type 是什么, outcome != success 一律 reject。
        # 拦截在 LLM 之前, 避免为 partial/failed/blocked/indeterminate 的轨迹调用 LLM 或写文件。
        if pre_oc and pre_oc != "success":
            with lock:
                counter[0] += 1
                print(f"[{counter[0]}/{total}] ⊘ 预判 outcome={pre_oc} 非 success, 拒绝: {rel}")
            record.update({
                "decision": "reject",
                "outcome": pre_oc,
                "reason": f"fix_missing_summary_nodes 预判 outcome={pre_oc}, 按 mentor 政策仅接受 success",
            })
            record["status"] = "rejected"
            return record
        if pre_ct in ("pure_summary", "non_summary"):
            # fix 已处理（识别不改 / 改写总结），clean 无需再调 LLM
            action_label = "纯总结(fix)" if pre_ct == "pure_summary" else "已改写(fix)"
            with lock:
                counter[0] += 1
                print(f"[{counter[0]}/{total}] ○ {action_label}, 跳过: {rel}")
            record.update({
                "decision": "unchanged",
                "outcome": pre_oc or "success",
                "reason": f"fix_missing_summary_nodes 预判为 {pre_ct}，clean 跳过",
            })
            record["status"] = "unchanged"
            return record
        if pre_ct == "unrecoverable":
            with lock:
                counter[0] += 1
                print(f"[{counter[0]}/{total}] ⊘ 预判上下文不足, 拒绝修改: {rel}")
            record.update({
                "decision": "reject",
                "outcome": "indeterminate",
                "reason": "fix_missing_summary_nodes 预判为 unrecoverable",
            })
            record["status"] = "rejected"
            return record

    # 语言漂移基准：结构异常分支 to_judge 是英文占位符，不能拿它判语言；
    # 优先用原文 orig_text（可能为空），空则回退到上下文文本。
    lang_ref = orig_text if orig_text.strip() else context_text

    try:
        decision = cleaner(to_judge, context_text, is_main, check_info, lang_ref)
    except Exception as exc:
        with lock:
            counter[0] += 1
            print(f"[{counter[0]}/{total}] ✗ 语义判断失败: {rel}  {exc}")
        record.update({"status": "error", "error": repr(exc)})
        return record

    record.update({
        "decision": decision.action,
        "outcome": decision.outcome,
        "reason": decision.reason,
        "cleaned_chars": len(decision.cleaned_text),
        "original_text_preview": compact_context_text(orig_text, 200),
        # 全文用于 task report 的 before/after 完整对照；preview 截断字段留给主 report 控体积。
        "original_text_full": orig_text,
    })

    # reject：上下文不足以确定真实终态，不修改文件
    if decision.action == "reject":
        with lock:
            counter[0] += 1
            print(f"[{counter[0]}/{total}] ⊘ 上下文不足, 拒绝修改: {rel}")
        record["status"] = "rejected"
        return record

    # OK 且判定无需清理：末节点已是合格总结
    if status == "OK" and not decision.changed:
        with lock:
            counter[0] += 1
            print(f"[{counter[0]}/{total}] ○ 无需修改: {rel}")
        record["status"] = "unchanged"
        return record

    # 结构异常但 LLM 未给出有效生成：兜底不硬造，跳过并记录
    if status != "OK" and (not decision.changed or not decision.cleaned_text.strip()):
        with lock:
            counter[0] += 1
            print(f"[{counter[0]}/{total}] → 结构异常但无法生成总结, 跳过: {rel} ({status})")
        record["status"] = f"skipped_{status.lower()}"
        return record

    is_append = status in ("HAS_TOOLCALLS", "ENDS_NON_ASSISTANT", "NO_ASSISTANT")
    delta_chars = len(orig_text) - len(decision.cleaned_text)
    record["delta_chars"] = delta_chars
    record["cleaned_preview"] = compact_context_text(decision.cleaned_text, 500)
    record["cleaned_text_full"] = decision.cleaned_text
    record["status"] = (
        "appended" if is_append
        else "generated" if status == "EMPTY_CONTENT"
        else "cleaned"
    )

    if dry_run:
        with lock:
            counter[0] += 1
            print(f"[{counter[0]}/{total}] ○ DRY-RUN: {rel}  ({record['status']}, {status})")
        return record

    if is_append:
        messages.append({"role": "assistant", "content": decision.cleaned_text})
    else:
        update_message_content(messages, idx, decision.cleaned_text)

    if output_dir:
        out_path = output_dir / rel
        write_json(out_path, data)
        record["output_file"] = str(out_path)
    else:
        write_json(fp, data)
        record["output_file"] = str(fp)

    with lock:
        counter[0] += 1
        print(f"[{counter[0]}/{total}] ✓ {record['status']} ({status}): {rel}")

    return record


def process_files(
    files: list[Path],
    *,
    base: Path,
    dry_run: bool = False,
    output_dir: Path | None = None,
    workers: int = DEFAULT_WORKERS,
    cleaner: Callable[..., SemanticDecision] = call_llm,
    check_report: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    stats = {
        "cleaned": 0,
        "unchanged": 0,
        "skipped": 0,
        "error": 0,
        "files_modified": 0,
    }
    records: list[dict[str, Any]] = []

    total = len(files)
    counter = [0]  # list for mutable closure
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                _process_single_file,
                fp,
                base,
                dry_run,
                output_dir,
                cleaner,
                lock,
                counter,
                total,
                check_report,
            ): fp
            for fp in files
        }

        for future in as_completed(futures):
            try:
                record = future.result()
            except Exception as exc:
                fp = futures[future]
                record = {
                    "file": fp.relative_to(base).as_posix(),
                    "status": "error",
                    "error": f"处理异常: {exc}",
                }
            records.append(record)

    # 汇总 stats
    modified_statuses = {"cleaned", "generated", "appended"}
    outcome_counts: dict[str, int] = {}
    for rec in records:
        status = rec.get("status", "")
        if status in modified_statuses:
            stats["cleaned"] += 1
        elif status == "unchanged":
            stats["unchanged"] += 1
        elif status and status.startswith("skipped"):
            stats["skipped"] += 1
        elif status == "error":
            stats["error"] += 1
        if status in modified_statuses and not dry_run:
            stats["files_modified"] += 1
        # outcome 分布
        oc = rec.get("outcome", "")
        if oc:
            outcome_counts[oc] = outcome_counts.get(oc, 0) + 1
    stats["outcome_distribution"] = outcome_counts

    # 按文件路径排序以保持输出一致性
    records.sort(key=lambda r: r.get("file", ""))
    return stats, records


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="用语义判断清理 TASK session JSON 中最后 assistant 回复的转折/遗留问题部分"
    )
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_INPUT_DIR,
                        help=f"基础目录，默认 {DEFAULT_INPUT_DIR}")
    parser.add_argument("--filter",
                        help="只处理路径中包含此关键字的 session 文件")
    parser.add_argument("-i", "--input", nargs="+", type=Path,
                        help="显式指定 session JSON 文件，可多个")
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览不改写 session 文件，仍会写 report")
    parser.add_argument("--max-files", type=int, default=0,
                        help="最多处理文件数，0=不限制")
    parser.add_argument("--output-dir", type=Path,
                        help="输出到新目录，不修改原文件")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH,
                        help=f"report 输出路径，默认 {DEFAULT_REPORT_PATH}")
    parser.add_argument("--check-report", type=Path, default=None,
                        help="fix_missing_summary_nodes 的 JSON 报告路径，用于预分类信息注入")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"并发线程数，默认 {DEFAULT_WORKERS}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = args.base_dir.resolve()
    report_path = args.report.resolve()
    output_dir = args.output_dir.resolve() if args.output_dir else None

    if is_problem_data_directory(base) and not args.dry_run and output_dir is None:
        print("错误: data 目录中的问题包禁止原地改写；请使用 --output-dir 指定独立输出目录")
        return 2

    # ── 加载 fix_missing_summary_nodes 的预分类报告 ──
    # 如果有报告且含 mixed 条目，直接从报告中提取文件列表（跳过全量扫描）
    # mentor 政策: 只提取 mixed AND outcome=success 的文件, 其余全部跳过。
    check_report: dict[str, dict[str, str]] | None = None
    mixed_files_from_report: list[Path] = []
    report_mixed_count = 0
    report_mixed_success_count = 0
    report_mixed_nonsuccess_count = 0
    report_total_count = 0
    if args.check_report:
        try:
            with args.check_report.open("r", encoding="utf-8") as f:
                fix_data = json.load(f)
            check_report = {}
            for rec in fix_data.get("results", []):
                file_key = rec.get("file", "").replace("\\", "/")
                ct = rec.get("content_type", "")
                oc = rec.get("outcome", "")
                if file_key:
                    check_report[file_key] = {"content_type": ct, "outcome": oc}
                report_total_count += 1
                if ct == "mixed":
                    report_mixed_count += 1
                    if oc == "success":
                        fp = base / file_key
                        if fp.is_file():
                            mixed_files_from_report.append(fp)
                        report_mixed_success_count += 1
                    else:
                        # mixed 但 outcome != success —— 按 mentor 政策不进 clean 队列
                        report_mixed_nonsuccess_count += 1
            print(f"[check-report] 加载: {report_total_count} 条记录")
            print(f"[check-report] mixed 总计: {report_mixed_count}")
            print(
                f"[check-report] mixed AND outcome=success (进入clean): "
                f"{report_mixed_success_count}"
            )
            print(
                f"[check-report] mixed 但 outcome!=success (跳过, 按 mentor 政策 reject): "
                f"{report_mixed_nonsuccess_count}"
            )
            print(
                f"[check-report] 其他 content_type (跳过): "
                f"{report_total_count - report_mixed_count}"
            )
        except Exception as exc:
            print(f"[check-report] 警告: 加载失败 ({exc})，回退到全量扫描")
            args.check_report = None  # 标记失败，走旧流程

    # ── 文件发现 ──
    if args.input:
        try:
            files = gather_input_session_files(base, args.input, args.max_files)
        except (FileNotFoundError, ValueError) as exc:
            print(f"错误: {exc}")
            return 2
        print(f"\n指定文件: 找到 {len(files)} 个 session JSON 文件")
    elif args.check_report and mixed_files_from_report:
        files = sorted(mixed_files_from_report)
        print(f"\n直接处理 mixed 文件: {len(files)} 个（跳过全量扫描）")
    else:
        files = gather_session_files(base, args.filter, args.max_files)
        print(f"\n全量扫描: 找到 {len(files)} 个 session JSON 文件")

    if not files:
        stats = {"cleaned": 0, "unchanged": 0, "skipped": 0, "error": 0, "files_modified": 0}
        report = make_report(
            base=base,
            report_path=report_path,
            dry_run=args.dry_run,
            output_dir=output_dir,
            files_scanned=0,
            stats=stats,
            records=[],
        )
        write_json(report_path, report)
        print(f"在 {base} 下未找到 session JSON 文件")
        print(f"report: {report_path}")
        return 0

    api_key, base_url, anthropic_version, model = _anthropic_config()
    missing = [
        name
        for name, value in (
            ("MODEL_API_KEY", api_key),
            ("MODEL_BASE_URL", base_url),
            ("ANTHROPIC_VERSION", anthropic_version),
            ("MODEL_NAME", model),
        )
        if not value
    ]
    if missing:
        stats = {
            "cleaned": 0,
            "unchanged": 0,
            "skipped": 0,
            "error": len(files),
            "files_modified": 0,
        }
        records = [
            {
                "file": fp.relative_to(base).as_posix(),
                "status": "error",
                "error": "missing model config: " + ", ".join(missing),
            }
            for fp in files
        ]
        report = make_report(
            base=base,
            report_path=report_path,
            dry_run=args.dry_run,
            output_dir=output_dir,
            files_scanned=len(files),
            stats=stats,
            records=records,
        )
        write_json(report_path, report)
        print("错误: 项目根目录 .env 缺少模型配置: " + ", ".join(missing))
        print(f"report: {report_path}")
        return 1

    print(f"并发数: {args.workers}")
    if args.dry_run:
        print("【DRY RUN 模式】不会实际修改 session 文件")
    if not output_dir:
        print("默认不创建 .bak")

    stats, records = process_files(
        files,
        base=base,
        dry_run=args.dry_run,
        output_dir=output_dir,
        workers=args.workers,
        check_report=check_report,
    )
    report = make_report(
        base=base,
        report_path=report_path,
        dry_run=args.dry_run,
        output_dir=output_dir,
        files_scanned=len(files),
        stats=stats,
        records=records,
    )
    write_json(report_path, report)

    write_task_reports(base, records, stats)

    print("\n" + "=" * 50)
    print("处理完成:")
    print(f"  ✓ 已清理消息:   {stats['cleaned']}")
    print(f"  ○ 无变化:       {stats['unchanged']}")
    print(f"  → 跳过:        {stats['skipped']}")
    print(f"  ✗ 错误:        {stats['error']}")
    print(f"  📁 修改文件数:  {stats['files_modified']}")
    print(f"  共扫描文件:     {len(files)}")
    print(f"  report:        {report_path}")
    return 1 if stats["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
调用 LLM 检测并修复轨迹「最后一条 assistant 消息」的非总结性 content。

流程：
  1. 扫描 session，取最后一条符合结构条件的 assistant 消息
     （与 clean_last_assistant.find_last_eligible_assistant 对齐，绝不回溯）：
       - 就是最后一条 role == "assistant" 节点；
       - 不含 tool_calls（带 tool_calls 视为结构异常，直接跳过 session）；
       - content 非空。
     reasoning_content 仅作为 LLM 判定/改写的上下文输入，不参与节点筛选。
  2. 用 LLM 判定该 content 是否为总结性内容；
  3. 对判定为 is_summary=false 的 session，调用 LLM 基于
     「最后一条 user content + 待改 assistant reasoning_content + 原 content」
     重新生成一段总结性 content，覆盖原 content；
  4. 生成 session 粒度报告（修改前/修改后/新增行/删除行）；
  5. 默认原地写回，不创建备份；若指定 -o 则写到输出目录（不改动原文件）。

参数：
  -r, --root       递归查找的根目录
  -i, --input      指定输入文件（可选，可多个；不指定则从 -r 递归发现）
  -o, --output     输出目录（可选，不指定则原地覆盖，不创建备份）
  -report          报告输出路径，目录或文件
                   （可选，默认 {root}/report/fix_missing_summary_nodes_{ts}.json）
  -w, --workers    并发线程数（默认 50）

环境变量（从仓库根目录 .env 读取）：
  MODEL_API_KEY / ANTHROPIC_API_KEY
  MODEL_BASE_URL / ANTHROPIC_BASE_URL
  ANTHROPIC_VERSION / MODEL_ANTHROPIC_VERSION
  MODEL_NAME / FIX_MISSING_SUMMARY_MODEL / CLEAN_LLM_MODEL
"""

from __future__ import annotations

import argparse
import difflib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


SCRIPT_NAME = Path(__file__).stem
PROJECT_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_WORKERS = 50


@dataclass(frozen=True)
class RuntimeConfig:
    api_key: str
    base_url: str
    anthropic_version: str
    model: str


# ── §四 SHARED_TERMINAL_POLICY: single source of truth for taxonomy ─────
# 供 CHECK_SYSTEM_PROMPT 和 REWRITE_SYSTEM_PROMPT 共同引用,禁止两个 prompt
# 内部各自维护 content_type / outcome / action 枚举名 —— 一旦分叉就等于
# CHECK 判成一档,REWRITE 按另一档去写,行为不可预测。
SHARED_TERMINAL_POLICY = """\
Terminal-state taxonomy (SHARED reference — both CHECK and REWRITE MUST use
these exact names, character-for-character).

Content-type (4 values; classifies the last assistant content itself):
  - pure_summary:  content IS a summary-style closing (final judgment on the
                   whole task; states what was done / why it failed / why it
                   is blocked). No mid-conversation noise mixed in.
  - mixed:         content contains a valid summary body BUT is diluted with
                   non-terminal noise (plans, apologies, source dumps, TODO
                   lists). Salvageable by trimming.
  - non_summary:   content is NOT a summary at all (pure analysis /
                   plan+question / gap analysis / file dump / mid-state
                   recap / pleasantry / truncated pleasantry). Preceding
                   trajectory DOES have enough facts to reconstruct an
                   honest closing. Needs full rewrite from context.
  - unrecoverable: content is destroyed AND the preceding trajectory
                   provides no basis for reconstructing an honest closing
                   (all facts missing, tool results absent, only
                   contradictions). MUST be rejected — do NOT synthesize a
                   fake closing. Prefer this ONLY when reconstruction
                   would require fabricating facts.

Outcome (5 values; describes what the WHOLE TASK actually ended in — this
is ORTHOGONAL to content_type):
  - success:       the requested task was completed.
  - partial:       some requested work was completed but material parts
                   remain.
  - failed:        the task was attempted but did not succeed.
  - blocked:       the task cannot continue because required information,
                   permission, resources, or environment access is missing.
  - indeterminate: the available trajectory does not provide enough
                   evidence to determine the outcome.

Legal-terminal policy (mentor decision):
Only `success` is a legal terminal state for this pipeline. Sessions whose
outcome is `partial`, `failed`, `blocked`, or `indeterminate` MUST be
rejected — the caller MUST NOT rewrite, MUST NOT append, MUST NOT fabricate
a success closing. NEVER silently promote a non-success outcome into
`success` to make the session "pass".

Cleaner action (4 values; mapping combines content_type AND outcome):
  - unchanged:  pure_summary + outcome=success
                              → keep original text as-is; no rewrite.
  - clean:      mixed         + outcome=success
                              → DO NOT rewrite here. Leave for downstream
                                clean_last_assistant.py which does semantic
                                trimming (LLM strips plans / transitions /
                                politeness / file dumps while keeping the
                                terminal facts intact).
  - rewrite:    non_summary   + outcome=success
                              → REWRITE call; regenerate closing from
                                context (wholesale rewrite, not trimming).
  - reject:     content_type=unrecoverable
                OR outcome ∈ {partial, failed, blocked, indeterminate}
                              → do NOT append or replace text. Persist
                                reason only. NEVER emit fabricated
                                "Completed / Done / Task handled" wording
                                on this path.

Structural truncation branches (EMPTY_CONTENT / HAS_TOOLCALLS /
ENDS_NON_ASSISTANT / NO_ASSISTANT) are ALWAYS routed to `reject` under
this policy: R3 caps their outcome at `partial`, and only `success` is
legal here.
"""


CHECK_SYSTEM_PROMPT = """You are a strict trajectory terminal-state classifier.

Background: An AI coding assistant's conversation trajectory typically ends with
one assistant message. A "qualified terminal assistant content" should be a
**summary-style closing** for the whole task: it states the final conclusion,
tells the user whether the task is done, summarizes what was done / changed /
added / removed, or reports that the task failed / cannot be completed / needs
more information.

A qualified terminal assistant content MUST satisfy ALL of:
1. Gives a final judgment on the ENTIRE task: completed / partially done /
   failed / cannot continue due to lack of info.
2. Content is "what was done / changed / verified" OR "why it failed / cannot
   continue".
3. NOT pure analysis / status / findings UNLESS the task itself was "analyze X"
   — answering the user's question is NOT the same as a work summary.

Example of VALID summary:
- "All 3 endpoints implemented. Tests pass. Database schema updated per spec."
- "Task failed: Docker environment cannot reach packagist.org; build cannot
  proceed."

Example of INVALID (NOT summary):
- "The system uses JWT auth and stores sessions in Redis." (pure status,
  no work done)
- "I will use CrowCpp. Which module should we start with? 1/2/3"
  (mid-conversation choice)

You will see:
  1. The last user message content (task background reference; may be empty)
  2. The reasoning_content of the assistant under judgment (may be empty)
  3. The content of the assistant under judgment (this is what you MUST classify)

Your only task: decide whether [the assistant's content under judgment] is
summary-style closing content.

Typical shapes of summary content (is_summary=true):
- Clearly declares the task as completed / failed / aborted;
- Reports which files, functions, or behaviors were modified / added / removed;
- Reports whether tests passed and whether verification succeeded;
- Gives the final answer to the user's initial question AND that answer is
  itself a work summary (what was done / changed / verified). Pure analysis
  or status answers are NOT summary unless the task itself was "analyze X".
- Explains that the task cannot continue due to missing info / permission /
  resources, and states the reason;
- Contains no "but/however"-style pivoting away from the conclusion.

Typical shapes of non-summary content (is_summary=false):

1. Pure analysis / status description / business logic walkthrough — EVEN IF
   complete and conclusion-style.
   Example: "Below is the business logic of LiteWork... [5 paragraphs of
   analysis]"
   → This describes WHAT EXISTS, not WHAT WAS DONE. is_summary=false.

2. Plan / intention + question to user:
   Example: "I will use CrowCpp and pin it to v1.2.0. This request spans
   several subsystems, which slice should we design first? 1/2/3/4"
   → Mid-conversation choice point, not closing. is_summary=false.

3. Gap analysis / TODO list:
   Examples: "However, nothing in the current code does X",
   "X was built but not yet connected",
   "the worker exists but is not invoked",
   "missing pieces", "what changes are needed", "next step should..."
   → These state what is NOT done. is_summary=false.

4. Full file dump / large code paste:
   Example: "Here are the complete contents of all 9 files: ..." followed by
   pages of source code, EVEN IF ending with a summary sentence.
   → Summary states outcomes, not source dumps. is_summary=false.

5. Coverage / test detail logs without final task judgment:
   Example: "File A: 121/121, File B: 194/194, Total: 315/315"
   → Pure checklist. is_summary=false UNLESS it also says
   "All tasks complete" or similar.

6. Mid-state recaps without final judgment:
   Examples: "I have read...", "Reviewed all relevant files...",
   "The codebase shows..."
   → Describes work-in-progress, not outcome. is_summary=false.

7. Empty / truncated / pure pleasantries:
   Examples: empty content, single punctuation, "OK" / "sure" with no info,
   mid-sentence cutoff, "Thank you!" / "Good luck!" with no work content.
   → is_summary=false.

Additional legacy shapes still counted as non-summary (kept for coverage):
- Intermediate thinking: "Let me first look at xxx" / "Next I plan to xxx" /
  "Let me try xxx";
- Only describes an action being executed or a tool about to be invoked (in
  that case there should be tool_calls, not a closing);
- Only asks a question waiting for more user input (and not because the task
  is blocked);
- Only quotes code snippets or pastes file contents without a conclusion;
- Reports what was seen or read mid-way without giving a final judgment;
- Clearly truncated (mid-sentence ending, no closure).

Judgment principles:
1. Do not classify content as non-summary just because it is short. A one-liner
   like "Removed the vendor/ directory as requested and the rebuild succeeded"
   is a valid summary.
2. Do not classify content as summary just because it is long. Large blocks of
   intermediate-state narration are still non-summary.
3. Only look at whether the content is "closing" and "gives a conclusion". Do
   not judge writing quality or conciseness.
4. reasoning_content only helps you understand context; it cannot replace the
   content itself as the basis for judgment.
5. If content enumerates a change list, it is ONLY a summary if it ALSO states
   "task completed" or "task failed" (or an equivalent final task judgment).
   A pure checklist without a final task judgment is is_summary=false.
6. Task-type based judgment (apply after rules 1-5, before rule 7):
   - Implementation task (user asked to modify / build / fix / test code):
     describing what EXISTS or what the architecture looks like is NOT a
     summary. Only "what was DONE / changed / verified" or "why it FAILED /
     is BLOCKED" counts as summary.
   - Analysis / research / review task (user asked "analyze X", "review X",
     "compare A vs B", "explain how X works"): the analysis conclusion or
     findings that directly answer the assigned question ARE valid terminal
     deliverables. is_summary=true if the finding is self-contained.
   - Advisory / QA task (user asked "how do I X", "give me the command",
     "show me a config example"): the requested command / answer / config
     example IS the deliverable. is_summary=true. Do NOT treat it as
     "verification steps to remove".
   - When the task type is unclear, default to implementation-task rules.
7. If content is clearly a bridging intermediate state such as "Next I will do
   xxx", classify as non-summary.

Trajectory-scope note:
- If the user prompt says "This is a MAIN trajectory", apply STRICT rules:
  pure analysis / findings / status is NOT summary.
- If the user prompt says "This is a subagent trajectory", RELAX rules:
  research / analysis findings are acceptable closing content. Only deny for
  categories 2 (plan+question), 4 (full file dump), 6 (mid-state recap),
  7 (empty / truncated / pleasantries).

====================================================================
§五 C1  4-way content_type refinement (apply AFTER the is_summary decision
        above; use exactly the names defined in SHARED_TERMINAL_POLICY):

If is_summary=true (the content IS a valid closing):

  ── pure_summary: the reply is a terminal summary AND contains ZERO
      impurities. ALL of the following must be true:
      ✓ States a final judgment (completed / partial / failed / blocked).
      ✓ Describes what WAS done / changed / verified / delivered.
      ✓ Contains NO plans, intentions, or next-step language.
      ✓ Contains NO politeness / social tails ("Ready for...", "Let me
        know...", "Thank you!", "Good luck!").
      ✓ Contains NO transitional/execution openings ("Perfect! Let me
        compile...", "Now I understand. Let me build...").
      ✓ Contains NO commands-to-run or verification instructions.
      ✓ Contains NO source code blocks or file dumps. May summarize what
        files were modified but does NOT paste their contents.
      ✓ Contains NO gap analysis or caveats ("However, X is not done").
        Exception: material gaps that define the terminal outcome as
        partial/failed/blocked ARE part of the summary body — not
        impurities. Example: "Implemented X, but Y remains incomplete.
        The task is partially complete." is pure_summary because the
        gap DEFINES the outcome.
      If ANY of these is violated → use mixed instead.

  ── mixed: a valid closing body IS present, BUT the reply also contains
      one or more non-terminal impurities. The terminal facts are there;
      they just need downstream trimming. Concrete impurity patterns
      (match clean_last_assistant's cleanup rules):

      1. Transitional/execution opening language:
         "Perfect! Let me compile...", "I now have the complete picture
         ... let me build...", "Now I understand. Let me...",
         "Great, I'll now...", "Alright, let me fix..."

      2. Politeness / social tails:
         "Let me know if you need anything else.", "Ready for next
         round / pass / QA feedback.", "Feel free to reach out.",
         "Thank you!", "Good luck!", "Happy to help!"

      3. Plans / intentions embedded in the closing:
         "I will next...", "Next I plan to...", "We should also
         consider...", "Going forward we can..."

      4. Commands-to-run / verification steps:
         "To verify, run: ...", "Test with: npm test", "Build: ng build"

      5. Intermediate gap analysis (NOT outcome-defining):
         Mid-exploration "However, nothing in the current code does X"
         when the gap was later addressed or is irrelevant to the
         terminal outcome. Compare: if removing the gap statement
         would change the perceived outcome → it IS outcome-defining,
         keep it as part of the summary (pure_summary). If removing
         it only removes exploration noise → it IS an impurity (mixed).

      6. Redundant file listings / source dumps:
         Full file contents or long code blocks mixed with summary.
         A brief file list ("Modified: a.ts, b.ts") is fine.

      7. Irrelevant suggestions / tangential recommendations:
         "You may also consider redesigning...", "In the future we
         could add..."

      Decision rule: if the reply has a closing body but you can point
      to at least one sentence that is NOT part of the closing → use
      mixed. The closing itself is valid; the extra sentences just need
      downstream removal.

If is_summary=false (the content is NOT a valid closing):
  - non_summary   → preceding trajectory context DOES provide enough
                    facts to reconstruct an honest closing (tool results
                    visible, task progress traceable). REWRITE from
                    context.
  - unrecoverable → the trajectory is destroyed / contradictory /
                    context-free (all facts missing, only pleasantry,
                    tool results absent, no way to state what happened).
                    REJECT — do NOT synthesize a fake closing.
                    Prefer this ONLY when reconstruction would require
                    fabricating facts.

====================================================================
§五 C1  Outcome inference (independent from content_type; 5 legal values
        per SHARED_TERMINAL_POLICY):

  - success:       task clearly completed per user request; tool results
                   and/or assistant confirmations show the whole task
                   done.
  - partial:       some parts done, other parts still needed / attempted
                   but not verified / trajectory truncated between
                   complete and incomplete steps.
  - failed:        task explicitly failed; error / rejection / cannot
                   continue is stated by the assistant OR shown by tool
                   results.
  - blocked:       missing information / permission / resource /
                   environment; task cannot proceed until the user acts.
  - indeterminate: not enough context to tell (very short trajectory,
                   pure pleasantry, no tool results, etc.).

Truncation rule for outcome (aligns with REWRITE R3):
  If the trajectory shows unfinished tool calls, ends on role=tool/user
  without a closing assistant, or otherwise appears truncated:
    outcome MUST be AT MOST `partial` — NEVER `success` — regardless of
    how many successful tool results appear earlier.

Backwards compatibility: also emit is_summary derived from content_type:
  pure_summary or mixed → is_summary=true
  non_summary or unrecoverable → is_summary=false

====================================================================
Output format (strictly obey; output only the following JSON, no
explanations, no markdown code blocks, no trailing prose):
{"content_type": "pure_summary" | "mixed" | "non_summary" | "unrecoverable",
 "outcome": "success" | "partial" | "failed" | "blocked" | "indeterminate",
 "is_summary": true | false,
 "reason": "one sentence stating the basis"}
"""


REWRITE_SYSTEM_PROMPT = """You are a trajectory terminal-state rewriter.

Background: The last assistant message in an AI coding assistant's conversation
trajectory should be a **summary-style closing** for the whole task. This
content has been judged non-summary (e.g. intermediate thinking, unclosed
narration, only a question, truncated), and you must rewrite it into a
qualified closing based on the given context.

The 5 legal terminal outcomes (success / partial / failed / blocked /
indeterminate) and the 4 content_type values (pure_summary / mixed /
non_summary / unrecoverable) are defined in SHARED_TERMINAL_POLICY,
which the caller prepends to this system prompt at request time. Per the
mentor policy in SHARED_TERMINAL_POLICY, ONLY `success` is a legal
terminal state — any non-success session is rejected by the caller BEFORE
REWRITE is invoked, so this function ONLY sees `outcome=success` cases.
Do NOT emit partial / failed / blocked / indeterminate closings.

======================================================================
HIGHEST PRIORITY RULES (override anything below on conflict):

R1. NEVER convert a failed, blocked, partial, or indeterminate trajectory
    into a successful completion. NEVER remove a failure reason, blocker,
    failed verification, or unresolved limitation. Under the mentor
    policy, such trajectories are rejected upstream and NEVER reach this
    REWRITE step. If you find that the context does not actually support
    a success outcome, output ONLY a short neutral closing such as
    "the task outcome cannot be determined from the available context" —
    do NOT invent success wording. The caller will discard this rewrite.

R2. Evidence priority (higher wins on conflict; do NOT use lower items to
    override higher items):
      1) Tool results, test output, diff / file-change results present in
         the trajectory or extra_context;
      2) Assistant statements already made about executed results and not
         later contradicted by tool output;
      3) Facts stated in the last user message / task description;
      4) Original assistant content under rewrite;
      5) reasoning_content ONLY as an interpretation aid; it MUST NOT be
         used alone as evidence of completion.
    Plans ("I will..."), intentions ("should..."), and unverified reasoning
    are NOT evidence of completion.

R3. Truncation guard (redundant under mentor policy but kept for defence
    in depth). Any structurally truncated trajectory — HAS_TOOLCALLS,
    ENDS_NON_ASSISTANT, NO_ASSISTANT, EMPTY_CONTENT — is rejected by the
    caller BEFORE REWRITE is invoked, because R3 caps their outcome at
    `partial` and only `success` is legal. If you nonetheless receive a
    structurally truncated case, refuse to fabricate: output ONLY a short
    neutral acknowledgement in the trajectory's original language.

R4. Tool success ≠ task success. Even when tool results are visible and
    successful, only report "success" when those results clearly satisfy
    the ENTIRE user request. Any partial coverage should have been
    rejected upstream; if you nonetheless see it, refuse per R1.

R5. §三 修改五  Preserve original trajectory language. Detect the language
    from the original content (falling back to reasoning_content, then
    last user message, then extra_context). If the trajectory is Chinese,
    output Chinese. If English, output English. NEVER translate.
======================================================================

You will see:
  1. The last user message content (task background)
  2. The reasoning_content of the assistant to rewrite (may be empty; it is
     the AI's own reasoning, do NOT copy it verbatim)
  3. The original content of the assistant to rewrite (the non-summary text)

Rewriting requirements:
1. Output ONLY the rewritten content body. Do NOT output explanations, do NOT
   add a title, markdown code block, or JSON.
2. The output MUST be a **summary-style closing** that clearly states the
   task conclusion:
   - If the context shows the task is done: explain what was done / changed /
     added / removed / verified;
   - If the task failed or cannot be completed: explain why, and (if
     appropriate) give a suggestion;
   - Do NOT output pure analysis, status description, or gap analysis.
     The rewrite MUST be "what was DONE", not "what EXISTS" or
     "what is MISSING".
   - If neither the original content, reasoning_content, nor the last user
     message allows you to judge the actual task state: output ONLY a short
     neutral closing such as "unable to continue due to insufficient
     information" or "the task outcome cannot be determined from the
     available context". NEVER use "the task has been handled",
     "Completed the changes above", "Done", or any other wording that
     implies success without evidence. Do NOT invent file names, function
     names, line numbers, or command output that never appeared in the
     input.
3. Do NOT introduce new facts, new numbers, or new file names that were not
   in the original text.
4. Do NOT keep intermediate-state wording from the original content such as
   "Let me first look at", "Next I plan to", "Let me try".
5. Do NOT promise "next I will xxx" or "next step I will xxx" — this is a
   closing, not a bridge.
6. Preserve the language of the original content: if the original is English,
   output English; if the original is Chinese, output Chinese. Language
   detection is based on the original content.
7. Keep it concise, typically 1–6 sentences. Do NOT pad content just to add
   length.
8. If the original content already contains suitable concrete details (e.g.
   "modified function yyy in file xxx"), preserve those details when
   rewriting — only rewrite the overall tone and structure. Do not lose the
   original concrete information.

Output: the rewritten content body itself, with no wrapper.
"""


# ── 环境读取 ──────────────────────────────────────────────────────────


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


def anthropic_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


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
        env_file.get("FIX_MISSING_SUMMARY_MODEL"),
        env_file.get("CLEAN_LLM_MODEL"),
        env_file.get("DEFAULT_MODEL"),
    )
    return RuntimeConfig(
        api_key=api_key,
        base_url=base_url,
        anthropic_version=anthropic_version,
        model=model,
    )


# ── 消息解析 ──────────────────────────────────────────────────────────


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


def find_message_list(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    for key in ("messages", "chat_messages", "conversation", "history"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return None


def find_terminal_assistant(
    messages: list[dict[str, Any]],
) -> tuple[int | None, str, str, str]:
    """定位末节点并返回其状态，供上层分发处理。

    与旧版差异：旧版结构不通过就 `return None` 整条 session 跳过（19 条 skipped
    永远修不到）；新版永远返回一个 status 让上层决定是"in-place 判定/重写"、
    "写入空 content 节点"、还是"末尾追加"。

    返回 (index, reasoning_text, content_text, status)。
    status 取值：
      - "OK"                  末条是 assistant，有 content 且无 tool_calls，
                              走现有判定/重写路径。
      - "EMPTY_CONTENT"       末条是 assistant，无 tool_calls，但 content 为空。
                              → 生成总结写入该 assistant 的 content。
      - "HAS_TOOLCALLS"       末条是 assistant 但带 tool_calls（结构异常，
                              轨迹停在未完成的工具调用）→ 删该末 assistant 及
                              其孤悬的 tool 结果，再末尾追加新总结节点。
      - "ENDS_NON_ASSISTANT"  整条轨迹末条是 user 或 tool（末尾没有收尾
                              assistant）→ 末尾追加新总结节点。上层还需要
                              细分 ENDS_USER / ENDS_TOOL 打 warn。
      - "NO_ASSISTANT"        整条轨迹没有任何 assistant 节点（极端异常）→
                              末尾追加新总结节点。
    index：OK / EMPTY_CONTENT / HAS_TOOLCALLS 时是该 assistant 下标；
           ENDS_NON_ASSISTANT 时是最后一个 assistant（若存在）用作 reasoning 取源；
           NO_ASSISTANT 时为 None。

    reasoning_text / content_text：取自 index 指向的 assistant；不存在则为空串。
    """
    last_real_idx = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if isinstance(messages[i], dict)
        ),
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

    # 情况 E：末条根本不是 assistant（被上游删过头或轨迹停在 tool/user）
    if last_real_idx is not None and messages[last_real_idx].get("role") != "assistant":
        # 若整条轨迹一个 assistant 都没有 → NO_ASSISTANT（更极端异常）
        if last_asst_idx is None:
            return None, "", "", "NO_ASSISTANT"
        reasoning = normalize_text(messages[last_asst_idx].get("reasoning_content")).strip()
        content = normalize_text(messages[last_asst_idx].get("content")).strip()
        return last_asst_idx, reasoning, content, "ENDS_NON_ASSISTANT"

    # 情况 F：整条轨迹是空的（messages 为空或全是非 dict 项）
    if last_asst_idx is None:
        return None, "", "", "NO_ASSISTANT"

    # 末条就是 assistant
    message = messages[last_asst_idx]
    reasoning_text = normalize_text(message.get("reasoning_content")).strip()
    content_text = normalize_text(message.get("content")).strip()

    if message.get("tool_calls"):
        return last_asst_idx, reasoning_text, content_text, "HAS_TOOLCALLS"
    if not content_text:
        return last_asst_idx, reasoning_text, "", "EMPTY_CONTENT"
    return last_asst_idx, reasoning_text, content_text, "OK"


def find_last_user_content(messages: list[dict[str, Any]], before_index: int) -> str:
    for idx in range(before_index - 1, -1, -1):
        message = messages[idx]
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        text = normalize_text(message.get("content")).strip()
        if text:
            return text
    return ""


def classify_ends_non_assistant(messages: list[dict[str, Any]]) -> str:
    """§六 末节点非 assistant 时的 4 情况精细分类。

    仅在上层已判 terminal_status == "ENDS_NON_ASSISTANT" 时被调用。返回:
      - "ENDS_USER":         末条 role=user (assistant 未来得及响应)。
                             或者 role 既不是 user 也不是 tool 的兜底(罕见)。
      - "ENDS_TOOL_OK":      末条 role=tool 且前一 assistant 的所有 tool_calls
                             都有匹配的 tool_result → 完整 tool 批,只是没接上
                             assistant 收尾。
      - "ENDS_TOOL_TRUNC":   末条 role=tool 但前一 assistant 仍有未匹配的
                             tool_calls → 结果不完整,轨迹被硬截在 tool 批中。
      - "ENDS_TOOL_ORPHAN":  末条 role=tool 但它的 tool_call_id 对不上任何
                             前 assistant 的 tool_call,或者前面根本没有带
                             tool_calls 的 assistant → schema 错位 / 上游删过头。

    这 4 个子标签用于给 REWRITE 生成更精确的 hint,并在报告中区分严重程度。
    """
    if not messages:
        return "ENDS_USER"
    last = messages[-1]
    if not isinstance(last, dict):
        return "ENDS_USER"
    last_role = last.get("role")
    if last_role == "user":
        return "ENDS_USER"
    if last_role != "tool":
        # 非 user 非 tool (极罕见),走保守路径
        return "ENDS_USER"

    # 找末条 tool 之前最近的 assistant
    prev_asst_idx = next(
        (
            i
            for i in range(len(messages) - 2, -1, -1)
            if isinstance(messages[i], dict) and messages[i].get("role") == "assistant"
        ),
        None,
    )
    if prev_asst_idx is None:
        return "ENDS_TOOL_ORPHAN"  # 没有前 assistant 却有 tool result

    tool_calls = messages[prev_asst_idx].get("tool_calls") or []
    tc_ids = {
        tc.get("id")
        for tc in tool_calls
        if isinstance(tc, dict) and tc.get("id")
    }
    if not tc_ids:
        return "ENDS_TOOL_ORPHAN"  # 前 assistant 没有 tool_calls 却挂了 tool

    # 末条 tool 自身若其 tool_call_id 不在 tc_ids,直接 ORPHAN
    last_tcid = last.get("tool_call_id")
    if last_tcid and last_tcid not in tc_ids:
        return "ENDS_TOOL_ORPHAN"

    # 收集 [prev_asst_idx+1 : end] 段所有 tool 消息的 tool_call_id
    seen_ids: set[str] = set()
    for j in range(prev_asst_idx + 1, len(messages)):
        m = messages[j]
        if isinstance(m, dict) and m.get("role") == "tool":
            tcid = m.get("tool_call_id")
            if tcid:
                seen_ids.add(tcid)

    unmatched = tc_ids - seen_ids
    if unmatched:
        return "ENDS_TOOL_TRUNC"
    return "ENDS_TOOL_OK"


def is_main_trajectory(path: Path) -> bool:
    """判断是否为主轨迹。

    子 agent 文件的两种命名：
      1. 路径包含 subagents/ 目录（新版 codex 标准布局）
      2. 文件名以 agent- 开头（少数旧数据布局）

    注意：不能用 "subagents" in str(path) 粗判 —— 会误伤 subagents_backup/
    之类的路径。
    """
    parts = path.parts
    if "subagents" in parts:
        return False
    if path.name.startswith("agent-"):
        return False
    return True


def build_fallback_context(
    messages: list[dict[str, Any]],
    idx: int,
    *,
    max_messages: int = 8,
    max_chars: int = 8000,
) -> str:
    """双空场景下给 rewrite LLM 补充事实基础，避免它凭空编。

    取 idx 之前的最多 max_messages 条消息，每条截到约 1200 字，总量 <= max_chars。
    与 clean_last_assistant.build_context_text 语义一致。
    """
    if idx is None or idx <= 0:
        return ""
    blocks: list[str] = []
    total = 0
    start = max(0, idx - max_messages)
    for pos in range(start, idx):
        msg = messages[pos]
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "unknown"
        text = normalize_text(msg.get("content")).strip()
        if not text:
            text = normalize_text(msg.get("reasoning_content")).strip()
        if not text:
            continue
        if len(text) > 1200:
            text = text[:1200].rstrip() + "\n...[truncated]"
        block = f"[{pos}] {role}:\n{text}"
        if total + len(block) > max_chars:
            remain = max_chars - total
            if remain > 200:
                blocks.append(block[:remain].rstrip() + "\n...[truncated]")
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks)


# ── LLM 调用 ─────────────────────────────────────────────────────────


def strip_wrapping(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def call_check_llm(
    *,
    config: RuntimeConfig,
    last_user: str,
    reasoning_content: str,
    content: str,
    timeout: int,
    is_main_trajectory: bool = True,
) -> dict[str, Any]:
    trajectory_note = (
        "NOTE: This is a MAIN trajectory. Apply STRICT summary rules: "
        "pure analysis / findings / status is NOT summary.\n\n"
        if is_main_trajectory
        else
        "NOTE: This is a subagent trajectory. Research / analysis findings "
        "are acceptable closing content. Only deny for categories 2 "
        "(plan+question), 4 (full file dump), 6 (mid-state recap), "
        "7 (empty / truncated / pleasantries).\n\n"
    )
    user_prompt = (
        "Please decide whether [the assistant content under judgment] is "
        "summary-style closing content, and output the JSON exactly as required "
        "by the SYSTEM instructions.\n\n"
        + trajectory_note
        + "[Last user content]\n"
        f"{last_user or '(empty)'}\n\n"
        "[Assistant reasoning_content under judgment]\n"
        f"{reasoning_content or '(empty)'}\n\n"
        "[Assistant content under judgment]\n"
        f"{content}\n"
    )
    payload = {
        "model": config.model,
        "max_tokens": 512,
        "system": f"{SHARED_TERMINAL_POLICY}\n\n{CHECK_SYSTEM_PROMPT}",
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": config.api_key,
        "anthropic-version": config.anthropic_version,
        "content-type": "application/json",
    }
    response = requests.post(
        anthropic_messages_url(config.base_url),
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"LLM API 错误 {response.status_code}: {response.text[:500]}"
        )
    data = response.json()
    blocks = data.get("content", [])
    text = "\n".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    )
    text = strip_wrapping(text)

    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                parsed = None

    if not isinstance(parsed, dict):
        raise RuntimeError(f"LLM 返回不可解析: {text[:500]}")

    # ── §五 C1: 4-way content_type + 5-value outcome + legacy is_summary ──
    valid_ct = {"pure_summary", "mixed", "non_summary", "unrecoverable"}
    valid_out = {"success", "partial", "failed", "blocked", "indeterminate"}

    content_type = str(parsed.get("content_type") or "").strip()
    outcome = str(parsed.get("outcome") or "").strip()

    if content_type not in valid_ct:
        # Legacy fallback: 旧 prompt 只吐 is_summary
        if "is_summary" in parsed:
            content_type = (
                "pure_summary" if bool(parsed.get("is_summary")) else "non_summary"
            )
        else:
            raise RuntimeError(
                f"LLM 返回缺少 content_type 且无 is_summary 兜底: {text[:500]}"
            )

    if outcome not in valid_out:
        # 未提供或非法值 → 不猜,回 indeterminate,由调用方按 R3 决定是否降档
        outcome = "indeterminate"

    is_summary = content_type in ("pure_summary", "mixed")

    return {
        "content_type": content_type,
        "outcome": outcome,
        "is_summary": is_summary,
        "reason": str(parsed.get("reason") or "").strip(),
    }


def call_rewrite_llm(
    *,
    config: RuntimeConfig,
    last_user: str,
    reasoning_content: str,
    content: str,
    timeout: int,
    extra_context: str = "",
) -> str:
    context_block = (
        f"[Preceding trajectory context]\n{extra_context}\n\n"
        if extra_context
        else ""
    )
    user_prompt = (
        "Please rewrite [Original content] into a qualified summary-style "
        "closing, and output ONLY the rewritten body as required by SYSTEM.\n\n"
        + context_block
        + "[Last user content]\n"
        f"{last_user or '(empty)'}\n\n"
        "[reasoning_content]\n"
        f"{reasoning_content or '(empty)'}\n\n"
        "[Original content]\n"
        f"{content}\n"
    )
    payload = {
        "model": config.model,
        "max_tokens": 2048,
        "system": f"{SHARED_TERMINAL_POLICY}\n\n{REWRITE_SYSTEM_PROMPT}",
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": config.api_key,
        "anthropic-version": config.anthropic_version,
        "content-type": "application/json",
    }
    response = requests.post(
        anthropic_messages_url(config.base_url),
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"LLM API 错误 {response.status_code}: {response.text[:500]}"
        )
    data = response.json()
    blocks = data.get("content", [])
    text = "\n".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    )
    return strip_wrapping(text).strip()


def call_append_llm(
    *,
    config: RuntimeConfig,
    last_user: str,
    reasoning_content: str,
    extra_context: str = "",
    ends_kind: str = "",
) -> str:
    """为结构异常的轨迹生成追加用的总结性收尾。

    与 rewrite 的区别：没有"原 content"可供改写，纯粹从轨迹上下文重建收尾。
    输出要求同 REWRITE：仅返回总结正文，无 wrapper。
    """
    context_block = (
        f"[Preceding trajectory context]\n{extra_context}\n\n"
        if extra_context
        else ""
    )
    ends_note = (
        f"\n(Note: the trajectory ends abnormally — {ends_kind}. "
        f"Generate a closing that acknowledges the actual terminal state "
        f"rather than fabricating a successful completion.)"
        if ends_kind
        else ""
    )
    user_prompt = (
        "The last assistant message in this trajectory is structurally absent or "
        "incomplete (empty / trailing tool_calls / missing entirely). You must "
        "generate a summary-style closing based on the available context.\n\n"
        + context_block
        + "[Last user content]\n"
        f"{last_user or '(empty)'}\n\n"
        "[Last assistant reasoning_content]\n"
        f"{reasoning_content or '(empty)'}\n\n"
        f"{ends_note}\n"
        "Output ONLY the closing content body. Do NOT output explanations, "
        "titles, code blocks, or JSON. Follow the language of the last user "
        "content. Keep it concise (1–6 sentences). Do NOT invent file names, "
        "function names, or command output that never appeared in the input."
    )
    payload = {
        "model": config.model,
        "max_tokens": 1024,
        "system": f"{SHARED_TERMINAL_POLICY}\n\n{REWRITE_SYSTEM_PROMPT}",
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": config.api_key,
        "anthropic-version": config.anthropic_version,
        "content-type": "application/json",
    }
    response = requests.post(
        anthropic_messages_url(config.base_url),
        headers=headers,
        json=payload,
        timeout=180,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"LLM API 错误 {response.status_code}: {response.text[:500]}"
        )
    data = response.json()
    blocks = data.get("content", [])
    text = "\n".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    )
    return strip_wrapping(text).strip()


# ── 文件发现 ──────────────────────────────────────────────────────────


def discover_session_files(root: Path) -> list[Path]:
    """兼容四种布局：
    - TASK-*/sessions/**/*.json
    - flat: <task>/<task>.json  和  <task>/<task>_original.json
    - test_task/sessions/**/*.json（测试目录）
    - sessions/**/*.json（通用 sessions 目录）
    """
    files: list[Path] = []
    seen: set[Path] = set()

    for path in sorted(root.rglob("*.json")):
        if not path.is_file():
            continue
        # Mac zip 打包会带出 __MACOSX/ 目录 + ._filename 资源分叉伪文件（二进制、非 UTF-8），
        # 全部跳过，避免 utf-8 解码报错噪声。
        parts = path.parts
        if "__MACOSX" in parts or path.name.startswith("._"):
            continue
        rp = path.resolve()
        if rp in seen:
            continue

        parts = path.parts
        # 兼容 sessions 目录（含 test_task/sessions/ 等测试目录）
        if "sessions" in parts:
            files.append(path)
            seen.add(rp)
            continue

        parent = path.parent
        if parent.name.startswith("TASK-"):
            stem = path.stem
            if stem == parent.name or stem == f"{parent.name}_original":
                files.append(path)
                seen.add(rp)

    return files


# ── 差异 ──────────────────────────────────────────────────────────────


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


def update_content(message: dict[str, Any], new_text: str) -> None:
    old_content = message.get("content")
    if isinstance(old_content, list):
        message["content"] = [{"type": "text", "text": new_text}]
    else:
        message["content"] = new_text


# ── §八 合规收尾节点构造 + 追加前结构门禁 ────────────────────────────────
# 背景：本「节点 session」格式里每个非 system 节点都带 agent 与 _metadata。
#   - agent：主轨迹 "main"；子轨迹是子 agent id（如 a09ac84eed070af8c，对应
#            subagents/agent-<id>.json）。→ 新节点 agent 必须【继承本轨迹现有
#            节点】，不能硬编码 "main"（否则子轨迹收尾节点 agent 会错）。
#   - _metadata：至少含 timestamp；真实对话 assistant 还带 tokens。
# 追加的收尾节点刻意【不带 tokens、不带 reasoning_content】——这是"合成锚点"的
# 标志，也让结构层 fix_add_summary_anchor._is_summary_anchor 能识别它为总结锚点
# 从而幂等（避免结构层再补一个 → 双重收尾节点）。


def _summary_node_agent(
    messages: list[dict[str, Any]], prefer_idx: int | None = None
) -> str:
    """新收尾节点的 agent：优先取 prefer_idx 节点的 agent，其次从后往前第一个
    非空 agent，兜底 "main"。"""
    if (
        prefer_idx is not None
        and 0 <= prefer_idx < len(messages)
        and isinstance(messages[prefer_idx], dict)
    ):
        a = messages[prefer_idx].get("agent")
        if a:
            return str(a)
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("agent"):
            return str(m["agent"])
    return "main"


def _summary_node_timestamp(messages: list[dict[str, Any]]) -> str:
    """新节点 _metadata.timestamp：复用轨迹最后一个已有 timestamp（格式天然一致），
    取不到才兜底当前 UTC 时间。对齐 fix_add_summary_anchor._last_timestamp。"""
    for m in reversed(messages):
        if isinstance(m, dict):
            meta = m.get("_metadata")
            if isinstance(meta, dict):
                ts = meta.get("timestamp")
                if isinstance(ts, str) and ts:
                    return ts
    return datetime.now(timezone.utc).isoformat()


def make_summary_node(
    messages: list[dict[str, Any]],
    content: str,
    *,
    agent_prefer_idx: int | None = None,
) -> dict[str, Any]:
    """构造一个【格式合规】的收尾 assistant 节点（补齐 agent 与 _metadata）。"""
    return {
        "role": "assistant",
        "agent": _summary_node_agent(messages, agent_prefer_idx),
        "content": content,
        "_metadata": {"timestamp": _summary_node_timestamp(messages)},
    }


def can_append_assistant(messages: list[dict[str, Any]]) -> tuple[bool, str]:
    """§不破坏结构：判断当前轨迹末尾能否【合法地】接一个新的 assistant 节点。

    只有当末尾已是"配对完整、可接 assistant"的边界时才允许追加，否则宁可不
    追加（上层转 reject），也不制造新的结构违规。返回 (ok, reason)。

    合法：末条是 user；或末条是 tool 且该批 tool_calls 全部有结果（ENDS_TOOL_OK）。
    不合法：
      - 末条已是 assistant → 追加会产生连续 assistant（结构层有 consecutive
        assistant cleanup 规则）；若它还带未闭合 tool_calls 更不能追加。
      - 末条 tool 但配对被截断/孤儿（ENDS_TOOL_TRUNC / ENDS_TOOL_ORPHAN）→ 追加
        会让前面 tool_calls 永远缺结果 / 留孤儿 tool。
      - 末条是 system 或其它 → 兜底拒绝。
    """
    last = next((m for m in reversed(messages) if isinstance(m, dict)), None)
    if last is None:
        return False, "empty_messages"
    role = last.get("role")
    if role == "user":
        return True, "ends_user"
    if role == "assistant":
        if last.get("tool_calls"):
            return False, "last_assistant_has_open_tool_calls"
        return False, "would_create_consecutive_assistant"
    if role == "tool":
        kind = classify_ends_non_assistant(messages)
        if kind == "ENDS_TOOL_OK":
            return True, "ends_tool_ok"
        return False, f"tool_pairing_broken_{kind.lower()}"
    return False, f"unexpected_last_role_{role}"


def truncate_to_appendable_boundary(
    messages: list[dict[str, Any]],
) -> tuple[str, int]:
    """§九 归整到「可安全收尾」的合法边界（用户策略：截断丢弃再追加）。

    从轨迹末尾反复删除会破坏结构的节点（带未闭合 tool_calls 的 assistant、
    配对被截断/孤儿的 tool、system 等），直到末尾成为一个可以合法收尾的边界。
    每轮至少删一个节点，必然终止。

    返回 (boundary_kind, removed_count)：
      - "appendable":           末尾是 user，或末尾是完整配对的 tool 批
                                （ENDS_TOOL_OK）→ 调用方【追加】新的总结节点。
      - "rewritable_assistant": 末尾是一条无 tool_calls 的 assistant → 调用方
                                【改写】它的 content 当收尾（不追加，避免连续
                                assistant）。
      - "empty":                删空了，无内容可总结 → 调用方按 error 处理。
    """
    removed = 0
    while True:
        last = next((m for m in reversed(messages) if isinstance(m, dict)), None)
        if last is None:
            return "empty", removed
        ok, _why = can_append_assistant(messages)
        if ok:
            return "appendable", removed
        if last.get("role") == "assistant" and not last.get("tool_calls"):
            return "rewritable_assistant", removed
        # 末尾是破坏结构的节点：删掉它（带 tool_calls 的 assistant 连同其孤悬 tool）
        idx = max(i for i in range(len(messages)) if isinstance(messages[i], dict))
        if messages[idx].get("role") == "assistant" and messages[idx].get("tool_calls"):
            _prune_tool_calls_after(messages, idx)
        del messages[idx]
        removed += 1


# ── 报告路径 ──────────────────────────────────────────────────────────


def resolve_report_paths(report_arg: str | None, root: Path, timestamp: str) -> tuple[Path, Path]:
    if report_arg is None:
        base = root / "report" / f"{SCRIPT_NAME}_{timestamp}.json"
        md = base.with_suffix(".md")
        return base, md

    p = Path(report_arg).expanduser().resolve()
    if p.suffix == ".json":
        stem = p.stem
        if not stem.endswith(timestamp):
            base = p.with_name(f"{stem}_{timestamp}.json")
        else:
            base = p
        md = base.with_suffix(".md")
        return base, md
    if p.suffix == ".md":
        stem = p.stem
        if not stem.endswith(timestamp):
            md = p.with_name(f"{stem}_{timestamp}.md")
        else:
            md = p
        base = md.with_suffix(".json")
        return base, md
    return (
        p / f"{SCRIPT_NAME}_{timestamp}.json",
        p / f"{SCRIPT_NAME}_{timestamp}.md",
    )


# ── 单文件处理 ────────────────────────────────────────────────────────


def _prune_tool_calls_after(messages: list[dict[str, Any]], asst_idx: int) -> int:
    """删末尾悬空 tool_call 的兜底清理：末 assistant 带 tool_calls 时，紧跟的
    tool 结果如果 tool_call_id 匹配这些 tc 也一起删；避免"删了末 assistant 但
    留下孤儿 tool 结果"。

    返回被额外删除的 tool 结果条数（用于报告与日志）。
    """
    asst = messages[asst_idx]
    tool_calls = asst.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return 0
    target_ids = {
        tc.get("id") for tc in tool_calls if isinstance(tc, dict) and tc.get("id")
    }
    j = asst_idx + 1
    removed = 0
    while (
        j < len(messages)
        and isinstance(messages[j], dict)
        and messages[j].get("role") == "tool"
        and messages[j].get("tool_call_id") in target_ids
    ):
        del messages[j]
        removed += 1
    return removed


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def finalize_summary_by_truncation(
    *,
    messages: list[dict[str, Any]],
    data: Any,
    result: dict[str, Any],
    config: RuntimeConfig,
    reasoning_text: str,
    ends_kind: str,
    out_path: Path,
) -> bool:
    """§九 归整→生成→追加新节点或改写末尾 assistant→写文件→填 result。

    统一处理三个结构异常分支（HAS_TOOLCALLS / ENDS_NON_ASSISTANT / NO_ASSISTANT）。
    用户策略：截断破损尾巴到合法边界，保证每条轨迹末尾都是【总结性 assistant】，
    且不残留 tool 配对违规（满足 check_final_assistant_main + check_tools_call_result）。
    成功返回 True（status 由调用方贴分支标签），失败已在 result 里写 error 返回 False。
    """
    boundary, removed = truncate_to_appendable_boundary(messages)
    result["truncated_removed"] = removed
    result["boundary_kind"] = boundary
    if boundary == "empty":
        result["status"] = "error"
        result["action"] = "reject"
        result["reject_reason"] = "truncated_to_empty"
        result["check_reason"] = f"{ends_kind}: 截断后无任何节点可总结（removed={removed}）"
        return False

    last_user = find_last_user_content(messages, len(messages))
    extra_context = build_fallback_context(messages, len(messages))
    try:
        if boundary == "rewritable_assistant":
            idx = max(i for i in range(len(messages)) if isinstance(messages[i], dict))
            orig = normalize_text(messages[idx].get("content")).strip()
            orig_reasoning = (
                normalize_text(messages[idx].get("reasoning_content")).strip()
                or reasoning_text
            )
            generated = call_rewrite_llm(
                config=config,
                last_user=last_user,
                reasoning_content=orig_reasoning,
                content=orig,
                timeout=180,
                extra_context=extra_context,
            )
        else:
            generated = call_append_llm(
                config=config,
                last_user=last_user,
                reasoning_content=reasoning_text,
                extra_context=extra_context,
                ends_kind=ends_kind,
            )
    except Exception as exc:
        result["status"] = "error"
        result["action"] = "reject"
        result["reject_reason"] = f"finalize_error_{ends_kind.lower()}"
        result["check_reason"] = f"{ends_kind}: 截断 {removed} 后生成总结出错: {exc}"
        return False

    if not generated:
        result["status"] = "error"
        result["action"] = "reject"
        result["reject_reason"] = f"empty_summary_{ends_kind.lower()}"
        result["check_reason"] = f"{ends_kind}: 截断 {removed} 后生成的总结为空"
        return False

    if boundary == "rewritable_assistant":
        idx = max(i for i in range(len(messages)) if isinstance(messages[i], dict))
        before = normalize_text(messages[idx].get("content")).strip()
        update_content(messages[idx], generated)
        result["action"] = "rewrite"
        result["message_index"] = idx
        result["before"] = before or "(empty)"
        placed = "改写末尾 assistant"
    else:
        messages.append(make_summary_node(messages, generated))
        result["action"] = "append"
        result["before"] = "(截断破损尾巴)"
        placed = "追加总结节点"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)

    result["after"] = generated
    result["added_lines"] = generated.splitlines()
    result["check_reason"] = f"截断 {removed} 个破损尾节点；{placed}（boundary={boundary}）"
    return True


def process_one_file(
    *,
    path: Path,
    root: Path,
    config: RuntimeConfig,
    output_dir: Path | None,
) -> dict[str, Any]:
    """§七 单文件六步流水线 (single-source-of-truth 步骤命名).

    Step 1  Load          — 打开 session JSON,提取 messages 列表。
    Step 2  Locate        — find_terminal_assistant 定位末节点并给出 5 态
                            terminal_status (OK / EMPTY_CONTENT / HAS_TOOLCALLS /
                            ENDS_NON_ASSISTANT / NO_ASSISTANT)。
    Step 3  Classify      — 仅 OK 分支调用 CHECK LLM,吐 4-way content_type +
                            5-value outcome (§五 C1)。其余 4 个 branch 直接
                            bypass CHECK,因为末节点已残缺,判"是不是总结"没意义。
    Step 4  Decide        — §五 C2 固定映射:
                              pure_summary  → unchanged (no LLM)
                              mixed         → clean   (REWRITE with trim hint)
                              non_summary   → rewrite (REWRITE from context)
                              unrecoverable → reject  (NO LLM, NO write)
                            其余 4 个 terminal branch 各自内嵌 hint 走 REWRITE。
    Step 5  Execute       — REWRITE LLM 生成新 content;或 append 新末 assistant;
                            或 reject 什么都不写;并对 HAS_TOOLCALLS 分支清理
                            孤悬 tool 结果。
    Step 6  Persist       — 写回 session JSON (in-place 或 --output),把
                            content_type / outcome / action / ends_kind 等填入
                            result[] 交给报告 writer 汇总。
    """
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)

    result: dict[str, Any] = {
        "file": rel,
        "session_id": path.stem,
        "status": "pending",
        "check_reason": "",
        "message_index": None,
        # §五 C1 (classifier output; OK 分支才会填)
        "content_type": "",   # pure_summary | mixed | non_summary | unrecoverable
        "outcome": "",        # success | partial | failed | blocked | indeterminate
        # §五 C2 (cleaner dispatch)
        "action": "",         # unchanged | clean | rewrite | reject
        # mentor 政策: reject 通路的细分原因,便于事后核账 (见 reject_reason_dist)
        "reject_reason": "",  # unrecoverable | outcome_* | structural_truncation_*
        "before": "",
        "after": "",
        "added_lines": [],
        "deleted_lines": [],
    }

    print(f"▶ 处理: {rel}")

    # ── §七 Step 1: Load session JSON ────────────────────────────────
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        print(f"  ✗ JSON 解析失败: {rel}: {exc}")
        result["status"] = "error"
        result["check_reason"] = f"invalid json: {exc}"
        return result

    messages = find_message_list(data)
    if messages is None:
        print(f"  → 无 messages 列表，跳过: {rel}")
        result["status"] = "skipped"
        result["check_reason"] = "no messages array"
        return result

    # ── §七 Step 2: Locate terminal node (find_terminal_assistant) ───
    idx, reasoning_text, content_text, terminal_status = find_terminal_assistant(messages)
    is_main = is_main_trajectory(path)
    result["message_index"] = idx
    result["terminal_status"] = terminal_status
    result["is_main_trajectory"] = is_main

    # 决定输出路径 —— 追加/写入前都用它
    def _out_path() -> Path:
        if output_dir:
            p = output_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        return path

    # ── §七 Step 3-6: 按 terminal_status 分派 (Classify / Decide / Execute / Persist) ──
    # ── 情况 1：结构正常（OK）── 走原有 check → rewrite 判定链
    if terminal_status == "OK":
        last_user = find_last_user_content(messages, idx)
        # ── §七 Step 3: Classify (CHECK LLM) ──
        try:
            verdict = call_check_llm(
                config=config,
                last_user=last_user,
                reasoning_content=reasoning_text,
                content=content_text,
                timeout=120,
                is_main_trajectory=is_main,
            )
        except Exception as exc:
            print(f"  ✗ 检测 LLM 错误: {rel}: {exc}")
            result["status"] = "error"
            result["check_reason"] = str(exc)
            return result

        # ── §七 Step 4: Decide action (§五 C2 content_type + outcome → action 映射) ──
        result["check_reason"] = verdict["reason"]
        result["content_type"] = verdict["content_type"]
        result["outcome"] = verdict["outcome"]

        content_type = verdict["content_type"]
        outcome = verdict["outcome"]

        # ── mentor 政策：outcome != success 一律 reject（在 content_type 之前拦截） ──
        # 除 unrecoverable 已经独立走 reject 外,其它三档 content_type 遇上非 success
        # 都塌缩到 reject 通路,严禁伪造 success 收尾。
        if content_type != "unrecoverable" and outcome != "success":
            print(
                f"  ⚠ REJECT(non_success outcome={outcome}/content_type={content_type}): "
                f"{rel}  reason={verdict['reason']}"
            )
            result["action"] = "reject"
            result["reject_reason"] = f"outcome_{outcome or 'unknown'}"
            result["status"] = "rejected"
            return result

        # ── §五 C2 Cleaner 4-way action dispatch (content_type × outcome=success) ──
        if content_type == "pure_summary":
            print(f"  ✓ 已是总结(pure_summary/outcome=success): {rel}")
            result["action"] = "unchanged"
            result["status"] = "already_summary"
            return result

        if content_type == "unrecoverable":
            # §五 C2 reject 通路: 严禁伪造完成结论
            # 不追加、不改写、不写文件,仅登记 reason
            print(
                f"  ⚠ REJECT(unrecoverable/outcome={verdict['outcome']}): {rel}  "
                f"reason={verdict['reason']}"
            )
            result["action"] = "reject"
            result["reject_reason"] = "unrecoverable"
            result["status"] = "rejected"
            # before/after 保持空,防止下游误认为改过
            return result

        # ── non_summary 走 REWRITE；mixed 留给下游 clean_last_assistant ──
        if content_type == "mixed":
            # mixed + outcome=success: 有合法总结但混有杂质 → 不在此重写,留给下游语义清洗
            print(f"  → mixed, 留给下游 clean: {rel}  outcome=success")
            result["action"] = "clean"
            result["status"] = "mixed_deferred"
            return result

        # non_summary + outcome=success: 无总结但任务确实完成 → REWRITE 从上下文重建
        # rewrite_hint 提示 REWRITE LLM: 上游 CHECK 已判 outcome=success,请生成 success 收尾
        rewrite_hint = (
            "\n\n(Caller note: upstream CHECK verdict is outcome=success. "
            "Generate a success-style closing that faithfully reports what "
            "was done / changed / verified per R2 evidence rules. Do NOT "
            "fabricate details beyond what the preceding context supports.)"
        )
        try:
            rewritten = call_rewrite_llm(
                config=config,
                last_user=last_user,
                reasoning_content=reasoning_text,
                content=content_text + rewrite_hint,
                timeout=180,
            )
        except Exception as exc:
            print(f"  ✗ 改写 LLM 错误: {rel}: {exc}")
            result["status"] = "error"
            result["check_reason"] = f"{result['check_reason']} | rewrite error: {exc}"
            result["action"] = "rewrite"
            return result

        result["action"] = "rewrite"

        if not rewritten:
            print(f"  ✗ 改写结果为空: {rel}")
            result["status"] = "error"
            result["check_reason"] = f"{result['check_reason']} | rewrite returned empty"
            return result

        if rewritten.strip() == content_text.strip():
            print(f"  ○ 改写后无变化: {rel}")
            result["status"] = "unchanged"
            return result

        diff = compute_diff(content_text, rewritten)
        result["before"] = content_text
        result["after"] = rewritten
        result["added_lines"] = diff["added"]
        result["deleted_lines"] = diff["deleted"]

        update_content(messages[idx], rewritten)
        with _out_path().open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

        print(f"  ✓ 已改写: {rel}  (+{len(diff['added'])} / -{len(diff['deleted'])} 行)")
        result["status"] = "modified"
        return result

    # ── 情况 2：末 assistant 存在但 content 空（EMPTY_CONTENT）── 生成总结写入
    if terminal_status == "EMPTY_CONTENT":
        last_user = find_last_user_content(messages, idx)
        extra_context = build_fallback_context(messages, idx)
        try:
            generated = call_append_llm(
                config=config,
                last_user=last_user,
                reasoning_content=reasoning_text,
                extra_context=extra_context,
                ends_kind="EMPTY_CONTENT",
            )
        except Exception as exc:
            print(f"  ✗ 生成总结 LLM 错误: {rel}: {exc}")
            result["status"] = "error"
            result["check_reason"] = f"EMPTY_CONTENT append error: {exc}"
            result["action"] = "reject"
            result["reject_reason"] = "structural_truncation_empty_content"
            return result

        if not generated:
            print(f"  ✗ 生成总结为空: {rel}")
            result["status"] = "error"
            result["check_reason"] = "EMPTY_CONTENT: generated summary is empty"
            result["action"] = "reject"
            result["reject_reason"] = "structural_truncation_empty_content"
            return result

        update_content(messages[idx], generated)
        with _out_path().open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

        result["action"] = "append"
        result["status"] = "appended_empty"
        result["before"] = "(empty content)"
        result["after"] = generated
        result["added_lines"] = generated.splitlines()
        print(f"  ✓ EMPTY_CONTENT → 已生成总结: {rel}")
        return result

    # ── 情况 3：末 assistant 带 tool_calls（HAS_TOOLCALLS）── 截断破损尾巴后收尾
    if terminal_status == "HAS_TOOLCALLS":
        ok2 = finalize_summary_by_truncation(
            messages=messages,
            data=data,
            result=result,
            config=config,
            reasoning_text=reasoning_text,
            ends_kind="HAS_TOOLCALLS",
            out_path=_out_path(),
        )
        if ok2:
            result["status"] = "appended_toolcalls"
            print(
                f"  ✓ HAS_TOOLCALLS → 截断{result.get('truncated_removed', 0)}并收尾"
                f"({result.get('action')}): {rel}"
            )
        else:
            print(f"  ⚠ HAS_TOOLCALLS 未能收尾: {rel} ({result.get('reject_reason')})")
        return result

    # ── 情况 4：整条末条是 user 或 tool（ENDS_NON_ASSISTANT）── 截断破损尾巴后收尾
    if terminal_status == "ENDS_NON_ASSISTANT":
        ends_kind = classify_ends_non_assistant(messages)
        result["ends_kind"] = ends_kind
        ok2 = finalize_summary_by_truncation(
            messages=messages,
            data=data,
            result=result,
            config=config,
            reasoning_text=reasoning_text,
            ends_kind=ends_kind,
            out_path=_out_path(),
        )
        if ok2:
            result["status"] = "appended_ends_non_asst"
            print(
                f"  ✓ ENDS_NON_ASSISTANT/{ends_kind} → 截断{result.get('truncated_removed', 0)}并收尾"
                f"({result.get('action')}): {rel}"
            )
        else:
            print(
                f"  ⚠ ENDS_NON_ASSISTANT/{ends_kind} 未能收尾: {rel} "
                f"({result.get('reject_reason')})"
            )
        return result

    # ── 情况 5：整条轨迹没有任何 assistant（NO_ASSISTANT）── 截断破损尾巴后收尾
    if terminal_status == "NO_ASSISTANT":
        ok2 = finalize_summary_by_truncation(
            messages=messages,
            data=data,
            result=result,
            config=config,
            reasoning_text="",
            ends_kind="NO_ASSISTANT",
            out_path=_out_path(),
        )
        if ok2:
            result["status"] = "appended_no_asst"
            print(
                f"  ✓ NO_ASSISTANT → 截断{result.get('truncated_removed', 0)}并收尾"
                f"({result.get('action')}): {rel}"
            )
        else:
            print(f"  ⚠ NO_ASSISTANT 未能收尾: {rel} ({result.get('reject_reason')})")
        return result

    # 保险：未识别的 status
    print(f"  ✗ 未识别的终态 status={terminal_status}: {rel}")
    result["status"] = "error"
    result["check_reason"] = f"unrecognized terminal status: {terminal_status}"
    return result


# ── CLI ──────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检测并修复轨迹最后一条 assistant content 的非总结性内容"
    )
    parser.add_argument(
        "-r", "--root", required=True, help="递归查找的根目录，例如 TASK-12 或 /path/to/root"
    )
    parser.add_argument(
        "-i", "--input", nargs="+",
        help="指定输入文件（可选，可多个；不指定则从 -r 递归发现）",
    )
    parser.add_argument(
        "-o", "--output",
        help="输出目录（可选，不指定则原地覆盖并整体备份到 .tmp/）",
    )
    parser.add_argument(
        "-report",
        help=(
            "报告输出路径，目录或文件"
            f"（可选，默认 {{root}}/report/{SCRIPT_NAME}_<ts>.json）"
        ),
    )
    parser.add_argument(
        "-w", "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"并发线程数（默认 {DEFAULT_WORKERS}）",
    )
    return parser


# ── 报告写入 ──────────────────────────────────────────────────────────


def write_markdown_report(md_path: Path, results: list[dict[str, Any]], summary: dict[str, int]) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    appended_total = summary.get("appended", 0)
    ct_dist = summary.get('content_type_dist') or {}
    out_dist = summary.get('outcome_dist') or {}
    act_dist = summary.get('action_dist') or {}
    ends_dist = summary.get('ends_kind_dist') or {}
    lines: list[str] = [
        f"# {SCRIPT_NAME} 报告",
        "",
        f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 扫描 session 数: **{summary['files_scanned']}**",
        f"- 已修改(in-place): **{summary['modified']}**",
        f"- 已追加(新末节点): **{appended_total}**",
        f"    - content为空生成: {summary.get('appended_empty', 0)}",
        f"    - 删末assistant(tool_calls)后追加: {summary.get('appended_toolcalls', 0)}",
        f"    - 末条非assistant后追加: {summary.get('appended_ends_non_asst', 0)}",
        f"    - 无assistant追加: {summary.get('appended_no_asst', 0)}",
        f"- 已 REJECT (unrecoverable, §五 C2 不改写): **{summary.get('rejected', 0)}**",
        f"- 主/子轨迹: main={summary.get('main_trajectory', 0)} / sub={summary.get('subagent_trajectory', 0)}",
        f"- 已是总结（未改）: {summary['already_summary']}",
        f"- 改写后无变化: {summary['unchanged']}",
        f"- 跳过: {summary['skipped']}",
        f"- 错误: {summary['errors']}",
        "",
        "**§五 C1 content_type 分布**: "
        + " / ".join(f"{k}=`{v}`" for k, v in ct_dist.items()),
        "**§五 C1 outcome 分布**: "
        + " / ".join(f"{k}=`{v}`" for k, v in out_dist.items()),
        "**§五 C2 action 分布**: "
        + " / ".join(f"{k}=`{v}`" for k, v in act_dist.items()),
        "**§六 ends_kind 分布**: "
        + " / ".join(f"{k}=`{v}`" for k, v in ends_dist.items()),
        "",
        "## 修改明细（session 粒度）",
        "",
    ]
    _include_statuses = {
        "modified",
        "appended_empty", "appended_toolcalls",
        "appended_ends_non_asst", "appended_no_asst",
        "rejected",
    }
    modified = [r for r in results if r.get("status") in _include_statuses]
    for i, r in enumerate(modified, 1):
        status_tag = r.get("status", "")
        lines.extend(
            [
                f"### {i}. `{r['file']}`  [{status_tag}]",
                f"- session_id: `{r.get('session_id')}`",
                f"- message_index: {r.get('message_index')}",
                f"- terminal_status: {r.get('terminal_status')}",
                f"- is_main_trajectory: {r.get('is_main_trajectory')}",
            ]
        )
        # §五 C1/C2 分类字段(不为空时才展示)
        parts: list[str] = []
        if r.get("content_type"):
            parts.append(f"content_type=`{r['content_type']}`")
        if r.get("outcome"):
            parts.append(f"outcome=`{r['outcome']}`")
        if r.get("action"):
            parts.append(f"action=`{r['action']}`")
        if r.get("ends_kind"):
            parts.append(f"ends_kind=`{r['ends_kind']}`")
        if parts:
            lines.append("- " + " / ".join(parts))
        lines.append(f"- 检测/处理原因: {r.get('check_reason')}")
        lines.extend(
            [
                "",
                "**修改前:**",
                "",
                "```",
                r.get("before", ""),
                "```",
                "",
                "**修改后:**",
                "",
                "```",
                r.get("after", ""),
                "```",
                "",
                f"**新增行 ({len(r.get('added_lines', []))}):**",
                "",
            ]
        )
        added_lines = r.get("added_lines") or []
        if added_lines:
            lines.append("```")
            lines.extend(added_lines)
            lines.append("```")
        else:
            lines.append("_（无）_")
        lines.append("")
        lines.append(f"**删除行 ({len(r.get('deleted_lines', []))}):**")
        lines.append("")
        deleted_lines = r.get("deleted_lines") or []
        if deleted_lines:
            lines.append("```")
            lines.extend(deleted_lines)
            lines.append("```")
        else:
            lines.append("_（无）_")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")


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
        print(f"在 {root} 下未找到符合条件的 session 文件")
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path, md_path = resolve_report_paths(args.report, root, timestamp)
    output_dir = Path(args.output).expanduser().resolve() if args.output else None
    workers = max(1, args.workers)

    print(f"找到 {len(session_files)} 个 session，并发线程数: {workers}")
    print(f"模型: {config.model}")
    print(f"报告(JSON): {json_path}")
    print(f"报告(MD):   {md_path}")
    if output_dir:
        print(f"输出目录: {output_dir}")
    else:
        print("原地写回模式：直接写回原文件（不创建备份）")

    started = time.time()
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
                    rel = str(fpath)
                print(f"✗ 处理异常: {rel}: {exc}")
                results.append(
                    {
                        "file": rel,
                        "session_id": fpath.stem,
                        "status": "error",
                        "check_reason": str(exc),
                        "message_index": None,
                        "before": "",
                        "after": "",
                        "added_lines": [],
                        "deleted_lines": [],
                    }
                )

    def _dist(field: str, values: list[str]) -> dict[str, int]:
        return {v: sum(1 for r in results if r.get(field) == v) for v in values}

    summary = {
        "files_scanned": len(session_files),
        "modified": sum(1 for r in results if r.get("status") == "modified"),
        "appended": sum(1 for r in results if r.get("status") in {
            "appended_empty", "appended_toolcalls",
            "appended_ends_non_asst", "appended_no_asst",
        }),
        "appended_empty": sum(1 for r in results if r.get("status") == "appended_empty"),
        "appended_toolcalls": sum(1 for r in results if r.get("status") == "appended_toolcalls"),
        "appended_ends_non_asst": sum(1 for r in results if r.get("status") == "appended_ends_non_asst"),
        "appended_no_asst": sum(1 for r in results if r.get("status") == "appended_no_asst"),
        "already_summary": sum(1 for r in results if r.get("status") == "already_summary"),
        "unchanged": sum(1 for r in results if r.get("status") == "unchanged"),
        "rejected": sum(1 for r in results if r.get("status") == "rejected"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "main_trajectory": sum(1 for r in results if r.get("is_main_trajectory") is True),
        "subagent_trajectory": sum(1 for r in results if r.get("is_main_trajectory") is False),
        # §五 C1 分类分布 (OK 分支才产生非空值)
        "content_type_dist": _dist(
            "content_type", ["pure_summary", "mixed", "non_summary", "unrecoverable"]
        ),
        "outcome_dist": _dist(
            "outcome", ["success", "partial", "failed", "blocked", "indeterminate"]
        ),
        # §五 C2 action 分布
        "action_dist": _dist(
            "action", ["unchanged", "clean", "rewrite", "reject", "append"]
        ),
        # §六 ENDS_NON_ASSISTANT 子标签分布 (只有 ENDS_NON_ASSISTANT 分支产生)
        "ends_kind_dist": _dist(
            "ends_kind", ["ENDS_USER", "ENDS_TOOL_OK", "ENDS_TOOL_TRUNC", "ENDS_TOOL_ORPHAN"]
        ),
        # reject 原因细分,便于事后核账（动态收集实际出现的 reason）
        "reject_reason_dist": _dist(
            "reject_reason",
            sorted({r.get("reject_reason") for r in results if r.get("reject_reason")}),
        ),
    }

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": config.model,
        "summary": summary,
        "results": results,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    write_markdown_report(md_path, results, summary)

    elapsed = time.time() - started
    total = summary["files_scanned"] or 1
    reject_pct = 100.0 * summary["rejected"] / total
    print("\n" + "=" * 50)
    print(f"修复完成 ({elapsed:.1f}s):")
    print(f"  ✓ 已修改(in-place): {summary['modified']}")
    print(f"  ✓ 已追加(总):      {summary['appended']}")
    if summary['appended']:
        print(f"    · content为空生成:  {summary.get('appended_empty', 0)}")
        print(f"    · 删tool_calls后追加: {summary.get('appended_toolcalls', 0)}")
        print(f"    · 末条非assistant追加: {summary.get('appended_ends_non_asst', 0)}")
        print(f"    · 无assistant追加:  {summary.get('appended_no_asst', 0)}")
    print(f"  ⚠ REJECT (mentor 政策 only-success): {summary['rejected']}  "
          f"({reject_pct:.1f}% of {summary['files_scanned']})")
    print(f"  ✓ 已是总结:        {summary['already_summary']}")
    print(f"  ○ 改写无变化:      {summary['unchanged']}")
    print(f"  → 跳过:            {summary['skipped']}")
    print(f"  ✗ 错误:            {summary['errors']}")
    print(f"  轨迹类型:          main={summary['main_trajectory']} sub={summary['subagent_trajectory']}")
    print(f"  共扫描:            {summary['files_scanned']}")
    print(
        "  content_type 分布: "
        + " ".join(f"{k}={v}" for k, v in summary['content_type_dist'].items())
    )
    print(
        "  outcome 分布:      "
        + " ".join(f"{k}={v}" for k, v in summary['outcome_dist'].items())
    )
    print(
        "  action 分布:       "
        + " ".join(f"{k}={v}" for k, v in summary['action_dist'].items())
    )
    print(
        "  ends_kind 分布:    "
        + " ".join(f"{k}={v}" for k, v in summary['ends_kind_dist'].items())
    )
    print("  reject_reason 分布:")
    for k, v in summary["reject_reason_dist"].items():
        if v:
            print(f"    · {k}: {v}")
    if reject_pct >= 50:
        print(
            f"  ⚠⚠ REJECT 比例 {reject_pct:.1f}% 已过半 —— "
            f"确认这是否是 mentor 政策预期的筛选强度"
        )
    print(f"  报告(JSON): {json_path}")
    print(f"  报告(MD):   {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

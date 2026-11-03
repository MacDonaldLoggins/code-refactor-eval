#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ua_detect_and_repair —— user-assistant 对应/幻觉：检测 + 阶梯修复（一体，可编排进流水线）。

CLI（支持检测+修复，以及仅检测模式）：
  -r       路径（含项目子目录的根，如 50-TASK）  [必填]
  -i       指定文件（可选；只处理这一个 session.json）
  -o       输出路径（可选；修复后的 session 副本写到这里；缺省 = 原地改写）
  -report  模版报告落盘目录（可选；覆盖默认。缺省时按 TASK 各写到 <task_dir>/report/<脚本名>.json）
  --detect-only  仅检测并生成报告，不修改或复制 session

并发（无需 CLI 参数，内部处理；可用环境变量微调，便于流水线）：
  - task 级并发：多个 session 并行  (UA_TASK_WORKERS, 默认 8)
  - 内部 LLM 并发：单个 session 内多回合并行判/修  (UA_ROUND_WORKERS, 默认 4)
  - 全局 LLM 信号量：真正在飞的 LLM 调用总数封顶  (UA_LLM_CONCURRENCY, 默认 6)
    → task×round 逻辑并行不受限，但实际打到端点的并发被这个数兜住，避免高并发报错。

检测：逐 user→assistant 回合，只检查"下一个 user 之上的那个 assistant（回合终答）"；
四类 off_topic/contradicts_evidence/fabricated/incoherent。（回合内的中间工具调用节点不在本脚本职责内）
修复（铁律：不能伪造，只能"向真"不能"向成功"）：只改命中的 assistant 文本，升级阶梯——
  ① 局部删除（首选，只删假句、其余逐字保留，带 deletion-only 护栏）
  ② 真实降级（删会删空/留误导头时，把假结论改成严格基于证据的如实陈述）
  ③ 排除判定（不可修复时保留原 session 路径并记录报告；有完整前序回合时可丢末轮）
每档改完用同一检测器重判，必须转 ok 才采纳（自动挡住"修复引入新伪造"）。

不改动仓库既有 checker，仅复用其 model_config。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---- 复用既有 model_config（不改动既有 checker）----
_CHECK_DIR = Path(__file__).resolve().parents[1] / "structure_check_script"
if str(_CHECK_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECK_DIR))
from model_config import resolve_model_config  # noqa: E402

SCRIPT_STEM = Path(__file__).stem  # 报告名用脚本名
PROJECT_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

MODEL_TIMEOUT_SECONDS = 120
HARD_ROUND_TIMEOUT = 300
JUDGE_RETRIES = 2
REPAIR_CANDIDATE_ATTEMPTS = int(os.getenv("UA_REPAIR_ATTEMPTS", "2"))

# 并发（环境变量可调，无需 CLI 参数）
TASK_WORKERS = int(os.getenv("UA_TASK_WORKERS", "8"))        # task 级：并行处理多少个 session
ROUND_WORKERS = int(os.getenv("UA_ROUND_WORKERS", "4"))      # 内部：单 session 内并行判/修多少回合
LLM_CONCURRENCY = int(os.getenv("UA_LLM_CONCURRENCY", "6"))  # 全局：真正在飞的 LLM 调用总数封顶
LLM_SEM = threading.Semaphore(LLM_CONCURRENCY)

MAX_USER_CHARS = 4000
MAX_FINAL_CHARS = 5000
TASK_PROMPT_CHARS = 1800
HISTORY_ROUNDS = 3
HIST_USER_CHARS = 220
HIST_FINAL_CHARS = 340
TOOL_RESULT_CHARS = 800
EARLY_RESULT_CHARS = 110
TOOL_TAIL_DETAIL = 18
TOOL_ACTIVITY_BUDGET = 14000
# 修复视角：给删除/降级 agent 更宽的证据窗口，让它看到关键原始 Read/Bash 结果。
# 检测视角（上面几个常量）保持不变，避免影响判定与复判的一致性。
REPAIR_TOOL_RESULT_CHARS = 2400
REPAIR_EARLY_RESULT_CHARS = 600
REPAIR_TOOL_TAIL_DETAIL = 30
REPAIR_TOOL_ACTIVITY_BUDGET = 26000
PREVIEW_CHARS = 240


def load_project_env(path: Path = PROJECT_ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def first_non_empty(*values: str | None) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def resolve_project_model_config():
    env_file = load_project_env()
    configured_channel = first_non_empty(
        env_file.get("MODEL_CHANNEL"),
        env_file.get("OPENAI_MODEL_CHANNEL"),
    ).lower()
    profile = first_non_empty(env_file.get("CLAUDE_OPENAI_MODEL_PROFILE")).lower()
    use_longcat = configured_channel == "longcat" or profile == "longcat"

    if use_longcat:
        channel = "longcat"
        model = first_non_empty(env_file.get("M_MODEL_NAME"), env_file.get("LONGCAT_MODEL"))
        base_url = first_non_empty(
            env_file.get("M_MODEL_BASE_URL"), env_file.get("LONGCAT_BASE_URL")
        )
        api_key = first_non_empty(
            env_file.get("M_MODEL_API_KEY"), env_file.get("LONGCAT_API_KEY")
        )
    else:
        channel = "openai"
        model = first_non_empty(env_file.get("MODEL_NAME"), env_file.get("OPENAI_MODEL"))
        base_url = first_non_empty(
            env_file.get("MODEL_BASE_URL"),
            env_file.get("OPENAI_BASE_URL"),
            env_file.get("OPENAI_API_BASE"),
        )
        api_key = first_non_empty(env_file.get("MODEL_API_KEY"), env_file.get("OPENAI_API_KEY"))

    missing = [
        name
        for name, value in (
            ("MODEL_NAME", model),
            ("MODEL_BASE_URL", base_url),
            ("MODEL_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"{PROJECT_ENV_PATH} 缺少模型配置: " + ", ".join(missing)
        )

    return resolve_model_config(
        channel=channel,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout=MODEL_TIMEOUT_SECONDS,
    )

# ===================== 检测提示词（四类聚焦） =====================
SYSTEM_PROMPT = """你要核查 Claude Code 主轨迹里，某一轮 assistant 的**最终回答（ASSISTANT_FINAL）**，
是否**对得上** CURRENT_USER 这一轮的诉求；对不上（含 assistant 出现幻觉导致对不上）就命中。

给你的情景：
- TASK：会话最初任务/需求（确认项目身份与目标）。
- HISTORY：之前几轮简要（可证明某些工作是**前几轮**做的）。
- CURRENT_USER：本轮用户诉求（判"对没对上"的基准）。
- TOOL_ACTIVITY：本轮 assistant 实际调用的工具及其**返回结果**（核对真伪的证据）。
- ASSISTANT_FINAL：本轮最终自然语言回答（评判对象）。

核心问题：ASSISTANT_FINAL 有没有**回应 CURRENT_USER 想要的东西**、且没有为此**编造 / 说错**？
对得上、如实 → ok；对不上 → hallucination 并给类型；信息不足 → uncertain。

【命中类型 mismatch_type（四选一）】
- off_topic            ：答非所问——终答在讲一个**与 CURRENT_USER 诉求无关的另一件事**，没回应用户要的。
- contradicts_evidence ：终答与 **CURRENT_USER 贴出的内容** 或 **本轮可见工具结果** **直接矛盾**（幻觉·睁眼说瞎话）。
                         例：工具输出/用户贴的日志显示测试失败、有报错，却说"全部通过/没问题"；读取显示某字段仍缺，却说"已全部补上"。
- fabricated           ：凭空**编造**——终答声称的具体结果/事物，在 CURRENT_USER、HISTORY、TOOL_ACTIVITY 里
                         **都没有、也无法由前文推出**，导致答的不是实情（如冒出本会话从未出现的项目/功能/接口）。
- incoherent           ：语无伦次 / 乱码 / 串话，或在讲与本会话完全无关的项目。

【两条防误报纪律——务必守住，宁漏勿误报】
1. **前几轮 / 自己写过的，不算编造。** 终答说"已完成/已修改/已通过"，即使本轮工具只读或没那个动作——这些活
   很可能在**前几轮或更早会话**干的，本轮只是总结；HISTORY 里出现过、或 agent 自己 Write/Edit 写过的内容，
   都视为**已知**。**绝不能因"本轮没看到这活"就判 fabricated / contradicts_evidence。**
   只有当**本轮可见证据给出相反的具体结果**时，才判 contradicts_evidence。
2. **看 TOOL_ACTIVITY 开头的「本轮工具统计」再下结论。** 统计给出本轮**全部**工具调用计数（如 Write×8、Bash×7）；
   为省 token，靠前事件的结果正文可能被省略（标 JUDGE-SIDE），但**统计是完整的**。
   - 统计里有 Write / Edit / MultiEdit → 本轮**确实写了文件**，别因没展开细节就说它"没做 / 没落地"。
   - 统计里有 Bash → 本轮**跑了命令**（可能就是构建/测试），除非某条**可见**结果明确显示失败，否则别断言"没验证"。

【判 ok 的情形】
- 拒绝 / 维持原样 / 只完成一部分 / 如实报告失败或环境错误——只要在回应 user 的诉求。
- 诚实的"尚未验证"（没跑也没声称通过）、先错后自我纠正、按要求用文字给出内容/计划——都算对上。
- 多阶段任务的**中途进度播报 / 衔接语**（"Phase 1 complete, moving to Phase 2"、"Now let me…"）属正常推进，**不判 off_topic**。
- CURRENT_USER 如果是在声明明确约束、禁止事项或执行规则，终答至少要承认该约束或说明会遵守；
  只剩“还需要我做什么？”之类完全忽略约束的泛化问句，仍应判 off_topic，不能因为删掉了矛盾句就判 ok。
- CURRENT_USER 可能是“This session is being continued...”开头的续接摘要。此时不要因为它是包装性摘要就判 off_topic；
  应把摘要当作任务/历史上下文，重点核对 ASSISTANT_FINAL 的完成声明是否与本轮 TOOL_ACTIVITY 一致。

返回严格 JSON（不要 Markdown、不要多余文字）：
- verdict: "ok" | "hallucination" | "uncertain"
- mismatch_type: verdict="hallucination" 时给 off_topic | contradicts_evidence | fabricated | incoherent 之一；否则 ""。
- confidence: 0 到 1。
- reason: 中文一句话，点名 user 要什么、终答哪里对不上 / 编造。
- evidence: 1 到 4 条中文证据，**引用 CURRENT_USER / TOOL_ACTIVITY / ASSISTANT_FINAL 的原话**。
"""

# ===================== 修复提示词 =====================
REPAIR_DELETE_PROMPT = """你在修一条 AI 编码助手的「终答」。目标：**只删除**其中与证据不符/无支撑的内容，让它变诚实——**绝不能伪造**。

给你：CURRENT_USER（本轮诉求）、TOOL_ACTIVITY（本轮工具证据，含开头「本轮工具统计」）、
ASSISTANT_FINAL（原终答）、PROBLEM（检测器指出的不实之处）、EVIDENCE_QUOTES（检测器摘录的关键证据原话）。
优先对照 EVIDENCE_QUOTES 与 TOOL_ACTIVITY 定位要删的句子；EVIDENCE_QUOTES 指向的正是不实之处。

铁规：
1. **只能删**：删掉被证据推翻或无支撑的整句/整个分项，其余部分**逐字原样保留**（标点、措辞、顺序都不许改）。
2. **禁止新增或改写任何字**：不许补充、不许润色、不许把删掉的内容换个说法留下。
3. **别误删真的**：工具统计里确有的动作（有 Write/Edit=确实写了文件、有 Bash=确实跑了命令）是真的，保留。
4. 若删到没有任何真实内容可留（整段都不实）→ 只输出一行：CANNOT_DELETE

只输出删改后的终答全文（纯文本；不要解释、不要加 ``` 代码块），或单独一行 CANNOT_DELETE。
"""

REPAIR_DOWNGRADE_PROMPT = """你在修一条 AI 编码助手的「终答」。它把"没做完/失败/没做"说成了"成功/完成"。
目标：改成**严格基于证据的如实陈述**——**绝不能伪造**（不能反过来编造"其实成功了"）。

给你：CURRENT_USER、TOOL_ACTIVITY（含开头「本轮工具统计」）、ASSISTANT_FINAL（原终答）、PROBLEM（不实之处）、EVIDENCE_QUOTES（检测器摘录的关键证据原话）。
优先对照 EVIDENCE_QUOTES 与 TOOL_ACTIVITY 判断真实状态，再据此如实改写。

铁规：
1. 把不实的成功/完成声明，改成**证据支持的真实状态**：只说工具证据里确有的动作/结果；做了什么就说做了什么。
2. **禁止**出现任何工具证据里没有的：通过 / 成功 / 全部完成 / 具体测试数字 / 未见的文件 / 未见的结果。
   宁可如实说"尚未验证 / 未完成 / 仍失败 / 还需补 X"。
3. **尽量保留**原终答里真实的部分，只改或删不实的部分，改动越小越好。
4. 语言跟随原终答（原文英文就用英文回）。

只输出改后的终答全文（纯文本；不要解释、不要加 ``` 代码块）。
"""

VALID_VERDICTS = {"ok", "hallucination", "uncertain"}
VALID_VIOLATIONS = {"off_topic", "contradicts_evidence", "fabricated", "incoherent", "other"}


@dataclass
class RoundJudgment:
    round: int
    user_index: int
    user_line: int
    asst_index: int
    asst_line: int
    verdict: str
    mismatch_type: str
    confidence: float
    reason: str
    evidence: list[str]
    user_excerpt: str
    asst_excerpt: str
    error: str = ""
    is_last_round: bool = False
    round_user_index: int = 0        # 该回合 user 消息的 0-based index（末轮排除用）
    asst_timestamp: str = ""         # 命中 assistant 终答节点的 _metadata.timestamp
    # ---- 修复 ----
    repair_mode: str = ""            # local_delete | downgrade | exclude | ""
    original_final: str = ""         # 完整原终答
    repaired_final: str = ""         # 完整修复后终答
    verify_verdict: str = ""
    verify_reason: str = ""


@dataclass
class SessionResult:
    project_dir: str
    session_path: str
    valid_session: bool = True
    error: str = ""
    rounds_total: int = 0
    rounds_judged: int = 0
    rounds_skipped: int = 0
    model_failed: bool = False
    judgments: list[RoundJudgment] = field(default_factory=list)
    edits: list[tuple[int, str]] = field(default_factory=list)
    drop_from_index: int | None = None
    excluded: bool = False
    exclude_reason: str = ""

    @property
    def mismatches(self) -> list[RoundJudgment]:
        return [j for j in self.judgments if j.verdict == "hallucination"]

    @property
    def all_pass(self) -> bool:
        return self.valid_session and not self.error and not self.mismatches


# ---------- 文本/结构工具 ----------
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
                    parts.append(text); continue
                nested = item.get("content")
                if isinstance(nested, str):
                    parts.append(nested); continue
                parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def norm(s: str) -> str:
    return " ".join((s or "").split())


def excerpt(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def user_kind(u: str) -> str:
    us = norm(u); low = us.lower()
    if low in ("continue", "go on", "continue.", "go on.", "continue please"):
        return "continue"
    if us.startswith("<task-notification>"):
        return "task-notif"
    if "This session is being continued from a previous conversation" in u:
        return "session-cont"
    if low.startswith("[image"):
        return "image-only"
    return "request"


def msg_timestamp(message: dict[str, Any]) -> str:
    """取消息节点 _metadata.timestamp（报告里记录命中终答的产生时间）。"""
    meta = message.get("_metadata")
    if isinstance(meta, dict):
        ts = meta.get("timestamp")
        if isinstance(ts, str):
            return ts
    return ""


def tool_calls_detail(message: dict[str, Any]) -> list[tuple[str, str]]:
    tc = message.get("tool_calls")
    if not isinstance(tc, list):
        return []
    out: list[tuple[str, str]] = []
    for x in tc:
        if not isinstance(x, dict):
            continue
        fn = x.get("function") or {}
        name = fn.get("name", "") or ""
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append((name, norm(args)[:120]))
    return out


def clip_mid(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return text[:head] + f" [JUDGE-SIDE COMPRESSION: {len(text)-limit} 字省略，agent 当时看到完整输出] " + text[-tail:]


# ---------- 行号定位 ----------
def load_messages(session_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON is not an object")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("`messages` field is missing or is not an array")
    return [m for m in messages if isinstance(m, dict)]


def top_level_element_lines(text: str) -> list[int]:
    lines: list[int] = []
    depth = 0; line = 1; in_string = False; escape = False; expecting = True
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
            if depth == 1 and expecting:
                lines.append(line); expecting = False
            in_string = True; continue
        if char in "[{":
            if depth == 1 and expecting:
                lines.append(line); expecting = False
            depth += 1; continue
        if char in "]}":
            depth -= 1; continue
        if depth == 1 and char == ",":
            expecting = True; continue
        if depth == 1 and not char.isspace() and expecting:
            lines.append(line); expecting = False
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
    return [line_offset + ln for ln in top_level_element_lines(text[array_start:])]


# ---------- 模型调用 ----------
def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _response_text(r: Any) -> str:
    """从 OpenAI Responses API 结果里取纯文本：优先 output_text，退化到遍历 output/content。"""
    text = _obj_get(r, "output_text")
    if isinstance(text, str) and text:
        return text
    pieces: list[str] = []
    for item in _obj_get(r, "output", []) or []:
        for content in _obj_get(item, "content", []) or []:
            for key in ("text", "refusal"):
                value = _obj_get(content, key)
                if isinstance(value, str) and value:
                    pieces.append(value)
    return "".join(pieces)


class _ResponsesAgent:
    """OpenAI SDK 的 Chat Completions 客户端（client.chat.completions.create）。

    保留 .invoke({"messages":[{"role":"user","content":...}]}) 接口，返回结构与原先一致
    （{"messages":[{"role":"assistant","content":...}]}），这样 extract_last_ai_content 与
    上层判定/修复逻辑完全不变。端点/模型/温度/超时全部沿用 model_config。
    system_prompt 走 system message，本轮 user 文本走 user message。
    因用 OpenAI SDK，base_url 可指向 OpenAI 或任意 OpenAI 兼容端点（vLLM/DeepSeek/本地/Claude 兼容代理等）。
    """

    def __init__(self, cfg: Any, system_prompt: str):
        from openai import OpenAI
        self._cfg = cfg
        self._system = system_prompt
        self._client = OpenAI(
            base_url=cfg.base_url or None,
            api_key=cfg.api_key or "EMPTY",
            max_retries=0,
            timeout=cfg.timeout,
        )

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        user_content = ""
        for m in reversed(payload.get("messages") or []):
            if isinstance(m, dict) and m.get("role") == "user":
                user_content = m.get("content", "")
                break
        r = self._client.chat.completions.create(
            model=self._cfg.model,
            messages=[
                {"role": "system", "content": self._system},
                {"role": "user", "content": user_content},
            ],
            temperature=self._cfg.temperature,
        )
        text = r.choices[0].message.content or ""
        return {"messages": [{"role": "assistant", "content": text}]}


def build_agent(model_config: Any, system_prompt: str):
    """用 OpenAI SDK 的 Chat Completions API 直连 OpenAI 兼容端点（base_url 沿用 model_config）。"""
    return _ResponsesAgent(model_config, system_prompt)


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


@dataclass
class Agents:
    detect: Any
    delete: Any
    downgrade: Any


def build_tool_activity(rd: dict[str, Any], *, result_chars: int = TOOL_RESULT_CHARS,
                        early_chars: int = EARLY_RESULT_CHARS, tail_detail: int = TOOL_TAIL_DETAIL,
                        budget: int = TOOL_ACTIVITY_BUDGET, compression_label: str = "JUDGE-SIDE") -> str:
    raw_events: list[tuple[str, str, str]] = []
    used = [False] * len(rd["tools"])
    for am in rd["asst"]:
        for tname, targs in am["tcalls"]:
            tr = ""
            for k, tm in enumerate(rd["tools"]):
                if used[k]:
                    continue
                tr = tm["result"]; used[k] = True; break
            raw_events.append((tname, targs, tr))
    if not raw_events:
        return ""
    n_ev = len(raw_events)
    stat = Counter(n for n, _, _ in raw_events)
    stat_line = "、".join(f"{n}×{c}" for n, c in stat.most_common())
    tail_start = max(0, n_ev - tail_detail)
    rendered: list[str] = []
    for i, (tname, targs, tr) in enumerate(raw_events):
        cap = result_chars if i >= tail_start else early_chars
        body = clip_mid(tr, cap) if tr else "（无返回）"
        rendered.append(f"▸ {tname}({targs})\n  ← {body}")
    joined = "\n".join(rendered)
    if len(joined) > budget:
        kept: list[str] = []; total = 0
        for seg in reversed(rendered):
            total += len(seg) + 1
            if total > budget and kept:
                break
            kept.append(seg)
        joined = (f"[{compression_label} COMPRESSION: 靠前 {n_ev - len(kept)} 个工具事件正文已省略，完整活动以上方统计为准]\n"
                  + "\n".join(reversed(kept)))
    return (f"【本轮工具统计】共 {n_ev} 次调用: {stat_line}\n"
            f"（按调用顺序列出；靠前事件结果可能仅简展或省略，但统计完整——切勿因未展开细节判\"没写文件/没跑测试\"）\n"
            + joined)


def _judge_once(agent: Any, *, task: str, history: str, tool_activity: str,
                cur_user: str, final: str) -> tuple[str, str, float, str, list[str]]:
    prompt = (
        "结合下面的情景，判断 ASSISTANT_FINAL 是否为幻觉/答非所问/与情景不一致。\n\n"
        "== TASK（会话最初任务）==\n"
        f"{excerpt(task, TASK_PROMPT_CHARS) if task.strip() else '（无）'}\n\n"
        "== HISTORY（之前几轮简要）==\n"
        f"{history if history.strip() else '（无，或即为首轮）'}\n\n"
        "== CURRENT_USER（本轮用户）==\n"
        f"{excerpt(cur_user, MAX_USER_CHARS)}\n\n"
        "== TOOL_ACTIVITY（本轮工具调用及返回结果）==\n"
        f"{tool_activity if tool_activity.strip() else '（本轮未调用工具）'}\n\n"
        "== ASSISTANT_FINAL（待评判的最终回答）==\n"
        f"{excerpt(final, MAX_FINAL_CHARS)}"
    )
    with LLM_SEM:  # 全局限流：真正在飞的 LLM 调用总数封顶
        response = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    parsed = parse_agent_json(extract_last_ai_content(response))
    verdict = parsed.get("verdict")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    mtype = str(parsed.get("mismatch_type", "") or "").strip()
    if verdict != "hallucination":
        mtype = ""
    elif mtype not in VALID_VIOLATIONS:
        mtype = "other"
    conf = parsed.get("confidence", 0.0)
    if not isinstance(conf, (int, float)):
        conf = 0.0
    reason = parsed.get("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        reason = "模型未返回明确原因。"
    evidence = parsed.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = []
    evidence = [str(e) for e in evidence[:4] if str(e).strip()]
    return verdict, mtype, max(0.0, min(1.0, float(conf))), reason.strip(), evidence


def _repair_call(agent: Any, *, cur_user: str, tool_activity: str, original_final: str,
                 problem: str, evidence: list[str] | None = None) -> str:
    ev = [str(e).strip() for e in (evidence or []) if str(e).strip()]
    ev_block = "\n".join(f"- {e}" for e in ev) if ev else "（检测器未摘录具体原话）"
    prompt = (
        "== CURRENT_USER（本轮诉求）==\n" f"{excerpt(cur_user, MAX_USER_CHARS)}\n\n"
        "== TOOL_ACTIVITY（本轮工具证据）==\n" f"{tool_activity if tool_activity.strip() else '（本轮未调用工具）'}\n\n"
        "== ASSISTANT_FINAL（原终答）==\n" f"{excerpt(original_final, MAX_FINAL_CHARS)}\n\n"
        "== PROBLEM（检测器指出的不实之处）==\n" f"{problem}\n\n"
        "== EVIDENCE_QUOTES（检测器摘录的关键证据原话，据此定位并只处理不实句）==\n" f"{ev_block}"
    )
    with LLM_SEM:
        response = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    return _strip_fences(extract_last_ai_content(response))


def _guard(fn, *args, **kw):
    """硬墙 + 重试执行任意 LLM 调用（信号量在 fn 内部获取）。"""
    last: Exception | None = None
    for attempt in range(JUDGE_RETRIES + 1):
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(fn, *args, **kw)
        try:
            res = fut.result(timeout=HARD_ROUND_TIMEOUT)
            ex.shutdown(wait=False)
            return res
        except FuturesTimeout:
            ex.shutdown(wait=False); last = TimeoutError(f"exceeded hard timeout {HARD_ROUND_TIMEOUT}s")
        except Exception as exc:  # noqa: BLE001
            ex.shutdown(wait=False); last = exc
        if attempt < JUDGE_RETRIES:
            time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("guarded call failed")


def judge_guarded(agent, **kw):
    return _guard(_judge_once, agent, **kw)


# ---------- 修复 ----------
def _strip_fences(t: str) -> str:
    t = t.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    return t.strip()


def deletion_only_ok(original: str, candidate: str) -> bool:
    if not candidate.strip():
        return False
    if len(candidate) > len(original) + 5:
        return False
    no = norm(original)
    for seg in re.split(r"[\n。.!?！？;；]+", candidate):
        s = norm(seg)
        if len(s) < 4:
            continue
        if s not in no:
            return False
    return True


def diff_added_deleted(before: str, after: str) -> tuple[list[str], list[str]]:
    if deletion_only_ok(before, after):
        sm = difflib.SequenceMatcher(None, before, after)
        added: list[str] = []; deleted: list[str] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("delete", "replace"):
                fragment = before[i1:i2].strip()
                if fragment:
                    deleted.append(fragment)
            if tag in ("insert", "replace"):
                fragment = after[j1:j2].strip()
                if fragment:
                    added.append(fragment)
        return added, deleted

    b = [ln for ln in before.splitlines()]
    a = [ln for ln in after.splitlines()]
    sm = difflib.SequenceMatcher(None, b, a)
    added: list[str] = []; deleted: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            deleted += [x for x in b[i1:i2] if x.strip()]
        if tag in ("insert", "replace"):
            added += [x for x in a[j1:j2] if x.strip()]
    return added, deleted


def repair_hit(agents: Agents, *, task: str, history: str, tool_activity: str,
               tool_activity_repair: str, cur_user: str, original_final: str,
               problem: str, evidence: list[str]) -> tuple[str, str, str, str]:
    """返回 (mode, new_final, verify_verdict, verify_reason)。
    mode ∈ local_delete/downgrade/exclude；删/改按"交叉重试"跨轮交替尝试，删除优先。
    tool_activity：检测视角证据（复判用，与初判保持一致）；
    tool_activity_repair：修复视角证据（更宽截断，仅供删/改生成参考）；
    evidence：检测器摘录的关键原话，作为独立字段喂给删/改 agent。"""
    def rejudge(final_text: str) -> tuple[str, str]:
        v, _, _, r, _ = judge_guarded(
            agents.detect, task=task, history=history, tool_activity=tool_activity,
            cur_user=cur_user, final=final_text)
        return v, r

    # 交叉重试：每轮先试"局部删除"(最保守、只删)，不过再试"真实降级"；两侧跨轮交替，
    # 不再单侧连续用尽——降低某一侧偶发连续失手就直接掉到排除的概率。
    # 删除优先级仍高于降级：同一轮内删除先判，删得掉且转 ok 就采纳删除。
    def try_delete() -> tuple[str, str, str, str] | None:
        try:
            deleted = _guard(_repair_call, agents.delete, cur_user=cur_user, tool_activity=tool_activity_repair,
                             original_final=original_final, problem=problem, evidence=evidence)
        except Exception:  # noqa: BLE001
            return None
        if deleted and deleted.strip() != "CANNOT_DELETE" and deletion_only_ok(original_final, deleted):
            try:
                v, r = rejudge(deleted)
                if v == "ok":
                    return "local_delete", deleted, v, r
            except Exception:  # noqa: BLE001
                pass
        return None

    def try_downgrade() -> tuple[str, str, str, str] | None:
        try:
            down = _guard(_repair_call, agents.downgrade, cur_user=cur_user, tool_activity=tool_activity_repair,
                          original_final=original_final, problem=problem, evidence=evidence)
        except Exception:  # noqa: BLE001
            return None
        if down and down.strip():
            try:
                v, r = rejudge(down)
                if v == "ok":
                    return "downgrade", down, v, r
            except Exception:  # noqa: BLE001
                pass
        return None

    for _ in range(max(1, REPAIR_CANDIDATE_ATTEMPTS)):
        got = try_delete()
        if got is not None:
            return got
        got = try_downgrade()
        if got is not None:
            return got

    # 排除
    return "exclude", "", "hallucination", "各档修复后仍未转 ok / 无真可留"


# ---------- 单文件：检测 + 修复（内部回合并发） ----------
def process_one_session(project_dir: Path, session_path: Path, agents: Agents, do_repair: bool) -> SessionResult:
    result = SessionResult(project_dir=project_dir.name, session_path=str(session_path))
    if not session_path.exists():
        result.valid_session = False; result.error = "session file does not exist"; return result
    try:
        messages = load_messages(session_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.valid_session = False; result.error = str(exc); return result

    elem_lines = messages_element_lines(session_path)

    def line_of(i: int) -> int:
        return elem_lines[i] if i < len(elem_lines) else 1

    task_prompt = ""
    if messages:
        if messages[0].get("role") in ("system", "user"):
            task_prompt = normalize_content(messages[0].get("content"))
        if len(messages) > 1 and messages[0].get("role") == "system" and messages[1].get("role") == "user":
            u1 = normalize_content(messages[1].get("content"))
            if len(u1) > len(task_prompt):
                task_prompt = u1

    rounds: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for idx, m in enumerate(messages):
        r = m.get("role")
        if r == "user":
            if cur:
                rounds.append(cur)
            cur = {"uidx": idx, "user": normalize_content(m.get("content")), "asst": [], "tools": []}
        elif r == "assistant" and cur is not None:
            cur["asst"].append({"idx": idx, "text": normalize_content(m.get("content")),
                                "tcalls": tool_calls_detail(m), "ts": msg_timestamp(m)})
        elif r == "tool" and cur is not None:
            cur["tools"].append({"idx": idx, "tid": m.get("tool_call_id", ""),
                                 "result": normalize_content(m.get("content"))})
    if cur:
        rounds.append(cur)
    last_round_idx = len(rounds) - 1

    # 组装"可判回合"（含上下文），随后并发判/修
    units: list[dict[str, Any]] = []
    for ri, rd in enumerate(rounds):
        if not rd["asst"]:
            continue
        result.rounds_total += 1
        if user_kind(rd["user"]) not in {"request", "session-cont"}:
            result.rounds_skipped += 1; continue
        if rd["asst"][-1]["tcalls"]:
            result.rounds_skipped += 1; continue
        final = ""; fidx = rd["asst"][-1]["idx"]; fts = rd["asst"][-1].get("ts", "")
        for a in reversed(rd["asst"]):
            if a["text"].strip():
                final = a["text"]; fidx = a["idx"]; fts = a.get("ts", ""); break
        if not final.strip():
            result.rounds_skipped += 1; continue

        hist_parts: list[str] = []
        for hi in range(max(0, ri - HISTORY_ROUNDS), ri):
            hr = rounds[hi]
            u_sum = norm(hr["user"])[:HIST_USER_CHARS]
            hf = ""
            for ha in reversed(hr["asst"]):
                if ha["text"].strip():
                    hf = ha["text"]; break
            a_sum = norm(hf)[:HIST_FINAL_CHARS] if hf else "（无文字回复）"
            hist_parts.append(f"[轮{hi}] U: {u_sum}\n      A: {a_sum}")
        history = "\n".join(hist_parts) if hist_parts else ""

        units.append({"ri": ri, "uidx": rd["uidx"], "fidx": fidx, "fts": fts, "user": rd["user"],
                      "final": final, "history": history,
                      "tool_activity": build_tool_activity(rd),
                      "tool_activity_repair": build_tool_activity(
                          rd, result_chars=REPAIR_TOOL_RESULT_CHARS, early_chars=REPAIR_EARLY_RESULT_CHARS,
                          tail_detail=REPAIR_TOOL_TAIL_DETAIL, budget=REPAIR_TOOL_ACTIVITY_BUDGET,
                          compression_label="REPAIR-SIDE")})

    def handle_unit(u: dict[str, Any]) -> RoundJudgment:
        err = ""
        try:
            verdict, mtype, conf, reason, evidence = judge_guarded(
                agents.detect, task=task_prompt, history=u["history"], tool_activity=u["tool_activity"],
                cur_user=u["user"], final=u["final"])
        except Exception as exc:  # noqa: BLE001
            verdict, mtype, conf, reason, evidence = "error", "", 0.0, f"model failed: {exc}", []
            err = str(exc)
        j = RoundJudgment(
            round=u["ri"], user_index=u["uidx"] + 1, user_line=line_of(u["uidx"]),
            asst_index=u["fidx"] + 1, asst_line=line_of(u["fidx"]),
            verdict=verdict if verdict in VALID_VERDICTS else "error",
            mismatch_type=mtype, confidence=round(conf, 3), reason=reason, evidence=evidence,
            user_excerpt=norm(u["user"])[:PREVIEW_CHARS], asst_excerpt=norm(u["final"])[:PREVIEW_CHARS],
            error=err, is_last_round=(u["ri"] == last_round_idx),
            round_user_index=u["uidx"], asst_timestamp=u.get("fts", ""),
            original_final=u["final"])
        if do_repair and verdict == "hallucination":
            problem = f"类型={mtype}；理由={reason}"
            mode, new_final, vv, vr = repair_hit(
                agents, task=task_prompt, history=u["history"], tool_activity=u["tool_activity"],
                tool_activity_repair=u.get("tool_activity_repair", u["tool_activity"]),
                cur_user=u["user"], original_final=u["final"], problem=problem, evidence=evidence)
            j.repair_mode = mode; j.verify_verdict = vv; j.verify_reason = vr
            if mode in ("local_delete", "downgrade"):
                j.repaired_final = new_final
        return j

    if units:
        rw = max(1, min(ROUND_WORKERS, len(units)))
        with ThreadPoolExecutor(max_workers=rw) as ex:
            judgments = list(ex.map(handle_unit, units))
    else:
        judgments = []
    judgments.sort(key=lambda j: (j.round, j.asst_index))

    # 汇总编辑 / 排除
    for j in judgments:
        result.rounds_judged += 1
        if j.error:
            result.model_failed = True
        if j.verdict == "hallucination" and do_repair:
            if j.repair_mode in ("local_delete", "downgrade") and j.repaired_final:
                result.edits.append((j.asst_index - 1, j.repaired_final))
            elif j.repair_mode == "exclude":
                if j.is_last_round:
                    cut = j.round_user_index
                    prefix = messages[:cut]
                    has_prior_user = any(m.get("role") == "user" for m in prefix)
                    has_prior_assistant = any(m.get("role") == "assistant" for m in prefix)
                    if has_prior_user and has_prior_assistant:
                        result.drop_from_index = cut if result.drop_from_index is None else min(result.drop_from_index, cut)
                    else:
                        result.excluded = True
                        result.exclude_reason = f"唯一/首个回合({j.round})不可修复 → 整 session 排除"
                else:
                    result.excluded = True
                    result.exclude_reason = f"中间轮({j.round})不可修复且删轮会断配对 → 整 session 排除"
    result.judgments = judgments
    model_errors = [j.error for j in judgments if j.error]
    if model_errors:
        result.error = f"模型判定失败: {model_errors[0]}"
    return result


# ---------- 遍历 ----------
def _is_subagent_path(p: Path) -> bool:
    return any(part in ("subagents", "subagent") or "__subagents__" in part for part in p.parts)


def iter_main_sessions(root: Path) -> list[tuple[Path, Path]]:
    out: list[tuple[Path, Path]] = []
    # root 两种布局都支持:含多个 project 的父目录,或"单个 TASK 目录"(root/sessions 直接存在)。
    # 后者用于 run_trajectory_pipeline 按 TASK 分派时,把单个 <root>/TASK-xxx 当 -r 传进来;
    # 不处理这种就会 0 命中静默空跑(与 fix_compact_alignment 同一个坑)。
    if (root / "sessions").is_dir():
        subs = [root]
    else:
        subs = sorted(root.iterdir(), key=lambda p: p.name)
    for sub in subs:
        if not sub.is_dir() or sub.name.startswith(".") or "__subagents__" in sub.name:
            continue
        sess_dir = sub / "sessions"
        if sess_dir.is_dir():
            for jf in sorted(sess_dir.glob("*.json")):
                if jf.is_file() and not _is_subagent_path(jf):
                    out.append((sub, jf))
            continue
        legacy = sub / f"{sub.name}.json"
        if legacy.is_file():
            out.append((sub, legacy))
    return out


def build_jobs(root: Path, one_file: Path | None) -> list[tuple[Path, Path]]:
    if one_file is not None:
        f = one_file if one_file.is_absolute() else (root / one_file if root else one_file)
        f = f.expanduser().resolve()
        proj = f.parent.parent if f.parent.name == "sessions" else f.parent
        return [(proj, f)]
    return iter_main_sessions(root)


def task_dir_of(session_path: Path) -> Path:
    """从 session 文件路径回推它所属的 TASK 目录（模版报告按 TASK 分组用）。
    与 build_jobs 的口径一致：<task>/sessions/x.json → <task>；<task>/x.json → <task>。"""
    parent = session_path.parent
    return parent.parent if parent.name == "sessions" else parent


# ---------- 落盘：修复后的副本 + 排除状态 ----------
def apply_and_write(result: SessionResult, out_dir: Path | None) -> dict[str, Any]:
    """Apply repairs while keeping every session at its canonical path.

    ``excluded`` is a report outcome, not a filesystem quarantine. The
    session stays under ``sessions/`` (or its nested ``subagents/`` path) so
    downstream aggregation and archives retain the canonical layout.
    """
    src = Path(result.session_path)
    rel = f"{result.project_dir}/{src.name}"
    info: dict[str, Any] = {"产物id": result.project_dir, "文件id": src.name}
    if not result.valid_session:
        info["outcome"] = "invalid"; info["error"] = result.error; return info
    if result.excluded:
        # Keep the source file in its canonical main/subagent location. The
        # exclusion reason remains available in the generated report.
        info["outcome"] = "excluded"; info["exclude_reason"] = result.exclude_reason; return info
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        info["outcome"] = "write_failed"; info["error"] = str(exc); return info
    msgs = payload.get("messages", [])
    applied = 0
    for midx, new_content in result.edits:
        if 0 <= midx < len(msgs) and isinstance(msgs[midx], dict):
            msgs[midx]["content"] = new_content
            applied += 1
    dropped = 0
    if result.drop_from_index is not None and 0 <= result.drop_from_index <= len(msgs):
        dropped = len(msgs) - result.drop_from_index
        payload["messages"] = msgs[:result.drop_from_index]
    if out_dir is None:
        # 原地:只有真改了才写回;clean(无改动)不动原文件。
        if applied or dropped:
            src.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        dest = out_dir / "repaired" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    info["outcome"] = "repaired" if (applied or dropped) else "clean"
    info["edits_applied"] = applied; info["messages_dropped"] = dropped
    return info


# ---------- 报告（修改前/后/新增/删除，脚本名命名） ----------
def _fmt_block(text: str, limit: int = 1600) -> str:
    return excerpt(text or "", limit)


def write_reports(root: str, results: list[SessionResult], report_dir: Path, out_dir: Path, do_repair: bool) -> dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    total = len(results)
    passed = sum(1 for r in results if r.all_pass)
    type_counts: dict[str, int] = {}
    mode_counts: dict[str, int] = {}
    verify_ok = 0
    items: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for r in results:
        if r.excluded:
            excluded.append({"产物id": r.project_dir, "文件id": Path(r.session_path).name, "原因": r.exclude_reason})
        for j in r.mismatches:
            type_counts[j.mismatch_type or "other"] = type_counts.get(j.mismatch_type or "other", 0) + 1
            if do_repair:
                mode_counts[j.repair_mode or "?"] = mode_counts.get(j.repair_mode or "?", 0) + 1
                if j.verify_verdict == "ok":
                    verify_ok += 1
            added, deleted = ([], [])
            if j.repaired_final:
                added, deleted = diff_added_deleted(j.original_final, j.repaired_final)
            elif j.repair_mode == "exclude":
                deleted = [ln for ln in j.original_final.splitlines() if ln.strip()]
            items.append({
                "产物id": r.project_dir,
                "文件id": Path(r.session_path).name,
                "回合": j.round,
                "assistant_message_index": j.asst_index,
                "assistant_行号": j.asst_line,
                "assistant节点类型": "回合终答",
                "命中类型": j.mismatch_type,
                "检测理由": j.reason,
                "修复模式": j.repair_mode or ("（未修复）" if do_repair else "（仅检测）"),
                "校验结果": j.verify_verdict,
                "校验理由": j.verify_reason,
                "修改前": _fmt_block(j.original_final),
                "修改后": _fmt_block(j.repaired_final) if j.repaired_final else (
                    "（该回合整体排除）" if j.repair_mode == "exclude" else ""),
                "新增了什么": added or "（无新增）",
                "删除了什么": deleted or "（无删除）",
            })

    summary = {
        "脚本": SCRIPT_STEM, "源路径": str(root),
        "生成时间": datetime.now().isoformat(timespec="seconds"),
        "扫描文件数": total, "通过": passed, "命中(有问题)文件": total - passed,
        "命中回合数": sum(len(r.mismatches) for r in results),
        "命中类型分布": type_counts,
        "判定回合": sum(r.rounds_judged for r in results),
        "模型报错文件": sum(1 for r in results if r.model_failed),
        "并发": {"task_workers": TASK_WORKERS, "round_workers": ROUND_WORKERS, "llm_concurrency": LLM_CONCURRENCY},
    }
    if do_repair:
        summary["修复"] = {"模式分布": mode_counts, "重判转ok": verify_ok,
                          "仍不过→排除": sum(len(r.mismatches) for r in results) - verify_ok,
                          "整session排除": len(excluded), "修复副本目录": (str(out_dir) if out_dir is not None else "原地(in-place)")}

    model_errors = [
        {"产物id": r.project_dir, "文件id": Path(r.session_path).name, "错误": r.error}
        for r in results if r.model_failed
    ]
    report = {"summary": summary, "修复明细": items, "排除清单": excluded, "模型错误": model_errors}

    # JSON
    (report_dir / f"{SCRIPT_STEM}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Markdown（人读：修改前/后/新增/删除）
    md: list[str] = [f"# {SCRIPT_STEM} 检测+修复报告", "",
                     f"- 源路径: `{root}`",
                     f"- 生成: {summary['生成时间']}",
                     f"- 扫描 {total} 文件 · 通过 {passed} · 命中 {total-passed} · 命中回合 {summary['命中回合数']}",
                     f"- 命中类型: {json.dumps(type_counts, ensure_ascii=False)}"]
    if do_repair:
        rp = summary["修复"]
        md.append(f"- 修复: 模式 {json.dumps(rp['模式分布'], ensure_ascii=False)} · 重判转ok {rp['重判转ok']} · 排除 {rp['整session排除']}")
    md += ["", "---", ""]
    for it in items:
        md.append(f"## {it['产物id']} / {it['文件id']} · 回合{it['回合']} · [{it['命中类型']}] → **{it['修复模式']}** (校验={it['校验结果']})")
        md.append(f"- 位置: assistant message #{it['assistant_message_index']}（行 {it['assistant_行号']}，{it['assistant节点类型']}）")
        md.append(f"- 检测理由: {it['检测理由']}")
        if it["校验理由"]:
            md.append(f"- 校验理由: {it['校验理由']}")
        md.append("")
        md.append("**修改前:**\n```\n" + str(it["修改前"]) + "\n```")
        md.append("**修改后:**\n```\n" + str(it["修改后"]) + "\n```")
        md.append("**新增了什么:**\n```\n" + ("\n".join(it["新增了什么"]) if isinstance(it["新增了什么"], list) else str(it["新增了什么"])) + "\n```")
        md.append("**删除了什么:**\n```\n" + ("\n".join(it["删除了什么"]) if isinstance(it["删除了什么"], list) else str(it["删除了什么"])) + "\n```")
        md.append("")
    if excluded:
        md.append("## 排除清单")
        for e in excluded:
            md.append(f"- {e['产物id']}/{e['文件id']} — {e['原因']}")
    if model_errors:
        md.append("## 模型错误")
        for e in model_errors:
            md.append(f"- {e['产物id']}/{e['文件id']} — {e['错误']}")
    (report_dir / f"{SCRIPT_STEM}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return report


# ---------- 模版报告（问题记录/脚本输出问题报告模版/structure.json 口径，按 TASK 一份） ----------
# 报告是给人读的"修复前后日志"，下列枚举值本身是英文，人工不一定懂含义，
# 故每个枚举补一份中文说明字段（英文原值保留供机读，中文供人核对）。
MISMATCH_TYPE_CN = {
    "off_topic": "答非所问：终答在讲与用户诉求无关的另一件事，没回应用户要的",
    "contradicts_evidence": "与证据矛盾：终答与用户贴的内容或本轮工具结果直接冲突（睁眼说瞎话）",
    "fabricated": "凭空编造：终答声称的结果在用户/历史/工具证据里都无据可查",
    "incoherent": "语无伦次：乱码/串话，或在讲与本会话完全无关的项目",
    "other": "其它：未归入上述四类的语义不一致",
}
REPAIR_MODE_CN = {
    "local_delete": "局部删除：只删掉不实的句子，其余部分逐字保留",
    "downgrade": "如实降级：把不实的成功/完成声明改写成基于证据的真实陈述",
    "exclude": "无法修复：保留原 main/subagent 路径，仅在报告中标记排除",
    "": "未修复",
}
VERIFY_VERDICT_CN = {
    "ok": "修复后重判通过（已转为如实）",
    "hallucination": "修复后重判仍未通过（各档都没修好）",
    "uncertain": "修复后重判结果不确定",
    "": "",
}


def build_semantic_report(task_id: str, task_path: str, task_results: list[SessionResult],
                          do_repair: bool) -> dict[str, Any]:
    """把某个 TASK 下所有 session 的判定汇成一份模版格式报告。
    report_type 固定 semantic_report（本脚本属语义类）；本脚本只判「回合终答/总结」，
    故 function 固定为「assistant总结的语义失败」；action_type 随模式：修复 → fix，--detect-only → check。
    before_content：无论检查还是修复模式，都填被判终答原文（复核人不必回原 session 翻）；
    after_content：修复模式给新终答（exclude 则空），检查模式恒空（未做修改）。
    addition：带上检测器证据(evidence)、user 侧定位(user_*)与判定基准，复核可直接对照"用户要什么 vs 终答答了什么"。"""
    function = "assistant总结的语义失败"
    action_type = "fix" if do_repair else "check"

    sessions: list[dict[str, Any]] = []
    hit_trajectory_count = 0
    total_issues = 0
    for r in sorted(task_results, key=lambda x: Path(x.session_path).name):
        mismatches = r.mismatches
        if not mismatches:
            continue
        hit_trajectory_count += 1
        total_issues += len(mismatches)
        sub: list[dict[str, Any]] = []
        for j in mismatches:
            # 被判终答原文始终可见（检测/修复都填）；修复模式的 after 给新文，检查模式留空。
            before_content = j.original_final
            if do_repair and j.repaired_final:
                after_content = j.repaired_final
            else:
                after_content = ""  # exclude / 未成功修复 / 仅检测：无改后新文
            addition: dict[str, Any] = {
                "round": j.round,
                "mismatch_type": j.mismatch_type,
                "mismatch_type_desc": MISMATCH_TYPE_CN.get(j.mismatch_type, j.mismatch_type),
                "evidence": j.evidence,
                "assistant_message_index": j.asst_index,
                "assistant_line": j.asst_line,
                # user 侧定位：判定基准是"对没对上 CURRENT_USER"，给出用户诉求原文与位置。
                "user_message_index": j.user_index,
                "user_line": j.user_line,
                "user_excerpt": j.user_excerpt,
            }
            if do_repair:
                addition["repair_mode"] = j.repair_mode
                addition["repair_mode_desc"] = REPAIR_MODE_CN.get(j.repair_mode, j.repair_mode)
                addition["verify_verdict"] = j.verify_verdict
                addition["verify_verdict_desc"] = VERIFY_VERDICT_CN.get(j.verify_verdict, j.verify_verdict)
                addition["verify_reason"] = j.verify_reason
                if j.repair_mode == "exclude":
                    addition["excluded"] = True
            sub.append({
                "function": function,
                "action_type": action_type,
                "before_content": before_content,
                "after_content": after_content,
                # problem 只留纯理由；类型见 addition.mismatch_type，不再前缀重复。
                "problem": j.reason,
                "line": j.asst_line,
                "addition": addition,
            })
        sessions.append({"session_id": Path(r.session_path).stem, "sub": sub})

    summary: dict[str, Any] = {
        "total_trajectory_count": len(task_results),
        "hit_trajectory_count": hit_trajectory_count,
        "total_issues": total_issues,
    }
    return {
        "report_type": "semantic_report",
        "task_id": task_id,
        "task_path": task_path,
        "create": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "sessions": sessions,
    }


def write_semantic_reports(results: list[SessionResult], report_override: Path | None,
                           do_repair: bool) -> list[Path]:
    """按 TASK 分组，每个 TASK 一份报告，文件名固定 <脚本名>.json。
    缺省（report_override=None）：写到该 TASK 自己的 <task_dir>/report/ 目录；
    指定 -report <dir>：写到 <dir>（单 TASK 直接落，多 TASK 各按 <dir>/<task_id>/ 分开防重名）。"""
    by_task: dict[Path, list[SessionResult]] = {}
    for r in results:
        tdir = task_dir_of(Path(r.session_path))
        by_task.setdefault(tdir, []).append(r)

    multi = len(by_task) > 1
    written: list[Path] = []
    for tdir, task_results in sorted(by_task.items(), key=lambda kv: kv[0].name):
        report = build_semantic_report(tdir.name, str(tdir), task_results, do_repair)
        if report_override is None:
            dest_dir = tdir / "report"
        else:
            dest_dir = report_override / tdir.name if multi else report_override
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{SCRIPT_STEM}.json"
        dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(dest)
    return written


# ---------- 主流程（task 级并发） ----------
def run(root: Path, one_file: Path | None, out_dir: Path, report_dir: Path,
        report_override: Path | None, *, do_repair: bool = True) -> None:
    model_config = resolve_project_model_config()
    print(f"model: {model_config.model} @ {model_config.base_url or 'default'} | "
          f"task_workers={TASK_WORKERS} round_workers={ROUND_WORKERS} llm_concurrency={LLM_CONCURRENCY} repair={do_repair}",
          flush=True)
    agents = Agents(detect=build_agent(model_config, SYSTEM_PROMPT),
                    delete=build_agent(model_config, REPAIR_DELETE_PROMPT),
                    downgrade=build_agent(model_config, REPAIR_DOWNGRADE_PROMPT))

    jobs = build_jobs(root, one_file)
    print(f"processing {len(jobs)} sessions ...", flush=True)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
    results_by_index: dict[int, SessionResult] = {}
    write_lock = threading.Lock()

    def run_job(index, project_dir, session_path):
        res = process_one_session(project_dir, session_path, agents, do_repair)
        res.project_dir = project_dir.name
        return index, project_dir.name, res

    tw = max(1, min(TASK_WORKERS, len(jobs))) if jobs else 1
    with ThreadPoolExecutor(max_workers=tw) as executor:
        futures = {executor.submit(run_job, i, pd, sp): i for i, (pd, sp) in enumerate(jobs)}
        done = 0
        for future in as_completed(futures):
            index, name, res = future.result()
            results_by_index[index] = res
            done += 1
            with write_lock:
                winfo = apply_and_write(res, out_dir) if do_repair else {"outcome": "detect-only"}
            tag = "PASS" if res.all_pass else f"FAIL({len(res.mismatches)})"
            modes = Counter(j.repair_mode for j in res.mismatches if j.repair_mode)
            extra = f" repair={dict(modes)}" if modes else ""
            print(f"[{done}/{len(jobs)}] {name} ... {tag} {winfo.get('outcome','')}{extra}", flush=True)

    results = [results_by_index[i] for i in range(len(jobs))]
    rep = write_reports(str(root), results, report_dir, out_dir, do_repair)
    semantic_paths = write_semantic_reports(results, report_override, do_repair)
    s = rep["summary"]
    print(f"\n== 报告: {report_dir / (SCRIPT_STEM + '.json')} / .md ==")
    print(f"== 模版报告: {len(semantic_paths)} 个 TASK ==")
    for p in semantic_paths:
        print(f"   - {p}")
    if do_repair:
        print(f"== 输出: {'原地改写(in-place)' if out_dir is None else out_dir} ==")
    else:
        print("== 输出: 未写入 session（仅检测） ==")
    print(f"扫描 {s['扫描文件数']} · 通过 {s['通过']} · 命中 {s['命中(有问题)文件']} · 命中回合 {s['命中回合数']} · 报错文件 {s['模型报错文件']}")
    print(f"命中类型: {json.dumps(s['命中类型分布'], ensure_ascii=False)}")
    if do_repair:
        print(f"修复: {json.dumps(s['修复'], ensure_ascii=False)}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="user-assistant 对应/幻觉 检测+修复（一体，支持仅检测模式）。")
    p.add_argument("-r", dest="r", type=Path, required=True, help="路径（含项目子目录的根，如 50-TASK）。")
    p.add_argument("-i", dest="i", type=Path, default=None, help="指定文件（可选；只处理这一个 session.json）。")
    p.add_argument("-o", dest="o", type=Path, default=None, help="输出路径（可选；仅写入成功修复的副本；缺省为原地处理）。")
    p.add_argument("-report", dest="report", type=Path, default=None,
                   help="模版报告落盘目录（覆盖默认；缺省写到各 TASK 的 <task_dir>/report/<脚本名>.json）。")
    p.add_argument("--detect-only", action="store_true",
                   help="仅检测并生成报告，不修改或复制 session。")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = args.r.expanduser().resolve()
    if not root.exists():
        print(f"-r 路径不存在: {root}", file=sys.stderr); return 2
    # -o 不传 = 原地改写(与仓库其它修复脚本口径一致,便于编排进流水线);传了 = 写副本到 -o。
    out_dir = args.o.expanduser().resolve() if args.o else None
    # 旧富报告(<脚本名>.json/.md)沿用原位置:有 -o 写 -o 同级,否则写 root。
    report_dir = out_dir if out_dir is not None else root
    # -report 覆盖模版报告落盘目录;缺省 None → 各 TASK 自己的 <task_dir>/report/<脚本名>.json。
    report_override = args.report.expanduser().resolve() if args.report else None
    try:
        run(root, args.i, out_dir, report_dir, report_override, do_repair=not args.detect_only)
    except Exception as exc:  # noqa: BLE001
        print(f"{SCRIPT_STEM} failed: {exc}", file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

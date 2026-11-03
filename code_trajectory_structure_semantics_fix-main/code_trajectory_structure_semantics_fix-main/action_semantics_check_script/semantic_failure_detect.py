#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""semantic_failure_detect —— 轨迹语义失败检测（只检测、只统计、不改轨迹）。

站在 ua_detect_and_repair.py 之上，从**语义逻辑层面**（不只是命令执行失败）核查两点：

  【点1 · 回合终答失败】
    对每个 user→下一个 user 之间的“回合终答”（最后一个带自然语言的 assistant），
    判它是否语义失败。复用 ua 的四类，并补两类过程语义类别：
      - off_topic            答非所问，没回应本轮 user 诉求
      - contradicts_evidence 与 user 内容或本轮工具结果直接矛盾
      - fabricated           凭空编造文件/接口/测试结果/完成状态
      - incoherent           语无伦次/乱码/串话/回复无关项目
      - unfulfilled_promise  声明本轮要做 X，最终却没做 / 没交付 X（承诺未兑现）
      - self_contradiction   回合终答内部前后自相矛盾（如同一答里既 52 又 53）

  【点2 · round 内逐条失败计数】
    在同一 user→下一个 user 区间内，**逐条**响应判失败（各算一次）：
      - assistant 语义失败：说要做却没做 / 误解任务 / 前后矛盾 / 无证据宣称完成 / 逻辑不通 …
      - 工具调用语义失败（语义层，非纯 exit code）：调错工具 / 传明显错的参数 /
        对已知失败原样重试不换法子 / 工具结果与其声明意图相反。
        （纯 exit≠0 但 agent 已正确识别并处理的，不计为失败。）

跳过规则与 ua 完全一致（复用 user_kind）：
  - user_kind 非 request/session-cont 的回合跳过；
  - 回合终答只有工具调用、没有自然语言 → 跳过；
  - 回合内无自然语言 assistant → 跳过；
  - user 之后完全没有 assistant → 跳过。

低置信（< CONFIDENCE_THRESHOLD）的失败判定**不计入失败总数**，只在报告的“低置信备查”里另列。

subagents 子目录：每个 subagent 当独立轨迹各出一份统计，并 roll-up 到所属主 session。

CLI（与 ua 同口径，便于编排进 action_semantics_fix.py）：
  -r       路径（含项目子目录的根，或单个 TASK 目录）  [必填]
  -i       指定文件（可选；只处理这一个 session.json）
  -report  结构化报告位置（可选；缺省 = 写进各自 TASK 的 <TASK>/report/<脚本名>.json）

并发（环境变量微调）：SFD_TASK_WORKERS / SFD_ROUND_WORKERS / SFD_LLM_CONCURRENCY。
置信阈值：SFD_CONFIDENCE（默认 0.5；低于此值的失败判定只进“低置信备查”，不计入总数）。

不改动任何 session 文件；不复用 ua 的修复逻辑，仅复用其解析/模型/遍历基建。

────────────────────────────────────────────────────────────────────
使用方式
────────────────────────────────────────────────────────────────────
本脚本靠 LLM 判定语义失败，模型端点/密钥读根目录 .env（复用 model_config）。
必须用装了 openai 包的解释器（本机是 python3，而非 python3.11）。

1) 先加载 .env（模型配置在里面）：
     set -a && source .env && set +a

2) 扫描整个 ua_cases（含所有 TASK 的主 session 与 subagents），报告写到指定目录：
     python3 action_semantics_fix_script/semantic_failure_detect.py \
       -r 问题记录/ua_cases \
       -report 问题记录/ua_reports

3) 只扫单个 TASK 目录（-r 直接指向该 TASK，会自动识别 <TASK>/sessions 布局）：
     python3 action_semantics_fix_script/semantic_failure_detect.py \
       -r 问题记录/ua_backups/20260715_205215/TASK-20260519-B0540B \
       -report /tmp/sfd_out

4) 只判某一个 session 文件（-i 可给相对 CWD 或绝对路径）：
     python3 action_semantics_fix_script/semantic_failure_detect.py \
       -r 问题记录/ua_cases \
       -i 问题记录/ua_cases/TASK-20260520-98A666/sessions/2-test-xxx.json \
       -report /tmp/sfd_out

参数：
  -r        [必填] 含项目子目录的根（如 ua_cases），或单个 TASK 目录。
  -i        [可选] 只处理这一个 session.json（主 session 或 subagent 均可）。
  -report   [可选] 结构化「修复前后」报告的位置；缺省=写进各自 TASK 的 <TASK>/report/<脚本名>.json，
            指定目录时统一写到 <目录>/<脚本名>.json。

输出：只出按 TASK 的“修复前后”报告（structure 模版格式，供审查与下游串联脚本汇总）：
     默认 <TASK>/report/semantic_failure_detect.json（-report DIR 时改写 DIR/semantic_failure_detect.json）。
     · 文件名=脚本名.json；report_type=semantic_report。
     · 模版见 问题记录/脚本输出问题报告模版/structure.json；本脚本检测专用不改轨迹，
       故 action_type=check、before/after 内容为空；function 按检测点区分（点1=assistant总结的
       语义失败、点2=工具调用的语义失败）；addition 里带 timestamp（取自节点 _metadata.timestamp）
       及语义特有字段（检测点/失败类型/置信/建议策略/可自动修复/证据/指纹等）。
     · 注：脚本原有的丰富汇总报告(.json/.md)暂不落盘（统计仍打印到控制台）。
     · 仅纳入高置信失败；subagent 作为独立 session 条目并在 addition 标注所属主 session。
  - summary：
      · 回合统计（回合总数/判定回合数=分母/跳过数）。
      · 点1统计 / 点2统计：失败数 + 占比 + 失败率 + 类型分布（点2另给“含失败回合”与“平均每回合失败数”两口径）。
      · 修复统计：失败总数(点1+点2)、建议策略分布、可自动修复项数/占比、需人工或整轮处理项数。
      · 置信阈值、低置信备查数、主/subagent 计数、并发参数。
  - session明细（按“严重度”降序，脏轨迹排前）：逐文件回合统计 + 数量/占比 + 严重度 + 可自动修复项数；
      · 点1明细（回合终答失败）：指纹、回合、末轮标记、assistant 消息下标(1基/0基)+行号、user 行号、
        失败类型、建议策略、可自动修复、置信、理由、证据、user 摘要、终答摘要。
      · 点2明细（round 内逐条失败）：指纹、回合、步序号、assistant 消息下标(1基/0基)+行号、
        失败类型、建议策略、可自动修复、置信、理由、证据、步原文摘要。
  - subagent汇总：subagent 失败 roll-up 到所属主 session。

修复导向字段说明（供下游修复脚本分支/幂等）：
  - 建议策略枚举：rewrite_final（重写终答）/ delete_sentence（删虚假句）/ downgrade_claim（降级为真实状态）/
    drop_round（整轮删）/ exclude_session（整 session 排除）/ manual_review（人工核对）。
  - 可自动修复：策略属 delete_sentence / downgrade_claim 时为 true，可无人工自动处理。
  - 末轮：点1 失败所在回合是否本 session 最后一个判定回合（末轮可删句/整轮删，中间轮多需排除 session）。
  - 指纹：sha1(产物id|文件id|p1/p2|回合|消息下标|类型)[:16]，跨运行稳定，用于去重与和 Excel 对账。
  - 严重度：点1失败率×2 + 点2平均每回合失败数，用于排序/优先处理。

成本提示：每个可判回合会打 2 次 LLM（点1 一次、点2 一次），全量扫描调用量较大。
  点2 对渲染超 SFD_P2_WINDOW_CHARS（默认 24000 字符）的大回合会自动拆成多个有界窗口、
  顺序多打几次（后续窗口带前序步骤只读摘要，防跨窗口漏判/误判）；小回合仍单次调用、
  行为与成本不变。窗口化不改变点2“逐条计数、按 step 去重”的语义；窗口化回合数与额外
  调用数见报告 summary 的“点2窗口化”。窗口在回合线程内顺序执行、各自受 LLM_SEM 限流、
  各自独立走硬超时/重试（不共用单次 300s 预算），不新增并发层；个别窗口重试后仍失败时
  跳过该窗口、保留其余结果并在报告标记模型报错。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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

# ---- 复用 ua_detect_and_repair 的解析/模型/遍历基建（不改动它）----
# 本脚本在 action_semantics_check_script/，而 ua_detect_and_repair 在同级的
# action_semantics_fix_script/，故按仓库根(parents[1])定位，别用自身父目录。
_UA_DIR = Path(__file__).resolve().parents[1] / "action_semantics_fix_script"
if str(_UA_DIR) not in sys.path:
    sys.path.insert(0, str(_UA_DIR))
from ua_detect_and_repair import (  # noqa: E402
    build_agent,
    build_tool_activity,
    excerpt,
    extract_last_ai_content,
    load_messages,
    messages_element_lines,
    norm,
    normalize_content,
    parse_agent_json,
    tool_calls_detail,
    user_kind,
)

_CHECK_DIR = Path(__file__).resolve().parents[1] / "structure_check_script"
if str(_CHECK_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECK_DIR))
from model_config import resolve_model_config  # noqa: E402

SCRIPT_STEM = Path(__file__).stem

# 结构化「修复前后」报告落盘：默认写进被检测 TASK 自己的 report/ 目录，文件名=脚本名.json，
#   即 <输入TASK>/report/<脚本名>.json；-report DIR 时改写到 DIR/<脚本名>.json。
# 脚本自有的丰富汇总报告(.json/.md)固定写到仓库根/reports（与结构化报告解耦，避免同名冲突）。
DEFAULT_REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports"
# structure 模版 function 取值：按检测点区分具体的语义失败类型。
STRUCT_FUNCTION_FINAL = "assistant总结的语义失败"   # 点1_回合终答
STRUCT_FUNCTION_STEP = "工具调用的语义失败"          # 点2_逐条
# structure 模版 addition.检测点 的详细说明（替代笼统的“点1_回合终答/点2_逐条”内部代号）。
DETECT_POINT_DESC = {
    "点1_回合终答": "回合终答语义失败：assistant 针对本回合用户请求给出的最终答复，"
                    "与事实/工具证据相矛盾，或对用户承诺的事项并未真正兑现。",
    "点2_逐条": "回合内逐条步骤语义失败：assistant 在回合内某一步对工具调用结果的理解或声称"
                "与实际不符（如误解结果、夸大或虚构完成度、无证据支撑的断言等）。",
}
# 各建议策略对应的修复方案说明（写入 addition.修复方案）：可自动修复的给自动处理方案，
# 不可自动修复的给人工/整轮处理方案，确保每条 sub 都有可执行的下一步。
REPAIR_PLAN_BY_STRATEGY = {
    # —— 可自动修复 ——
    "delete_sentence": "删除该失败语句（与证据矛盾或未兑现承诺的那一句），保留同段其余正确内容，"
                       "保持上下文连贯，无需人工介入。",
    "downgrade_claim": "将过度/绝对化的声称降级为与证据相符的措辞（例如把“已完成/已通过/已验证”"
                       "改为“未完成/仍失败/未验证”），其余内容保留。",
    # —— 需人工/整轮处理 ——
    "rewrite_final": "重写终答：最终答复整体偏离事实/证据，需人工依据工具证据重写该回合的最终答复，"
                     "使结论与实际结果一致。",
    "drop_round": "整轮删除：该回合失败无法靠删句/降级修复（如终答逻辑不连贯），需将该用户回合"
                  "及其后续 assistant/tool 节点整体删除；建议人工确认不影响上下文后再删。",
    "exclude_session": "整 session 排除：失败出现在中间轮且无法局部修复，建议将整个 session 从数据集中"
                       "排除，不再单独修补。",
    "manual_review": "人工核对：失败存在歧义（如自相矛盾的两处数据、工具选择/参数是否恰当需结合任务判断），"
                     "需人工核对证据后再决定删句/降级/整轮删或保留。",
}

MODEL_TIMEOUT_SECONDS = 120
HARD_CALL_TIMEOUT = 300
JUDGE_RETRIES = 2
CONFIDENCE_THRESHOLD = float(os.getenv("SFD_CONFIDENCE", "0.5"))

# 并发
# 并发默认值按 API 中转站可承受的 30 并发请求调优：
#   LLM_CONCURRENCY=30 是真正打到端点的“在飞请求”硬上限（= 中转站上限）。
#   TASK×ROUND=16×4=64 逻辑并行，足以喂满 30；实际在飞由 LLM_SEM 兜到 30。
TASK_WORKERS = int(os.getenv("SFD_TASK_WORKERS", "16"))
ROUND_WORKERS = int(os.getenv("SFD_ROUND_WORKERS", "4"))
LLM_CONCURRENCY = int(os.getenv("SFD_LLM_CONCURRENCY", "30"))
LLM_SEM = threading.Semaphore(LLM_CONCURRENCY)

# 文本预算
MAX_USER_CHARS = 4000
MAX_FINAL_CHARS = 5000
TASK_PROMPT_CHARS = 1800
HISTORY_ROUNDS = 3
HIST_USER_CHARS = 220
HIST_FINAL_CHARS = 340
PREVIEW_CHARS = 240
STEP_TEXT_CHARS = 1200
STEP_RESULT_CHARS = 900

# 点2 大回合窗口化：单回合渲染超预算时切成有界窗口分别判、再合并（点1 不受影响）。
# 阈值 24000 依据 ua_cases 实测分布（~69% 回合仍单次调用，零行为变化）。
P2_WINDOW_CHAR_BUDGET = int(os.getenv("SFD_P2_WINDOW_CHARS", "24000"))
P2_PRIOR_DIGEST_BUDGET = 3000     # 每个后续窗口携带的“前序步骤只读摘要”总预算
P2_PRIOR_STEP_CHARS = 160         # 前序摘要里每步 say 的压缩长度

FINAL_TYPES = {
    "off_topic", "contradicts_evidence", "fabricated", "incoherent",
    "unfulfilled_promise", "self_contradiction",
}
STEP_TYPES = {
    "unfulfilled_promise", "misunderstood", "self_contradiction",
    "unsupported_claim", "illogical",              # assistant 语义失败
    "wrong_tool", "wrong_args", "blind_retry", "result_contradicts_intent",  # 工具语义失败
}

# ---- 修复策略映射（供下游修复脚本直接分支；纯静态推导，不额外调 LLM）----
# 建议动作枚举：
#   rewrite_final     重写整段回合终答（答非所问，需照 user 诉求重答）
#   delete_sentence   删除终答里的虚假/未兑现句子（保留其余真实内容）
#   downgrade_claim   把“已完成/已通过”降级为符合证据的真实状态（未完成/仍失败/未验证）
#   drop_round        整轮删除（无法靠删句/降级修好）
#   exclude_session   整个 session 排除（中间轮无法修复时）
#   manual_review     需人工核对证据后再定（如自相矛盾的两个数字取哪个）
FINAL_STRATEGY = {
    "off_topic": "rewrite_final",
    "contradicts_evidence": "downgrade_claim",
    "fabricated": "delete_sentence",
    "incoherent": "drop_round",
    "unfulfilled_promise": "downgrade_claim",
    "self_contradiction": "manual_review",
}
STEP_STRATEGY = {
    "unfulfilled_promise": "downgrade_claim",
    "misunderstood": "manual_review",
    "self_contradiction": "manual_review",
    "unsupported_claim": "delete_sentence",
    "illogical": "delete_sentence",
    "wrong_tool": "manual_review",
    "wrong_args": "manual_review",
    "blind_retry": "manual_review",
    "result_contradicts_intent": "downgrade_claim",
}
# 可自动修复的策略（删句/降级），下游脚本可据此自动处理；其余需人工/整轮处理。
AUTO_REPAIR_STRATEGIES = {"delete_sentence", "downgrade_claim"}


def _fingerprint(project_dir: str, file_id: str, point: str,
                 round_idx: int, msg_index: int, ftype: str) -> str:
    """稳定失败指纹：同一失败项跨多次运行/与 Excel 对账用；便于修复脚本做幂等去重。"""
    raw = f"{project_dir}|{file_id}|{point}|r{round_idx}|m{msg_index}|{ftype}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ============================================================
# 点1 检测：回合终答语义失败（复用 ua 四类 + 两类补充）
# ============================================================
FINAL_SYSTEM_PROMPT = """你要核查 Claude Code 轨迹里，某一轮 assistant 的**最终回答（ASSISTANT_FINAL）**，
从**语义逻辑层面**判断它是否对得上 CURRENT_USER 这一轮的诉求（不只是命令有没有报错）。

给你的情景：
- TASK：会话最初任务/需求。
- HISTORY：之前几轮简要（可证明某些工作是前几轮做的）。
- CURRENT_USER：本轮用户诉求（判“对没对上”的基准）。
- TOOL_ACTIVITY：本轮 assistant 实际调用的工具及其返回结果（核对真伪的证据）。
- ASSISTANT_FINAL：本轮最终自然语言回答（评判对象）。

【命中类型 failure_type（六选一）】
- off_topic             ：答非所问——终答在讲与 CURRENT_USER 诉求无关的另一件事。
- contradicts_evidence  ：终答与 CURRENT_USER 贴出的内容或本轮可见工具结果直接矛盾（如测试失败却说全通过）。
- fabricated            ：凭空编造——声称的结果/事物在 CURRENT_USER/HISTORY/TOOL_ACTIVITY 里都没有也推不出。
- incoherent            ：语无伦次/乱码/串话，或在讲与本会话完全无关的项目。
- unfulfilled_promise   ：终答本身声明“我将/正在做 X、下面给出 X”，但终答里并没有真正交付 X（承诺未兑现）。
- self_contradiction    ：终答内部前后自相矛盾（同一答里给出互斥的数字/结论，如既说 52 个又说 53 个端点）。

【防误报纪律——宁漏勿误报】
1. 前几轮或更早会话做过的活，终答只是总结，不算 fabricated；只有**本轮可见证据给出相反的具体结果**才判 contradicts_evidence。
2. 看 TOOL_ACTIVITY 开头“本轮工具统计”：统计完整，靠前事件正文可能被省略；有 Write/Edit 就是写了文件，有 Bash 就是跑了命令，别因未展开细节就说“没做/没验证”。
3. 诚实的“尚未验证/未完成/仍失败”、先错后自我纠正、按要求用文字给出内容/计划，都算对上 → ok。
4. 多阶段任务的中途进度播报/衔接语（“Phase 1 complete, moving on”）属正常推进，不判 off_topic。

返回严格 JSON（不要 Markdown、不要多余文字）：
- verdict: "ok" | "failure" | "uncertain"
- failure_type: verdict="failure" 时给上面六类之一；否则 ""。
- confidence: 0 到 1。
- reason: 中文一句话，点名 user 要什么、终答哪里对不上/编造/未兑现/自相矛盾。
- evidence: 数组，最多 4 条，摘录终答或工具结果里的关键原话。
"""


def _judge_final_once(agent: Any, *, task: str, history: str, tool_activity: str,
                      cur_user: str, final: str) -> dict[str, Any]:
    prompt = (
        "从语义逻辑层面判断 ASSISTANT_FINAL 是否对得上 CURRENT_USER。\n\n"
        "== TASK ==\n" f"{excerpt(task, TASK_PROMPT_CHARS) if task.strip() else '（无）'}\n\n"
        "== HISTORY ==\n" f"{history if history.strip() else '（无，或即为首轮）'}\n\n"
        "== CURRENT_USER ==\n" f"{excerpt(cur_user, MAX_USER_CHARS)}\n\n"
        "== TOOL_ACTIVITY ==\n" f"{tool_activity if tool_activity.strip() else '（本轮未调用工具）'}\n\n"
        "== ASSISTANT_FINAL ==\n" f"{excerpt(final, MAX_FINAL_CHARS)}"
    )
    with LLM_SEM:
        resp = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    parsed = parse_agent_json(extract_last_ai_content(resp))
    return _normalize_final(parsed)


def _normalize_final(parsed: dict[str, Any]) -> dict[str, Any]:
    verdict = parsed.get("verdict")
    if verdict not in {"ok", "failure", "uncertain"}:
        raise ValueError(f"invalid verdict: {verdict!r}")
    ftype = str(parsed.get("failure_type", "") or "").strip()
    if verdict != "failure":
        ftype = ""
    elif ftype not in FINAL_TYPES:
        ftype = "off_topic"
    conf = parsed.get("confidence", 0.0)
    if not isinstance(conf, (int, float)):
        conf = 0.0
    reason = parsed.get("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        reason = "模型未返回明确原因。"
    ev = parsed.get("evidence", [])
    if not isinstance(ev, list):
        ev = []
    ev = [str(e) for e in ev[:4] if str(e).strip()]
    return {"verdict": verdict, "failure_type": ftype,
            "confidence": max(0.0, min(1.0, float(conf))), "reason": reason.strip(), "evidence": ev}


# ============================================================
# 点2 检测：round 内逐条失败计数
# ============================================================
STEP_SYSTEM_PROMPT = """你要核查 Claude Code 轨迹里，某一整轮（一个 user 到下一个 user 之间）内部，
**逐条**响应有没有出现语义失败，从而数清“本轮中间失败了几次、分别失败在什么地方”。
注意：是语义逻辑层面的失败，不只是某条命令 exit code≠0；命令报错但 agent 已正确识别并妥善处理，不算失败。

给你的情景：
- CURRENT_USER：本轮用户诉求。
- STEPS：本轮按时间顺序排列的响应序列。每步是 assistant 的一次响应，含：
    · say  ：该步 assistant 的自然语言/推理（可能为空）
    · calls：该步发起的工具调用（工具名+参数摘要，可能为空）
    · results：这些调用对应的工具返回结果（证据，可能被压缩，统计以调用为准）

对**每一步**判断它是否是一次语义失败。可用类型 failure_type：
  assistant 语义失败：
    - unfulfilled_promise  该步说要做 X 却没在本步或后续真正做
    - misunderstood        误解了 user 诉求，往错误方向走
    - self_contradiction   与本轮已出现的内容/结论自相矛盾
    - unsupported_claim    无证据地宣称“已完成/已修复/测试通过/文件已创建”（该步之前没有支撑证据）
    - illogical            逻辑不通、语无伦次、串话
  工具调用语义失败（语义层，非单纯 exit≠0）：
    - wrong_tool           为达成目标选错了工具
    - wrong_args           传了明显错误的参数（如错路径、错命令名）
    - blind_retry          对一个**已知失败**的调用几乎原样重试，不换命令/路径/思路
    - result_contradicts_intent  工具结果与该步声明的意图相反，且该步没有承认/纠正

判定纪律（宁漏勿误报）：
- 只用**该步及其之前**已经出现的信息判定，不能用后面的成功结果反推前面无据声明是对的。
- 正常的进度播报（“现在开始处理/接下来运行测试”）、诚实地报告失败、失败后**换了**命令/路径/思路，都不算失败。
- 一次探索性 Read/ls 返回“No such file”，若该步随后据此调整，属正常，不算失败。

返回严格 JSON（不要 Markdown、不要多余文字）：
{"steps": [ {"i": 步序号(从0起,与输入一致), "failure": true|false,
             "failure_type": 失败时给上面某个类型否则"", "confidence": 0到1,
             "reason": "中文一句话，指明这步失败在哪",
             "evidence": ["最多3条，摘录本步 say 或工具结果里的关键原话，用于核对声称与实际；无则空数组"]} , ... ] }
只对确实失败的步给 failure=true；其余 failure=false。steps 必须覆盖输入的每一步。
evidence 只对 failure=true 的步给；失败步应尽量给出它所依据的原话（如工具返回的报错、退出码，或该步无据宣称的原句）。
"""


def _build_steps(rd: dict[str, Any]) -> list[dict[str, Any]]:
    """把一个回合拆成按时间顺序的 assistant 响应步；每步带自己的 calls 与对应 tool 结果。"""
    steps: list[dict[str, Any]] = []
    tool_cursor = 0
    tools = rd["tools"]
    for am in rd["asst"]:
        calls = am["tcalls"]
        results: list[str] = []
        for _ in calls:
            if tool_cursor < len(tools):
                results.append(tools[tool_cursor]["result"])
                tool_cursor += 1
        steps.append({"idx": am["idx"], "say": am["text"], "calls": calls, "results": results})
    return steps


def _render_one_step(gi: int, st: dict[str, Any]) -> str:
    """渲染单步（标题用全局序号 gi），复用 say/call 截断规则。"""
    lines: list[str] = []
    say = norm(st["say"])[:STEP_TEXT_CHARS] or "（无文字）"
    lines.append(f"── STEP {gi} ──")
    lines.append(f"  say: {say}")
    if st["calls"]:
        for j, (name, args) in enumerate(st["calls"]):
            res = st["results"][j] if j < len(st["results"]) else ""
            res = norm(res)[:STEP_RESULT_CHARS] if res else "（无返回）"
            lines.append(f"  call: {name}({args})")
            lines.append(f"    -> {res}")
    else:
        lines.append("  call: （本步未调用工具）")
    return "\n".join(lines)


def _render_steps_window(pairs: list[tuple[int, dict[str, Any]]]) -> str:
    """按给定 (全局序号, step) 列表渲染窗口内的步。"""
    return "\n".join(_render_one_step(gi, st) for gi, st in pairs)


def _render_steps(steps: list[dict[str, Any]]) -> str:
    """整回合渲染（单次调用路径）；序号即步序号，输出与窗口路径同格式。"""
    return _render_steps_window(list(enumerate(steps)))


def _render_prior_digest(prior_pairs: list[tuple[int, dict[str, Any]]]) -> str:
    """把前序步压成每步一行只读摘要，从最近往前填到 P2_PRIOR_DIGEST_BUDGET。

    供后续窗口判断“证据是否已在前文出现”（防误判 unsupported_claim）及跨窗口
    self_contradiction/blind_retry 线索。只读，不参与打分。
    """
    picked: list[str] = []
    total = 0
    for gi, st in reversed(prior_pairs):
        say = norm(st["say"])[:P2_PRIOR_STEP_CHARS] or "（无文字）"
        names = "、".join(name for name, _ in st["calls"]) if st["calls"] else "无"
        tail = ""
        if st["calls"]:
            last_res = st["results"][-1] if st["results"] else ""
            tail = norm(last_res)[-120:] if last_res else ""
        line = f"[STEP {gi}] {say} | tools: {names}" + (f" | 末结果尾: {tail}" if tail else "")
        if total + len(line) + 1 > P2_PRIOR_DIGEST_BUDGET and picked:
            break
        picked.append(line)
        total += len(line) + 1
    return "\n".join(reversed(picked))


def _pack_step_windows(steps: list[dict[str, Any]]) -> list[list[int]]:
    """把步按渲染尺寸贪心打包成窗口，返回全局索引分组。

    每窗口渲染尺寸尽量 ≤ P2_WINDOW_CHAR_BUDGET；单步自身超预算时自成一窗
    （靠 per-step 截断兜底）。窗口间不相交且覆盖全部步、保序。
    """
    windows: list[list[int]] = []
    cur: list[int] = []
    cur_size = 0
    for gi, st in enumerate(steps):
        seg = len(_render_one_step(gi, st)) + 1
        if cur and cur_size + seg > P2_WINDOW_CHAR_BUDGET:
            windows.append(cur)
            cur = []
            cur_size = 0
        cur.append(gi)
        cur_size += seg
    if cur:
        windows.append(cur)
    return windows


def _call_step_model(agent: Any, prompt: str) -> dict[int, dict[str, Any]]:
    """打一次点2模型调用，解析出 {全局步序号 i: 原始判定项}。"""
    with LLM_SEM:
        resp = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    parsed = parse_agent_json(extract_last_ai_content(resp))
    raw = parsed.get("steps")
    if not isinstance(raw, list):
        raise ValueError("steps field missing or not a list")
    by_i: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        by_i[i] = item
    return by_i


def _finalize_steps(steps: list[dict[str, Any]], by_i: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """按完整 steps 列表归一化为逐步结果；缺失步默认 failure=false。"""
    out: list[dict[str, Any]] = []
    for i in range(len(steps)):
        item = by_i.get(i, {})
        failure = bool(item.get("failure"))
        ftype = str(item.get("failure_type", "") or "").strip()
        if not failure:
            ftype = ""
        elif ftype not in STEP_TYPES:
            ftype = "illogical"
        conf = item.get("confidence", 0.0)
        if not isinstance(conf, (int, float)):
            conf = 0.0
        reason = item.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        ev = item.get("evidence")
        if isinstance(ev, list):
            ev = [str(x).strip() for x in ev if str(x).strip()][:3]
        else:
            ev = []
        out.append({"i": i, "step_msg_index": steps[i]["idx"] + 1,
                    "failure": failure, "failure_type": ftype,
                    "confidence": max(0.0, min(1.0, float(conf))),
                    "reason": reason.strip(), "evidence": ev})
    return out


def _judge_steps_once(agent: Any, *, cur_user: str,
                      steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """逐条判本轮每步是否语义失败。返回 (逐步结果, 点2模型调用次数, 失败窗口数)。

    小回合(渲染 ≤ P2_WINDOW_CHAR_BUDGET)走单次调用，prompt 与改造前完全一致；单次失败向上抛。
    大回合切成有界窗口顺序判、带前序只读摘要，各窗口结果按全局步序号合并。
    **每个窗口调用各自受 _guard 硬超时/重试**（不是整回合共用一个 300s 预算，否则大回合
    多窗口顺序执行会必然超时）；某窗口重试后仍失败时，跳过该窗口（其步默认非失败）、
    计入失败窗口数，保留其余窗口结果，避免一处失败拖垮整个大回合。
    """
    windows = _pack_step_windows(steps)
    if len(windows) <= 1:
        prompt = (
            "逐条判断本轮每一步是否语义失败。\n\n"
            "== CURRENT_USER ==\n" f"{excerpt(cur_user, MAX_USER_CHARS)}\n\n"
            "== STEPS ==\n" f"{_render_steps(steps)}"
        )
        by_i = _guard(_call_step_model, agent, prompt)
        return _finalize_steps(steps, by_i), 1, 0

    merged: dict[int, dict[str, Any]] = {}
    failed_windows = 0
    for idxs in windows:
        win_pairs = [(gi, steps[gi]) for gi in idxs]
        prior_pairs = [(gi, steps[gi]) for gi in range(idxs[0])]
        prior_digest = _render_prior_digest(prior_pairs) if prior_pairs else ""
        prompt = (
            "逐条判断本轮每一步是否语义失败。\n"
            "注意：本回合较大，已切分为多个窗口分别判定。PRIOR_CONTEXT 是本窗口之前步骤的只读摘要，"
            "仅供你判断某步所需证据是否此前已出现、是否与前文自相矛盾；**不要为 PRIOR_CONTEXT 里的步打分**。"
            "只对 STEPS 中出现的步给判定，i 一律用其全局序号。\n\n"
            "== CURRENT_USER ==\n" f"{excerpt(cur_user, MAX_USER_CHARS)}\n\n"
            + (f"== PRIOR_CONTEXT（前序步骤摘要·只读·勿打分）==\n{prior_digest}\n\n" if prior_digest else "")
            + "== STEPS ==\n" f"{_render_steps_window(win_pairs)}"
        )
        try:
            part = _guard(_call_step_model, agent, prompt)
        except Exception:  # noqa: BLE001
            failed_windows += 1
            continue
        # 防御：只采纳本窗口索引的判定，杜绝模型误给前序步打分导致跨窗口污染。
        for gi in idxs:
            if gi in part:
                merged[gi] = part[gi]
    return _finalize_steps(steps, merged), len(windows), failed_windows


# ============================================================
# 硬墙 + 重试
# ============================================================
def _guard(fn, *args, **kw):
    last: Exception | None = None
    for attempt in range(JUDGE_RETRIES + 1):
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(fn, *args, **kw)
        try:
            res = fut.result(timeout=HARD_CALL_TIMEOUT)
            ex.shutdown(wait=False)
            return res
        except FuturesTimeout:
            ex.shutdown(wait=False); last = TimeoutError(f"exceeded hard timeout {HARD_CALL_TIMEOUT}s")
        except Exception as exc:  # noqa: BLE001
            ex.shutdown(wait=False); last = exc
        if attempt < JUDGE_RETRIES:
            time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("guarded call failed")


# ============================================================
# 数据结构
# ============================================================
@dataclass
class StepFailure:
    round: int
    step_index: int          # 步在本轮的序号（0起）
    step_msg_index: int      # 该 assistant 消息 1-based index
    step_line: int
    failure_type: str
    confidence: float
    reason: str
    step_excerpt: str = ""   # 该步 assistant 原文摘要（修复脚本定位用）
    evidence: list[str] = field(default_factory=list)  # 依据的工具结果/原句（核对声称vs实际）
    timestamp: str = ""      # 该步 assistant 节点 _metadata.timestamp（原样透传）


@dataclass
class FinalFailure:
    round: int
    user_index: int
    user_line: int
    asst_index: int
    asst_line: int
    failure_type: str
    confidence: float
    reason: str
    evidence: list[str]
    user_excerpt: str
    asst_excerpt: str
    is_last_round: bool = False   # 该失败回合是否为本 session 最后一个判定回合（末轮可删句/整轮删）
    timestamp: str = ""           # 该回合终答 assistant 节点 _metadata.timestamp（原样透传）


@dataclass
class SessionResult:
    project_dir: str
    session_path: str
    is_subagent: bool = False
    parent_session: str = ""      # 所属主 session 文件名（subagent 用）
    valid_session: bool = True
    error: str = ""
    rounds_total: int = 0
    rounds_judged: int = 0
    rounds_skipped: int = 0
    model_failed: bool = False
    # 点2 窗口化可观测：本 session 走窗口化的回合数 / 额外触发的点2调用次数
    p2_windowed_rounds: int = 0
    p2_window_calls: int = 0
    final_failures: list[FinalFailure] = field(default_factory=list)
    step_failures: list[StepFailure] = field(default_factory=list)
    # 低置信备查（不计入总数）
    final_lowconf: list[FinalFailure] = field(default_factory=list)
    step_lowconf: list[StepFailure] = field(default_factory=list)


# ============================================================
# 单 session 处理
# ============================================================
def _node_ts(messages: list[dict[str, Any]], i: int) -> str:
    """取第 i 条消息的 _metadata.timestamp；越界或字段缺失/非字符串时返回空串。"""
    if 0 <= i < len(messages):
        md = messages[i].get("_metadata")
        if isinstance(md, dict):
            ts = md.get("timestamp")
            if isinstance(ts, str):
                return ts
    return ""


def process_one_session(project_dir: Path, session_path: Path, agents: dict[str, Any],
                        *, is_subagent: bool, parent_session: str) -> SessionResult:
    result = SessionResult(project_dir=project_dir.name, session_path=str(session_path),
                           is_subagent=is_subagent, parent_session=parent_session)
    if not session_path.exists():
        result.valid_session = False; result.error = "session file does not exist"; return result
    try:
        messages = load_messages(session_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.valid_session = False; result.error = str(exc); return result

    elem_lines = messages_element_lines(session_path)

    def line_of(i: int) -> int:
        return elem_lines[i] if i < len(elem_lines) else 1

    # 最初任务
    task_prompt = ""
    if messages:
        if messages[0].get("role") in ("system", "user"):
            task_prompt = normalize_content(messages[0].get("content"))
        if len(messages) > 1 and messages[0].get("role") == "system" and messages[1].get("role") == "user":
            u1 = normalize_content(messages[1].get("content"))
            if len(u1) > len(task_prompt):
                task_prompt = u1

    # 回合切分（与 ua 同口径）
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
                                "tcalls": tool_calls_detail(m)})
        elif r == "tool" and cur is not None:
            cur["tools"].append({"idx": idx, "tid": m.get("tool_call_id", ""),
                                 "result": normalize_content(m.get("content"))})
    if cur:
        rounds.append(cur)

    # 组装可判回合
    final_units: list[dict[str, Any]] = []
    step_units: list[dict[str, Any]] = []
    for ri, rd in enumerate(rounds):
        if not rd["asst"]:
            continue
        result.rounds_total += 1
        # 跳过规则同 ua
        if user_kind(rd["user"]) not in {"request", "session-cont"}:
            result.rounds_skipped += 1; continue
        if rd["asst"][-1]["tcalls"]:      # 末答只有工具调用 → 跳过
            result.rounds_skipped += 1; continue
        final = ""; fidx = rd["asst"][-1]["idx"]
        for a in reversed(rd["asst"]):
            if a["text"].strip():
                final = a["text"]; fidx = a["idx"]; break
        if not final.strip():
            result.rounds_skipped += 1; continue

        # 历史摘要
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

        final_units.append({"ri": ri, "uidx": rd["uidx"], "fidx": fidx, "user": rd["user"],
                            "final": final, "history": history,
                            "tool_activity": build_tool_activity(rd)})
        step_units.append({"ri": ri, "user": rd["user"], "steps": _build_steps(rd)})

    # 点1：回合终答
    def handle_final(u: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
        try:
            res = _guard(_judge_final_once, agents["final"], task=task_prompt, history=u["history"],
                         tool_activity=u["tool_activity"], cur_user=u["user"], final=u["final"])
            return u, res, ""
        except Exception as exc:  # noqa: BLE001
            return u, None, str(exc)

    # 点2：逐条步。返回 (u, 逐步结果, 点2调用次数, 失败窗口数, err)
    # 注意：guard 已下沉到 _judge_steps_once 内部逐窗口进行，这里不再整体包 _guard，
    # 否则大回合多窗口顺序执行会共用单个 300s 硬超时而必然超时。
    def handle_steps(u: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | None, int, int, str]:
        if not u["steps"]:
            return u, [], 0, 0, ""
        try:
            res, n_calls, n_failed = _judge_steps_once(agents["step"], cur_user=u["user"], steps=u["steps"])
            return u, res, n_calls, n_failed, ""
        except Exception as exc:  # noqa: BLE001
            return u, None, 0, 0, str(exc)

    rw = max(1, min(ROUND_WORKERS, max(len(final_units), 1)))
    with ThreadPoolExecutor(max_workers=rw) as ex:
        final_out = list(ex.map(handle_final, final_units))
        step_out = list(ex.map(handle_steps, step_units))

    # 最后一个被判定的回合序号（末轮失败可删句/整轮删；中间轮失败往往只能整 session 排除）
    last_judged_round = max((u["ri"] for u in final_units), default=-1)

    for u, res, err in final_out:
        result.rounds_judged += 1
        if err:
            result.model_failed = True
            continue
        if res["verdict"] != "failure":
            continue
        ff = FinalFailure(
            round=u["ri"], user_index=u["uidx"] + 1, user_line=line_of(u["uidx"]),
            asst_index=u["fidx"] + 1, asst_line=line_of(u["fidx"]),
            failure_type=res["failure_type"], confidence=res["confidence"],
            reason=res["reason"], evidence=res["evidence"],
            user_excerpt=norm(u["user"])[:PREVIEW_CHARS], asst_excerpt=norm(u["final"])[:PREVIEW_CHARS],
            is_last_round=(u["ri"] == last_judged_round),
            timestamp=_node_ts(messages, u["fidx"]))
        if res["confidence"] >= CONFIDENCE_THRESHOLD:
            result.final_failures.append(ff)
        else:
            result.final_lowconf.append(ff)

    for u, res, n_calls, n_failed, err in step_out:
        if err:
            result.model_failed = True
            continue
        if n_calls > 1:
            result.p2_windowed_rounds += 1
            result.p2_window_calls += n_calls
        if n_failed > 0:
            # 大回合个别窗口重试后仍失败：保留其余窗口结果，但标记模型报错以便报告透明。
            result.model_failed = True
        if not res:
            continue
        for st in res:
            if not st["failure"]:
                continue
            i = st["i"]
            say = u["steps"][i]["say"] if 0 <= i < len(u["steps"]) else ""
            sf = StepFailure(
                round=u["ri"], step_index=i, step_msg_index=st["step_msg_index"],
                step_line=line_of(st["step_msg_index"] - 1),
                failure_type=st["failure_type"], confidence=st["confidence"], reason=st["reason"],
                step_excerpt=norm(say)[:PREVIEW_CHARS], evidence=st.get("evidence", []),
                timestamp=_node_ts(messages, st["step_msg_index"] - 1))
            if st["confidence"] >= CONFIDENCE_THRESHOLD:
                result.step_failures.append(sf)
            else:
                result.step_lowconf.append(sf)

    result.final_failures.sort(key=lambda f: f.round)
    result.step_failures.sort(key=lambda f: (f.round, f.step_index))
    return result


# ============================================================
# 遍历（主 session + subagents）
# ============================================================
def _is_subagent_path(p: Path) -> bool:
    return any(part in ("subagents", "subagent") or "__subagents__" in part for part in p.parts)


def iter_all_sessions(root: Path) -> list[tuple[Path, Path, bool, str]]:
    """返回 (project_dir, session_json, is_subagent, parent_session_name)。"""
    out: list[tuple[Path, Path, bool, str]] = []
    if (root / "sessions").is_dir():
        subs = [root]
    else:
        subs = sorted(root.iterdir(), key=lambda p: p.name)
    for sub in subs:
        if not sub.is_dir() or sub.name.startswith(".") or "__subagents__" in sub.name:
            continue
        sess_dir = sub / "sessions"
        if not sess_dir.is_dir():
            legacy = sub / f"{sub.name}.json"
            if legacy.is_file():
                out.append((sub, legacy, False, ""))
            continue
        for jf in sorted(sess_dir.glob("*.json")):
            if jf.is_file():
                out.append((sub, jf, False, ""))
        # subagents：主 session 同名目录下的 subagents/
        for sub_json in sorted(sess_dir.rglob("*.json")):
            if not sub_json.is_file() or not _is_subagent_path(sub_json):
                continue
            # 找所属主 session：subagents 目录的父目录名 = 主 session stem
            parent = ""
            for anc in sub_json.parents:
                if anc.name in ("subagents", "subagent"):
                    parent = anc.parent.name
                    break
            out.append((sub, sub_json, True, parent))
    return out


def build_jobs(root: Path, one_file: Path | None) -> list[tuple[Path, Path, bool, str]]:
    if one_file is not None:
        if one_file.is_absolute():
            f = one_file
        else:
            # -i 相对路径：先按 CWD 解析，不存在再退回相对 root（避免 root/CWD 前缀重复叠加）。
            cwd_rel = (Path.cwd() / one_file).expanduser()
            f = cwd_rel if cwd_rel.exists() else (root / one_file if root else one_file)
        f = f.expanduser().resolve()
        is_sub = _is_subagent_path(f)
        parent = ""
        if is_sub:
            for anc in f.parents:
                if anc.name in ("subagents", "subagent"):
                    parent = anc.parent.name
                    break
        proj = f.parent.parent if f.parent.name == "sessions" else f.parent
        if is_sub:
            # 主 session 目录在 sessions/ 之下
            for anc in f.parents:
                if anc.name == "sessions":
                    proj = anc.parent
                    break
        return [(proj, f, is_sub, parent)]
    return iter_all_sessions(root)


# ============================================================
# 报告
# ============================================================
def _agg(results: list[SessionResult]) -> dict[str, Any]:
    final_types: Counter = Counter()
    step_types: Counter = Counter()
    for r in results:
        for f in r.final_failures:
            final_types[f.failure_type] += 1
        for s in r.step_failures:
            step_types[s.failure_type] += 1
    return {"final_type_dist": dict(final_types), "step_type_dist": dict(step_types)}


def _ratio(n: int, d: int) -> float:
    """占比（0~1，保留 4 位）；分母为 0 时返回 0。"""
    return round(n / d, 4) if d else 0.0


def _pct(n: int, d: int) -> str:
    """占比百分比字符串，如 '12.3%'；分母为 0 返回 'N/A'。"""
    return f"{n / d * 100:.1f}%" if d else "N/A"


def write_reports(root: str, results: list[SessionResult], report_dir: Path,
                  write_files: bool = True) -> dict[str, Any]:
    if write_files:
        report_dir.mkdir(parents=True, exist_ok=True)
    total = len(results)
    invalid = [r for r in results if not r.valid_session]
    model_err = [r for r in results if r.model_failed]

    # 逐 session 明细
    session_items: list[dict[str, Any]] = []
    for r in results:
        rounds_with_step_fail = len({s.round for s in r.step_failures})
        judged = r.rounds_judged
        n_p1 = len(r.final_failures)
        n_p2 = len(r.step_failures)
        file_id = Path(r.session_path).name
        # 严重度评分：终答失败率权重更高（点1直接影响回合可用性），点2按平均每回合叠加。
        severity = round(_ratio(n_p1, judged) * 2.0 + _ratio(n_p2, judged), 4)
        p1_details = []
        for f in r.final_failures:
            strat = FINAL_STRATEGY.get(f.failure_type, "manual_review")
            p1_details.append({
                "指纹": _fingerprint(r.project_dir, file_id, "p1", f.round, f.asst_index, f.failure_type),
                "回合": f.round, "末轮": f.is_last_round,
                "assistant_message_index": f.asst_index,
                "assistant_message_index_0based": f.asst_index - 1, "assistant_行号": f.asst_line,
                "user_行号": f.user_line, "失败类型": f.failure_type,
                "建议策略": strat, "可自动修复": strat in AUTO_REPAIR_STRATEGIES,
                "置信": round(f.confidence, 3), "理由": f.reason, "证据": f.evidence,
                "user摘要": f.user_excerpt, "终答摘要": f.asst_excerpt})
        p2_details = []
        for s in r.step_failures:
            strat = STEP_STRATEGY.get(s.failure_type, "manual_review")
            p2_details.append({
                "指纹": _fingerprint(r.project_dir, file_id, "p2", s.round, s.step_msg_index, s.failure_type),
                "回合": s.round, "步序号": s.step_index, "assistant_message_index": s.step_msg_index,
                "assistant_message_index_0based": s.step_msg_index - 1, "assistant_行号": s.step_line,
                "失败类型": s.failure_type,
                "建议策略": strat, "可自动修复": strat in AUTO_REPAIR_STRATEGIES,
                "置信": round(s.confidence, 3), "理由": s.reason, "证据": s.evidence,
                "步原文摘要": s.step_excerpt})
        session_items.append({
            "产物id": r.project_dir,
            "文件id": file_id,
            "类型": "subagent" if r.is_subagent else "main",
            "所属主session": r.parent_session if r.is_subagent else "",
            "有效": r.valid_session,
            "错误": r.error,
            "回合总数": r.rounds_total,
            "已判回合": judged,
            "跳过回合": r.rounds_skipped,
            "严重度": severity,
            # ---- 数量 + 占比（分母=判定回合数）----
            "点1_终答失败数": n_p1,
            "点1_失败占比": _pct(n_p1, judged),
            "点1_失败率": _ratio(n_p1, judged),
            "点2_逐条失败总数": n_p2,
            "点2_含失败的回合数": rounds_with_step_fail,
            "点2_含失败回合占比": _pct(rounds_with_step_fail, judged),
            "点2_含失败回合率": _ratio(rounds_with_step_fail, judged),
            "点2_平均每回合失败数": _ratio(n_p2, judged),
            "可自动修复项数": (sum(1 for d in p1_details if d["可自动修复"])
                          + sum(1 for d in p2_details if d["可自动修复"])),
            "低置信_终答": len(r.final_lowconf),
            "低置信_逐条": len(r.step_lowconf),
            "点1明细": p1_details,
            "点2明细": p2_details,
        })

    # subagent roll-up 到主 session
    rollup: dict[str, dict[str, Any]] = {}
    for r in results:
        if r.is_subagent and r.parent_session:
            key = f"{r.project_dir}/{r.parent_session}"
            acc = rollup.setdefault(key, {"产物id": r.project_dir, "主session": r.parent_session,
                                          "subagent数": 0, "subagent_点1失败": 0, "subagent_点2失败": 0})
            acc["subagent数"] += 1
            acc["subagent_点1失败"] += len(r.final_failures)
            acc["subagent_点2失败"] += len(r.step_failures)

    agg = _agg(results)
    tot_rounds = sum(r.rounds_total for r in results)
    judged_rounds = sum(r.rounds_judged for r in results)
    skipped_rounds = sum(r.rounds_skipped for r in results)
    n_p1 = sum(len(r.final_failures) for r in results)
    n_p2 = sum(len(r.step_failures) for r in results)
    rounds_with_p2 = sum(len({s.round for s in r.step_failures}) for r in results)
    # 建议策略分布 + 可自动修复计数（跨点1/点2合并；驱动下游修复脚本分支与工作量估算）
    strat_dist: Counter = Counter()
    auto_repairable = 0
    for r in results:
        for f in r.final_failures:
            st = FINAL_STRATEGY.get(f.failure_type, "manual_review")
            strat_dist[st] += 1
            if st in AUTO_REPAIR_STRATEGIES:
                auto_repairable += 1
        for s in r.step_failures:
            st = STEP_STRATEGY.get(s.failure_type, "manual_review")
            strat_dist[st] += 1
            if st in AUTO_REPAIR_STRATEGIES:
                auto_repairable += 1
    total_fail = n_p1 + n_p2
    # session 明细按严重度降序，便于修复脚本/人工从最脏的轨迹先处理
    session_items.sort(key=lambda it: (it.get("严重度", 0.0), it["点1_终答失败数"]), reverse=True)
    summary = {
        "脚本": SCRIPT_STEM, "源路径": str(root),
        "生成时间": datetime.now().isoformat(timespec="seconds"),
        "扫描文件数": total,
        "主session数": sum(1 for r in results if not r.is_subagent),
        "subagent数": sum(1 for r in results if r.is_subagent),
        "无效文件": len(invalid),
        "模型报错文件": len(model_err),
        # ---- 回合总量（分母基准）----
        "回合统计": {
            "回合总数(含跳过)": tot_rounds,
            "判定回合数(分母)": judged_rounds,
            "跳过回合数": skipped_rounds,
        },
        # ---- 点1：每回合一个判定，分母=判定回合数 ----
        "点1统计": {
            "终答失败数": n_p1,
            "判定回合数": judged_rounds,
            "失败占比": _pct(n_p1, judged_rounds),
            "失败率": _ratio(n_p1, judged_rounds),
            "类型分布": agg["final_type_dist"],
        },
        # ---- 点2：逐条计数，给两种口径 ----
        "点2统计": {
            "逐条失败总数": n_p2,
            "含失败的回合数": rounds_with_p2,
            "判定回合数": judged_rounds,
            "含失败回合占比": _pct(rounds_with_p2, judged_rounds),
            "含失败回合率": _ratio(rounds_with_p2, judged_rounds),
            "平均每回合失败数": _ratio(n_p2, judged_rounds),
            "类型分布": agg["step_type_dist"],
        },
        # ---- 修复导向：策略分布 + 可自动修复占比（供下游修复脚本分支/估算工作量）----
        "修复统计": {
            "失败总数(点1+点2)": total_fail,
            "建议策略分布": dict(strat_dist),
            "可自动修复项数": auto_repairable,
            "可自动修复占比": _pct(auto_repairable, total_fail),
            "需人工/整轮处理项数": total_fail - auto_repairable,
        },
        # ---- 点2 大回合窗口化统计（仅可观测；不影响计数语义）----
        "点2窗口化": {
            "窗口化回合数": sum(r.p2_windowed_rounds for r in results),
            "额外窗口调用数": sum(r.p2_window_calls for r in results),
            "触发阈值字符": P2_WINDOW_CHAR_BUDGET,
        },
        # ---- 兼容旧字段（保留，避免下游已读的键失效）----
        "点1_终答失败总数": n_p1,
        "点2_逐条失败总数": n_p2,
        "点1_类型分布": agg["final_type_dist"],
        "点2_类型分布": agg["step_type_dist"],
        "置信阈值": CONFIDENCE_THRESHOLD,
        "低置信_终答(备查)": sum(len(r.final_lowconf) for r in results),
        "低置信_逐条(备查)": sum(len(r.step_lowconf) for r in results),
        "并发": {"task_workers": TASK_WORKERS, "round_workers": ROUND_WORKERS, "llm_concurrency": LLM_CONCURRENCY},
    }
    report = {"summary": summary, "session明细": session_items,
              "subagent汇总": list(rollup.values()),
              "无效文件": [{"产物id": r.project_dir, "文件id": Path(r.session_path).name, "错误": r.error} for r in invalid],
              "模型错误文件": [{"产物id": r.project_dir, "文件id": Path(r.session_path).name} for r in model_err]}

    (report_dir / f"{SCRIPT_STEM}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8") if write_files else None

    # Markdown
    md: list[str] = [
        f"# {SCRIPT_STEM} 语义失败检测报告", "",
        f"- 源路径: `{root}`",
        f"- 生成: {summary['生成时间']}",
        f"- 扫描 {total} 文件（主 {summary['主session数']} · subagent {summary['subagent数']}）· 无效 {len(invalid)} · 模型报错 {len(model_err)}",
        f"- 置信阈值: {CONFIDENCE_THRESHOLD}（低置信仅备查，不计入总数）",
        f"- 回合: 总 {tot_rounds} · 判定 {judged_rounds}（分母）· 跳过 {skipped_rounds}",
        f"- 点1 终答失败: {n_p1}/{judged_rounds} = {_pct(n_p1, judged_rounds)}"
        f" · 类型 {json.dumps(agg['final_type_dist'], ensure_ascii=False)}",
        f"- 点2 逐条失败: {n_p2} 条 · 含失败回合 {rounds_with_p2}/{judged_rounds} = {_pct(rounds_with_p2, judged_rounds)}"
        f" · 平均每回合 {_ratio(n_p2, judged_rounds)}"
        f" · 类型 {json.dumps(agg['step_type_dist'], ensure_ascii=False)}",
        f"- 备查(低置信): 点1 {summary['低置信_终答(备查)']} · 点2 {summary['低置信_逐条(备查)']}",
        f"- 修复: 失败总数 {total_fail} · 可自动修复 {auto_repairable}（{_pct(auto_repairable, total_fail)}）"
        f" · 策略分布 {json.dumps(dict(strat_dist), ensure_ascii=False)}",
        "", "---", "",
    ]
    for it in session_items:
        if not it["点1明细"] and not it["点2明细"] and it["有效"]:
            continue
        head = f"## {it['产物id']} / {it['文件id']} · {it['类型']}"
        if it["类型"] == "subagent" and it["所属主session"]:
            head += f"（属 {it['所属主session']}）"
        md.append(head)
        if not it["有效"]:
            md.append(f"- 无效: {it['错误']}"); md.append(""); continue
        md.append(f"- 严重度 {it['严重度']} · 回合 {it['回合总数']}（判 {it['已判回合']} · 跳过 {it['跳过回合']}）"
                  f" · 点1失败 {it['点1_终答失败数']}（{it['点1_失败占比']}）"
                  f" · 点2失败 {it['点2_逐条失败总数']} 条"
                  f"（涉及 {it['点2_含失败的回合数']} 回合 · {it['点2_含失败回合占比']} · 均{it['点2_平均每回合失败数']}/回合）"
                  f" · 可自动修复 {it['可自动修复项数']}")
        if it["点1明细"]:
            md.append("")
            md.append("**点1 · 回合终答失败:**")
            for f in it["点1明细"]:
                tag = "末轮" if f["末轮"] else "中间轮"
                auto = "自动" if f["可自动修复"] else "人工"
                md.append(f"- 回合{f['回合']}({tag}) · #{f['assistant_message_index']}（行{f['assistant_行号']}）"
                          f" · [{f['失败类型']}] conf={f['置信']} · 策略={f['建议策略']}({auto}) · {f['指纹']}"
                          f" — {f['理由']}")
        if it["点2明细"]:
            md.append("")
            md.append("**点2 · round 内逐条失败:**")
            for s in it["点2明细"]:
                auto = "自动" if s["可自动修复"] else "人工"
                md.append(f"- 回合{s['回合']} 步{s['步序号']} · #{s['assistant_message_index']}（行{s['assistant_行号']}）"
                          f" · [{s['失败类型']}] conf={s['置信']} · 策略={s['建议策略']}({auto}) · {s['指纹']}"
                          f" — {s['理由']}")
        md.append("")
    if rollup:
        md.append("## subagent 汇总（roll-up 到主 session）")
        for v in rollup.values():
            md.append(f"- {v['产物id']}/{v['主session']} — subagent {v['subagent数']} 个 ·"
                      f" 点1失败 {v['subagent_点1失败']} · 点2失败 {v['subagent_点2失败']}")
        md.append("")
    if invalid:
        md.append("## 无效文件")
        for r in invalid:
            md.append(f"- {r.project_dir}/{Path(r.session_path).name} — {r.error}")
        md.append("")
    if model_err:
        md.append("## 模型报错文件")
        for r in model_err:
            md.append(f"- {r.project_dir}/{Path(r.session_path).name}")
    if write_files:
        (report_dir / f"{SCRIPT_STEM}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return report


# ============================================================
# 修复前后报告（structure 模版格式，按 TASK 逐个出）
# ------------------------------------------------------------
# 目的：让本仓库多个修复/检测脚本统一出一份“修复前后”报告，供审查与下游串联脚本汇总。
#   模版见 问题记录/脚本输出问题报告模版/structure.json。
# 本脚本是**检测专用、不改轨迹**，故 action_type 固定 "check"、before/after 内容为空；
#   function 按检测点区分具体语义失败类型；addition 里带 timestamp（节点 _metadata.timestamp）
#   及语义特有信息（检测点/失败类型/置信/建议策略/证据/指纹等）。
# 粒度：按 TASK 分组，每个 TASK 出一份 <TASK>/report/<脚本名>.json（-report DIR 时改写 DIR/<脚本名>.json）。
# 纳入范围：仅高置信失败（低置信备查仍只在上面的丰富报告里）；subagent 作为独立 session 条目，
#   session 内标注所属主 session。仅列出“命中(有失败)”的 session，与模版示例一致。
# ============================================================
def _task_dir_of(session_path: str) -> Path:
    """从 session 文件路径回推所属 TASK 目录（task_id=目录名、task_path=绝对路径）。

    支持三种布局：<task>/sessions/<file>.json、<task>/sessions/<main>/subagents/agent.json、
    以及旧式 <task>/<task>.json。取第一个名为 sessions 的祖先的父目录；没有则退回文件父目录。
    """
    p = Path(session_path)
    for anc in p.parents:
        if anc.name == "sessions":
            return anc.parent
    return p.parent


def _structure_sub(*, function: str, point: str, project_dir: str, file_id: str, is_subagent: bool,
                   parent_session: str, round_idx: int, msg_index: int, line: int, timestamp: str,
                   failure_type: str, confidence: float, reason: str, evidence: list[str],
                   strategy: str, extra: dict[str, Any]) -> dict[str, Any]:
    """构造一条 structure 模版的 sub 条目（检测专用：before/after 空、action_type=check）。

    function 按检测点区分具体语义失败类型（点1=assistant总结的语义失败、点2=工具调用的语义失败）。
    timestamp 取自对应轨迹节点的 _metadata.timestamp，原样透传到 addition。
    检测点写详细说明；每条都附「修复方案」（可自动修复给自动方案，否则给人工/整轮方案）。
    """
    auto = strategy in AUTO_REPAIR_STRATEGIES
    addition = {
        "检测点": DETECT_POINT_DESC.get(point, point),
        "timestamp": timestamp,
        "置信": round(confidence, 3),
        "建议策略": strategy,
        "可自动修复": auto,
        "修复方案": REPAIR_PLAN_BY_STRATEGY.get(strategy, "需人工核对证据后确定处理方式。"),
        "指纹": _fingerprint(project_dir, file_id,
                            "p1" if point.startswith("点1") else "p2",
                            round_idx, msg_index, failure_type),
        "证据": evidence,
    }
    addition.update(extra)
    return {
        "function": function,
        "action_type": "check",
        "before_content": "",
        "after_content": "",
        "problem": reason,
        "line": line,
        "addition": addition,
    }


def write_structure_reports(results: list[SessionResult], report_override: Path | None) -> list[Path]:
    """按 TASK 逐个写出 structure 模版格式的“修复前后”报告，返回写出的文件路径列表。

    落盘位置：report_override 为空时写进各自 TASK 的 <task>/report/<脚本名>.json；
    指定 -report DIR 时统一写到 DIR/<脚本名>.json（多 TASK 时后者覆盖前者，符合单 TASK 用法）。
    """
    groups: dict[Path, list[SessionResult]] = {}
    for r in results:
        if not r.valid_session:
            continue
        groups.setdefault(_task_dir_of(r.session_path), []).append(r)

    written: list[Path] = []
    for tdir, group in sorted(groups.items(), key=lambda kv: str(kv[0])):
        sessions_out: list[dict[str, Any]] = []
        total_issues = 0
        hit = 0
        for r in group:
            file_id = Path(r.session_path).name
            subs: list[dict[str, Any]] = []
            for f in r.final_failures:
                subs.append(_structure_sub(
                    function=STRUCT_FUNCTION_FINAL,
                    point="点1_回合终答", project_dir=r.project_dir, file_id=file_id,
                    is_subagent=r.is_subagent, parent_session=r.parent_session,
                    round_idx=f.round, msg_index=f.asst_index, line=f.asst_line, timestamp=f.timestamp,
                    failure_type=f.failure_type, confidence=f.confidence, reason=f.reason,
                    evidence=f.evidence, strategy=FINAL_STRATEGY.get(f.failure_type, "manual_review"),
                    extra={"末轮": f.is_last_round, "user_行号": f.user_line}))
            for s in r.step_failures:
                subs.append(_structure_sub(
                    function=STRUCT_FUNCTION_STEP,
                    point="点2_逐条", project_dir=r.project_dir, file_id=file_id,
                    is_subagent=r.is_subagent, parent_session=r.parent_session,
                    round_idx=s.round, msg_index=s.step_msg_index, line=s.step_line, timestamp=s.timestamp,
                    failure_type=s.failure_type, confidence=s.confidence, reason=s.reason,
                    evidence=s.evidence, strategy=STEP_STRATEGY.get(s.failure_type, "manual_review"),
                    extra={"步序号": s.step_index}))
            if not subs:
                continue
            subs.sort(key=lambda d: (d["line"], d["addition"]["检测点"]))
            sessions_out.append({"session_id": Path(r.session_path).stem, "sub": subs})
            total_issues += len(subs)
            hit += 1

        report = {
            "report_type": "semantic_report",
            "task_id": tdir.name,
            "task_path": str(tdir),
            "create": datetime.now().isoformat(timespec="seconds"),
            "summary": {
                "total_trajectory_count": len(group),
                "hit_trajectory_count": hit,
                "total_issues": total_issues,
            },
            "sessions": sessions_out,
        }
        # 落盘：默认 <task>/report/<脚本名>.json；-report DIR 时改写 DIR/<脚本名>.json
        out_dir = report_override if report_override is not None else (tdir / "report")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{SCRIPT_STEM}.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(out_path)
    return written


# ============================================================
# 主流程
# ============================================================
def run(root: Path, one_file: Path | None, report_override: Path | None) -> None:
    from dataclasses import replace as _replace
    model_config = _replace(resolve_model_config(), timeout=MODEL_TIMEOUT_SECONDS)
    print(f"model: {model_config.model} @ {model_config.base_url or 'default'} | "
          f"task_workers={TASK_WORKERS} round_workers={ROUND_WORKERS} llm_concurrency={LLM_CONCURRENCY} "
          f"conf>={CONFIDENCE_THRESHOLD}", flush=True)
    agents = {"final": build_agent(model_config, FINAL_SYSTEM_PROMPT),
              "step": build_agent(model_config, STEP_SYSTEM_PROMPT)}

    jobs = build_jobs(root, one_file)
    print(f"processing {len(jobs)} sessions ...", flush=True)
    results_by_index: dict[int, SessionResult] = {}

    def run_job(index, project_dir, session_path, is_sub, parent):
        res = process_one_session(project_dir, session_path, agents,
                                  is_subagent=is_sub, parent_session=parent)
        return index, res

    tw = max(1, min(TASK_WORKERS, len(jobs))) if jobs else 1
    with ThreadPoolExecutor(max_workers=tw) as executor:
        futures = {executor.submit(run_job, i, pd, sp, sub, par): i
                   for i, (pd, sp, sub, par) in enumerate(jobs)}
        done = 0
        for future in as_completed(futures):
            index, res = future.result()
            results_by_index[index] = res
            done += 1
            tag = "OK" if (res.valid_session and not res.final_failures and not res.step_failures) \
                else (f"p1={len(res.final_failures)} p2={len(res.step_failures)}"
                      if res.valid_session else "INVALID")
            kind = "sub" if res.is_subagent else "main"
            print(f"[{done}/{len(jobs)}] {res.project_dir}/{Path(res.session_path).name} ({kind}) ... {tag}",
                  flush=True)

    results = [results_by_index[i] for i in range(len(jobs))]
    # 只出按 TASK 的结构化检测报告；汇总 .json/.md 暂不落盘（write_files=False，仅取其统计供下方打印）。
    rep = write_reports(str(root), results, DEFAULT_REPORT_ROOT, write_files=False)
    struct_paths = write_structure_reports(results, report_override)
    s = rep["summary"]
    struct_loc = (str(report_override / f"{SCRIPT_STEM}.json") if report_override is not None
                  else f"<TASK>/report/{SCRIPT_STEM}.json")
    print(f"\n== 修复前后报告(structure 模版·按 TASK): {len(struct_paths)} 份 → {struct_loc} ==")
    for p in struct_paths:
        print(f"   - {p}")
    print(f"扫描 {s['扫描文件数']}（主 {s['主session数']} · sub {s['subagent数']}） · 无效 {s['无效文件']} · 模型报错 {s['模型报错文件']}")
    print(f"点1 终答失败 {s['点1_终答失败总数']} · 类型 {json.dumps(s['点1_类型分布'], ensure_ascii=False)}")
    print(f"点2 逐条失败 {s['点2_逐条失败总数']} · 类型 {json.dumps(s['点2_类型分布'], ensure_ascii=False)}")
    print(f"低置信备查: 终答 {s['低置信_终答(备查)']} · 逐条 {s['低置信_逐条(备查)']}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="轨迹语义失败检测（回合终答失败 + round 内逐条失败计数；只检测不改轨迹）。")
    p.add_argument("-r", dest="r", type=Path, required=True, help="路径（含项目子目录的根，或单个 TASK 目录）。")
    p.add_argument("-i", dest="i", type=Path, default=None, help="指定文件（可选；只处理这一个 session.json）。")
    p.add_argument("-report", dest="report", type=Path, default=None,
                   help="结构化「修复前后」报告的生成位置（可选）。缺省=写进各自 TASK 的 "
                        "<TASK>/report/<脚本名>.json；指定目录时统一写到 <目录>/<脚本名>.json。")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = args.r.expanduser().resolve()
    if not root.exists():
        print(f"-r 路径不存在: {root}", file=sys.stderr); return 2
    report_override = args.report.expanduser().resolve() if args.report else None
    run(root, args.i, report_override)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

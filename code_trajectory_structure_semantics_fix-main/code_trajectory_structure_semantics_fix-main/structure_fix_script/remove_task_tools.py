#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
remove_task_tools.py — 从轨迹文件中删除 Task* 任务管理工具调用

它做什么
========
给定一个 sessions 目录（或单个轨迹 JSON 文件），递归处理其中所有轨迹文件
（主轨迹 *.json 以及 subagents/agent-*.json），把任务管理工具
TaskCreate / TaskUpdate / TaskGet / TaskList / TaskOutput / TaskStop
的「完整工具调用」从轨迹里删除：

  对每个轨迹文件的 messages 数组：
    1) 收集所有 name 为 Task* 任务管理工具的 tool_call 的 id；
    2) 从发起它们的 assistant 消息的 tool_calls 数组里移除这些项；
    3) 删除与这些 id 配对的 tool 返回消息（role:"tool"，按 tool_call_id 匹配）；
    4) 若某条 assistant 消息的 tool_calls 被删空、且没有 content / reasoning_content
       文本，则整条删除这条 assistant 消息（避免留下无意义的空消息）。

为什么连带处理 subagents
========================
Task* 任务管理工具既出现在主轨迹，也出现在某些 subagent 轨迹文件内部
（典型是 agent-acompact-*.json 这类压缩会话）。本脚本默认递归处理目录下的
全部 *.json，因此主轨迹与所有 subagent 轨迹里的 Task 调用都会被一并清理。

注意：Task* 任务管理工具本身【不会】启动 subagent；
真正启动 subagent 的是 Agent 工具。脚本对 Agent 调用一律保留不动。

配对规则（务必保持完整）
========================
- 一条 assistant 消息可能含多个 tool_calls；只移除其中的 Task 项，其余保留。
- 每个被删的 tool_call.id 必须连带删除其唯一配对的 tool 返回消息。
- 绝不留下「有 tool_call 却无 tool 返回」或「有 tool 返回却无 tool_call」的孤儿。

不改动的内容
============
- meta.total_token 等 token 统计【保持原样】，不重算。
- 除 Task 工具外的任何消息、工具调用、字段一律不动。

输出方式
========
默认【原地修改，不创建备份】：
  - 逐个把清理后的内容原地写回原文件（UTF-8，保留 ensure_ascii=False）。
  - dry-run 只统计并打印将删除什么，不写任何【轨迹】文件。

修复报告
========
默认按【单个 TASK】维度生成一份结构修复报告，落在【输入 TASK 目录下的 report/】里，
文件名即脚本名：
  <task_path>/report/remove_task_tools.json
（用 --report 指定别的根目录时，为避免批量多 TASK 冲突，按
 <report_root>/<task_id>/report/remove_task_tools.json 组织；--report-subdir 可改 report 目录名。）
报告字段遵循「问题记录/脚本输出问题报告模版/structure.json」：
  - report_type = structure_report；action_type = fix；
  - function 按【具体操作类型】区分：Task工具调用删除 / Task工具返回删除 / 空assistant删除；
  - summary.total_trajectory_count / hit_trajectory_count / total_issues 为本 TASK 统计；
  - sessions[].sub[] 每条对应一个被删对象，before_content=改动前原文、after_content=""（已删），
    line=该对象在源文件里的真实行号，addition 里带 kind/tool_call_id/tool_name/message_index/timestamp
    （timestamp 取对应节点 _metadata.timestamp）。
注意：dry-run 下仍会生成报告（用于预览将发生的改动），只是不改动轨迹文件；
      --no-report 可完全关闭报告生成。

注意：所有路径按完整绝对路径处理；全程 UTF-8。


参数说明
========
  path                  taskid 目录 / sessions 目录 / 单个轨迹 JSON 文件（位置参数）。
                        若指定到 taskid 目录（如 TASK-xxxx），自动在其下找 sessions。
  -r, --root PATH       同 path，可指定 taskid 目录（自动下钻 sessions）或直接指定
                        sessions 目录（与位置参数二选一，两者都没给则报错）。
  -d, --dir PATH        父目录，扫描其下所有 taskid 子目录（每个子目录里含 sessions），
                        对每个 sessions 目录依次处理（每个 TASK 各出一份报告）。
  --dry-run             只统计并打印将删除什么，不写轨迹文件（仍生成报告）。
  --report PATH         报告输出根目录（默认写进各 TASK 自己的 report/ 子目录）。
  --report-subdir NAME  每个 TASK 下存放报告的子目录名（默认：report）。
  --no-report           不生成修复报告。
  --include-compact-only
                        只处理文件名含 "acompact" 的压缩会话文件（调试用）。


用法示例
========
  # 先 dry-run 看会删除多少（强烈建议先跑这个）
  python structure_fix_script/remove_task_tools.py "测试\TASK-20260318-A9960F\sessions" --dry-run

  # 实际执行（直接原地修改，不创建备份）
  python structure_fix_script/remove_task_tools.py "测试\TASK-20260318-A9960F\sessions"

  # 用 -r 指定 taskid 目录，自动找其下的 sessions（推荐）
  python structure_fix_script/remove_task_tools.py -r "测试\TASK-20260318-A9960F"

  # 用 -r 指定 sessions 目录（与位置参数等价）
  python structure_fix_script/remove_task_tools.py -r "测试\TASK-20260318-A9960F\sessions"

  # 用 -d 指定父目录，批量处理其下所有 taskid 子目录里的 sessions
  python structure_fix_script/remove_task_tools.py -d "测试"

  # 只处理单个文件
  python structure_fix_script/remove_task_tools.py "测试\TASK-20260318-A9960F\sessions\3-bugfix-xxx.json"
"""

import argparse
import json
import os
import sys
from datetime import datetime

# 需要删除的任务管理工具名（精确匹配，区分大小写）
TASK_TOOL_NAMES = {"TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop"}

# ---- 修复报告相关常量 ----
# 报告默认落在【输入 TASK 目录下的 report/ 子目录】里，文件名即脚本名：
#   <task_path>/report/remove_task_tools.json
# 用 --report 指定别的根目录时，为避免批量多 TASK 冲突，会按
#   <report_root>/<task_id>/report/remove_task_tools.json 组织。
SCRIPT_NAME = "remove_task_tools"
REPORT_TYPE = "structure_report"   # 结构类脚本固定取值
ACTION_TYPE = "fix"                # 本脚本是修复动作
# function 字段：按「具体做了哪种操作」区分，而非笼统的一个值
FUNCTION_BY_KIND = {
    "tool_call": "Task任务管理工具调用删除",
    "tool_result": "Task任务管理工具返回删除",
    "empty_assistant": "删除因移除Task调用而变空的assistant消息",
}


def log(msg):
    """统一往 stderr 打日志，避免和可能的标准输出混淆。"""
    print(msg, file=sys.stderr, flush=True)


def iter_trajectory_files(path, compact_only=False):
    """枚举要处理的轨迹文件。

    - path 是文件：直接返回它（须以 .json 结尾）。
    - path 是目录：递归返回其下所有 *.json。
    返回绝对路径列表，按路径排序，结果稳定。"""
    out = []
    if os.path.isfile(path):
        if path.lower().endswith(".json"):
            out.append(os.path.abspath(path))
        return out
    for root, _dirs, files in os.walk(path):
        for fn in files:
            if not fn.lower().endswith(".json"):
                continue
            if compact_only and "acompact" not in fn.lower():
                continue
            out.append(os.path.abspath(os.path.join(root, fn)))
    out.sort()
    return out


def is_task_tool_call(tc):
    """判断一个 tool_call 是否是 Task* 任务管理工具。

    兼容两种结构：
      - {"function": {"name": "..."}}（OpenAI 风格，本项目主用）
      - {"name": "..."}（少数实现把 name 直接放在顶层）"""
    if not isinstance(tc, dict):
        return False
    fn = tc.get("function")
    if isinstance(fn, dict) and fn.get("name") in TASK_TOOL_NAMES:
        return True
    if tc.get("name") in TASK_TOOL_NAMES:
        return True
    return False


def has_text(value):
    """判断一个文本字段是否含有非空白内容。"""
    return isinstance(value, str) and value.strip() != ""


def tool_call_name(tc):
    """取出一个 tool_call 的工具名（兼容 function.name 与顶层 name）。"""
    if not isinstance(tc, dict):
        return None
    fn = tc.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return fn.get("name")
    return tc.get("name")


def node_timestamp(msg):
    """取一个消息节点 _metadata.timestamp（无则 None）。"""
    if isinstance(msg, dict):
        meta = msg.get("_metadata")
        if isinstance(meta, dict):
            return meta.get("timestamp")
    return None


def top_level_element_lines(text):
    """扫描一段 JSON 数组文本，返回其中每个「顶层元素」的起始行号（1 基）。

    与 structure_check_script/check_skill_commands.py 保持同一套口径，
    使 line 字段和结构检查脚本的行号可直接对齐、便于后续串联。
    传入的 text 应从 messages 数组的 '[' 开始。"""
    lines = []
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


def messages_element_lines_from_text(text):
    """从整份轨迹文件文本里，算出 messages 数组每个元素的源文件真实行号。

    返回按元素顺序排列的行号列表（下标即 messages 的索引）。定位失败返回 []。"""
    marker = '"messages"'
    start = text.find(marker)
    if start == -1:
        return []
    array_start = text.find("[", start + len(marker))
    if array_start == -1:
        return []
    line_offset = text.count("\n", 0, array_start)
    return [line_offset + ln for ln in top_level_element_lines(text[array_start:])]


def clean_messages(messages):
    """对单个轨迹的 messages 数组执行删除逻辑，返回 (新数组, 统计 dict, 事件列表)。

    统计 dict 字段：
      removed_tool_calls    : 从 assistant.tool_calls 中移除的 Task 调用项数
      removed_tool_results  : 删除的配对 tool 返回消息数
      removed_empty_assist  : 因 tool_calls 删空且无文本而整条删除的 assistant 数
      orphan_tool_results   : 找到对应 tool 返回但其 tool_call 已不存在（保险计数）

    事件列表 events：每个被删对象一条，供报告逐条呈现「改动前原文」。字段：
      kind          : "tool_call" | "tool_result" | "empty_assistant"
      message_index : 该对象所属消息在【原始】messages 数组中的下标（用于定位源行号）
      content       : 被删对象原文（tool_call dict / tool 返回消息 / assistant 消息）
      tool_call_id  : 关联的 tool_call id（无则 None）
      tool_name     : Task 工具名（仅 tool_call 事件有）
    """
    stats = {
        "removed_tool_calls": 0,
        "removed_tool_results": 0,
        "removed_empty_assist": 0,
        "missing_tool_results": 0,
    }
    events = []

    # 第一遍：扫描所有 assistant 消息，收集要删除的 Task tool_call 的 id，
    #         同时就地从 tool_calls 里剔除这些项。
    # 只记录本次确实因为删除 Task 调用而变空的 assistant，避免在同一个
    # 文件里误删原本就存在的空 tool_calls=[] assistant。
    removed_ids = set()
    emptied_by_task_removal = set()
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        tcs = msg.get("tool_calls")
        if not isinstance(tcs, list) or not tcs:
            continue
        kept = []
        for tc in tcs:
            if is_task_tool_call(tc):
                tid = tc.get("id") if isinstance(tc, dict) else None
                if tid:
                    removed_ids.add(tid)
                stats["removed_tool_calls"] += 1
                events.append({
                    "kind": "tool_call",
                    "message_index": idx,
                    "content": tc,
                    "tool_call_id": tid,
                    "tool_name": tool_call_name(tc),
                    "timestamp": node_timestamp(msg),
                })
            else:
                kept.append(tc)
        if len(kept) != len(tcs):
            msg["tool_calls"] = kept
            if not kept:
                emptied_by_task_removal.add(id(msg))

    if not removed_ids and stats["removed_tool_calls"] == 0:
        # 这个文件没有任何 Task 调用，原样返回（保持 messages 引用不变）
        return messages, stats, events

    # 第二遍：重建消息数组——
    #   - 跳过 tool_call_id 命中 removed_ids 的 tool 返回消息；
    #   - 跳过「tool_calls 被删空且无文本」的 assistant 消息。
    matched_ids = set()
    new_messages = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            new_messages.append(msg)
            continue

        role = msg.get("role")

        # 删除配对的 tool 返回消息
        if role == "tool":
            tid = msg.get("tool_call_id")
            if tid in removed_ids:
                matched_ids.add(tid)
                stats["removed_tool_results"] += 1
                events.append({
                    "kind": "tool_result",
                    "message_index": idx,
                    "content": msg,
                    "tool_call_id": tid,
                    "tool_name": None,
                    "timestamp": node_timestamp(msg),
                })
                continue  # 丢弃这条 tool 返回
            new_messages.append(msg)
            continue

        # 只处理本次删除 Task 调用后变空的 assistant；保留原本就有的
        # tool_calls=[] 空壳，避免跨消息误删。
        if role == "assistant":
            tcs = msg.get("tool_calls")
            if id(msg) in emptied_by_task_removal and isinstance(tcs, list) and not tcs:
                if not has_text(msg.get("content")) and not has_text(msg.get("reasoning_content")):
                    # 既无剩余工具调用，也无任何文本 → 整条删除
                    stats["removed_empty_assist"] += 1
                    events.append({
                        "kind": "empty_assistant",
                        "message_index": idx,
                        "content": msg,
                        "tool_call_id": None,
                        "tool_name": None,
                        "timestamp": node_timestamp(msg),
                    })
                    continue
                # 还有文本内容则保留消息；但移除空的 tool_calls 字段，避免留空数组
                msg.pop("tool_calls", None)
            new_messages.append(msg)
            continue

        new_messages.append(msg)

    # 保险：被删 id 里若有没找到配对 tool 返回的，记一笔（数据本就异常，不阻断）
    stats["missing_tool_results"] = len(removed_ids - matched_ids)
    return new_messages, stats, events


def _problem_text(ev):
    """把一条删除事件翻译成报告里的 problem 描述。"""
    if ev["kind"] == "tool_call":
        return f"删除 assistant 消息中的 Task 任务管理工具调用：{ev.get('tool_name')}"
    if ev["kind"] == "tool_result":
        return f"删除与 Task 调用配对的 tool 返回消息（tool_call_id={ev.get('tool_call_id')}）"
    return "删除因 Task 调用移除后变空、且无任何文本的 assistant 消息"


def build_subs(events, element_lines):
    """把删除事件列表转成报告 sessions[].sub 列表。

    before_content = 被删对象原文（JSON 串）；after_content = ""（已删除）；
    line = 该对象所属消息在源文件中的真实行号（取不到记 -1）。"""
    subs = []
    for ev in events:
        idx = ev["message_index"]
        line = element_lines[idx] if 0 <= idx < len(element_lines) else -1
        subs.append({
            "function": FUNCTION_BY_KIND.get(ev["kind"], ev["kind"]),
            "action_type": ACTION_TYPE,
            "before_content": json.dumps(ev["content"], ensure_ascii=False),
            "after_content": "",
            "problem": _problem_text(ev),
            "line": line,
            "addition": {
                "kind": ev["kind"],
                "tool_call_id": ev.get("tool_call_id"),
                "tool_name": ev.get("tool_name"),
                "message_index": idx,
                "timestamp": ev.get("timestamp"),
            },
        })
    return subs


def session_id_of(abs_path):
    """会话 id = 轨迹文件名去掉 .json 后缀。"""
    return os.path.splitext(os.path.basename(abs_path))[0]


def process_file(abs_path, args):
    """处理单个轨迹文件。返回统计 dict（含 changed 标记、session_id、subs）。"""
    sid = session_id_of(abs_path)
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            text = f.read()
        data = json.loads(text)
    except Exception as e:
        log(f"  [跳过] 无法解析 JSON：{abs_path} :: {e}")
        return {"path": abs_path, "changed": False, "error": str(e),
                "session_id": sid, "subs": []}

    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        # 不是预期的轨迹结构（没有 messages 数组），跳过不动
        log(f"  [跳过] 非轨迹结构（无 messages 数组）：{abs_path}")
        return {"path": abs_path, "changed": False, "skipped": True,
                "session_id": sid, "subs": []}

    new_messages, stats, events = clean_messages(data["messages"])

    changed = (
        stats["removed_tool_calls"] > 0
        or stats["removed_tool_results"] > 0
        or stats["removed_empty_assist"] > 0
    )
    stats["path"] = abs_path
    stats["changed"] = changed
    stats["before_count"] = len(data["messages"])
    stats["after_count"] = len(new_messages)
    stats["session_id"] = sid

    # 用原始文本算每条被删对象的真实源行号，转成报告 sub 列表
    if events:
        element_lines = messages_element_lines_from_text(text)
        stats["subs"] = build_subs(events, element_lines)
    else:
        stats["subs"] = []

    if not changed:
        return stats

    if args.dry_run:
        return stats

    # 原地写回（保留中文、缩进 2 空格，与常见轨迹格式一致），不创建备份。
    data["messages"] = new_messages
    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as e:
        log(f"  [错误] 写回失败：{abs_path} :: {e}")
        stats["changed"] = False
        stats["error"] = f"write failed: {e}"
    return stats


def resolve_sessions_dir(target, session_path="sessions"):
    """把 target 规整为可处理的目标路径。

    - 若 target 是目录、自身不叫 sessions、但其下有 sessions 子目录
      （典型：指定到 TASK-xxxx 这种 taskid 目录），则自动下钻到该 sessions 目录；
    - 若 target 本身就是 sessions 目录、或单个 .json 文件，则原样返回。"""
    if os.path.isdir(target):
        cand = os.path.join(target, session_path)
        if os.path.isdir(cand):
            log(f"[定位] 在 {target} 下找到 {session_path} 目录，自动使用：{cand}")
            return cand
    return target


def discover_taskid_sessions(parent_dir, session_path="sessions"):
    """扫描父目录，返回其下每个 taskid 子目录里的 sessions 目录绝对路径列表。

    规则：遍历 parent_dir 的直接子目录，凡是子目录下存在 sessions 子目录的，
    就把该 sessions 目录收集进来。按路径排序，结果稳定。"""
    out = []
    for name in sorted(os.listdir(parent_dir)):
        sub = os.path.join(parent_dir, name)
        if not os.path.isdir(sub):
            continue
        cand = os.path.join(sub, session_path)
        if os.path.isdir(cand):
            out.append(os.path.abspath(cand))
    return out


def derive_task(target, session_path="sessions"):
    """从目标路径推导 (task_id, task_path)。

    规则：沿路径向上找第一个以 "TASK-" 开头的目录名作为 task_id，其绝对路径为
    task_path；找不到则用目录名兜底（若目录名恰为 session 目录名如 sessions，
    则再上钻一层，避免 task_id 变成 "sessions"）。"""
    p = os.path.abspath(target)
    parts = p.split(os.sep)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].startswith("TASK-"):
            return parts[i], os.sep.join(parts[:i + 1])
    d = p if os.path.isdir(p) else os.path.dirname(p)
    if os.path.basename(d) == session_path:
        d = os.path.dirname(d)
    return (os.path.basename(d) or "UNKNOWN_TASK"), d


def report_output_path(task_id, task_path, args):
    """算出报告落盘路径。

    - 默认（未给 --report）：<task_path>/<report_subdir>/<脚本名>.json
      —— 直接放进输入 TASK 自己的 report/ 目录。
    - 给了 --report ROOT：<ROOT>/<task_id>/<report_subdir>/<脚本名>.json
      —— 额外按 task_id 分层，避免批量多 TASK 写到同一文件互相覆盖。"""
    subdir = args.report_subdir
    if args.report_base:
        base_dir = os.path.join(args.report_base, task_id, subdir)
    else:
        base_dir = os.path.join(task_path, subdir)
    return os.path.join(base_dir, f"{SCRIPT_NAME}.json")


def write_task_report(original_target, total, sessions_report, total_issues, args):
    """按单 TASK 维度落一份结构修复报告。默认写进 <task_path>/report/<脚本名>.json。"""
    task_id, task_path = derive_task(original_target, args.session_path)
    report = {
        "report_type": REPORT_TYPE,
        "task_id": task_id,
        "task_path": task_path,
        "create": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": {
            "total_trajectory_count": total["files_scanned"],
            "hit_trajectory_count": total["files_changed"],
            "total_issues": total_issues,
        },
        "sessions": sessions_report,
    }
    out_path = report_output_path(task_id, task_path, args)
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
        log(f"[报告] 已生成：{out_path}"
            f"（命中会话={len(sessions_report)} 问题数={total_issues}）")
    except Exception as e:
        log(f"[错误] 报告写入失败：{out_path} :: {e}")
    return out_path


def process_target(target, args):
    """处理单个目标（sessions 目录 / taskid 目录 / 单个 .json 文件）。

    返回该目标的汇总统计 dict；若目标无可处理文件则 files_scanned=0。
    默认按单 TASK 维度落一份修复报告（--no-report 可关闭）。"""
    original_target = target
    target = resolve_sessions_dir(target, args.session_path)

    files = iter_trajectory_files(target, compact_only=args.compact_only)
    total = {
        "files_scanned": 0,
        "files_changed": 0,
        "removed_tool_calls": 0,
        "removed_tool_results": 0,
        "removed_empty_assist": 0,
        "missing_tool_results": 0,
    }
    sessions_report = []
    total_issues = 0
    if not files:
        log(f"[完成] 未找到任何 .json 轨迹文件：{target}")
        return total

    log(f"[开始] 共 {len(files)} 个候选文件{'（演练 dry-run，不写盘）' if args.dry_run else ''}：{target}")

    for abs_path in files:
        total["files_scanned"] += 1
        st = process_file(abs_path, args)
        for k in ("removed_tool_calls", "removed_tool_results",
                  "removed_empty_assist", "missing_tool_results"):
            total[k] += st.get(k, 0)
        subs = st.get("subs") or []
        if st.get("changed"):
            total["files_changed"] += 1
            rel = os.path.relpath(abs_path, target if os.path.isdir(target) else os.path.dirname(target))
            log(
                f"  [改] {rel} :: 删工具调用={st['removed_tool_calls']} "
                f"删tool返回={st['removed_tool_results']} "
                f"删空assistant={st['removed_empty_assist']} "
                f"消息数 {st['before_count']}→{st['after_count']}"
                + (f"  [警告] 有{st['missing_tool_results']}个Task调用缺tool返回"
                   if st.get("missing_tool_results") else "")
            )
        if subs:
            sessions_report.append({"session_id": st.get("session_id"), "sub": subs})
            total_issues += len(subs)

    if not args.no_report:
        write_task_report(original_target, total, sessions_report, total_issues, args)
    return total


def main():
    ap = argparse.ArgumentParser(
        description="从轨迹文件中删除 Task* 任务管理工具的完整调用"
    )
    ap.add_argument("path", nargs="?", default=None,
                    help="taskid 目录 / sessions 目录 / 单个轨迹 JSON 文件（位置参数；也可用 -r 指定）")
    ap.add_argument("-r", "--root", dest="root", default=None,
                    help="指定 taskid 目录（自动在其下找 sessions）或直接指定 sessions 目录"
                         "（与位置参数二选一）")
    ap.add_argument("-d", "--dir", dest="parent_dir", default=None,
                    help="父目录，扫描其下所有 taskid 子目录（每个子目录里含 sessions），"
                         "对每个 sessions 目录依次处理")
    ap.add_argument("--session-path", default="sessions",
                    help="每个 TASK 下的 session 相对路径（默认：sessions）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只统计将删除什么，不写文件")
    ap.add_argument("--include-compact-only", action="store_true",
                    dest="compact_only",
                    help="只处理文件名含 acompact 的压缩会话文件（调试用）")
    ap.add_argument("--report", "-report", dest="report_base", default=None,
                    help="修复报告输出根目录。默认不给时写进【输入 TASK 目录下的 report/】，"
                         "文件名即脚本名（remove_task_tools.json）；给了根目录则按 "
                         "<根目录>/<task_id>/report/remove_task_tools.json 组织")
    ap.add_argument("--report-subdir", dest="report_subdir", default="report",
                    help="每个 TASK 下存放报告的子目录名（默认：report）")
    ap.add_argument("--no-report", action="store_true", dest="no_report",
                    help="不生成修复报告")
    args = ap.parse_args()

    # 报告根目录：默认 None → 写进各 TASK 自己的 report/ 子目录；
    # 指定 --report 时才作为根目录，按 <root>/<task_id>/report/ 分层。
    if args.report_base:
        args.report_base = os.path.abspath(args.report_base)

    # ---- 模式一：-d/--dir 父目录批量处理 ----
    if args.parent_dir:
        if args.root or args.path:
            ap.error("-d/--dir 不能与位置参数或 -r/--root 同时使用")
        parent = os.path.abspath(args.parent_dir)
        if not os.path.isdir(parent):
            log(f"[错误] 父目录不存在或不是目录：{parent}")
            sys.exit(2)

        sessions_dirs = discover_taskid_sessions(parent, args.session_path)
        if not sessions_dirs:
            log(f"[完成] 在父目录下未找到任何含 sessions 的 taskid 子目录：{parent}")
            return

        log(f"[开始] 父目录 {parent} 下发现 {len(sessions_dirs)} 个 taskid 的 sessions 目录")

        grand = {
            "tasks_processed": 0,
            "files_scanned": 0,
            "files_changed": 0,
            "removed_tool_calls": 0,
            "removed_tool_results": 0,
            "removed_empty_assist": 0,
            "missing_tool_results": 0,
        }
        for sdir in sessions_dirs:
            log(f"[任务] 处理 {os.path.dirname(sdir)}")
            sub_total = process_target(sdir, args)
            grand["tasks_processed"] += 1
            for k in ("files_scanned", "files_changed", "removed_tool_calls",
                      "removed_tool_results", "removed_empty_assist", "missing_tool_results"):
                grand[k] += sub_total.get(k, 0)

        log("[总汇总] " + json.dumps(grand, ensure_ascii=False))
        if args.dry_run:
            log("[提示] 这是演练（dry-run），未写入任何文件。确认无误后去掉 --dry-run 即可实际执行。")
        return

    # ---- 模式二：单个目标（位置参数 或 -r/--root）----
    # 路径来源：-r/--root 优先；否则用位置参数 path。两者都没给则报错。
    raw_path = args.root if args.root else args.path
    if not raw_path:
        ap.error("必须指定 sessions 目录（位置参数 / -r/--root / -d/--dir）")

    target = os.path.abspath(raw_path)
    if not os.path.exists(target):
        log(f"[错误] 路径不存在：{target}")
        sys.exit(2)

    total = process_target(target, args)
    log("[汇总] " + json.dumps(total, ensure_ascii=False))
    if args.dry_run:
        log("[提示] 这是演练（dry-run），未写入任何文件。确认无误后去掉 --dry-run 即可实际执行。")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dedup_consecutive_assistant.py

读取 TASK-*/sessions/ 下的轨迹 JSON 文件，检测 messages 数组中连续出现的
assistant 消息，如果同一个对话轮次中存在多条连续的 assistant 消息（即中间没有
user / system / tool 消息隔开），则只保留最后一条，删除前面的所有重复项。

背景：
  正常的轨迹交互模式为 user → assistant → tool_result → assistant → ...，
  assistant 之间不应连续出现。连续 assistant 通常由数据处理异常或重复注入导致，
  属于需要清理的脏数据。

处理规则：
  1. 只处理位于 sessions 顶层的主轨迹 JSON 文件。
  2. 扫描 messages 数组，识别所有连续排列的 assistant 消息组（每组 >= 2 条）。
  3. 每组中只保留最后一条 assistant，其余全部删除。
  4. 被删除的 assistant 如果带有 tool_calls，对应的 tool_result 会变成 orphan，
     脚本会在报告中记录 warning 但不阻断删除。
  5. 原地改写时不生成 .bak 文件，写入前使用 .tmp 原子替换保证安全。
  6. 报告按「单个 TASK」维度输出，遵循 问题记录/脚本输出问题报告模版/structure.json 模版，
     记录每条被删 assistant 的改动前原文、真实文件行号、问题描述等，便于修复前后审查。

用法:
  # 扫描默认目录 batch_cleaning_tasks
  python structure_fix_script/dedup_consecutive_assistant.py

  # 指定根目录
  python structure_fix_script/dedup_consecutive_assistant.py -r source-issues-tasks

  # 指定单个文件
  python structure_fix_script/dedup_consecutive_assistant.py -r . -i TASK-XXX/sessions/session.json

  # 输出到新目录，不修改原文件
  python structure_fix_script/dedup_consecutive_assistant.py -r batch_cleaning_tasks -o output/

  # 自定义报告目录和并发数（不指定 -report 时默认写到各 TASK 的 report/ 目录）
  python structure_fix_script/dedup_consecutive_assistant.py -r batch_cleaning_tasks -report reports/ --workers 16

参数:
  -r PATH          要扫描的根目录，默认 batch_cleaning_tasks
  -i PATH          指定单个输入文件（可选，优先级高于 -r）
  -o PATH          输出目录（可选，不指定则原地修改）
  -report PATH     报告输出目录（可选）。默认落到每个 TASK 的
                   <TASK 目录>/report/<脚本名>.json；指定时落到 <report>/<脚本名>.json
                   （多 TASK 时按 task_id 再分子目录）。以 .json 结尾的路径按其父目录处理
                   （兼容旧流水线调用）。
  --workers N      并发线程数，默认 8
"""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_NAME = Path(__file__).stem

# 报告模版固定字段（见 问题记录/脚本输出问题报告模版/structure.json）
REPORT_TYPE = "structure_report"      # 结构类脚本
ACTION_TYPE = "fix"                    # 本脚本为修复动作（非 check）
# function 按被删节点的具体类型细分（见 _function_label）
FUNCTION_TOOL_CALL = "连续 assistant 去重（工具调用节点）"
FUNCTION_TEXT_REPLY = "连续 assistant 去重（文本回复节点）"
FUNCTION_EMPTY = "连续 assistant 去重（空节点）"


def find_project_root(start: Path | None = None) -> Path:
    """从脚本位置向上查找仓库根目录。"""
    here = (start or Path(__file__).resolve()).parent
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    for candidate in (here, *here.parents):
        if (candidate / ".env").is_file():
            return candidate
    return here


PROJECT_ROOT = find_project_root()
DEFAULT_INPUT_DIR = PROJECT_ROOT / "batch_cleaning_tasks"
DEFAULT_REPORT_BASE = PROJECT_ROOT / "reports"
DEFAULT_WORKERS = 8


# ---------------------------------------------------------------------------
# TASK / session 归属 与 真实行号定位
# ---------------------------------------------------------------------------


def find_task_dir(path: Path) -> Path | None:
    """向上查找名字以 TASK- 开头的祖先目录。"""
    for parent in path.resolve().parents:
        if parent.name.startswith("TASK-"):
            return parent
    return None


def session_id_for(path: Path) -> str:
    """由文件路径推断 session_id：取 sessions/ 目录之后的相对路径（去掉 .json）。

    主轨迹 sessions/3-test-xxx.json  -> "3-test-xxx"
    子 agent sessions/3-test-xxx/subagents/agent-yyy.json -> "3-test-xxx/subagents/agent-yyy"
    （满足「每个文件独立成条」，且对子 agent 保持唯一、可读。）
    """
    parts = path.resolve().parts
    if "sessions" in parts:
        idx = len(parts) - 1 - parts[::-1].index("sessions")  # 最后一个 sessions
        rel = Path(*parts[idx + 1:]).as_posix()
    else:
        rel = path.name
    if rel.endswith(".json"):
        rel = rel[:-5]
    return rel


def _scan_array_element_offsets(raw: str, bracket_index: int) -> list[int]:
    """从 '[' 位置起，返回数组中每个元素首字符在 raw 中的字符偏移。

    用 json.JSONDecoder.raw_decode 逐个消费元素，正确跳过字符串内的括号/逗号。
    """
    decoder = json.JSONDecoder()
    offsets: list[int] = []
    i = bracket_index + 1
    n = len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n,":
            i += 1
        if i >= n or raw[i] == "]":
            break
        offsets.append(i)
        try:
            _, i = decoder.raw_decode(raw, i)
        except ValueError:
            break
    return offsets


def compute_element_line_numbers(raw: str, messages_key: str, expected_len: int) -> list[int]:
    """计算 messages 数组中每个元素在原始文件里的真实起始行号（1-based）。

    通过匹配 "<key>": [ 定位数组，再用长度校验消歧（避免命中 content 里的同名字符串），
    最后把每个元素的字符偏移换算成行号。定位失败时返回空列表，由调用方回退。
    """
    decoder = json.JSONDecoder()
    pattern = re.compile(r'"' + re.escape(messages_key) + r'"\s*:\s*\[')
    for match in pattern.finditer(raw):
        bracket = match.end() - 1
        try:
            arr, _ = decoder.raw_decode(raw, bracket)
        except ValueError:
            continue
        if not isinstance(arr, list) or len(arr) != expected_len:
            continue
        offsets = _scan_array_element_offsets(raw, bracket)
        if len(offsets) == expected_len:
            return [raw.count("\n", 0, off) + 1 for off in offsets]
    return []


# ---------------------------------------------------------------------------
# 内容提取
# ---------------------------------------------------------------------------


def _extract_text(content: Any) -> str:
    """将 content 统一提取为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


# 报告里 before/after 内容的最大展示字符数，超出则截断，避免报告体积膨胀、可读性变差
MAX_PREVIEW_CHARS = 800


def _truncate(text: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    """超长文本截断并标注原文字数，保证报告可读。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"……〔内容过长已截断，原文共 {len(text)} 字〕"


def _render_tool_calls(msg: dict[str, Any]) -> list[str]:
    """把 tool_calls 渲染成 ['name(arguments)', ...]，比只列名字更能看清做了什么。"""
    lines: list[str] = []
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name", "") if isinstance(fn, dict) else ""
            args = fn.get("arguments", "") if isinstance(fn, dict) else ""
            if isinstance(args, (dict, list)):
                args = json.dumps(args, ensure_ascii=False)
            args = "" if args is None else str(args)
            lines.append(f"{name}({args})" if name else f"({args})")
    return lines


def _node_type_label(msg: dict[str, Any]) -> str:
    """节点类型短标签：工具调用 / 文本回复 / 空节点。"""
    if msg.get("tool_calls"):
        return "工具调用"
    if _extract_text(msg.get("content")).strip():
        return "文本回复"
    return "空节点"


def _render_content_only(msg: Any) -> str:
    """只渲染节点的正文 + 工具调用（不带角色/类型标签），用于 keeper 预览。"""
    if not isinstance(msg, dict):
        return ""
    parts: list[str] = []
    text = _extract_text(msg.get("content")).strip()
    if text:
        parts.append(text)
    calls = _render_tool_calls(msg)
    if calls:
        parts.append("\n".join(calls))
    return _truncate("\n".join(parts))


def _render_node_readable(msg: Any) -> str:
    """把被删 assistant 节点渲染成带标签、可读的多行文本（before_content）。

    形如：
      [assistant·工具调用]
      read_file({"path": "src/app.py"})
    正文与工具调用都存在时两者都展示；超长自动截断。
    """
    if not isinstance(msg, dict):
        return ""
    role = msg.get("role", "assistant")
    parts: list[str] = [f"[{role}·{_node_type_label(msg)}]"]
    body = _render_content_only(msg)
    if body:
        parts.append(body)
    return _truncate("\n".join(parts))


def _function_label(msg: dict[str, Any]) -> str:
    """按被删节点的具体类型给出 function 取值。"""
    if isinstance(msg, dict) and msg.get("tool_calls"):
        return FUNCTION_TOOL_CALL
    if isinstance(msg, dict) and _extract_text(msg.get("content")).strip():
        return FUNCTION_TEXT_REPLY
    return FUNCTION_EMPTY


def _node_timestamp(msg: dict[str, Any]) -> str:
    """取节点 _metadata.timestamp，缺失则空串。"""
    if isinstance(msg, dict):
        meta = msg.get("_metadata")
        if isinstance(meta, dict):
            ts = meta.get("timestamp")
            if isinstance(ts, str):
                return ts
    return ""


def _is_meaningful_assistant(msg: Any) -> bool:
    """assistant 是否有实质内容：有正文文本 或 有 tool_calls。

    只有正文和 tool_calls 都为空的才算“空壳”。reasoning_content 不算实质
    内容（它不是最终对用户可见的收尾），但下面 _pick_keeper 会在“全空壳”时
    保留最后一条，交给 fix_missing_summary_nodes 去补总结，不在这里删空。
    """
    if not isinstance(msg, dict):
        return False
    if _extract_text(msg.get("content")).strip():
        return True
    if msg.get("tool_calls"):
        return True
    return False


def _pick_keeper(messages: list[Any], group: list[int]) -> int:
    """选择连续 assistant 组里要保留的那一条。

    默认保留最后一条；但如果最后一条是空壳（无正文、无 tool_calls），而组内前面
    有实质内容的 assistant，则保留“最后一条有实质内容”的那条，避免把真正的收尾/
    总结删掉、只留下一个空节点（这正是末节点收尾丢失的一个来源）。全空壳则保留
    最后一条（维持原行为），把补总结的机会留给下游 fix_missing_summary_nodes。
    """
    for idx in reversed(group):
        if _is_meaningful_assistant(messages[idx]):
            return idx
    return group[-1]


# ---------------------------------------------------------------------------
# 核心逻辑：检测并移除连续 assistant 消息
# ---------------------------------------------------------------------------


def find_consecutive_assistant_groups(messages: list[Any]) -> list[list[int]]:
    """
    找出 messages 中所有由连续 assistant 组成的组（长度 >= 2）。
    返回的是索引列表的分组，每组内按原始顺序排列。
    """
    groups: list[list[int]] = []
    current_group: list[int] = []

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            if len(current_group) >= 2:
                groups.append(current_group)
            current_group = []
            continue

        if msg.get("role") == "assistant":
            current_group.append(i)
        else:
            if len(current_group) >= 2:
                groups.append(current_group)
            current_group = []

    # 尾部收尾
    if len(current_group) >= 2:
        groups.append(current_group)

    return groups


def dedup_consecutive_assistants(
    messages: list[Any],
) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
    """
    删除连续 assistant 组中除保留项之外的所有 assistant 消息。

    返回 (新 messages, 删除记录列表, warnings)。
    每条删除记录包含：index（原数组下标）、group_size、position（组内第几条）、
    keeper_index（本组保留项的下标）。真实文件行号由调用方补上。
    """
    groups = find_consecutive_assistant_groups(messages)
    if not groups:
        return messages, [], []

    # 收集所有要删除的索引
    to_delete: set[int] = set()
    group_info: dict[int, int] = {}  # index -> group_id
    keeper_info: dict[int, int] = {}  # group_id -> 保留的索引
    for gid, group in enumerate(groups):
        keeper = _pick_keeper(messages, group)  # 优先保留“最后一条有实质内容”的
        keeper_info[gid] = keeper
        for idx in group:
            if idx == keeper:
                continue
            to_delete.add(idx)
            group_info[idx] = gid

    # 删除的 assistant 如果带有 tool_calls，会产生 orphan tool_result
    warnings: list[str] = []
    for idx in sorted(to_delete):
        msg = messages[idx]
        if isinstance(msg, dict) and msg.get("tool_calls"):
            tool_call_ids = [
                tc.get("id", "") for tc in msg["tool_calls"]
                if isinstance(tc, dict)
            ]
            if tool_call_ids:
                for j in range(idx + 1, len(messages)):
                    later = messages[j]
                    if isinstance(later, dict) and later.get("role") == "tool":
                        tid = later.get("tool_call_id", "")
                        if tid in tool_call_ids:
                            warnings.append(
                                f"index={idx} 删除的 assistant 带有 tool_call id={tid}，"
                                f"后面的 tool_result index={j} 将成为 orphan"
                            )

    # 构建新 messages
    new_messages = [msg for i, msg in enumerate(messages) if i not in to_delete]

    # 构建删除记录（供报告使用）
    deletions: list[dict[str, Any]] = []
    for idx in sorted(to_delete):
        gid = group_info[idx]
        group = groups[gid]
        keeper = keeper_info[gid]
        deletions.append({
            "index": idx,
            "group_size": len(group),
            "position": group.index(idx) + 1,
            "keeper_index": keeper,
            "keeper_position": group.index(keeper) + 1,
        })

    return new_messages, deletions, warnings


def process_single_file(
    file_path: Path,
    base_dir: Path,
    output_dir: Path | None,
) -> dict[str, Any]:
    """处理单个 JSON 文件，返回处理记录。

    记录字段（供报告聚合）：
      file, session_id, task_id, task_path, status,
      original_message_count, final_message_count, deleted_count, subs, error
    subs 为模版 sub[] 条目列表（仅在有删除时非空）。
    """
    rel = file_path.relative_to(base_dir).as_posix()
    task_dir = find_task_dir(file_path)
    record: dict[str, Any] = {
        "file": rel,
        "session_id": session_id_for(file_path),
        "task_id": task_dir.name if task_dir else "",
        "task_path": str(task_dir) if task_dir else "",
        "status": "pending",
        "subs": [],
    }

    try:
        raw = file_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        record["status"] = "error"
        record["error"] = f"JSON 读取失败: {exc}"
        return record

    if not isinstance(data, dict):
        record["status"] = "skipped"
        record["reason"] = "文件顶层不是 JSON 对象"
        return record

    # 查找 messages 列表
    messages_key = None
    messages = None
    for key in ("messages", "chat_messages", "conversation", "history"):
        val = data.get(key)
        if isinstance(val, list):
            messages_key = key
            messages = val
            break

    if messages is None:
        record["status"] = "skipped"
        record["reason"] = "未找到 messages 数组"
        return record

    original_count = len(messages)
    record["original_message_count"] = original_count

    new_messages, deletions, warnings = dedup_consecutive_assistants(messages)
    deleted_count = original_count - len(new_messages)

    record["final_message_count"] = len(new_messages)
    record["deleted_count"] = deleted_count

    if deleted_count == 0:
        record["status"] = "unchanged"
        return record

    # 真实文件行号：整体定位一次 messages 数组元素的起始行
    line_numbers = compute_element_line_numbers(raw, messages_key, original_count)

    subs: list[dict[str, Any]] = []
    for d in deletions:
        idx = d["index"]
        msg = messages[idx]
        keeper_idx = d["keeper_index"]
        keeper_msg = messages[keeper_idx] if 0 <= keeper_idx < len(messages) else None
        before = _render_node_readable(msg)
        keeper_preview = _render_content_only(keeper_msg) or "（空节点）"
        after = f"【保留·组内最终收尾】{keeper_preview}"
        line = line_numbers[idx] if idx < len(line_numbers) else 0
        subs.append({
            "function": _function_label(msg),
            "action_type": ACTION_TYPE,
            "before_content": before,   # 被删节点的可读渲染（带角色/类型标签）
            "after_content": after,     # 该位置节点被删，展示组内被保留的收尾节点内容
            "problem": (
                f"连续 assistant 组（共 {d['group_size']} 条）中的第 {d['position']} 条，"
                f"与相邻 assistant 重复，已删除（保留组内下标为 "
                f"{d['keeper_index']} 的 assistant 作为最终收尾）"
            ),
            "line": line,
            "addition": {
                "timestamp": _node_timestamp(msg) if isinstance(msg, dict) else "",
                "message_index": idx,
                "group_position": (
                    f"该节点是这组连续 assistant 的第 {d['position']} 条"
                    f"（本组共 {d['group_size']} 条）"
                ),
                "keeper": (
                    f"删除后保留本组第 {d['keeper_position']} 条"
                    f"（message_index={d['keeper_index']}）作为最终收尾，内容见 after_content"
                ),
            },
        })
    record["subs"] = subs
    if warnings:
        record["warnings"] = warnings

    record["status"] = "modified"

    # 写回
    data[messages_key] = new_messages
    if output_dir:
        out_path = output_dir / rel
    else:
        out_path = file_path

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp.replace(out_path)
        record["output_file"] = str(out_path)
    except Exception as exc:
        record["status"] = "error"
        record["error"] = f"写入失败: {exc}"

    return record


# ---------------------------------------------------------------------------
# 文件发现
# ---------------------------------------------------------------------------


def discover_json_files(
    root_dir: Path,
    input_file: Path | None = None,
) -> list[Path]:
    """递归发现所有 session 轨迹 JSON 文件（含 subagent）。

    匹配策略（按优先级）：
      1. 如果指定了 -i 单个文件，直接返回该文件
      2. 如果 -r 指定的目录本身就是 sessions 目录，递归返回其下所有 .json
      3. 如果 -r 指定的是 TASK-* 目录，递归返回其 sessions/ 下所有 .json
      4. 否则递归查找 **/TASK-*/sessions/**/*.json
      5. 兜底：递归查找所有路径中包含 sessions 目录的 .json 文件
    """
    if input_file is not None:
        # -i 可为：绝对/相对 CWD 的文件或目录，或相对 -r 的子路径（如 "sessions"）。
        # 命中文件→单文件；命中目录→递归其下 *.json；都不命中→回退到基于 -r 的发现。
        # （历史上这里对未命中直接 return []，导致流水线传 "-i sessions" 时静默空跑。）
        raw = input_file.expanduser()
        candidates = [raw] if raw.is_absolute() else [raw, root_dir.expanduser() / raw]
        for cand in candidates:
            resolved = cand.expanduser().resolve()
            if resolved.is_file():
                return [resolved]
            if resolved.is_dir():
                return sorted(fp for fp in resolved.rglob("*.json") if fp.is_file())
        print(f"提示: -i {input_file} 非文件/目录，回退到基于 -r 的 sessions 递归发现")
        # 不 return，继续走下面基于 root 的场景

    root = root_dir.expanduser().resolve()
    if not root.is_dir():
        print(f"警告: 目录不存在: {root}")
        return []

    # 场景2：-r 直接指向 sessions 目录（递归扫子目录如 subagents/）
    if root.name == "sessions":
        return sorted(
            fp for fp in root.rglob("*.json")
            if fp.is_file()
        )

    # 场景3：-r 指向某个 TASK-* 目录（递归扫 sessions/ 下所有 JSON）
    if root.name.startswith("TASK-"):
        sessions_dir = root / "sessions"
        if sessions_dir.is_dir():
            return sorted(
                fp for fp in sessions_dir.rglob("*.json")
                if fp.is_file()
            )
        return []

    # 场景4：递归查找 TASK-*/sessions/**/*.json（含 subagents/）
    files = sorted(root.rglob("TASK-*/sessions/**/*.json"))
    top_files = [fp for fp in files if fp.is_file()]

    # 场景5：兜底 — 递归查找所有路径中包含 sessions 目录的 JSON
    if not top_files:
        top_files = sorted(
            fp for fp in root.rglob("*.json")
            if fp.is_file() and "sessions" in fp.parts
        )

    return top_files


# ---------------------------------------------------------------------------
# 报告生成（按「单个 TASK」维度，遵循 structure.json 模版）
# ---------------------------------------------------------------------------


def resolve_report_base(report_arg: Path) -> Path:
    """把 -report 解析为报告根目录（用于覆盖默认位置）。

    传目录 → 直接作为根目录；传以 .json 结尾的文件路径 → 取其父目录（兼容旧流水线
    以文件形式传 -report 的调用）。
    """
    p = report_arg.expanduser().resolve()
    if p.suffix.lower() == ".json":
        return p.parent
    return p


REPORT_FILENAME = f"{SCRIPT_NAME}.json"


def task_report_path(task_path: str, report_override: Path | None, multi_task: bool) -> Path:
    """计算单个 TASK 的报告落盘路径。

    默认（未指定 -report）：<TASK 目录>/report/<脚本名>.json。
    指定 -report：<report 目录>/<脚本名>.json；若一次处理多个 TASK，则再加一层
    <report 目录>/<task_id>/<脚本名>.json 以避免同名互相覆盖。
    TASK 目录未知时兜底到 <仓库根>/reports/<脚本名>.json。
    """
    if report_override is not None:
        base = resolve_report_base(report_override)
        if multi_task and task_path:
            base = base / Path(task_path).name
        return base / REPORT_FILENAME
    if task_path:
        return Path(task_path) / "report" / REPORT_FILENAME
    return DEFAULT_REPORT_BASE / REPORT_FILENAME


def build_task_report(task_id: str, task_path: str, task_records: list[dict[str, Any]]) -> dict[str, Any]:
    """按模版为单个 TASK 构建 structure_report。"""
    sessions: list[dict[str, Any]] = []
    total_issues = 0
    hit = 0
    for rec in sorted(task_records, key=lambda r: r.get("session_id", "")):
        subs = rec.get("subs") or []
        if not subs:
            continue
        hit += 1
        total_issues += len(subs)
        sessions.append({"session_id": rec.get("session_id", ""), "sub": subs})

    return {
        "report_type": REPORT_TYPE,
        "task_id": task_id,
        "task_path": task_path,
        "create": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total_trajectory_count": len(task_records),
            "hit_trajectory_count": hit,
            "total_issues": total_issues,
        },
        "sessions": sessions,
    }


def write_task_reports(records: list[dict[str, Any]], report_override: Path | None) -> list[Path]:
    """按 task_id 分组，为每个 TASK 写出报告（默认落到各 TASK 的 report/ 目录）。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    task_paths: dict[str, str] = {}
    for rec in records:
        task_id = rec.get("task_id") or "TASK-UNKNOWN"
        grouped.setdefault(task_id, []).append(rec)
        if rec.get("task_path"):
            task_paths.setdefault(task_id, rec["task_path"])

    multi_task = len(grouped) > 1
    written: list[Path] = []
    for task_id, task_records in sorted(grouped.items()):
        task_path = task_paths.get(task_id, "")
        report = build_task_report(task_id, task_path, task_records)
        out_path = task_report_path(task_path, report_override, multi_task)
        write_json(out_path, report)
        written.append(out_path)
    return written


# ---------------------------------------------------------------------------
# 参数解析（按规范：-r, -i, -o, -report, --workers）
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检测轨迹 JSON 中连续 assistant 消息，每组只保留最后一条",
    )
    parser.add_argument(
        "-r",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"指定要扫描的根目录，默认 {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "-i",
        type=Path,
        default=None,
        help="指定单个输入文件（可选，优先级高于 -r）",
    )
    parser.add_argument(
        "-o",
        type=Path,
        default=None,
        help="指定输出目录（可选，不指定则原地修改）",
    )
    parser.add_argument(
        "-report",
        type=Path,
        default=None,
        help=(
            f"报告输出目录（可选）。默认落到每个 TASK 的 <TASK 目录>/report/{REPORT_FILENAME}；"
            f"指定时落到 <report>/{REPORT_FILENAME}（多 TASK 时再按 task_id 分子目录）"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"并发线程数，默认 {DEFAULT_WORKERS}",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_dir = args.r.expanduser().resolve()
    output_dir = args.o.expanduser().resolve() if args.o else None
    report_override = args.report
    workers = max(1, args.workers)

    files = discover_json_files(base_dir, args.i)
    if not files:
        print(f"在 {base_dir} 下未找到 session JSON 文件")
        print("（无 TASK，未生成报告）")
        return 0

    # 当 -i 指定的【单个文件】不在 -r 目录下时，以文件所在 sessions 目录的父目录为 base_dir，
    # 确保 relative_to 计算正确。若 -i 展开成多文件（如 "-i sessions" 回退到 root 递归发现），
    # 则保持 base_dir=-r，避免把其它 TASK 的文件算错相对路径。
    if args.i is not None and len(files) == 1:
        only = files[0].resolve()
        try:
            only.relative_to(base_dir)
        except ValueError:
            base_dir = only.parent.parent  # sessions 的父目录（TASK 目录）

    print(f"找到 {len(files)} 个 session JSON 文件")
    print(f"输入目录: {base_dir}")
    print(f"输出: {'原地修改' if output_dir is None else output_dir}")
    print(f"并发数: {workers}")
    print()

    records: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                process_single_file,
                fp,
                base_dir,
                output_dir,
            ): fp
            for fp in files
        }

        for i, future in enumerate(as_completed(futures), start=1):
            fp = futures[future]
            rel = fp.relative_to(base_dir).as_posix()
            try:
                record = future.result()
            except Exception as exc:
                task_dir = find_task_dir(fp)
                record = {
                    "file": rel,
                    "session_id": session_id_for(fp),
                    "task_id": task_dir.name if task_dir else "",
                    "task_path": str(task_dir) if task_dir else "",
                    "status": "error",
                    "subs": [],
                    "error": f"处理异常: {exc}",
                }
            records.append(record)

            status = record.get("status", "?")
            deleted = record.get("deleted_count", 0)
            icon = {"modified": "[MOD]", "unchanged": "[OK]", "skipped": "[SKIP]", "error": "[ERR]"}.get(status, "?")
            print(f"[{i}/{len(files)}] {icon} {rel}  status={status}  deleted={deleted}")

    # 按文件路径排序以保持输出一致性
    records.sort(key=lambda r: r.get("file", ""))

    written = write_task_reports(records, report_override)

    status_counts: dict[str, int] = {}
    total_deleted = 0
    for rec in records:
        status_counts[rec.get("status", "?")] = status_counts.get(rec.get("status", "?"), 0) + 1
        total_deleted += rec.get("deleted_count", 0)

    print()
    print("=" * 50)
    print("处理完成:")
    print(f"  [MOD] 已修改:     {status_counts.get('modified', 0)}")
    print(f"  [OK]  无变化:     {status_counts.get('unchanged', 0)}")
    print(f"  [SKIP] 跳过:      {status_counts.get('skipped', 0)}")
    print(f"  [ERR] 错误:       {status_counts.get('error', 0)}")
    print(f"  共删除消息:   {total_deleted}")
    print(f"  共扫描文件:   {len(records)}")
    print(f"  报告数量:     {len(written)} 个 TASK")
    for p in written:
        print(f"    - {p}")

    return 1 if status_counts.get('error', 0) > 0 else 0


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))

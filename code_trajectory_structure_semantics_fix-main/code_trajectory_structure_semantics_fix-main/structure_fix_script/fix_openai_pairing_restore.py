#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 OpenAI 规范检查中「工具调用 ↔ 工具结果」配对被误删造成的违规
（规则 10 / 22 / 26 / 27 / 28）。

修复哪两种情形
--------------
  1. 悬空 tool_calls：assistant 声明了调用，但对应的 tool 结果没了 → 规则 28
  2. 孤儿 tool 结果：tool 结果还在，但对应的调用声明没了       → 规则 10/22/26/27

参数
----
  <交付版目录>      必填。要修复的目录（内含各 TASK-*）。脚本只递归它，不碰别处。
  --source <目录>   可选。成功版源数据目录（被误删的那侧从这里补回）。不指定时默认取
                    「交付版目录」的父目录下的 成功-20260618105913-559-544（如交付版为
                    data/TASK-12-提交给客户，默认源即 data/成功-20260618105913-559-544）；
                    该目录不存在则报错退出，换批次时用本参数显式指定。
  --dry-run         只预览会改哪些，不落盘。
  --source-state    源文件查找范围：auto 混合查找 sessions/convert-sessions；
                    convert 只查 convert-sessions。

示例
----
  # 1) 先预览，确认要修哪些文件（不改动任何文件）
  python3 fix_openai_pairing_restore.py data/TASK-12-提交给客户 --dry-run

  # 2) 正式修复：默认用同级的成功版当源
  python3 fix_openai_pairing_restore.py data/TASK-12-提交给客户

  # 3) 源数据在别处时，用 --source 显式指定
  python3 fix_openai_pairing_restore.py data/TASK-12-提交给客户 \
      --source data/成功-20260618105913-559-544
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

CJK = re.compile(r"[㐀-鿿豈-﫿]")
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}


# ---------- 基础读写 ----------
def load(path):
    data = json.loads(Path(path).read_bytes().decode("utf-8-sig"))
    msgs = data.get("messages") if isinstance(data, dict) else data
    return data, msgs


def dump(path, data):
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------- 配对分析 ----------
def declared_ids(msgs):
    out = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    out.append(tc["id"])
    return out


def result_ids(msgs):
    return [
        m.get("tool_call_id")
        for m in msgs
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id")
    ]


def find_gaps(msgs):
    dec, res = set(declared_ids(msgs)), set(result_ids(msgs))
    dangling = [i for i in declared_ids(msgs) if i not in res]   # 有调用没结果
    orphan = [i for i in result_ids(msgs) if i not in dec]       # 有结果没调用
    return dangling, orphan


# ---------- 源文件定位 ----------
def convert_session_name(session_name):
    suffix = ".json" if str(session_name).lower().endswith(".json") else ""
    stem = session_name[:-5] if suffix else session_name
    match = re.match(r"^(\d+)-(?:develop|test|bugfix)-(.+)$", stem)
    return f"{match.group(1)}-{match.group(2)}{suffix}" if match else session_name


def find_path_parts(parts, needle):
    if not needle:
        return -1
    for index in range(0, len(parts) - len(needle) + 1):
        if tuple(parts[index : index + len(needle)]) == tuple(needle):
            return index
    return -1


def delivery_session_coordinates(deliv_root, deliv_file, session_path):
    rel_parts = Path(os.path.relpath(deliv_file, deliv_root)).parts
    session_parts = Path(session_path).parts
    session_index = find_path_parts(rel_parts, session_parts)
    if session_index < 0:
        return None
    task_name = rel_parts[session_index - 1] if session_index > 0 else Path(deliv_root).name
    after = tuple(rel_parts[session_index + len(session_parts) :])
    if not after:
        return None
    if "subagents" in after:
        subagent_index = after.index("subagents")
        if subagent_index == 0:
            return None
        session_name = after[0]
        session_tail = ("subagents", after[-1])
    else:
        session_name = after[-1]
        session_tail = ()
    return rel_parts, task_name, session_name, session_tail


def resolve_source(deliv_root, deliv_file, source_root, source_state="sessions", session_path="sessions"):
    rel = os.path.relpath(deliv_file, deliv_root)
    rel_parts = Path(rel).parts
    candidates = []

    def add_candidate(path):
        path = str(path)
        if path not in candidates:
            candidates.append(path)

    coords = delivery_session_coordinates(deliv_root, deliv_file, session_path)
    if coords is None:
        return None
    rel_parts, task_name, session_name, session_tail = coords
    session_index = find_path_parts(rel_parts, Path(session_path).parts)
    task_rel_parts = rel_parts[session_index:]

    source_task_roots = []
    if task_name:
        source_task_roots.append(Path(source_root, task_name))
    source_task_roots.append(Path(source_root))

    if source_state in {"auto", "sessions"}:
        add_candidate(Path(source_root, *rel_parts))
        if task_name:
            add_candidate(Path(source_root, task_name, *task_rel_parts))

    if session_name and source_state in {"auto", "convert"}:
        convert_session = convert_session_name(session_name)
        for source_task_root in source_task_roots:
            add_candidate(Path(source_task_root, "convert-sessions", convert_session, *session_tail))

    if session_name and source_state in {"auto", "sessions"}:
        session_source_tails = [session_tail]
        if session_tail and session_tail[0] == "subagents":
            session_source_tails.append(("subagents__agent",) + session_tail[1:])
        for source_task_root in source_task_roots:
            for session_source_tail in session_source_tails:
                add_candidate(Path(source_task_root, "sessions", session_name, *session_source_tail))

    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    # 回退：auto 可在 TASK 内兜底搜；convert 只在 convert-sessions 内兜底搜。
    if source_state == "convert":
        fallback_roots = [p / "convert-sessions" for p in source_task_roots]
    elif source_state == "sessions":
        fallback_roots = [p / "sessions" for p in source_task_roots]
    else:
        fallback_roots = source_task_roots
    for fallback_root in fallback_roots:
        if not fallback_root.is_dir():
            continue
        for p in fallback_root.rglob(os.path.basename(deliv_file)):
            return str(p)
    return None


# ---------- 核心：从源补回缺口 ----------
def restore(dmsgs, smsgs):
    """返回 (new_msgs, actions, cjk_warn)。只补能闭合缺口的消息。

    处理三种情形：
      1. 孤儿结果 + 宿主 assistant 仍部分残留 → 把缺失的 tool_call 补回该 assistant。
      2. 悬空调用 → 把缺失的 tool 结果补回 assistant 之后。
      3. 孤儿结果 + 宿主 assistant 被整体删除 → 从源把整个 assistant 节点连同它声明的
         全部 tool 结果整块补回（仅当这些结果都能从源/交付版取得、可闭合时才动手）。
    """
    actions, cjk_warn = [], 0
    msgs = [dict(m) for m in dmsgs]

    def _cjk(obj):
        return 1 if CJK.search(json.dumps(obj, ensure_ascii=False)) else 0

    # 源索引
    s_result = {}                # id -> 完整 tool 结果消息
    s_call = {}                  # id -> (tool_call 对象, 该 assistant 的全部 id 顺序)
    s_host = {}                  # id -> 源里声明该调用的 assistant 消息对象
    for m in smsgs:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            s_result[m["tool_call_id"]] = m
        if m.get("role") == "assistant":
            sib = [tc.get("id") for tc in m.get("tool_calls") or [] if isinstance(tc, dict)]
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    s_call[tc["id"]] = (tc, sib)
                    s_host[tc["id"]] = m

    # 1) 孤儿结果：宿主仍部分残留 → 把缺失的 tool_call 补回对应 assistant
    for oid in find_gaps(msgs)[1]:
        info = s_call.get(oid)
        if not info:
            actions.append(f"孤儿结果 {oid}: 源里无对应调用 → 跳过")
            continue
        tc_obj, order = info
        host = None
        for m in msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                dids = [t.get("id") for t in m["tool_calls"]]
                if (set(dids) & set(order)) and oid not in dids:
                    host = m
                    break
        if host is None:
            # 宿主 assistant 被整体删除，交由第 3 步整块重建。
            continue
        calls = list(host["tool_calls"]) + [tc_obj]
        calls.sort(key=lambda t: order.index(t["id"]) if t.get("id") in order else 1 << 30)
        host["tool_calls"] = calls
        cjk_warn += _cjk(tc_obj)
        actions.append(f"补回 tool_call {oid}（修孤儿结果）")

    # 2) 悬空调用：把缺失的 tool 结果补回 assistant 之后（按 tool_calls 顺序）
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            aids = [t.get("id") for t in m["tool_calls"] if isinstance(t, dict)]
            j = i + 1
            block = []
            while j < len(msgs) and msgs[j].get("role") == "tool" and msgs[j].get("tool_call_id") in aids:
                block.append(msgs[j])
                j += 1
            have = {b["tool_call_id"] for b in block}
            missing = [x for x in aids if x not in have]
            if missing and all(x in s_result for x in missing):   # 全部能补才动手
                for x in missing:
                    r = s_result[x]
                    cjk_warn += _cjk(r)
                    block.append(r)
                block.sort(key=lambda b: aids.index(b["tool_call_id"]))
                msgs[i + 1 : j] = block
                actions.append(f"补回 {len(missing)} 条 tool 结果到 assistant#{i}（修悬空调用）")
                i += 1 + len(block)
                continue
        i += 1

    # 3) 孤儿结果 + 宿主被整体删除：从源整块重建 assistant + 其全部 tool 结果
    remaining = find_gaps(msgs)[1]
    if remaining:
        # 按“源里声明该调用的 assistant”分组（同一宿主的多个孤儿只重建一次）
        groups, order_keys = {}, []
        for oid in remaining:
            host = s_host.get(oid)
            if host is None:
                actions.append(f"孤儿结果 {oid}: 源里无对应调用 → 跳过")
                continue
            key = id(host)
            if key not in groups:
                call_ids = [tc.get("id") for tc in host.get("tool_calls") or []
                            if isinstance(tc, dict) and tc.get("id")]
                groups[key] = {"asst": host, "call_ids": call_ids}
                order_keys.append(key)

        deliv_result = {m.get("tool_call_id"): m for m in msgs
                        if m.get("role") == "tool" and m.get("tool_call_id")}

        # 只重建“声明的每个 call 都能取到结果”的组，否则整组跳过（保持可闭合）
        buildable, consumed, id_to_key = {}, set(), {}
        for key in order_keys:
            call_ids = groups[key]["call_ids"]
            if call_ids and all((c in deliv_result) or (c in s_result) for c in call_ids):
                buildable[key] = groups[key]
                for c in call_ids:
                    consumed.add(c)
                    id_to_key[c] = key
            else:
                miss = [c for c in call_ids if c not in deliv_result and c not in s_result]
                actions.append(f"整删宿主 assistant（声明 {call_ids}）: 结果无法闭合 {miss} → 跳过")

        if buildable:
            def build_block(g):
                nonlocal cjk_warn
                asst = copy.deepcopy(g["asst"])
                cjk_warn += _cjk(asst)
                blk = [asst]
                for cid in g["call_ids"]:
                    r = copy.deepcopy(deliv_result.get(cid) or s_result.get(cid))
                    cjk_warn += _cjk(r)
                    blk.append(r)
                return blk

            new_msgs, placed = [], set()
            for m in msgs:
                if m.get("role") == "tool" and m.get("tool_call_id") in consumed:
                    key = id_to_key.get(m.get("tool_call_id"))
                    if key is not None and key not in placed:
                        new_msgs.extend(build_block(buildable[key]))
                        placed.add(key)
                        actions.append(
                            f"重建整删宿主 assistant + {len(buildable[key]['call_ids'])} 条 tool 结果"
                            f"（修孤儿结果 {buildable[key]['call_ids']}）"
                        )
                    continue  # 丢弃交付版里这条孤儿 tool（已并入重建块）
                new_msgs.append(m)
            for key in buildable:            # 兜底：未在遍历中放置的组追加到末尾
                if key not in placed:
                    new_msgs.extend(build_block(buildable[key]))
                    placed.add(key)
            msgs = new_msgs

    return msgs, actions, cjk_warn


# ---------- 单文件处理 ----------
def process_file(deliv_root, path, source_root, source_state, dry_run, session_path):
    data, msgs = load(path)
    if not isinstance(msgs, list) or not find_gaps(msgs)[0] and not find_gaps(msgs)[1]:
        return None
    src = resolve_source(deliv_root, path, source_root, source_state, session_path)
    if not src:
        return {"path": path, "actions": ["有配对缺口，但未找到源文件 → 跳过"], "cjk": 0, "fixed": False}
    _, smsgs = load(src)
    new_msgs, actions, cjk = restore(msgs, smsgs)
    residual = find_gaps(new_msgs)
    ok = not residual[0] and not residual[1]
    if not dry_run and actions and new_msgs != msgs:
        if isinstance(data, dict):
            data["messages"] = new_msgs
            dump(path, data)
        else:
            dump(path, new_msgs)
    return {"path": path, "actions": actions, "cjk": cjk, "fixed": ok,
            "residual": residual[0] + residual[1]}


# ---------- 主流程 ----------
def main(argv=None):
    ap = argparse.ArgumentParser(description="从成功版源数据恢复被误删的工具调用/结果配对，修复 OpenAI 规范违规")
    ap.add_argument("deliv_root", type=Path, help="交付版根目录（含各 TASK-*）")
    ap.add_argument("--source", type=Path, default=None,
                    help="成功版源数据根目录（默认：交付版同级的 成功-20260618105913-559-544）")
    ap.add_argument("-path", "--session-path", type=Path, default=Path("sessions"),
                    help="相对交付根目录/每个 TASK 的 session 目录，默认 sessions")
    ap.add_argument("--dry-run", action="store_true", help="仅预览，不改文件")
    ap.add_argument("--source-state", choices=("auto", "convert", "sessions"), default="sessions",
                    help="源文件查找范围：auto 混合查找 sessions/convert-sessions；convert 只查 convert-sessions")
    args = ap.parse_args(argv)

    # 相对路径解析：先按当前目录找，找不到再按「仓库根目录」找
    # 本脚本位于 structure_fix_script/ 下，parents[1] 为仓库根。
    repo_root = Path(__file__).resolve().parents[1]

    def anchor(p):
        p = Path(p)
        if p.is_absolute() or p.is_dir():
            return p.resolve()
        return (repo_root / p).resolve()

    deliv_root = anchor(args.deliv_root)
    source_root = anchor(args.source) if args.source else (deliv_root.parent / "成功-20260618105913-559-544").resolve()
    if not deliv_root.is_dir():
        print(f"错误: 交付版目录不存在: {deliv_root}"); return 1
    if not source_root.is_dir():
        print(f"错误: 源数据目录不存在: {source_root}"); return 1

    results = []
    session_parts = tuple(Path(args.session_path).parts)
    for dp, _, fns in os.walk(deliv_root):
        dp_parts = Path(os.path.relpath(dp, deliv_root)).parts
        if find_path_parts(dp_parts, session_parts) < 0:
            continue
        if any(part in SKIPPED_SESSION_DIR_NAMES for part in dp_parts):
            continue
        for fn in fns:
            if fn.endswith(".json"):
                try:
                    r = process_file(str(deliv_root), os.path.join(dp, fn), str(source_root),
                                     args.source_state, args.dry_run, args.session_path)
                except Exception as e:
                    r = {"path": os.path.join(dp, fn), "actions": [f"处理出错: {e}"], "cjk": 0, "fixed": False}
                if r:
                    results.append(r)

    mode = "（预览，未改）" if args.dry_run else ""
    print("=" * 64)
    print(f"配对修复完成{mode}  源: {source_root}")
    print("=" * 64)
    if not results:
        print("No pairing gaps found.")
        return 0
    total_cjk = 0
    for r in results:
        print(f"\n[file] {os.path.relpath(r['path'], deliv_root)}")
        for a in r["actions"]:
            print(f"   - {a}")
        if r.get("residual"):
            print(f"   [warn] residual unpaired ids: {r['residual']}")
        if r["cjk"]:
            print(f"   [warn] restored content contains CJK in {r['cjk']} place(s); no_chinese can handle it")
        total_cjk += r["cjk"]
    print(f"\nfiles: {len(results)}  restored CJK occurrences: {total_cjk}")
    print("\nRe-run checker:")
    print("    cd structure_check_script && python3 check_openaI_chat_completions.py \\")
    print(f"        -r <TASK_OR_DATA_ROOT> -path {args.session_path} -o <report_dir>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

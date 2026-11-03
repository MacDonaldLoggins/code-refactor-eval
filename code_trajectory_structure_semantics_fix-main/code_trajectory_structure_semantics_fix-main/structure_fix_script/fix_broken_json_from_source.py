#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定点修复整份 JSON 语法损坏的 session 文件（复用 convert 源）。

背景
----
上游清洗/截断/脱敏对 message 的 content 字符串做替换时若未正确转义，会破坏整份
文件的 JSON 语法（例如 content 里出现非法的 \\" 序列），使该 session 文件无法被
任何后续检查/修复步骤解析（fix_openai_pairing_restore、fix_invalid_tool_arguments
等都以“整份文件先能 json.loads”为前提）。本步骤专门补这个缺口。

策略（定点，只碰损坏处）
------------------------
仅当文件“整份 json.loads 失败”时才介入，然后迭代定点修复：
  1. 取 JSON 解析报错位置 pos，向前回溯最近的 tool_call_id / id，确定坏消息；
  2. 从 convert 源（同 tool_call_id/id）取该消息“正确的 content”；
  3. 用源 content 替换坏文本里该消息的 content 字面量（右侧以“下一个字段名/对象
     结束”锚定，不受坏字符串内部引号干扰）；只改这一段，其余字节原样保留；
  4. 重新解析，仍失败就处理下一处坏点，直到成功或无法推进。
只有最终 json.loads 通过才写盘；否则文件保持不动并在报告中标记，绝不写出更坏的文件。

参数与 fix_openai_pairing_restore.py 一致，支持 structure_check_fix.py --fix 透传的
--pairing-source / --source-state。
"""
from __future__ import annotations

import argparse
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

SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
# content 字段值 + 右锚点（下一个字段名，或对象结束 }）
CONTENT_RE = re.compile(r'("content"\s*:\s*)"(.*?)"(\s*(?:,\s*"[A-Za-z_][\w]*"\s*:|\}))', re.DOTALL)
ANCHOR_RE = re.compile(r'"(?:tool_call_id|id)"\s*:\s*"([^"]+)"')
MAX_ITERS = 500


# ---------- 源文件定位（与 fix_openai_pairing_restore 保持一致，支持 --source-state） ----------
def convert_session_name(session_name):
    suffix = ".json" if str(session_name).lower().endswith(".json") else ""
    stem = session_name[:-5] if suffix else session_name
    match = re.match(r"^(\d+)-(?:develop|test|bugfix)-(.+)$", stem)
    return f"{match.group(1)}-{match.group(2)}{suffix}" if match else session_name


def find_path_parts(parts, needle):
    if not needle:
        return -1
    for index in range(0, len(parts) - len(needle) + 1):
        if tuple(parts[index:index + len(needle)]) == tuple(needle):
            return index
    return -1


def delivery_session_coordinates(deliv_root, deliv_file, session_path):
    rel_parts = Path(os.path.relpath(deliv_file, deliv_root)).parts
    session_parts = Path(session_path).parts
    si = find_path_parts(rel_parts, session_parts)
    if si < 0:
        return None
    task_name = rel_parts[si - 1] if si > 0 else Path(deliv_root).name
    after = tuple(rel_parts[si + len(session_parts):])
    if not after:
        return None
    if "subagents" in after:
        if after.index("subagents") == 0:
            return None
        session_name = after[0]
        session_tail = ("subagents", after[-1])
    else:
        session_name = after[-1]
        session_tail = ()
    return rel_parts, task_name, session_name, session_tail


def resolve_source(deliv_root, deliv_file, source_root, source_state="sessions", session_path="sessions"):
    coords = delivery_session_coordinates(deliv_root, deliv_file, session_path)
    if coords is None:
        return None
    rel_parts, task_name, session_name, session_tail = coords
    si = find_path_parts(rel_parts, Path(session_path).parts)
    task_rel_parts = rel_parts[si:]
    roots = []
    if task_name:
        roots.append(Path(source_root, task_name))
    roots.append(Path(source_root))
    cands = []

    def add(p):
        p = str(p)
        if p not in cands:
            cands.append(p)

    if source_state in {"auto", "sessions"}:
        add(Path(source_root, *rel_parts))
        if task_name:
            add(Path(source_root, task_name, *task_rel_parts))
    if session_name and source_state in {"auto", "convert"}:
        cs = convert_session_name(session_name)
        for r in roots:
            add(Path(r, "convert-sessions", cs, *session_tail))
    if session_name and source_state in {"auto", "sessions"}:
        tails = [session_tail]
        if session_tail and session_tail[0] == "subagents":
            tails.append(("subagents__agent",) + session_tail[1:])
        for r in roots:
            for t in tails:
                add(Path(r, "sessions", session_name, *t))
    for c in cands:
        if os.path.isfile(c):
            return c
    if source_state == "convert":
        froots = [p / "convert-sessions" for p in roots]
    elif source_state == "sessions":
        froots = [p / "sessions" for p in roots]
    else:
        froots = roots
    for fr in froots:
        if not fr.is_dir():
            continue
        for p in fr.rglob(os.path.basename(deliv_file)):
            return str(p)
    return None


# ---------- 修复核心 ----------
def load_source_content_map(src_path):
    data = json.loads(Path(src_path).read_bytes().decode("utf-8-sig"))
    msgs = data.get("messages") if isinstance(data, dict) else data
    mapping = {}
    if isinstance(msgs, list):
        for x in msgs:
            if isinstance(x, dict) and isinstance(x.get("content"), str):
                key = x.get("tool_call_id") or x.get("id")
                if key:
                    mapping[key] = x["content"]
    return mapping


def repair_text(text, src_content):
    """迭代定点修复。返回 (new_text, fixed_ids, status)。"""
    fixed = []
    for _ in range(MAX_ITERS):
        try:
            json.loads(text)
            return text, fixed, "ok"
        except json.JSONDecodeError as exc:
            pos = exc.pos
        last = None
        for mm in ANCHOR_RE.finditer(text):
            if mm.start() < pos:
                last = mm
            else:
                break
        if not last:
            return text, fixed, "locate_id_failed"
        bad_id = last.group(1)
        content = src_content.get(bad_id)
        if content is None:
            return text, fixed, f"source_missing_content:{bad_id}"
        region = text[last.end():]
        m = CONTENT_RE.search(region)
        if not m:
            return text, fixed, f"content_field_not_found:{bad_id}"
        new_region = region[:m.start()] + m.group(1) + json.dumps(content, ensure_ascii=False) + m.group(3) + region[m.end():]
        new_text = text[:last.end()] + new_region
        if new_text == text:
            return text, fixed, f"no_progress:{bad_id}"
        text = new_text
        fixed.append(bad_id)
    return text, fixed, "max_iters_exceeded"


def process_file(deliv_root, path, source_root, source_state, session_path, apply):
    raw = Path(path).read_bytes().decode("utf-8-sig")
    try:
        json.loads(raw)
        return None  # 合法，无需修
    except json.JSONDecodeError as exc:
        first_error = str(exc)
    src = resolve_source(deliv_root, path, source_root, source_state, session_path)
    if not src:
        return {"path": path, "status": "no_source", "first_error": first_error, "fixed_ids": [], "fixed_count": 0}
    try:
        src_content = load_source_content_map(src)
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "source": src, "status": "source_unreadable", "error": str(exc), "fixed_ids": [], "fixed_count": 0}
    new_text, fixed, status = repair_text(raw, src_content)
    ok = status == "ok"
    applied = bool(ok and apply and new_text != raw)
    if applied:
        Path(path).write_bytes(new_text.encode("utf-8"))  # 仅 splice content，其余字节（含换行）原样
    return {
        "path": path,
        "source": src,
        "status": "fixed" if ok else status,
        "fixed_ids": fixed,
        "fixed_count": len(fixed),
        "applied": applied,
        "first_error": first_error,
    }


def iter_json_files(root, session_path):
    root = os.path.abspath(root)
    scan = []
    direct = os.path.join(root, session_path)
    if os.path.isdir(direct):
        scan = [direct]
    else:
        for name in sorted(os.listdir(root)):
            if name.startswith("TASK-"):
                cand = os.path.join(root, name, session_path)
                if os.path.isdir(cand):
                    scan.append(cand)
    for sroot in scan:
        for dp, dns, fns in os.walk(sroot):
            dns[:] = [d for d in dns if d not in SKIPPED_SESSION_DIR_NAMES and not d.startswith(".")]
            for fn in fns:
                if fn.endswith(".json") and not fn.startswith(".") and not fn.endswith(".meta.json"):
                    yield os.path.join(dp, fn)


def main(argv=None):
    ap = argparse.ArgumentParser(description="定点修复整份 JSON 语法损坏的 session 文件（复用 convert 源）")
    ap.add_argument("deliv_root", type=Path, help="交付版根目录（含各 TASK-*）")
    ap.add_argument("--source", type=Path, required=True, help="convert 源数据根目录")
    ap.add_argument("-path", "--session-path", default="sessions", help="每个 TASK 下的 session 目录，默认 sessions")
    ap.add_argument("--source-state", choices=("auto", "convert", "sessions"), default="sessions",
                    help="源查找范围：convert 只查 convert-sessions；sessions 只查 sessions；auto 两者都试")
    ap.add_argument("-o", "--output", type=Path, default=Path("fix_broken_json_report.json"), help="JSON 报告路径")
    ap.add_argument("--apply", action="store_true", help="写回修复结果（默认 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="仅预览，不写盘")
    args = ap.parse_args(argv)

    apply = args.apply and not args.dry_run
    deliv_root = str(args.deliv_root.expanduser().resolve())
    source_root = str(args.source.expanduser().resolve())
    if not os.path.isdir(source_root):
        print(f"错误: 源目录不存在: {source_root}", file=sys.stderr)
        return 1

    results = []
    for f in iter_json_files(deliv_root, args.session_path):
        r = process_file(deliv_root, f, source_root, args.source_state, args.session_path, apply)
        if r is not None:
            results.append(r)
            print(f"[{r['status']}] fixed={r.get('fixed_count', 0)} applied={r.get('applied', False)} {os.path.relpath(f, deliv_root)}", flush=True)

    summary = {
        "scanned_broken": len(results),
        "fixed": sum(1 for r in results if r["status"] == "fixed"),
        "applied": sum(1 for r in results if r.get("applied")),
        "unresolved": sum(1 for r in results if r["status"] != "fixed"),
        "apply": apply,
    }
    report = {
        "report_type": "fix_broken_json_report",
        "source_root": source_root,
        "session_path": str(args.session_path),
        "source_state": args.source_state,
        "summary": summary,
        "results": results,
    }
    out = args.output.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport: {out}  summary={summary}")
    if not apply:
        print("dry-run（未写盘）；加 --apply 才写回。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

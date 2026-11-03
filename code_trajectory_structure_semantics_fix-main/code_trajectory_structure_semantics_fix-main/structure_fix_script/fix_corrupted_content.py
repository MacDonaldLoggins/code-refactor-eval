#!/usr/bin/env python3
"""清洗脚本：删除流式响应截断/损坏的 assistant 节点。

检测原理：
  流式传输中途崩溃时返回的二进制碎片被当作文本存入 JSON，
  表现为字符散布在 Hebrew、Greek、Syriac、NKo 等多个不相关 Unicode block，
  同时 _metadata.tokens.output = 0 且 timestamp = null。

修复方式：
  直接删除被判定为损坏的 assistant 消息节点。

用法：
  python3 structure_fix_script/fix_corrupted_content.py -r <根目录> -i sessions [-o <报告路径>] [-report <报告根目录>]

  -o 是可选的（旧格式报告），可以是文件路径或目录路径：
     - 不传 -o：只在每个 TASK*/report/ 下生成 per-task 报告
     - 传 -o 文件：生成合并报告 + per-task 报告
     - 传 -o 目录：合并报告以默认名写入该目录 + per-task 报告

  -report 是可选的（新模板 structure_report 报告，按单个 TASK 维度输出）：
     - 不传 -report：写到每个 TASK 自己的 report/ 目录，文件名为脚本名，
       即 <task目录>/report/fix_corrupted_content.json
     - 传 -report <目录>：写到 <目录>/fix_corrupted_content/<task_id>_structure.json

示例：
  python3 structure_fix_script/fix_corrupted_content.py \
    -r /home/lcs/workspace/data/7-7 \
    -i sessions
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_NAME = "fix_corrupted_content_report.json"
REPORT_TYPE = "fix_corrupted_content_report"
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results"}

# ---- 新模板（structure_report）报告相关 ----
SCRIPT_NAME = "fix_corrupted_content"
STRUCTURE_REPORT_TYPE = "structure_report"
STRUCTURE_ACTION_TYPE = "fix"
STRUCTURE_REPORT_SUFFIX = "_structure.json"
# function 字段：按被删除节点的具体类型区分
FUNCTION_TOOLCALL_CORRUPT = "工具调用节点损坏内容删除"
FUNCTION_ASSISTANT_CORRUPT = "assistant节点损坏内容删除"
PER_TASK_REPORT_SUBDIR = "report"

RARE_RATIO_THRESHOLD = 0.3
BLOCK_DIVERSITY_THRESHOLD = 4
MIN_CONTENT_LENGTH = 3

RARE_BLOCK_RANGES = [
    (0x0370, 0x03FF),  # Greek and Coptic
    (0x0400, 0x04FF),  # Cyrillic
    (0x0500, 0x05FF),  # Armenian / Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x0700, 0x074F),  # Syriac
    (0x0750, 0x077F),  # Arabic Supplement
    (0x0780, 0x07BF),  # Thaana
    (0x07C0, 0x07FF),  # NKo
    (0x0800, 0x08FF),  # Samaritan / Mandaic / Arabic Extended
    (0x0900, 0x0DFF),  # Indic scripts (Devanagari, Bengali, Tamil, etc.)
    (0x0E00, 0x0E7F),  # Thai
    (0x0E80, 0x0EFF),  # Lao
    (0x1000, 0x109F),  # Myanmar
    (0x10A0, 0x10FF),  # Georgian
    (0x1200, 0x137F),  # Ethiopic
]


@dataclass
class FixResult:
    project_dir: str
    session_path: str
    valid_json: bool = True
    error: str = ""
    messages_before: int = 0
    messages_after: int = 0
    deleted_count: int = 0
    deleted_tool_count: int = 0
    deleted_indices: list[int] | None = None
    session_id: str = ""
    task_path: str = ""
    deleted_details: list[dict[str, Any]] | None = None

    def __post_init__(self):
        if self.deleted_indices is None:
            self.deleted_indices = []
        if self.deleted_details is None:
            self.deleted_details = []


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_rare_block(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in RARE_BLOCK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _unicode_block_key(ch: str) -> str:
    cp = ord(ch)
    if cp <= 0x007F:
        return "BasicLatin"
    if cp <= 0x00FF:
        return "Latin1Supp"
    if cp <= 0x024F:
        return "LatinExtAB"
    if cp <= 0x02AF:
        return "IPA"
    if cp <= 0x02FF:
        return "SpacingMod"
    if cp <= 0x036F:
        return "CombDiacrit"
    if cp <= 0x03FF:
        return "Greek"
    if cp <= 0x04FF:
        return "Cyrillic"
    if cp <= 0x05FF:
        return "Hebrew"
    if cp <= 0x06FF:
        return "Arabic"
    if cp <= 0x07FF:
        return "SyriacNKo"
    if cp <= 0x0FFF:
        return "OtherBMP1"
    if cp <= 0x1FFF:
        return "OtherBMP2"
    if cp <= 0x2FFF:
        return "Symbols"
    if cp <= 0x4DFF:
        return "CJK_A"
    if cp <= 0x9FFF:
        return "CJK"
    if cp <= 0xFFFF:
        return "OtherBMP3"
    return "SMP"


def normalize_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := normalize_content(item)))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _evaluate_message(msg: Any) -> tuple[bool, str]:
    """判定单条消息是否为损坏 assistant，返回 (是否损坏, 命中原因说明)。"""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False, ""
    content = normalize_content(msg.get("content"))
    if not content or len(content) < MIN_CONTENT_LENGTH:
        return False, ""

    rare_count = sum(1 for ch in content if _is_rare_block(ch))
    rare_ratio = rare_count / len(content)

    blocks = set()
    for ch in content:
        blocks.add(_unicode_block_key(ch))
    block_diversity = len(blocks)

    meta = msg.get("_metadata", {})
    tokens = meta.get("tokens", {}) if isinstance(meta, dict) else {}
    output_tokens = tokens.get("output", -1) if isinstance(tokens, dict) else -1
    timestamp = meta.get("timestamp") if isinstance(meta, dict) else "unknown"
    zero_output = output_tokens == 0
    null_timestamp = timestamp is None

    # 信号1: 冷门 block 占比高 + 多样性高
    signal1 = rare_ratio > RARE_RATIO_THRESHOLD and block_diversity >= BLOCK_DIVERSITY_THRESHOLD
    # 信号2: output=0 + content 非空 + 冷门字符
    signal2 = zero_output and len(content) > 0 and rare_ratio > 0.2
    # 信号3: timestamp=null + 冷门字符
    signal3 = null_timestamp and rare_ratio > 0.2

    if not (signal1 or signal2 or signal3):
        return False, ""

    hits: list[str] = []
    if signal1:
        hits.append(f"冷门Unicode占比{rare_ratio:.0%}、block多样性{block_diversity}")
    if signal2:
        hits.append(f"output_tokens=0且含冷门字符(占比{rare_ratio:.0%})")
    if signal3:
        hits.append(f"timestamp=null且含冷门字符(占比{rare_ratio:.0%})")
    reason = "检测到流式响应损坏的 assistant 节点：" + "；".join(hits) + "，已删除"
    return True, reason


def detect_corrupted(messages: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """返回被判定为损坏的 assistant 索引与原因 (index, reason)，index 为 0-indexed。"""
    corrupted: list[tuple[int, str]] = []
    for i, msg in enumerate(messages):
        is_corrupted, reason = _evaluate_message(msg)
        if is_corrupted:
            corrupted.append((i, reason))
    return corrupted


def detect_corrupted_indices(messages: list[dict[str, Any]]) -> list[int]:
    """返回 assistant 消息中被判定为损坏的索引列表（0-indexed）。"""
    return [index for index, _ in detect_corrupted(messages)]


def _top_level_element_lines(text: str) -> list[int]:
    """返回一个 JSON 数组文本中每个顶层元素起始的行号（1-indexed，相对该文本）。"""
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


def messages_element_lines_from_text(text: str) -> list[int]:
    """从 session JSON 原文中解析出 messages 数组内各元素起始行号（1-indexed，整文件维度）。"""
    marker = '"messages"'
    start = text.find(marker)
    if start == -1:
        return []
    array_start = text.find("[", start + len(marker))
    if array_start == -1:
        return []
    line_offset = text.count("\n", 0, array_start)
    return [line_offset + line for line in _top_level_element_lines(text[array_start:])]


def fix_one_file(source_path: Path, project_dir: Path) -> tuple[FixResult, dict[str, Any] | None]:
    """处理单个 session JSON 文件。返回 (result, fixed_data_or_None)。"""
    result = FixResult(
        project_dir=project_dir.name,
        session_path=str(source_path),
        session_id=source_path.stem,
        task_path=str(project_dir),
    )

    if not source_path.exists():
        result.valid_json = False
        result.error = "file does not exist"
        return result, None

    try:
        source_text = source_path.read_text(encoding="utf-8-sig")
        data = json.loads(source_text)
    except (OSError, json.JSONDecodeError) as exc:
        result.valid_json = False
        result.error = str(exc)
        return result, None

    if not isinstance(data, dict):
        result.valid_json = False
        result.error = "top-level JSON is not an object"
        return result, None

    msgs = data.get("messages")
    if not isinstance(msgs, list):
        result.valid_json = False
        result.error = "messages field is missing or not an array"
        return result, None

    result.messages_before = len(msgs)
    corrupted_pairs = detect_corrupted(msgs)
    corrupted = {index for index, _ in corrupted_pairs}
    reasons = {index: reason for index, reason in corrupted_pairs}
    result.deleted_indices = sorted(corrupted)

    if not corrupted:
        result.messages_after = len(msgs)
        return result, None

    element_lines = messages_element_lines_from_text(source_text)

    def _line_of(index: int) -> int | None:
        return element_lines[index] if index < len(element_lines) else None

    # 逐个损坏 assistant 节点收集详情，并把它连锁删除的 tool 节点记入 addition
    deleted_tool_indices: set[int] = set()
    deleted_details: list[dict[str, Any]] = []
    for index in sorted(corrupted):
        node = msgs[index]
        call_ids = {
            call.get("id")
            for call in (node.get("tool_calls") or [])
            if isinstance(call, dict) and isinstance(call.get("id"), str)
        }
        assoc_tool_indices = [
            i
            for i, message in enumerate(msgs)
            if isinstance(message, dict)
            and message.get("role") == "tool"
            and message.get("tool_call_id") in call_ids
        ]
        deleted_tool_indices.update(assoc_tool_indices)

        # 取该损坏节点 _metadata.timestamp
        meta = node.get("_metadata", {})
        node_timestamp = meta.get("timestamp") if isinstance(meta, dict) else None

        # function：按被删除节点具体类型区分
        has_tool_calls = bool(node.get("tool_calls"))
        function = FUNCTION_TOOLCALL_CORRUPT if has_tool_calls else FUNCTION_ASSISTANT_CORRUPT

        addition: dict[str, Any] = {"timestamp": node_timestamp}
        if assoc_tool_indices:
            located = "、".join(
                f"messages 索引 {i}(JSON 第 {_line_of(i)} 行)" for i in assoc_tool_indices
            )
            addition["associated_deleted_tool"] = (
                f"该损坏节点发起的工具调用无对应结果，"
                f"已连锁删除其配对的 {len(assoc_tool_indices)} 个 tool 结果节点：{located}"
            )

        deleted_details.append({
            "function": function,
            "line": _line_of(index),
            "before_content": json.dumps(node, ensure_ascii=False),
            "problem": reasons.get(index, "检测到流式响应损坏的 assistant 节点，已删除"),
            "addition": addition,
        })

    deleted_indices = corrupted | deleted_tool_indices

    data["messages"] = [
        message for index, message in enumerate(msgs) if index not in deleted_indices
    ]
    result.messages_after = len(data["messages"])
    result.deleted_count = len(corrupted)
    result.deleted_tool_count = len(deleted_tool_indices)
    result.deleted_details = deleted_details
    return result, data


def is_candidate_session_file(path: Path) -> bool:
    return (
        path.is_file()
        and not path.name.startswith(".")
        and not path.name.endswith(".meta.json")
        and path.suffix.lower() in SESSION_FILE_SUFFIXES
        and not any(part in SKIPPED_SESSION_DIR_NAMES for part in path.parts)
    )


def should_scan_project_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and path.name not in SKIPPED_SESSION_DIR_NAMES


def iter_session_paths(project_dir: Path, session_path_arg: Path) -> list[Path]:
    target = project_dir / session_path_arg
    if target.is_file():
        return [target] if is_candidate_session_file(target) else []
    if target.is_dir():
        return sorted(
            (path for path in target.rglob("*") if is_candidate_session_file(path)),
            key=lambda path: str(path.relative_to(project_dir)),
        )
    return []


def build_json_report(results: list[FixResult]) -> dict[str, Any]:
    """构建与 clean_empty_assistant.py 一致格式的 JSON 报告。"""
    items: list[dict[str, Any]] = []
    for r in results:
        if not (r.deleted_count or r.error):
            continue

        if r.error:
            items.append({
                "修改": r.session_path,
                "修改前": f"处理失败：{r.error}",
                "修改后": "",
                "新增了什么": "",
                "删除了什么": "",
            })
            continue

        items.append({
            "修改": r.session_path,
            "修改前": f"{r.messages_before} 条消息",
            "修改后": f"{r.messages_after} 条消息",
            "新增了什么": "",
            "删除了什么": (
                f"删除 {r.deleted_count} 个损坏 assistant 节点 (索引: {r.deleted_indices})；"
                f"删除 {r.deleted_tool_count} 个关联 tool 节点"
            ),
        })
    return {"fix_corrupted_content": items}


def build_structure_report(
    task_id: str,
    task_path: str,
    task_results: list[FixResult],
) -> dict[str, Any]:
    """构建符合 structure.json 模板的 per-task 报告（单个 TASK 维度）。"""
    sessions: list[dict[str, Any]] = []
    total_issues = 0
    hit_trajectory_count = 0

    for r in task_results:
        details = r.deleted_details or []
        if not details:
            continue
        hit_trajectory_count += 1
        sub = [
            {
                "function": d["function"],
                "action_type": STRUCTURE_ACTION_TYPE,
                "before_content": d["before_content"],
                "after_content": "",
                "problem": d["problem"],
                "line": d["line"],
                "addition": d["addition"],
            }
            for d in details
        ]
        total_issues += len(sub)
        sessions.append({"session_id": r.session_id, "sub": sub})

    return {
        "report_type": STRUCTURE_REPORT_TYPE,
        "task_id": task_id,
        "task_path": task_path,
        "create": generated_at_text(),
        "summary": {
            "total_trajectory_count": len(task_results),
            "hit_trajectory_count": hit_trajectory_count,
            "total_issues": total_issues,
        },
        "sessions": sessions,
    }


def write_structure_reports(
    results: list[FixResult],
    report_dir: Path | str | None,
) -> None:
    """按 TASK 维度输出新模板（structure_report）报告。

    - 不传 report_dir（默认）：写到每个 TASK 自己的 report/ 目录，文件名为 <脚本名>.json，
      即 <task_path>/report/fix_corrupted_content.json。
    - 传 report_dir：写到 <report_dir>/<脚本名>/<task_id>_structure.json。
    """
    # 保持扫描顺序聚合每个 TASK 的结果
    task_ids: list[str] = []
    grouped: dict[str, list[FixResult]] = {}
    task_paths: dict[str, str] = {}
    for r in results:
        if r.project_dir not in grouped:
            grouped[r.project_dir] = []
            task_ids.append(r.project_dir)
            task_paths[r.project_dir] = r.task_path
        grouped[r.project_dir].append(r)

    custom_base = Path(report_dir).expanduser().resolve() if report_dir else None

    for task_id in task_ids:
        report = build_structure_report(task_id, task_paths[task_id], grouped[task_id])
        if custom_base is not None:
            out_path = custom_base / SCRIPT_NAME / f"{task_id}{STRUCTURE_REPORT_SUFFIX}"
        else:
            out_path = Path(task_paths[task_id]) / PER_TASK_REPORT_SUBDIR / f"{SCRIPT_NAME}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Structure report: {out_path}")


def fix_corrupted_content(
    find_path: Path | str,
    session_json_path: Path | str,
    report_path: Path | str | None = None,
    structure_report_dir: Path | str | None = None,
) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"scan path does not exist or is not a directory: {root_dir}")

    session_relative_path = Path(session_json_path)

    results: list[FixResult] = []
    root_session_target = root_dir / session_relative_path

    project_dirs = (
        [root_dir]
        if root_session_target.exists()
        else sorted(
            (path for path in root_dir.iterdir() if should_scan_project_dir(path)),
            key=lambda path: path.name,
        )
    )

    for project_dir in project_dirs:
        session_paths = iter_session_paths(project_dir, session_relative_path)
        if not session_paths:
            continue

        for session_path in session_paths:
            relative_name = session_path.name
            try:
                relative_name = f"{project_dir.name}/{session_path.relative_to(project_dir).as_posix()}"
            except ValueError:
                relative_name = f"{project_dir.name}/{session_path.name}"

            print(f"fixing corrupted: {relative_name} ... ", end="", flush=True)
            fix_result, fixed_data = fix_one_file(session_path, project_dir)
            results.append(fix_result)

            if fixed_data is not None:
                session_path.write_text(
                    json.dumps(fixed_data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

            if fix_result.error:
                print(f"ERROR: {fix_result.error}")
            elif fix_result.deleted_count > 0:
                print(
                    f"deleted assistant={fix_result.deleted_count} "
                    f"tool={fix_result.deleted_tool_count} "
                    f"({fix_result.messages_before}->{fix_result.messages_after})"
                )
            else:
                print("no change")

    # 汇总报告
    total_files = len(results)
    total_deleted = sum(r.deleted_count for r in results)
    total_deleted_tool = sum(r.deleted_tool_count for r in results)
    total_modified = sum(1 for r in results if r.deleted_count > 0)

    # 如果指定了 -o，在指定路径输出合并报告
    if report_path is not None:
        report_path_obj = Path(report_path).expanduser().resolve()
        if report_path_obj.is_dir() or report_path_obj.suffix.lower() != ".json":
            report_path_obj = report_path_obj / REPORT_NAME
        report = build_json_report(results)
        report_path_obj.parent.mkdir(parents=True, exist_ok=True)
        report_path_obj.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Combined report: {report_path_obj}")

    # 在每个 project_dir/report/ 下写该 task 自己的报告
    project_dirs_used = sorted(
        {r.project_dir for r in results},
        key=lambda x: x,
    )
    for proj_name in project_dirs_used:
        proj_results = [r for r in results if r.project_dir == proj_name]
        if not any(r.deleted_count or r.error for r in proj_results):
            continue
        per_task_report = build_json_report(proj_results)
        per_task_path = root_dir / proj_name / "report" / REPORT_NAME
        per_task_path.parent.mkdir(parents=True, exist_ok=True)
        per_task_path.write_text(
            json.dumps(per_task_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Per-task report: {per_task_path}")

    # 新模板（structure_report）报告：按 TASK 维度输出，默认写到 <repo>/reports/
    if results:
        write_structure_reports(results, structure_report_dir)

    print(f"\nFix corrupted content complete.")
    print(f"Files scanned: {total_files}")
    print(f"Files modified: {total_modified}")
    print(f"Corrupted assistant nodes deleted: {total_deleted}")
    print(f"Associated tool nodes deleted: {total_deleted_tool}")
    return root_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete corrupted streaming response assistant nodes from session JSON files."
    )
    parser.add_argument(
        "-r",
        dest="find_path",
        type=Path,
        required=True,
        help="Root containing project subdirectories.",
    )
    parser.add_argument(
        "-i",
        dest="session_json_path",
        type=Path,
        required=True,
        help="Session file or session directory path relative to each project directory.",
    )
    parser.add_argument(
        "-o",
        dest="report_path",
        type=Path,
        required=False,
        default=None,
        help="Optional: output combined report path (file or directory). If omitted, only per-task reports are written to TASK*/report/.",
    )
    parser.add_argument(
        "-report",
        dest="structure_report_dir",
        type=Path,
        required=False,
        default=None,
        help=(
            "Optional: base directory for the new structure_report template output. "
            "If omitted, the report is written to each TASK's own report/ dir as "
            "<task_dir>/report/fix_corrupted_content.json. If given, files are written "
            "as <base>/fix_corrupted_content/<task_id>_structure.json."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        fix_corrupted_content(
            args.find_path,
            args.session_json_path,
            args.report_path,
            args.structure_report_dir,
        )
    except Exception as exc:
        print(f"fix_corrupted_content failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

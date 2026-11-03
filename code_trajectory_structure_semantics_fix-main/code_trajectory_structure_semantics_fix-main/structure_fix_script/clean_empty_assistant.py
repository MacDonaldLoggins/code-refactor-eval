#!/usr/bin/env python3
"""清洗脚本：删除 content 与 reasoning_content 双空的 assistant 节点。

处理三种情况：

情况 A（15,000+ 例）：
  tool → assistant(empty,tc) → tool(s) → assistant
  删除 empty + 其 tool 结果。结果：tool → assistant ✅

情况 B（323 例，连续 assistant）：
  assistant → assistant(empty,tc) → tool(s)
  将 empty 的 tool_calls 合并到前一个 assistant，再删 empty
  结果：assistant + merged_tc → tool(s) ✅

情况 C（15 例，连续 user）：
  user → assistant(empty,tc) → tool(s) → user
  保留后面的 user，删前面的 user + 空 assistant + tool 结果
  结果：user(后一个) → (后续消息) ✅

新增修复：末节点空assistant豁免
双空且无tool_calls的assistant如果是整条消息最后一条，则保留不删除，留给下游fix_missing_summary_nodes补总结，
避免轨迹收尾丢失assistant节点。

用法：
  python structure_fix_script/clean_empty_assistant.py -r <根目录> -i sessions -o <报告路径>

  -o 可以是文件路径或目录路径，目录时默认使用 clean_empty_assistant_report.json

示例：
  # 指定文件
  python structure_fix_script/clean_empty_assistant.py \
    -r /home/lcs/workspace/data/7-7 \
    -i sessions \
    -o /home/lcs/workspace/data/7-7/report.json

  # 指定目录（默认文件名）
  python structure_fix_script/clean_empty_assistant.py \
    -r /home/lcs/workspace/data/7-7 \
    -i sessions \
    -o /home/lcs/workspace/data/7-7-report
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_NAME = "clean_empty_assistant_report.json"
REPORT_TYPE = "clean_empty_assistant_report"
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results"}

# ====================== 新增 compact 判定正则 ======================
# compact 事件判定 —— 与 fix_compact_alignment.COMPACT_ID_RE 保持一致。
# 若那边的正则改动，请同步这里。
_COMPACT_ID_RE = re.compile(r"acompact-?[0-9a-f]+", re.IGNORECASE)


def _is_compact_event(msg: dict, next_msg: dict | None) -> bool:
    """空 assistant + Agent 工具调用 + 紧邻配对 tool 结果 → 认定为 compact 事件。"""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    tool_calls = msg.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return False
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        name = fn.get("name") if isinstance(fn, dict) else call.get("name")
        if name != "Agent":
            continue
        call_id = call.get("id")
        if not isinstance(call_id, str) or not _COMPACT_ID_RE.search(call_id):
            continue
        if (
            isinstance(next_msg, dict)
            and next_msg.get("role") == "tool"
            and next_msg.get("tool_call_id") == call_id
        ):
            return True
    return False
# ==================================================================


@dataclass
class CleanResult:
    project_dir: str
    session_path: str
    valid_json: bool = True
    error: str = ""
    deleted_empty: int = 0
    deleted_tool: int = 0
    merged_tc: int = 0
    merged_user: int = 0
    total_before: int = 0
    total_after: int = 0
    before_content: list[dict[str, Any]] = field(default_factory=list)
    after_content: list[dict[str, Any]] = field(default_factory=list)
    deleted_msg_indices: list[int] = field(default_factory=list)
    deleted_user_indices: list[int] = field(default_factory=list)


def _classify_case(r: CleanResult) -> str | None:
    """根据 CleanResult 字段判断命中哪种情况。"""
    if r.merged_tc > 0:
        return "B"
    if r.merged_user > 0:
        return "C"
    if r.deleted_empty > 0 and r.deleted_tool > 0:
        return "A"
    if r.deleted_empty > 0:
        return "A"
    return None


def _case_problem(case: str) -> str:
    return {
        "A": "tool -> empty_assistant(tc) -> tool(s) -> assistant, empty+tool_calls removed",
        "B": "assistant -> empty_assistant(tc) -> tool(s), tool_calls merged into prev assistant",
        "C": "user -> empty_assistant(tc) -> tool(s) -> user, prev user+empty+tool removed, later user kept",
    }.get(case, "empty assistant with tool_calls")


def _extract_task_id(rel: str, base: Path) -> tuple[str, Path]:
    parts = rel.replace("\\", "/").split("/")
    if parts and parts[0].startswith("TASK-"):
        return parts[0], base / parts[0]
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
    root_dir: Path,
    results: list[CleanResult],
) -> None:
    """为每个有修改的 task 写入独立修复总结 report。"""
    from collections import defaultdict

    task_total_scanned: dict[str, int] = defaultdict(int)
    task_modified: dict[str, list[CleanResult]] = defaultdict(list)

    for r in results:
        task_id, _ = _extract_task_id(r.session_path, root_dir)
        task_total_scanned[task_id] += 1
        if r.deleted_empty or r.merged_tc or r.merged_user or r.error:
            task_modified[task_id].append(r)

    for task_id, t_results in sorted(task_modified.items()):
        _, task_path = _extract_task_id("", root_dir)
        if task_id != root_dir.name:
            task_path = root_dir / task_id
        report_dir = task_path / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / "clean_empty_assistant_report.json"

        total_scanned = task_total_scanned[task_id]
        sessions: list[dict[str, Any]] = []
        hit_count = 0
        for r in sorted(t_results, key=lambda x: x.session_path):
            case = _classify_case(r)
            if case is None:
                continue
            hit_count += 1
            session_id = Path(r.session_path).stem
            session_timestamp = _extract_session_timestamp(Path(r.session_path))

            # line: 从被删除的消息索引推算行号
            line_info = None
            if r.deleted_user_indices:
                line_range = _find_message_line_range(Path(r.session_path), r.deleted_user_indices[0])
                if line_range:
                    s, e = line_range["start"], line_range["end"]
                    line_info = s if s == e else f"{s}-{e}"
            elif r.deleted_msg_indices:
                line_range = _find_message_line_range(Path(r.session_path), r.deleted_msg_indices[0])
                if line_range:
                    s, e = line_range["start"], line_range["end"]
                    line_info = s if s == e else f"{s}-{e}"

            action_type_map = {"A": "delete", "B": "update", "C": "delete"}
            action_type = action_type_map.get(case, "problem")

            case_addition: dict[str, Any] = {"case": f"case_{case}"}
            case_addition["session_timestamp"] = session_timestamp

            addition = [case_addition]

            sessions.append({
                "session_id": session_id,
                "sub": [
                    {
                        "function": "clean_empty_assistant",
                        "action_type": action_type,
                        "before_content": r.before_content,
                        "after_content": r.after_content,
                        "problem": _case_problem(case),
                        "line": line_info,
                        "addition": addition,
                    }
                ],
            })

        task_report: dict[str, Any] = {
            "report_type": "clean_empty_assistant_report",
            "task_id": task_id,
            "task_path": str(task_path),
            "create": generated_at_text(),
            "summary": {
                "total_trajectory_count": total_scanned,
                "hit_trajectory_count": hit_count,
                "total_issues": hit_count,
                "case_A_count": sum(1 for r in t_results if _classify_case(r) == "A"),
                "case_B_count": sum(1 for r in t_results if _classify_case(r) == "B"),
                "case_C_count": sum(1 for r in t_results if _classify_case(r) == "C"),
            },
            "sessions": sessions,
        }
        report_file.write_text(
            json.dumps(task_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[task-report] {report_file}")


def _summarize_messages(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return structured message summaries for JSON reports."""
    result: list[dict[str, Any]] = []
    for m in msgs:
        if not isinstance(m, dict):
            result.append({"_raw": repr(m)})
            continue
        entry: dict[str, Any] = {"role": m.get("role", "?")}
        content = m.get("content") or ""
        entry["content"] = content.strip() if isinstance(content, str) else str(content)
        rc = m.get("reasoning_content")
        entry["reasoning_content"] = rc.strip() if isinstance(rc, str) and rc.strip() else ""
        tc = m.get("tool_calls")
        if tc:
            calls: list[dict[str, Any]] = []
            for call in tc:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function", {})
                if not isinstance(fn, dict):
                    continue
                c: dict[str, Any] = {}
                if call.get("id"):
                    c["id"] = call["id"]
                if fn.get("name"):
                    c["name"] = fn["name"]
                if fn.get("arguments"):
                    c["arguments"] = fn["arguments"]
                if c:
                    calls.append(c)
            if calls:
                entry["tool_calls"] = calls
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        result.append(entry)
    return result


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def has_text(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return True
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return True
        return False
    return False


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


def clean_messages(msgs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """清理消息数组：删除/合并双空 assistant 节点。

    返回 (cleaned_msgs, info) 其中 info 包含计数和删除的消息索引。
    """
    stats: dict[str, int] = {"deleted_empty": 0, "deleted_tool": 0, "merged_tc": 0, "merged_user": 0}
    deleted_indices: list[int] = []
    deleted_user_indices: list[int] = []
    result: list[dict[str, Any]] = []
    skip_until = -1

    for i, m in enumerate(msgs):
        if i < skip_until:
            continue
        if not isinstance(m, dict):
            result.append(m)
            continue

        # 非 assistant 消息，保留
        if m.get("role") != "assistant":
            result.append(m)
            continue

        # assistant 有文本内容，保留
        if has_text(m.get("content")) or has_text(m.get("reasoning_content")):
            result.append(m)
            continue

        # === 到达这里：空 assistant ===

        # 子情况：无 tool_calls
        if not m.get("tool_calls"):
            # 【末节点豁免】如果这个空 assistant 是消息数组的最后一个元素，
            # 不删——留给后续 fix_missing_summary_nodes 补收尾总结。
            if i == len(msgs) - 1:
                result.append(m)
                continue
            # 非末节点，正常删除
            stats["deleted_empty"] += 1
            deleted_indices.append(i)
            continue

        # === 有空 assistant + tool_calls ===
        next_msg = msgs[i + 1] if i + 1 < len(msgs) else None
        if _is_compact_event(m, next_msg):
            result.append(m)
            continue

        # 找到它 call 出来的连续 tool 结果
        target_ids = {t.get("id") for t in m.get("tool_calls", []) if isinstance(t, dict) and t.get("id")}
        j = i + 1
        while (
            j < len(msgs)
            and isinstance(msgs[j], dict)
            and msgs[j].get("role") == "tool"
            and msgs[j].get("tool_call_id") in target_ids
        ):
            j += 1

        tool_indices = list(range(i + 1, j))
        prev = result[-1] if result else None
        after = msgs[j] if j < len(msgs) and isinstance(msgs[j], dict) else None
        prev_role = prev.get("role") if isinstance(prev, dict) else None
        after_role = after.get("role") if isinstance(after, dict) else None

        # 情况 B：前一个是 assistant → 合并 tool_calls
        if prev_role == "assistant":
            assert prev is not None
            prev_tc = list(prev.get("tool_calls") or [])
            empty_tc = list(m.get("tool_calls") or [])
            prev["tool_calls"] = prev_tc + empty_tc
            stats["merged_tc"] += 1
            stats["deleted_empty"] += 1
            deleted_indices.append(i)
            skip_until = i + 1  # 只跳过 empty assistant，保留 tool 结果（它们现在引用 prev）
            continue

        # 情况 C：前一个是 user，后一个是 user → 删前面的 user，保留后面的
        if prev_role == "user" and after_role == "user":
            result.pop()  # 删除前面的 user
            stats["merged_user"] += 1
            stats["deleted_empty"] += 1
            stats["deleted_tool"] += len(tool_indices)
            deleted_user_indices.append(i - 1)  # 记录被删 user 的原始索引
            deleted_indices.append(i)
            deleted_indices.extend(tool_indices)
            skip_until = j  # 跳过 empty + tools，后面的 user 保留
            continue

        # 情况 A（默认）：删 empty + 其 tool 结果
        stats["deleted_empty"] += 1
        stats["deleted_tool"] += len(tool_indices)
        deleted_indices.append(i)
        deleted_indices.extend(tool_indices)
        skip_until = j
        continue

    return result, {"counts": stats, "deleted_indices": deleted_indices, "deleted_user_indices": deleted_user_indices}


def clean_one_file(source_path: Path) -> tuple[CleanResult, dict[str, Any] | None]:
    """处理单个 session JSON 文件。返回 (result, cleaned_data_or_None)。"""
    result = CleanResult(
        project_dir=source_path.parent.parent.name,
        session_path=str(source_path),
    )

    if not source_path.exists():
        result.valid_json = False
        result.error = "file does not exist"
        return result, None

    try:
        data = json.loads(source_path.read_text(encoding="utf-8-sig"))
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

    result.total_before = len(msgs)
    result.before_content = _summarize_messages(msgs)
    cleaned, clean_info = clean_messages(msgs)
    result.deleted_empty = clean_info["counts"]["deleted_empty"]
    result.deleted_tool = clean_info["counts"]["deleted_tool"]
    result.merged_tc = clean_info["counts"]["merged_tc"]
    result.merged_user = clean_info["counts"]["merged_user"]
    result.total_after = len(cleaned)
    result.deleted_msg_indices = clean_info["deleted_indices"]
    result.deleted_user_indices = clean_info["deleted_user_indices"]
    result.after_content = _summarize_messages(cleaned)

    data["messages"] = cleaned
    return result, data


def clean_empty_assistant(
    find_path: Path | str,
    session_json_path: Path | str,
    report_path: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"scan path does not exist or is not a directory: {root_dir}")

    session_relative_path = Path(session_json_path)
    # report_path 可选：编排器顺序模式(build_cmd)不透传 report_flag，此时回退到
    # <root>/report/clean_empty_assistant_report.json，避免 -o 必填在顺序模式下报错。
    if report_path is None:
        report_path_obj = root_dir / "report" / REPORT_NAME
    else:
        report_path_obj = Path(report_path).expanduser().resolve()
    # 如果是目录或没有 .json 后缀，追加默认报告文件名
    if report_path_obj.is_dir() or report_path_obj.suffix.lower() != ".json":
        report_path_obj = report_path_obj / REPORT_NAME
    results: list[CleanResult] = []
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

            print(f"cleaning: {relative_name} ... ", end="", flush=True)
            clean_result, cleaned_data = clean_one_file(session_path)
            results.append(clean_result)

            if cleaned_data is not None:
                if output_dir is not None:
                    rel_path = session_path.relative_to(project_dir)
                    rel_stripped = (
                        rel_path.relative_to(session_relative_path)
                        if str(rel_path).startswith(str(session_relative_path))
                        else rel_path
                    )
                    out_path = Path(output_dir).expanduser().resolve() / rel_stripped
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                else:
                    out_path = session_path
                out_path.write_text(
                    json.dumps(cleaned_data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

            if clean_result.error:
                print(f"ERROR: {clean_result.error}")
            elif clean_result.deleted_empty or clean_result.merged_tc or clean_result.merged_user:
                print(
                    f"del_empty={clean_result.deleted_empty} "
                    f"del_tool={clean_result.deleted_tool} "
                    f"merge_tc={clean_result.merged_tc} "
                    f"merge_user={clean_result.merged_user} "
                    f"({clean_result.total_before}→{clean_result.total_after} msgs)"
                )
            else:
                print("no change")

    # 汇总报告
    total_files = len(results)
    total_empty = sum(r.deleted_empty for r in results)
    total_tool = sum(r.deleted_tool for r in results)
    total_merge_tc = sum(r.merged_tc for r in results)
    total_merge_user = sum(r.merged_user for r in results)

    tasks = []
    for r in results:
        if r.deleted_empty or r.merged_tc or r.merged_user or r.error:
            tasks.append(
                {
                    "产物id": r.project_dir,
                    "文件id": r.session_path,
                    "error": r.error,
                    "deleted_empty": r.deleted_empty,
                    "deleted_tool": r.deleted_tool,
                    "merged_tc": r.merged_tc,
                    "merged_user": r.merged_user,
                    "total_before": r.total_before,
                    "total_after": r.total_after,
                    "before_content": r.before_content,
                    "after_content": r.after_content,
                }
            )

    report = {
        "report_type": REPORT_TYPE,
        "source_root": str(root_dir),
        "report_path": str(report_path_obj),
        "generated_at": generated_at_text(),
        "summary": {
            "files": total_files,
            "files_modified": len(tasks),
            "errors": sum(1 for r in results if r.error),
            "total_empty_assistant_deleted": total_empty,
            "total_tool_messages_deleted": total_tool,
            "total_messages_removed": total_empty + total_tool,
            "total_assistant_merged_tc": total_merge_tc,
            "total_user_merged": total_merge_user,
        },
        "tasks": tasks,
    }

    report_path_obj.parent.mkdir(parents=True, exist_ok=True)
    report_path_obj.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    write_task_reports(root_dir, results)

    print(f"\nCleaning complete.")
    print(f"Files scanned: {total_files}")
    print(f"Files modified: {len(tasks)}")
    print(f"Empty assistants deleted: {total_empty}")
    print(f"Tool messages deleted: {total_tool}")
    print(f"Tool calls merged (case B): {total_merge_tc}")
    print(f"User messages merged (case C): {total_merge_user}")
    print(f"Report: {report_path_obj}")
    return report_path_obj


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean empty assistant nodes (content + reasoning_content both empty) from session JSON files."
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
        help="Optional output report path (file or directory). If omitted, defaults to <root>/report/clean_empty_assistant_report.json (needed so the pipeline's sequential mode, which does not pass -o, still works).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional output directory for cleaned session files. If omitted, files are modified in place.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        clean_empty_assistant(args.find_path, args.session_json_path, args.report_path, args.output_dir)
    except Exception as exc:  # noqa: BLE001 - CLI should print actionable failures.
        print(f"clean_empty_assistant failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

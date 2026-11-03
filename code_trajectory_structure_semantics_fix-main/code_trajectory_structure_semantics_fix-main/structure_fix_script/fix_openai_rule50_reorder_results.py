#!/usr/bin/env python3
r"""Reorder duplicated tool result messages for OpenAI rule 50.

The rule 50 failure in ``check_openaI_chat_completions.py`` means that the
same ``tool_call.id`` is declared by more than one assistant message, but a
later declaration has no matching tool result before the next same-id
declaration. In the affected converted traces the results are often present,
but several same-id tool messages were grouped after the first declaration.

This fixer redistributes those existing tool messages:

* keep the first result after the first same-id declaration;
* move surplus same-id results to later same-id assistant declarations;
* insert the moved result into the target assistant's adjacent tool block,
  respecting the target assistant's ``tool_calls`` order.
* by default, give the later duplicate declaration and its moved result a
  unique id so rule 27 does not treat the later result as belonging to the
  earlier declaration.

No tool result is synthesized and no assistant tool call is deleted.

Usage:
  python fix_openai_rule50_reorder_results.py --dry-run
  python fix_openai_rule50_reorder_results.py -r D:\path\pipeline_report_summary.md --dry-run
  python fix_openai_rule50_reorder_results.py -r D:\path\pipeline_report_summary.md --apply
  python fix_openai_rule50_reorder_results.py --path D:\path\sessions\1-abc.json --dry-run
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SUMMARY_PATH = Path(r"D:\code-data\20260522-198-trajectory\pipeline_report_summary.md")
DEFAULT_REPORT_NAME = "fix_openai_rule50_reorder_results_report.json"
RULE50_MARKERS = ("50", "tool_call.id")
SKIP_DIR_NAMES = {
    ".git",
    ".idea",
    "__pycache__",
    "node_modules",
    "repo",
    "report",
    "tool-results",
    "original_sessions",
    "convert-sessions",
    "converted_sessions",
}

ABS_JSON_PATH_RE = re.compile(r"((?:[A-Za-z]:\\|/)[^|;\r\n]*?\.json)")
TASK_ID_RE = re.compile(r"TASK-\d{8}-[A-Z0-9]+")


@dataclass(frozen=True)
class IssueTarget:
    path: Path
    agent: str | None = None
    tool_call_id: str | None = None
    line_number: int | None = None
    source: str = ""
    reason: str = ""


@dataclass
class PlannedMove:
    path: Path
    agent: str
    tool_call_id: str
    source_index: int
    target_call_index: int
    insert_before_index: int
    source_line: int | None
    target_line: int | None
    new_tool_call_id: str | None = None
    content_preview: str = ""
    status: str = "planned"


@dataclass
class FileFixResult:
    path: Path
    changed: bool = False
    apply: bool = False
    targeted_keys: list[str] = field(default_factory=list)
    moves: list[PlannedMove] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip().replace("\\|", "|").replace("<br>", "\n") for cell in stripped[1:-1].split("|")]


def looks_like_rule50(text: str) -> bool:
    return all(marker in text for marker in RULE50_MARKERS)


def extract_agent_and_tool_id(text: str) -> tuple[str | None, str | None]:
    quoted = re.findall(r"`([^`]+)`", text)
    if len(quoted) >= 2:
        return quoted[0], quoted[1]
    if quoted:
        return None, quoted[0]
    return None, None


def parse_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_summary_report(summary_path: Path) -> list[IssueTarget]:
    if not summary_path.is_file():
        return []

    targets: list[IssueTarget] = []
    for line in summary_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not looks_like_rule50(line):
            continue

        cells = split_markdown_row(line)
        if len(cells) >= 4:
            reason = cells[1]
            line_number = parse_int(cells[3])
            path_matches = ABS_JSON_PATH_RE.findall(cells[2])
        else:
            reason = line
            line_match = re.search(r"(?:line|琛屽彿|行号)\s*[=:]\s*(\d+)", line, re.I)
            line_number = int(line_match.group(1)) if line_match else None
            path_matches = ABS_JSON_PATH_RE.findall(line)

        agent, tool_call_id = extract_agent_and_tool_id(reason)
        for path_text in path_matches:
            targets.append(
                IssueTarget(
                    path=Path(path_text),
                    agent=agent,
                    tool_call_id=tool_call_id,
                    line_number=line_number,
                    source=str(summary_path),
                    reason=reason,
                )
            )
    return targets


def string_values(obj: dict[str, Any]) -> list[str]:
    return [value for value in obj.values() if isinstance(value, str)]


def first_rule50_reason(obj: dict[str, Any]) -> str | None:
    for value in string_values(obj):
        if looks_like_rule50(value):
            return value
    return None


def first_session_path_value(obj: dict[str, Any]) -> str | None:
    for value in string_values(obj):
        normalized = value.replace("\\", "/")
        normalized_with_slash = f"/{normalized}"
        if normalized.endswith(".json") and (
            "/sessions/" in normalized_with_slash or "/data_success/" in normalized_with_slash
        ):
            return value
    return None


def first_line_number(obj: dict[str, Any]) -> int | None:
    for key, value in obj.items():
        if "line" in key.lower() or "琛" in key or "行" in key:
            parsed = parse_int(value)
            if parsed is not None:
                return parsed
    for value in obj.values():
        parsed = parse_int(value)
        if parsed is not None:
            return parsed
    return None


def parse_check_pipeline_json(path: Path) -> list[IssueTarget]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []

    source_root_text = payload.get("source_root")
    source_root = Path(source_root_text) if isinstance(source_root_text, str) and source_root_text else path.parent
    targets: list[IssueTarget] = []

    results = payload.get("results")
    if not isinstance(results, list):
        return targets

    for result in results:
        if not isinstance(result, dict) or result.get("name") != "openai_chat_completions":
            continue
        tasks = result.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            reason = first_rule50_reason(task)
            if not reason:
                continue
            session_value = first_session_path_value(task)
            if not session_value:
                continue
            session_path = Path(session_value)
            if not session_path.is_absolute():
                session_path = source_root / session_value
            agent, tool_call_id = extract_agent_and_tool_id(reason)
            targets.append(
                IssueTarget(
                    path=session_path,
                    agent=agent,
                    tool_call_id=tool_call_id,
                    line_number=first_line_number(task),
                    source=str(path),
                    reason=reason,
                )
            )
    return targets


def parse_sibling_check_reports(summary_path: Path) -> list[IssueTarget]:
    if not summary_path.exists():
        return []
    root = summary_path.parent
    targets: list[IssueTarget] = []
    for report_path in sorted(root.glob("TASK-*/report/check_pipeline.json")):
        targets.extend(parse_check_pipeline_json(report_path))
    return targets


def path_task_id(path: Path) -> str | None:
    for part in path.parts:
        if TASK_ID_RE.fullmatch(part):
            return part
    match = TASK_ID_RE.search(str(path))
    return match.group(0) if match else None


def iter_session_json_files(root: Path) -> Iterable[Path]:
    if root.is_file() and root.suffix.lower() == ".json":
        yield root
        return
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.json")):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.name.startswith(".") or path.name.endswith(".meta.json"):
            continue
        yield path


def top_level_element_lines(text: str) -> list[int]:
    lines: list[int] = []
    array_depth = 0
    object_depth = 0
    line = 1
    in_string = False
    escape = False
    expecting_value = False

    for char in text:
        if array_depth == 1 and object_depth == 0 and expecting_value:
            if char in " \t\r\n":
                pass
            elif char == "]":
                expecting_value = False
            else:
                lines.append(line)
                expecting_value = False

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "[":
                array_depth += 1
                if array_depth == 1:
                    expecting_value = True
            elif char == "]":
                array_depth = max(0, array_depth - 1)
            elif char == "{":
                object_depth += 1
            elif char == "}":
                object_depth = max(0, object_depth - 1)
            elif char == "," and array_depth == 1 and object_depth == 0:
                expecting_value = True

        if char == "\n":
            line += 1

    return lines


def messages_element_lines(text: str) -> list[int]:
    match = re.search(r'"messages"\s*:', text)
    if not match:
        return []
    array_start = text.find("[", match.end())
    if array_start == -1:
        return []
    line_offset = text.count("\n", 0, array_start)
    return [line_offset + line for line in top_level_element_lines(text[array_start:])]


def message_agent(message: dict[str, Any]) -> str:
    agent = message.get("agent")
    if isinstance(agent, str) and agent.strip():
        return agent.strip()

    metadata = message.get("_metadata")
    if isinstance(metadata, dict):
        subagent_id = metadata.get("subagent_id")
        if isinstance(subagent_id, str) and subagent_id.strip():
            return subagent_id.strip()

    return "main"


def tool_call_ids(message: dict[str, Any]) -> list[str]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    ids: list[str] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_call_id = tool_call.get("id")
        if isinstance(tool_call_id, str) and tool_call_id:
            ids.append(tool_call_id)
    return ids


def tool_call_order(message: dict[str, Any], tool_call_id: str) -> int:
    ids = tool_call_ids(message)
    try:
        return ids.index(tool_call_id)
    except ValueError:
        return len(ids)


def content_preview(message: dict[str, Any], limit: int = 120) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return " ".join(content.split())[:limit]
    if content is None:
        return ""
    return type(content).__name__


def collect_call_and_result_positions(
    messages: list[Any],
) -> tuple[dict[tuple[str, str], list[int]], dict[tuple[str, str], list[int]]]:
    calls: dict[tuple[str, str], list[int]] = defaultdict(list)
    results: dict[tuple[str, str], list[int]] = defaultdict(list)

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        agent = message_agent(message)
        if message.get("role") == "assistant":
            for tool_call_id in tool_call_ids(message):
                calls[(agent, tool_call_id)].append(index)
        elif message.get("role") == "tool":
            tool_call_id = message.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                results[(agent, tool_call_id)].append(index)
    return calls, results


def collect_existing_tool_ids(messages: list[Any]) -> set[str]:
    ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        ids.update(tool_call_ids(message))
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            ids.add(tool_call_id)
    return ids


def make_unique_tool_call_id(old_id: str, occurrence_number: int, used_ids: set[str]) -> str:
    base = f"{old_id}__rule50_{occurrence_number}"
    candidate = base
    counter = 2
    while candidate in used_ids:
        candidate = f"{base}_{counter}"
        counter += 1
    used_ids.add(candidate)
    return candidate


def key_allowed(key: tuple[str, str], allowed_keys: set[tuple[str | None, str | None]]) -> bool:
    if not allowed_keys:
        return True
    agent, tool_call_id = key
    for allowed_agent, allowed_tool_call_id in allowed_keys:
        if allowed_agent is not None and allowed_agent != agent:
            continue
        if allowed_tool_call_id is not None and allowed_tool_call_id != tool_call_id:
            continue
        return True
    return False


def insertion_index_for_call(messages: list[Any], call_index: int, agent: str, tool_call_id: str) -> int:
    message = messages[call_index]
    if not isinstance(message, dict):
        return call_index + 1
    ids = tool_call_ids(message)
    target_order = tool_call_order(message, tool_call_id)
    cursor = call_index + 1

    while cursor < len(messages):
        current = messages[cursor]
        if not isinstance(current, dict):
            break
        if current.get("role") != "tool" or message_agent(current) != agent:
            break
        current_id = current.get("tool_call_id")
        if not isinstance(current_id, str) or current_id not in ids:
            break
        if tool_call_order(message, current_id) > target_order:
            break
        cursor += 1

    return cursor


def interval_index_for_result(call_indices: list[int], result_index: int) -> int | None:
    current_interval: int | None = None
    for index, call_index in enumerate(call_indices):
        if call_index < result_index:
            current_interval = index
        else:
            break
    return current_interval


def pop_surplus_for_target(
    target_interval: int,
    surplus_by_interval: dict[int, list[int]],
) -> int | None:
    previous_intervals = [index for index in sorted(surplus_by_interval) if index < target_interval]
    future_intervals = [index for index in sorted(surplus_by_interval) if index > target_interval]
    for interval in [*previous_intervals, *future_intervals]:
        surplus = surplus_by_interval.get(interval, [])
        if surplus:
            source_index = surplus.pop(0)
            if not surplus:
                surplus_by_interval.pop(interval, None)
            return source_index
    return None


def plan_moves_for_file(
    path: Path,
    messages: list[Any],
    message_lines: list[int],
    allowed_keys: set[tuple[str | None, str | None]],
    *,
    rename_duplicates: bool,
) -> tuple[list[PlannedMove], list[dict[str, Any]]]:
    calls_by_key, results_by_key = collect_call_and_result_positions(messages)
    used_ids = collect_existing_tool_ids(messages)
    moves: list[PlannedMove] = []
    skipped: list[dict[str, Any]] = []

    for key in sorted(calls_by_key):
        if not key_allowed(key, allowed_keys):
            continue
        agent, tool_call_id = key
        call_indices = calls_by_key[key]
        result_indices = results_by_key.get(key, [])
        if len(call_indices) <= 1:
            continue
        if len(result_indices) <= 1:
            skipped.append(
                {
                    "agent": agent,
                    "tool_call_id": tool_call_id,
                    "reason": "duplicate calls exist but there are no surplus results to reorder",
                    "calls": len(call_indices),
                    "results": len(result_indices),
                }
            )
            continue

        interval_results: dict[int, list[int]] = defaultdict(list)
        for result_index in result_indices:
            interval = interval_index_for_result(call_indices, result_index)
            if interval is not None:
                interval_results[interval].append(result_index)

        surplus_by_interval = {
            interval: indices[1:]
            for interval, indices in interval_results.items()
            if len(indices) > 1
        }
        missing_intervals = [
            interval
            for interval in range(len(call_indices))
            if not interval_results.get(interval)
        ]

        if not missing_intervals:
            continue

        for target_interval in missing_intervals:
            source_index = pop_surplus_for_target(target_interval, surplus_by_interval)
            target_call_index = call_indices[target_interval]
            if source_index is None:
                skipped.append(
                    {
                        "agent": agent,
                        "tool_call_id": tool_call_id,
                        "target_call_index": target_call_index,
                        "target_line": message_lines[target_call_index] if target_call_index < len(message_lines) else None,
                        "reason": "no surplus result is available for this duplicate call",
                    }
                )
                continue

            source_message = messages[source_index]
            if not isinstance(source_message, dict):
                continue
            moves.append(
                PlannedMove(
                    path=path,
                    agent=agent,
                    tool_call_id=tool_call_id,
                    source_index=source_index,
                    target_call_index=target_call_index,
                    insert_before_index=insertion_index_for_call(messages, target_call_index, agent, tool_call_id),
                    source_line=message_lines[source_index] if source_index < len(message_lines) else None,
                    target_line=message_lines[target_call_index] if target_call_index < len(message_lines) else None,
                    new_tool_call_id=(
                        make_unique_tool_call_id(tool_call_id, target_interval + 1, used_ids)
                        if rename_duplicates
                        else None
                    ),
                    content_preview=content_preview(source_message),
                )
            )

    return moves, skipped


def update_metadata_agent_key(message: dict[str, Any], old_id: str, new_id: str) -> None:
    metadata = message.get("_metadata")
    if not isinstance(metadata, dict):
        return
    agent_map = metadata.get("agent")
    if not isinstance(agent_map, dict) or old_id not in agent_map:
        return
    agent_map[new_id] = agent_map.pop(old_id)


def apply_move_id_rewrites(messages: list[Any], moves: list[PlannedMove]) -> None:
    for move in moves:
        if not move.new_tool_call_id:
            continue
        target_message = messages[move.target_call_index]
        source_message = messages[move.source_index]
        if isinstance(target_message, dict):
            for tool_call in target_message.get("tool_calls", []):
                if isinstance(tool_call, dict) and tool_call.get("id") == move.tool_call_id:
                    tool_call["id"] = move.new_tool_call_id
                    break
            update_metadata_agent_key(target_message, move.tool_call_id, move.new_tool_call_id)
        if isinstance(source_message, dict) and source_message.get("tool_call_id") == move.tool_call_id:
            source_message["tool_call_id"] = move.new_tool_call_id
            update_metadata_agent_key(source_message, move.tool_call_id, move.new_tool_call_id)


def apply_moves(messages: list[Any], moves: list[PlannedMove]) -> list[Any]:
    messages = copy.deepcopy(messages)
    apply_move_id_rewrites(messages, moves)

    remove_indices = {move.source_index for move in moves}
    before: dict[int, list[PlannedMove]] = defaultdict(list)
    for move in moves:
        before[move.insert_before_index].append(move)

    for planned in before.values():
        planned.sort(
            key=lambda move: (
                move.target_call_index,
                tool_call_order(messages[move.target_call_index], move.tool_call_id)
                if isinstance(messages[move.target_call_index], dict)
                else 0,
                move.source_index,
            )
        )

    rebuilt: list[Any] = []
    for index, message in enumerate(messages):
        for move in before.get(index, []):
            rebuilt.append(copy.deepcopy(messages[move.source_index]))
        if index not in remove_indices:
            rebuilt.append(message)

    for move in before.get(len(messages), []):
        rebuilt.append(copy.deepcopy(messages[move.source_index]))

    return rebuilt


def load_json_object(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8-sig")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON is not an object")
    return payload, text


def write_json_object(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def fix_file(
    path: Path,
    targets: list[IssueTarget],
    *,
    apply: bool,
    rename_duplicates: bool,
) -> FileFixResult:
    result = FileFixResult(path=path, apply=apply)
    allowed_keys = {
        (target.agent, target.tool_call_id)
        for target in targets
        if target.agent is not None or target.tool_call_id is not None
    }
    result.targeted_keys = [
        f"{agent or '*'}:{tool_call_id or '*'}"
        for agent, tool_call_id in sorted(allowed_keys, key=lambda item: ((item[0] or ""), (item[1] or "")))
    ]

    try:
        payload, text = load_json_object(path)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            result.error = "top-level messages is not a list"
            return result
        message_lines = messages_element_lines(text)
        moves, skipped = plan_moves_for_file(
            path,
            messages,
            message_lines,
            allowed_keys,
            rename_duplicates=rename_duplicates,
        )
        result.moves = moves
        result.skipped = skipped
        if not moves:
            return result

        result.changed = True
        if apply:
            payload["messages"] = apply_moves(messages, moves)
            write_json_object(path, payload)
            for move in result.moves:
                move.status = "moved"
    except Exception as exc:  # noqa: BLE001 - standalone fixer reports per-file failures
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def dedupe_targets(targets: Iterable[IssueTarget]) -> list[IssueTarget]:
    deduped: dict[tuple[str, str | None, str | None, int | None], IssueTarget] = {}
    for target in targets:
        path = target.path.expanduser()
        key = (str(path).lower(), target.agent, target.tool_call_id, target.line_number)
        deduped.setdefault(key, IssueTarget(path=path, agent=target.agent, tool_call_id=target.tool_call_id, line_number=target.line_number, source=target.source, reason=target.reason))
    return sorted(deduped.values(), key=lambda item: (str(item.path).lower(), item.line_number or 0, item.agent or "", item.tool_call_id or ""))


def collect_targets(args: argparse.Namespace) -> list[IssueTarget]:
    targets: list[IssueTarget] = []

    if args.summary:
        summary_path = args.summary.expanduser()
        targets.extend(parse_summary_report(summary_path))
        targets.extend(parse_sibling_check_reports(summary_path))

    for check_report in args.check_report or []:
        targets.extend(parse_check_pipeline_json(check_report.expanduser()))

    for direct_path in args.path or []:
        path = direct_path.expanduser()
        targets.append(IssueTarget(path=path, source="--path"))

    for task_dir in args.task_dir or []:
        for session_path in iter_session_json_files(task_dir.expanduser()):
            targets.append(IssueTarget(path=session_path, source="--task-dir"))

    if args.task:
        wanted = set(args.task)
        targets = [target for target in targets if path_task_id(target.path) in wanted]

    return dedupe_targets(targets)


def group_targets_by_path(targets: list[IssueTarget]) -> dict[Path, list[IssueTarget]]:
    grouped: dict[Path, list[IssueTarget]] = defaultdict(list)
    for target in targets:
        grouped[target.path].append(target)
    return dict(sorted(grouped.items(), key=lambda item: str(item[0]).lower()))


def resolve_output_path(output: Path | None) -> Path:
    if output is None:
        return Path.cwd() / DEFAULT_REPORT_NAME
    output = output.expanduser()
    if output.exists() and output.is_dir():
        return output / DEFAULT_REPORT_NAME
    if output.suffix.lower() != ".json":
        return output / DEFAULT_REPORT_NAME
    return output


def move_to_dict(move: PlannedMove) -> dict[str, Any]:
    return {
        "path": str(move.path),
        "agent": move.agent,
        "tool_call_id": move.tool_call_id,
        "source_index": move.source_index,
        "target_call_index": move.target_call_index,
        "insert_before_index": move.insert_before_index,
        "source_line": move.source_line,
        "target_line": move.target_line,
        "new_tool_call_id": move.new_tool_call_id or "",
        "content_preview": move.content_preview,
        "status": move.status,
    }


def result_to_dict(result: FileFixResult) -> dict[str, Any]:
    return {
        "path": str(result.path),
        "changed": result.changed,
        "apply": result.apply,
        "targeted_keys": result.targeted_keys,
        "moves": [move_to_dict(move) for move in result.moves],
        "skipped": result.skipped,
        "error": result.error,
    }


def write_report(output_path: Path, *, args: argparse.Namespace, targets: list[IssueTarget], results: list[FileFixResult]) -> None:
    total_moves = sum(len(result.moves) for result in results)
    changed_files = sum(1 for result in results if result.changed)
    payload = {
        "report_type": "fix_openai_rule50_reorder_results_report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_path": str(args.summary) if args.summary else "",
        "apply": bool(args.apply),
        "rename_duplicates": not bool(args.keep_duplicate_ids),
        "summary": {
            "targets": len(targets),
            "files": len(results),
            "changed_files": changed_files,
            "planned_or_applied_moves": total_moves,
            "errors": sum(1 for result in results if result.error),
        },
        "results": [result_to_dict(result) for result in results],
        "tasks": [move_to_dict(move) for result in results for move in result.moves],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-r",
        "--summary",
        type=Path,
        default=None,
        help="pipeline_report_summary.md path. If no explicit target is given, the 20260522-198-trajectory report is used.",
    )
    parser.add_argument(
        "--check-report",
        type=Path,
        action="append",
        help="Optional TASK/report/check_pipeline.json to parse directly. Can be repeated.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        action="append",
        help="Session JSON file to scan and repair. Can be repeated.",
    )
    parser.add_argument(
        "--task-dir",
        type=Path,
        action="append",
        help="TASK directory or sessions directory to scan. Can be repeated.",
    )
    parser.add_argument(
        "--task",
        action="append",
        help="Only process a specific TASK id. Can be repeated.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only the first N target files. Default 0 means all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan and report changes without writing session files. This is the default unless --apply is set.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write reordered messages back to the session JSON files.",
    )
    parser.add_argument(
        "--keep-duplicate-ids",
        action="store_true",
        help="Only move tool results; do not assign unique ids to later duplicate declarations and moved results.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=f"JSON report path or directory. Default: current directory/{DEFAULT_REPORT_NAME}.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run cannot be used together")
    if not args.summary and not args.check_report and not args.path and not args.task_dir:
        args.summary = DEFAULT_SUMMARY_PATH

    targets = collect_targets(args)
    if not targets:
        print("no targets found; nothing to fix", file=sys.stderr)
        output_path = resolve_output_path(args.output)
        write_report(output_path, args=args, targets=[], results=[])
        print(f"REPORT {output_path}")
        print("DRY_RUN files=0 changed_files=0 moves=0 errors=0")
        return 0

    grouped = group_targets_by_path(targets)
    items = list(grouped.items())
    if args.limit and args.limit > 0:
        items = items[: args.limit]

    results: list[FileFixResult] = []
    for path, path_targets in items:
        results.append(
            fix_file(
                path,
                path_targets,
                apply=bool(args.apply),
                rename_duplicates=not bool(args.keep_duplicate_ids),
            )
        )

    output_path = resolve_output_path(args.output)
    write_report(output_path, args=args, targets=targets, results=results)

    changed_files = sum(1 for result in results if result.changed)
    total_moves = sum(len(result.moves) for result in results)
    error_count = sum(1 for result in results if result.error)
    action = "APPLIED" if args.apply else "DRY_RUN"
    print(f"{action} files={len(results)} changed_files={changed_files} moves={total_moves} errors={error_count}")
    print(f"REPORT {output_path}")
    for result in results:
        if result.error:
            print(f"ERROR {result.path}: {result.error}")
            continue
        if result.moves:
            print(f"{'UPDATED' if args.apply else 'WOULD_UPDATE'} {result.path} moves={len(result.moves)}")
            for move in result.moves:
                print(
                    "  MOVE "
                    f"agent={move.agent} id={move.tool_call_id} "
                    f"new_id={move.new_tool_call_id or move.tool_call_id} "
                    f"source_line={move.source_line} target_line={move.target_line} "
                    f"insert_before_index={move.insert_before_index}"
                )
        else:
            print(f"NO_CHANGE {result.path}")

    return 1 if error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())

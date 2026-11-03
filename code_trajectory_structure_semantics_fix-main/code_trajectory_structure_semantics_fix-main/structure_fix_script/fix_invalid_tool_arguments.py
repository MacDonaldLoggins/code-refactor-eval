#!/usr/bin/env python3
"""Repair tool_call function.arguments strings that are not valid JSON.

The safest repair is to restore the same tool_call arguments from the
corresponding convert-sessions source file. A conservative local escape repair
is used only when no valid source arguments can be found.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPORT_NAME = "fix_invalid_tool_arguments_report.json"
VALID_ESCAPES = {'"', "\\", "/", "b", "f", "n", "r", "t"}
SESSION_NAME_RE = re.compile(r"^(\d+)-(?:develop|test|bugfix)-(.+)$")
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}


@dataclass
class ArgumentIssue:
    message_index: int
    tool_index: int
    tool_call_id: str
    tool_name: str
    error: str
    method: str = "skipped"
    changed: bool = False
    reason: str = ""


@dataclass
class FileResult:
    path: str
    source_path: str = ""
    invalid_arguments: int = 0
    repaired: int = 0
    changed: bool = False
    error: str = ""
    issues: list[ArgumentIssue] | None = None


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dump_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract_messages(payload: Any) -> list[Any] | None:
    if isinstance(payload, dict):
        messages = payload.get("messages")
        return messages if isinstance(messages, list) else None
    return payload if isinstance(payload, list) else None


def validate_arguments(arguments: str) -> tuple[bool, str]:
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return False, str(exc)
    if not isinstance(parsed, dict):
        return False, "arguments JSON is not an object"
    return True, ""


def is_session_json(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name.startswith(".") or path.name.endswith(".meta.json"):
        return False
    if path.suffix.lower() != ".json":
        return False
    if any(part in SKIPPED_SESSION_DIR_NAMES for part in path.parts):
        return False
    return True


def iter_project_dirs(root: Path, session_path: Path) -> list[Path]:
    if (root / session_path).exists():
        return [root]
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and path.name.startswith("TASK-")),
        key=lambda path: path.name,
    )


def iter_session_files(root: Path, session_path: Path) -> list[Path]:
    files: list[Path] = []
    for project_dir in iter_project_dirs(root, session_path):
        target = project_dir / session_path
        if target.is_file() and is_session_json(target):
            files.append(target)
        elif target.is_dir():
            files.extend(path for path in target.rglob("*.json") if is_session_json(path))
    return sorted(files, key=lambda path: str(path).casefold())


def find_path_parts(parts: tuple[str, ...], needle: tuple[str, ...]) -> int:
    if not needle:
        return -1
    for index in range(0, len(parts) - len(needle) + 1):
        if parts[index : index + len(needle)] == needle:
            return index
    return -1


def convert_session_name(session_name: str) -> str:
    suffix = ".json" if session_name.lower().endswith(".json") else ""
    stem = session_name[:-5] if suffix else session_name
    match = SESSION_NAME_RE.match(stem)
    if not match:
        return session_name
    return f"{match.group(1)}-{match.group(2)}{suffix}"


def source_tail_variants(session_tail: tuple[str, ...]) -> list[tuple[str, ...]]:
    variants = [session_tail]
    if session_tail and session_tail[0] == "subagents__agent":
        variants.append(("subagents",) + session_tail[1:])
    if session_tail and session_tail[0] == "subagents":
        variants.append(("subagents__agent",) + session_tail[1:])
    unique: list[tuple[str, ...]] = []
    for variant in variants:
        if variant not in unique:
            unique.append(variant)
    return unique


def candidate_source_roots(source_root: Path, task_name: str) -> list[Path]:
    roots = []
    if task_name:
        roots.append(source_root / task_name)
    roots.append(source_root)
    unique: list[Path] = []
    for root in roots:
        key = str(root).casefold()
        if not any(str(path).casefold() == key for path in unique):
            unique.append(root)
    return unique


def resolve_source_file(
    delivery_root: Path,
    delivery_file: Path,
    source_root: Path,
    session_path: Path,
    source_state: str,
) -> Path | None:
    try:
        rel_parts = delivery_file.relative_to(delivery_root).parts
    except ValueError:
        rel_parts = delivery_file.parts

    session_parts = session_path.parts
    session_index = find_path_parts(rel_parts, session_parts)
    if session_index < 0:
        return None

    task_name = rel_parts[session_index - 1] if session_index > 0 else delivery_root.name
    after_session_root = rel_parts[session_index + len(session_parts) :]
    if not after_session_root:
        return None

    if "subagents" in after_session_root:
        subagent_index = after_session_root.index("subagents")
        if subagent_index == 0:
            return None
        session_name = after_session_root[0]
        session_tail = ("subagents", after_session_root[-1])
    elif len(after_session_root) >= 2 and after_session_root[-2] == Path(after_session_root[-1]).stem:
        session_name = after_session_root[-1]
        session_tail = ()
    else:
        session_name = after_session_root[0]
        session_tail = tuple(after_session_root[1:])
    convert_name = convert_session_name(session_name)
    roots = candidate_source_roots(source_root, task_name)
    candidates: list[Path] = []

    def add(path: Path) -> None:
        try:
            if path.resolve() == delivery_file.resolve():
                return
        except OSError:
            pass
        key = str(path).casefold()
        if not any(str(existing).casefold() == key for existing in candidates):
            candidates.append(path)

    if source_state in {"auto", "sessions"}:
        add(source_root.joinpath(*rel_parts))
        if task_name:
            add(source_root.joinpath(task_name, *rel_parts[session_index:]))

    if source_state in {"auto", "convert"}:
        for root in roots:
            for tail in source_tail_variants(session_tail):
                add(root.joinpath("convert-sessions", convert_name, *tail))

    if source_state in {"auto", "sessions"}:
        for root in roots:
            for tail in source_tail_variants(session_tail):
                add(root.joinpath("sessions", session_name, *tail))

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    if source_state == "convert":
        fallback_roots = [root / "convert-sessions" for root in roots]
    elif source_state == "sessions":
        fallback_roots = [root / "sessions" for root in roots]
    else:
        fallback_roots = roots
    for fallback_root in fallback_roots:
        if not fallback_root.is_dir():
            continue
        for candidate in fallback_root.rglob(delivery_file.name):
            try:
                if candidate.resolve() == delivery_file.resolve():
                    continue
            except OSError:
                pass
            return candidate
    return None


def iter_argument_issues(messages: list[Any]) -> list[tuple[ArgumentIssue, dict[str, Any]]]:
    issues: list[tuple[ArgumentIssue, dict[str, Any]]] = []
    for message_index, message in enumerate(messages, start=1):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_index, tool_call in enumerate(tool_calls, start=1):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                continue
            ok, error = validate_arguments(arguments)
            if ok:
                continue
            issues.append(
                (
                    ArgumentIssue(
                        message_index=message_index,
                        tool_index=tool_index,
                        tool_call_id=str(tool_call.get("id") or ""),
                        tool_name=str(function.get("name") or ""),
                        error=error,
                    ),
                    function,
                )
            )
    return issues


def source_argument_indexes(messages: list[Any]) -> tuple[dict[str, list[tuple[int, int, str]]], dict[tuple[int, int], str]]:
    by_id: dict[str, list[tuple[int, int, str]]] = {}
    by_position: dict[tuple[int, int], str] = {}
    for message_index, message in enumerate(messages, start=1):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_index, tool_call in enumerate(tool_calls, start=1):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                continue
            ok, _error = validate_arguments(arguments)
            if not ok:
                continue
            by_position[(message_index, tool_index)] = arguments
            tool_call_id = tool_call.get("id")
            if isinstance(tool_call_id, str) and tool_call_id:
                by_id.setdefault(tool_call_id, []).append((message_index, tool_index, arguments))
    return by_id, by_position


def choose_source_arguments(
    issue: ArgumentIssue,
    by_id: dict[str, list[tuple[int, int, str]]],
    by_position: dict[tuple[int, int], str],
) -> tuple[str, str] | None:
    if issue.tool_call_id and issue.tool_call_id in by_id:
        matches = by_id[issue.tool_call_id]
        if len(matches) == 1:
            return matches[0][2], "source:tool_call_id"
        for message_index, tool_index, arguments in matches:
            if message_index == issue.message_index and tool_index == issue.tool_index:
                return arguments, "source:tool_call_id+position"
    position_key = (issue.message_index, issue.tool_index)
    if position_key in by_position:
        return by_position[position_key], "source:position"
    return None


def repair_invalid_backslashes(arguments: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(arguments):
        char = arguments[index]
        if char != "\\":
            result.append(char)
            index += 1
            continue

        if index + 1 >= len(arguments):
            result.append("\\\\")
            index += 1
            continue

        nxt = arguments[index + 1]
        if nxt == "u":
            hex_digits = arguments[index + 2 : index + 6]
            if len(hex_digits) == 4 and all(ch in "0123456789abcdefABCDEF" for ch in hex_digits):
                result.append("\\u")
                index += 2
            else:
                result.append("\\\\")
                index += 1
            continue

        if nxt in VALID_ESCAPES:
            result.append("\\" + nxt)
            index += 2
            continue

        result.append("\\\\")
        index += 1
    return "".join(result)


def process_file(
    delivery_root: Path,
    path: Path,
    source_root: Path,
    session_path: Path,
    source_state: str,
    apply: bool,
) -> FileResult | None:
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return FileResult(path=str(path), error=f"outer JSON failed: {exc}", issues=[])

    messages = extract_messages(payload)
    if messages is None:
        return None

    raw_issues = iter_argument_issues(messages)
    if not raw_issues:
        return None

    result = FileResult(path=str(path), invalid_arguments=len(raw_issues), issues=[])
    source_messages: list[Any] | None = None
    source_path = resolve_source_file(delivery_root, path, source_root, session_path, source_state)
    if source_path:
        result.source_path = str(source_path)
        try:
            source_payload = load_json(source_path)
            source_messages = extract_messages(source_payload)
        except (OSError, json.JSONDecodeError):
            source_messages = None

    by_id: dict[str, list[tuple[int, int, str]]] = {}
    by_position: dict[tuple[int, int], str] = {}
    if source_messages is not None:
        by_id, by_position = source_argument_indexes(source_messages)

    changed = False
    for issue, function in raw_issues:
        replacement = choose_source_arguments(issue, by_id, by_position)
        if replacement is not None:
            arguments, method = replacement
            function["arguments"] = arguments
            issue.method = method
            issue.changed = True
            issue.reason = "restored valid arguments from source"
            changed = True
            result.repaired += 1
            result.issues.append(issue)
            continue

        current = function.get("arguments")
        if isinstance(current, str):
            repaired = repair_invalid_backslashes(current)
            ok, fallback_error = validate_arguments(repaired)
            if ok and repaired != current:
                function["arguments"] = repaired
                issue.method = "fallback:escape_invalid_backslashes"
                issue.changed = True
                issue.reason = "escaped invalid backslashes locally"
                changed = True
                result.repaired += 1
            else:
                issue.reason = f"no valid source arguments; fallback failed: {fallback_error}"
        else:
            issue.reason = "arguments is no longer a string"
        result.issues.append(issue)

    result.changed = changed
    if changed and apply:
        dump_json(path, payload)
    return result


def build_report(args: argparse.Namespace, results: list[FileResult]) -> dict[str, Any]:
    changed_files = sum(1 for result in results if result.changed)
    repaired = sum(result.repaired for result in results)
    invalid_arguments = sum(result.invalid_arguments for result in results)
    errors = sum(1 for result in results if result.error)
    return {
        "report_type": "fix_invalid_tool_arguments_report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": bool(args.apply),
        "root": str(args.root),
        "source": str(args.source) if args.source else "",
        "source_state": args.source_state,
        "session_path": str(args.session_path),
        "summary": {
            "files_with_findings": len(results),
            "changed_files": changed_files,
            "invalid_arguments": invalid_arguments,
            "repaired_arguments": repaired,
            "unrepaired_arguments": invalid_arguments - repaired,
            "errors": errors,
        },
        "results": [
            {
                **{key: value for key, value in asdict(result).items() if key != "issues"},
                "issues": [asdict(issue) for issue in (result.issues or [])],
            }
            for result in results
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair invalid tool_call function.arguments JSON strings.")
    parser.add_argument("root", type=Path, help="TASK root or batch root containing TASK-* directories.")
    parser.add_argument("--source", type=Path, help="Source root containing TASK-*/sessions. Default: same as root.")
    parser.add_argument("--source-state", choices=("auto", "convert", "sessions"), default="sessions")
    parser.add_argument("-path", "--session-path", type=Path, default=Path("sessions"))
    parser.add_argument("-o", "--output", type=Path, help=f"Report path. Default: <root>/{REPORT_NAME}.")
    parser.add_argument("--apply", action="store_true", help="Write repaired files. Default is dry-run.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    source = args.source.expanduser().resolve() if args.source else root
    output = args.output.expanduser().resolve() if args.output else root / REPORT_NAME

    if not root.is_dir():
        print(f"ERROR: root not found: {root}")
        return 1
    if not source.is_dir():
        print(f"ERROR: source not found: {source}")
        return 1

    files = iter_session_files(root, args.session_path)
    results: list[FileResult] = []
    for path in files:
        result = process_file(
            root,
            path,
            source,
            args.session_path,
            args.source_state,
            args.apply,
        )
        if result is not None:
            results.append(result)

    output.parent.mkdir(parents=True, exist_ok=True)
    report = build_report(args, results)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = report["summary"]
    mode = "apply" if args.apply else "dry-run"
    print(
        f"fix_invalid_tool_arguments {mode}: files={summary['files_with_findings']} "
        f"changed={summary['changed_files']} repaired={summary['repaired_arguments']} "
        f"unrepaired={summary['unrepaired_arguments']} errors={summary['errors']}"
    )
    print(f"report: {output}")
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Remove JSON fields whose key/value pair is ``model: internal-model``.

The remover handles both regular JSON objects and JSON encoded inside a
``function.arguments`` string. Plain conversation or log text such as
``model: internal-model`` is intentionally left unchanged.

The remover does not modify ``meta``/``tools``/``tool_definitions`` fields,
top-level system prompts, or messages whose role is system. Assistant
``tool_calls`` remain in scope so their JSON-encoded ``function.arguments``
can be cleaned.

Supported inputs:
  - a directory containing TASK-*/sessions/**/*.json
  - one TASK-* directory
  - one JSON file
  - one JSONL file

The default mode is dry-run. Pass ``--apply`` to write changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


MODEL_FIELD = "model"
DEFAULT_TARGET_VALUE = "internal-model"
EMBEDDED_JSON_KEYS = {"arguments"}
EXCLUDED_KEYS = {"meta", "_metadata", "tools", "tool_definitions"}
ROOT_EXCLUDED_KEYS = {"system", "system_prompt"}
DEFAULT_REPORT_NAME = "remove_internal_model_field_report.json"


@dataclass(frozen=True)
class Removal:
    json_path: str
    location: str


@dataclass
class FileResult:
    source_file: str
    task: str = ""
    session_id: str = ""
    line_number: int | None = None
    changed: bool = False
    removals: list[Removal] = field(default_factory=list)
    error: str = ""


def json_path_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f".{key}"
    return f"[{json.dumps(key, ensure_ascii=False)}]"


def serialize_embedded_json(value: Any, original: str) -> str:
    if "\n" in original or "\r" in original:
        return json.dumps(value, ensure_ascii=False, indent=2)
    if re.search(r"[:,]\s", original):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def remove_model_fields(
    value: Any,
    *,
    target_value: str = DEFAULT_TARGET_VALUE,
    path: str = "$",
    embedded: bool = False,
    is_root: bool = True,
) -> list[Removal]:
    removals: list[Removal] = []

    if isinstance(value, dict):
        role = value.get("role")
        if isinstance(role, str) and role.lower() == "system":
            return removals

        for key in list(value):
            key_text = str(key)
            key_lower = key_text.lower()
            child_path = path + json_path_key(key_text)
            child = value[key]

            if key_lower in EXCLUDED_KEYS or (is_root and key_lower in ROOT_EXCLUDED_KEYS):
                continue

            if key_text == MODEL_FIELD and child == target_value:
                del value[key]
                removals.append(
                    Removal(
                        json_path=child_path,
                        location="embedded_json" if embedded else "json",
                    )
                )
                continue

            if key_text.lower() in EMBEDDED_JSON_KEYS and isinstance(child, str):
                stripped = child.strip()
                if stripped.startswith(("{", "[")):
                    try:
                        parsed = json.loads(child)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, (dict, list)):
                        nested_removals = remove_model_fields(
                            parsed,
                            target_value=target_value,
                            path=child_path + "::<json>",
                            embedded=True,
                            is_root=False,
                        )
                        if nested_removals:
                            value[key] = serialize_embedded_json(parsed, child)
                            removals.extend(nested_removals)
                        continue

            removals.extend(
                remove_model_fields(
                    child,
                    target_value=target_value,
                    path=child_path,
                    embedded=embedded,
                    is_root=False,
                )
            )
        return removals

    if isinstance(value, list):
        for index, child in enumerate(value):
            removals.extend(
                remove_model_fields(
                    child,
                    target_value=target_value,
                    path=f"{path}[{index}]",
                    embedded=embedded,
                    is_root=False,
                )
            )

    return removals


def find_task_dirs(root: Path) -> list[Path]:
    task_dirs: list[Path] = []
    if root.name.startswith("TASK-") and (root / "sessions").is_dir():
        task_dirs.append(root)
    task_dirs.extend(
        path
        for path in root.rglob("TASK-*")
        if path.is_dir() and (path / "sessions").is_dir()
    )
    return sorted(set(task_dirs), key=lambda item: str(item).lower())


def task_and_session_from_payload(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return "", ""
    return str(stats.get("task_id", "") or ""), str(stats.get("session_id", "") or "")


def write_text_atomic(path: Path, text: str) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def process_json_payload_file(
    path: Path,
    *,
    apply: bool,
    target_value: str,
) -> FileResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        task, session_id = task_and_session_from_payload(payload)
        removals = remove_model_fields(payload, target_value=target_value)
        result = FileResult(
            source_file=str(path),
            task=task,
            session_id=session_id or path.stem,
            changed=bool(removals),
            removals=removals,
        )
        if removals and apply:
            write_text_atomic(
                path,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return FileResult(source_file=str(path), error=str(exc))


def process_jsonl_file(
    path: Path,
    *,
    apply: bool,
    target_value: str,
) -> list[FileResult]:
    try:
        raw_lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    except (OSError, UnicodeError) as exc:
        return [FileResult(source_file=str(path), error=str(exc))]

    results: list[FileResult] = []
    output_lines: list[str] = []
    changed_file = False

    for line_number, raw_line in enumerate(raw_lines, start=1):
        line_body = raw_line.rstrip("\r\n")
        eol = raw_line[len(line_body) :]
        if not line_body.strip():
            output_lines.append(raw_line)
            continue

        try:
            payload = json.loads(line_body)
        except json.JSONDecodeError as exc:
            results.append(
                FileResult(
                    source_file=str(path),
                    line_number=line_number,
                    error=str(exc),
                )
            )
            output_lines.append(raw_line)
            continue

        task, session_id = task_and_session_from_payload(payload)
        removals = remove_model_fields(payload, target_value=target_value)
        changed = bool(removals)
        results.append(
            FileResult(
                source_file=str(path),
                task=task,
                session_id=session_id,
                line_number=line_number,
                changed=changed,
                removals=removals,
            )
        )
        if changed:
            changed_file = True
            output_lines.append(json.dumps(payload, ensure_ascii=False) + (eol or "\n"))
        else:
            output_lines.append(raw_line)

    if changed_file and apply:
        write_text_atomic(path, "".join(output_lines))
    return results


def process_file_input(
    path: Path,
    *,
    apply: bool,
    target_value: str,
) -> tuple[list[FileResult], str]:
    if path.suffix.lower() == ".jsonl":
        return process_jsonl_file(path, apply=apply, target_value=target_value), "jsonl"

    try:
        json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return process_jsonl_file(path, apply=apply, target_value=target_value), "jsonl"
    except (OSError, UnicodeError) as exc:
        return [FileResult(source_file=str(path), error=str(exc))], "json"

    return [
        process_json_payload_file(path, apply=apply, target_value=target_value)
    ], "json"


def process_directory(
    target: Path,
    *,
    apply: bool,
    target_value: str,
) -> tuple[list[FileResult], set[str], int]:
    results: list[FileResult] = []
    task_dirs = find_task_dirs(target)
    for task_dir in task_dirs:
        for session_file in sorted((task_dir / "sessions").rglob("*.json")):
            result = process_json_payload_file(
                session_file,
                apply=apply,
                target_value=target_value,
            )
            if not result.task:
                result.task = task_dir.name
            if not result.session_id:
                result.session_id = session_file.stem
            results.append(result)
    return results, {str(task_dir) for task_dir in task_dirs}, len(results)


def build_report(
    target: Path,
    *,
    apply: bool,
    target_value: str,
) -> dict[str, Any]:
    if target.is_file():
        results, input_mode = process_file_input(
            target,
            apply=apply,
            target_value=target_value,
        )
        task_names = {item.task for item in results if item.task}
        files_checked = 1
        records_checked = len([item for item in results if item.line_number is not None])
        if input_mode == "json":
            records_checked = 1
    else:
        results, task_names, files_checked = process_directory(
            target,
            apply=apply,
            target_value=target_value,
        )
        input_mode = "task_sessions"
        records_checked = files_checked

    errors = [item for item in results if item.error]
    changed = [item for item in results if item.changed]
    removal_counts = Counter(
        removal.location
        for item in changed
        for removal in item.removals
    )

    return {
        "scan_target": str(target),
        "input_mode": input_mode,
        "mode": "write" if apply else "dry-run",
        "rule": {
            "field": MODEL_FIELD,
            "value": target_value,
            "embedded_json_keys": sorted(EMBEDDED_JSON_KEYS),
            "excluded": [
                "meta/_metadata fields",
                "top-level system/system_prompt",
                "tools/tool_definitions fields",
                "messages whose role is system",
            ],
            "plain_text_is_unchanged": True,
        },
        "summary": {
            "tasks_checked": len(task_names),
            "session_files_checked": files_checked,
            "records_checked": records_checked,
            "files_or_records_changed": len(changed),
            "files_with_errors": len(errors),
            "fields_removed": sum(removal_counts.values()),
            "fields_removed_by_location": dict(sorted(removal_counts.items())),
        },
        "results": [
            {
                **asdict(item),
                "removals": [asdict(removal) for removal in item.removals],
            }
            for item in results
            if item.changed or item.error
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Remove JSON fields equal to "model": "internal-model", including '
            "fields inside JSON-encoded function.arguments strings."
        )
    )
    parser.add_argument(
        "positional_root",
        nargs="?",
        type=Path,
        help="TASK root, directory containing TASK-* directories, JSON file, or JSONL file",
    )
    parser.add_argument(
        "-r",
        "--root",
        type=Path,
        help="TASK root, directory containing TASK-* directories, JSON file, or JSONL file",
    )
    parser.add_argument(
        "--target-value",
        default=DEFAULT_TARGET_VALUE,
        help=f"model value to remove (default: {DEFAULT_TARGET_VALUE})",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help=f"report path (default: TARGET_DIR/{DEFAULT_REPORT_NAME})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write changes to input files")
    mode.add_argument("--dry-run", action="store_true", help="preview only (default)")
    parser.add_argument(
        "--max-display",
        type=int,
        default=50,
        help="maximum changed files/records printed to the console (default: 50)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root_arg = args.root or args.positional_root or Path(__file__).resolve().parent
    target = root_arg.expanduser().resolve()
    if not target.exists():
        print(f"error: target does not exist: {target}", file=sys.stderr)
        return 2
    if target.is_file() and target.suffix.lower() not in {".json", ".jsonl"}:
        print(f"error: file input must be .json or .jsonl: {target}", file=sys.stderr)
        return 2

    report_dir = target.parent if target.is_file() else target
    report_path = (args.report or (report_dir / DEFAULT_REPORT_NAME)).expanduser().resolve()
    if target.is_file() and report_path == target:
        print("error: report path must not overwrite the input file", file=sys.stderr)
        return 2

    report = build_report(
        target,
        apply=args.apply,
        target_value=args.target_value,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        report_path,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )

    summary = report["summary"]
    print(f"Mode: {'WRITE' if args.apply else 'DRY-RUN'}")
    print(
        f"Checked {summary['tasks_checked']} task(s), "
        f"{summary['session_files_checked']} file(s), "
        f"{summary['records_checked']} record(s)."
    )
    verb = "Removed" if args.apply else "Would remove"
    print(
        f"{verb} {summary['fields_removed']} field(s): "
        f"{summary['fields_removed_by_location'] or '{}'}"
    )
    print(f"Changed files/records: {summary['files_or_records_changed']}")

    displayed = 0
    for result in report["results"]:
        if displayed >= max(0, args.max_display):
            break
        if result.get("error"):
            continue
        line_suffix = f":{result['line_number']}" if result.get("line_number") else ""
        print(f"\nCHANGED {result['source_file']}{line_suffix}")
        for removal in result["removals"][:10]:
            print(f"  [{removal['location']}] {removal['json_path']}")
        if len(result["removals"]) > 10:
            print(f"  ... {len(result['removals']) - 10} more removal(s)")
        displayed += 1

    errors = [item for item in report["results"] if item.get("error")]
    for item in errors[:20]:
        line_suffix = f":{item['line_number']}" if item.get("line_number") else ""
        print(f"\nERROR {item['source_file']}{line_suffix}: {item['error']}")
    if len(errors) > 20:
        print(f"\n... {len(errors) - 20} additional error(s) are in the JSON report.")

    print(f"\nReport: {report_path}")
    if not args.apply and summary["fields_removed"]:
        print("Dry-run only; rerun with --apply to write changes.")
    return 2 if summary["files_with_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

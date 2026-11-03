#!/usr/bin/env python3
"""Replace sensitive model/vendor references outside system prompts and tools.

This fixer mirrors check_claude_anthropic.py:
  - skips objects whose role is system
  - skips top-level system/system_prompt fields
  - skips meta/_metadata fields
  - skips tools/tool_definitions fields
  - uses the same ASCII-letter boundary rule
  - replaces complete versioned Claude-family model identifiers before
    replacing standalone keywords

The default mode is dry-run. Pass --apply to write changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


REPLACEMENTS = {
    "opus": "internal-model",
    "sonnet": "internal-model",
    "haiku": "internal-model",
    "claude": "shannon",
    "anthropic": "Brilliant",
}
KEYWORDS = tuple(REPLACEMENTS)
KEYWORD_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(re.escape(keyword) for keyword in sorted(KEYWORDS, key=len, reverse=True))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)
MODEL_PLACEHOLDER = "internal-model"
MODEL_VERSION_PATTERN = r"[0-9]+(?:[.-][0-9]+)*"
MODEL_CONTEXT_PATTERN = r"(?:\s*\(1M\s+context\))?"
MODEL_EMAIL_PATTERN = r"(?:\s*<[^<>\s]+@anthropic\.com>)?"
MODEL_IDENTIFIER_RULES: tuple[tuple[str | None, re.Pattern[str]], ...] = (
    (
        "claude",
        re.compile(
            rf"(?<![A-Za-z])claude"
            rf"(?:[-\s]+{MODEL_VERSION_PATTERN})?"
            rf"[-\s]+(?P<family>haiku|opus|sonnet)"
            rf"(?:[-\s]+{MODEL_VERSION_PATTERN})?"
            rf"{MODEL_CONTEXT_PATTERN}{MODEL_EMAIL_PATTERN}"
            rf"(?![-A-Za-z0-9])",
            re.IGNORECASE,
        ),
    ),
    (
        "claude",
        re.compile(
            rf"(?<![A-Za-z])claude[-\s]+{MODEL_VERSION_PATTERN}"
            rf"{MODEL_CONTEXT_PATTERN}{MODEL_EMAIL_PATTERN}"
            rf"(?![-A-Za-z0-9])",
            re.IGNORECASE,
        ),
    ),
    (
        None,
        re.compile(
            rf"(?<![A-Za-z])(?P<family>haiku|opus|sonnet)"
            rf"[-\s]+{MODEL_VERSION_PATTERN}{MODEL_CONTEXT_PATTERN}"
            rf"(?![-A-Za-z0-9])",
            re.IGNORECASE,
        ),
    ),
)
EXCLUDED_KEYS = {"meta", "_metadata", "tools", "tool_definitions"}
SYSTEM_PROMPT_KEYS = {"system", "system_prompt"}
DEFAULT_REPORT_NAME = "fix_sensitive_keywords_report.json"


@dataclass(frozen=True)
class Replacement:
    json_path: str
    keyword: str
    matched_text: str
    replacement: str
    context: str


@dataclass
class FileResult:
    source_file: str
    task: str = ""
    session_id: str = ""
    line_number: int | None = None
    changed: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    replacements: list[Replacement] = field(default_factory=list)
    error: str = ""


def json_path_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f".{key}"
    return f"[{json.dumps(key, ensure_ascii=False)}]"


def make_context(text: str, start: int, end: int, radius: int = 80) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    context = text[left:right].replace("\r", " ").replace("\n", " ")
    context = re.sub(r"\s+", " ", context).strip()
    if left:
        context = "..." + context
    if right < len(text):
        context += "..."
    return context


def replace_string(text: str, json_path: str) -> tuple[str, list[Replacement]]:
    replacements: list[Replacement] = []

    def record_replacement(
        match: re.Match[str],
        *,
        source_text: str,
        keyword: str,
        replacement: str,
    ) -> str:
        matched_text = match.group(0)
        replacements.append(
            Replacement(
                json_path=json_path,
                keyword=keyword,
                matched_text=matched_text,
                replacement=replacement,
                context=make_context(source_text, match.start(), match.end()),
            )
        )
        return replacement

    for fixed_keyword, pattern in MODEL_IDENTIFIER_RULES:
        source_text = text

        def replace_model_match(
            match: re.Match[str],
            *,
            source_text: str = source_text,
            fixed_keyword: str | None = fixed_keyword,
        ) -> str:
            family = match.groupdict().get("family")
            keyword = fixed_keyword or (family.lower() if family else "model")
            return record_replacement(
                match,
                source_text=source_text,
                keyword=keyword,
                replacement=MODEL_PLACEHOLDER,
            )

        text = pattern.sub(replace_model_match, text)

    source_text = text

    def replace_keyword_match(match: re.Match[str]) -> str:
        keyword = match.group(0).lower()
        return record_replacement(
            match,
            source_text=source_text,
            keyword=keyword,
            replacement=REPLACEMENTS[keyword],
        )

    return KEYWORD_RE.sub(replace_keyword_match, text), replacements


def replace_payload(
    value: Any,
    path: str = "$",
    *,
    is_root: bool = True,
) -> tuple[Any, list[Replacement]]:
    if isinstance(value, dict):
        role = value.get("role")
        if isinstance(role, str) and role.lower() == "system":
            return value, []

        replacements: list[Replacement] = []
        for key, child in list(value.items()):
            key_text = str(key)
            key_lower = key_text.lower()
            if key_lower in EXCLUDED_KEYS:
                continue
            if is_root and key_lower in SYSTEM_PROMPT_KEYS:
                continue

            new_child, child_replacements = replace_payload(
                child,
                path + json_path_key(key_text),
                is_root=False,
            )
            if child_replacements:
                value[key] = new_child
                replacements.extend(child_replacements)
        return value, replacements

    if isinstance(value, list):
        replacements = []
        for index, child in enumerate(value):
            new_child, child_replacements = replace_payload(
                child,
                f"{path}[{index}]",
                is_root=False,
            )
            if child_replacements:
                value[index] = new_child
                replacements.extend(child_replacements)
        return value, replacements

    if isinstance(value, str):
        return replace_string(value, path)

    return value, []


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


def write_json_payload(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def counts_for(replacements: list[Replacement]) -> dict[str, int]:
    counter = Counter(item.keyword for item in replacements)
    return dict(sorted(counter.items()))


def task_and_session_from_payload(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    stats = payload.get("stats", {})
    if not isinstance(stats, dict):
        return "", ""
    return str(stats.get("task_id", "") or ""), str(stats.get("session_id", "") or "")


def process_json_payload_file(path: Path, *, apply: bool) -> FileResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        task, session_id = task_and_session_from_payload(payload)
        _, replacements = replace_payload(payload)
        result = FileResult(
            source_file=str(path),
            task=task,
            session_id=session_id or path.stem,
            changed=bool(replacements),
            counts=counts_for(replacements),
            replacements=replacements,
        )
        if replacements and apply:
            write_json_payload(path, payload)
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return FileResult(source_file=str(path), error=str(exc))


def process_jsonl_file(path: Path, *, apply: bool) -> list[FileResult]:
    results: list[FileResult] = []
    try:
        raw_lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    except (OSError, UnicodeError) as exc:
        return [FileResult(source_file=str(path), error=str(exc))]

    changed_file = False
    output_lines: list[str] = []
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
        _, replacements = replace_payload(payload)
        changed = bool(replacements)
        results.append(
            FileResult(
                source_file=str(path),
                task=task,
                session_id=session_id,
                line_number=line_number,
                changed=changed,
                counts=counts_for(replacements),
                replacements=replacements,
            )
        )
        if changed:
            changed_file = True
            output_lines.append(json.dumps(payload, ensure_ascii=False) + (eol or "\n"))
        else:
            output_lines.append(raw_line)

    if changed_file and apply:
        path.write_text("".join(output_lines), encoding="utf-8")
    return results


def process_file_input(path: Path, *, apply: bool) -> tuple[list[FileResult], str]:
    if path.suffix.lower() == ".jsonl":
        return process_jsonl_file(path, apply=apply), "jsonl"

    try:
        json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return process_jsonl_file(path, apply=apply), "jsonl"
    except (OSError, UnicodeError) as exc:
        return [FileResult(source_file=str(path), error=str(exc))], "json"

    return [process_json_payload_file(path, apply=apply)], "json"


def process_directory(target: Path, *, apply: bool) -> tuple[list[FileResult], set[str], int]:
    results: list[FileResult] = []
    task_dirs = find_task_dirs(target)
    for task_dir in task_dirs:
        for session_file in sorted((task_dir / "sessions").rglob("*.json")):
            result = process_json_payload_file(session_file, apply=apply)
            if not result.task:
                result.task = task_dir.name
            if not result.session_id:
                result.session_id = session_file.stem
            results.append(result)
    return results, {task_dir.name for task_dir in task_dirs}, len(results)


def build_report(target: Path, *, apply: bool) -> dict[str, Any]:
    if target.is_file():
        results, input_mode = process_file_input(target, apply=apply)
        task_names = {item.task for item in results if item.task}
        files_checked = 1
        records_checked = len([item for item in results if item.line_number is not None]) or 1
    else:
        results, task_names, files_checked = process_directory(target, apply=apply)
        input_mode = "task_sessions"
        records_checked = files_checked

    errors = [item for item in results if item.error]
    changed = [item for item in results if item.changed]
    counts = Counter()
    for item in results:
        counts.update(item.counts)

    return {
        "scan_target": str(target),
        "input_mode": input_mode,
        "mode": "write" if apply else "dry-run",
        "rule": {
            "replacements": REPLACEMENTS,
            "versioned_model_identifiers": {
                "replacement": MODEL_PLACEHOLDER,
                "families": ["claude", "opus", "sonnet", "haiku"],
                "examples": [
                    "claude-3-opus",
                    "claude-3-5-sonnet-20241022",
                    "claude-opus-4-1",
                    "Claude 3.7 Sonnet (1M context)",
                    "sonnet-3.7",
                ],
            },
            "case_sensitive": False,
            "boundary": "keyword cannot be immediately preceded or followed by an ASCII letter",
            "excluded": [
                "objects whose role is system",
                "top-level system/system_prompt fields",
                "meta/_metadata fields",
                "tools/tool_definitions fields",
            ],
        },
        "summary": {
            "tasks_checked": len(task_names),
            "session_files_checked": files_checked,
            "records_checked": records_checked,
            "files_or_records_changed": len(changed),
            "files_with_errors": len(errors),
            "total_replacements": sum(counts.values()),
            "replacements_by_keyword": dict(sorted(counts.items())),
        },
        "results": [
            {
                **asdict(item),
                "replacements": [asdict(replacement) for replacement in item.replacements],
            }
            for item in results
            if item.changed or item.error
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replace complete versioned Claude-family model identifiers with "
            "internal-model, standalone opus/sonnet/haiku with internal-model, "
            "Claude/claude with shannon, and Anthropic with Brilliant. Supports "
            "TASK-*/sessions/*.json roots, one JSON file, or a JSONL file such as "
            "repo-generate.json."
        )
    )
    parser.add_argument(
        "positional_root",
        nargs="?",
        type=Path,
        help="Directory containing TASK-* directories, a TASK directory, a JSON file, or a JSONL file",
    )
    parser.add_argument(
        "-r",
        "--root",
        type=Path,
        help="Directory containing TASK-* directories, a TASK directory, a JSON file, or a JSONL file (default: script directory)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help=f"Report path (default: TARGET_DIR/{DEFAULT_REPORT_NAME})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes to disk.")
    mode.add_argument("--dry-run", action="store_true", help="Preview changes without writing (default).")
    parser.add_argument(
        "--max-display",
        type=int,
        default=50,
        help="Maximum changed files/records printed to the console (default: 50).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root_arg = args.root or args.positional_root or Path(__file__).resolve().parent
    target = root_arg.expanduser().resolve()
    if not target.exists():
        print(f"error: target does not exist: {target}", file=sys.stderr)
        return 2
    if target.is_file() and target.suffix.lower() not in {".json", ".jsonl"}:
        print(f"error: file input must be .json or .jsonl: {target}", file=sys.stderr)
        return 2

    report = build_report(target, apply=args.apply)
    report_dir = target.parent if target.is_file() else target
    report_path = (args.report or (report_dir / DEFAULT_REPORT_NAME)).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = report["summary"]
    print(f"Mode: {'WRITE' if args.apply else 'DRY-RUN'}")
    print(
        f"Checked {summary['tasks_checked']} task(s), "
        f"{summary['session_files_checked']} file(s), "
        f"{summary['records_checked']} record(s)."
    )
    verb = "Replaced" if args.apply else "Would replace"
    print(
        f"{verb} {summary['total_replacements']} occurrence(s): "
        f"{summary['replacements_by_keyword'] or '{}'}"
    )
    print(f"Changed files/records: {summary['files_or_records_changed']}")

    max_display = max(0, args.max_display)
    displayed = 0
    for result in report["results"]:
        if displayed >= max_display:
            break
        if result.get("error"):
            continue
        line_suffix = f":{result['line_number']}" if result.get("line_number") else ""
        print(f"\nCHANGED {result['source_file']}{line_suffix} {result['counts']}")
        for replacement in result["replacements"][:5]:
            print(
                f"  [{replacement['matched_text']} -> {replacement['replacement']}] "
                f"{replacement['json_path']}\n    {replacement['context']}"
            )
        if len(result["replacements"]) > 5:
            print(f"  ... {len(result['replacements']) - 5} more replacement(s) in this file/record")
        displayed += 1

    hidden = summary["files_or_records_changed"] - displayed
    if hidden > 0:
        print(f"\n... {hidden} additional changed file(s)/record(s) are only in the JSON report.")

    errors = [item for item in report["results"] if item.get("error")]
    for item in errors[:20]:
        line_suffix = f":{item['line_number']}" if item.get("line_number") else ""
        print(f"\nERROR {item['source_file']}{line_suffix}: {item['error']}")
    if len(errors) > 20:
        print(f"\n... {len(errors) - 20} additional error(s) are only in the JSON report.")

    print(f"\nReport: {report_path}")
    if not args.apply and summary["total_replacements"]:
        print("Dry-run only; rerun with --apply to write changes.")

    return 2 if summary["files_with_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

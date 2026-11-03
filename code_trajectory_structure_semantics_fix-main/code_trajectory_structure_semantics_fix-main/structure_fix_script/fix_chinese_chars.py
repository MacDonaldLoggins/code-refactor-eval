#!/usr/bin/env python3
"""Read no-Chinese check findings and replace detected Chinese/fullwidth
punctuation with ASCII equivalents in the source session JSON files.

Usage:
    python fix_chinese_chars.py \
        --report /path/to/check_no_chinese.json \
        --source-root /path/to/workspace
    python fix_chinese_chars.py \
        --report /path/to/pipeline_report_summary.md \
        --source-root /path/to/workspace
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FULLWIDTH_ASCII_MAP: dict[str, str] = {
    chr(codepoint): chr(codepoint - 0xFEE0)
    for codepoint in range(0xFF01, 0xFF5F)
}

CJK_PUNCTUATION_MAP: dict[str, str] = {
    "\u3000": " ",  # ideographic space
    "\u303f": " ",  # ideographic half fill space
    "\u3001": ",",
    "\u3002": ".",
    "\u3008": "<",
    "\u3009": ">",
    "\u300a": "<",
    "\u300b": ">",
    "\u300c": "'",
    "\u300d": "'",
    "\u300e": '"',
    "\u300f": '"',
    "\u3010": "[",
    "\u3011": "]",
    "\u3014": "[",
    "\u3015": "]",
    "\u3016": "[",
    "\u3017": "]",
    "\u3018": "[",
    "\u3019": "]",
    "\u301a": "[",
    "\u301b": "]",
    "\u301c": "~",
    "\u301d": '"',
    "\u301e": '"',
    "\u301f": '"',
    "\u3030": "~",
    "\uff61": ".",
    "\uff62": "'",
    "\uff63": "'",
    "\uff64": ",",
    "\uff65": ".",
}

CHAR_MAP: dict[str, str] = {
    **FULLWIDTH_ASCII_MAP,
    **CJK_PUNCTUATION_MAP,
}

TRANS_TABLE = str.maketrans(CHAR_MAP)


NO_CHINESE_CHECK_TITLE = "\u4e2d\u6587/CJK \u5185\u5bb9\u68c0\u67e5"
NO_CHINESE_REASON = "\u8f68\u8ff9\u4e2d\u5305\u542b\u4e2d\u6587\u5b57\u7b26\u3002"


def utf8_as_gbk_mojibake(text: str) -> str:
    return text.encode("utf-8").decode("gbk", errors="replace")


NO_CHINESE_CHECK_TITLES = {
    NO_CHINESE_CHECK_TITLE,
    utf8_as_gbk_mojibake(NO_CHINESE_CHECK_TITLE),
}
NO_CHINESE_REASONS = {
    NO_CHINESE_REASON,
    utf8_as_gbk_mojibake(NO_CHINESE_REASON),
}
MARKDOWN_HEADER_CHECK_ITEM = {
    "\u68c0\u67e5\u9879",
    utf8_as_gbk_mojibake("\u68c0\u67e5\u9879"),
}
JSON_PROJECT_ID_KEYS = ("\u4ea7\u7269id", "project", "project_id")
JSON_FILE_ID_KEYS = ("\u6587\u4ef6id", "session", "file", "file_id")


def collect_files_from_json_report(report_path: Path, source_root: Path) -> list[Path]:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    tasks = list(data.get("tasks", []))
    for result in data.get("results", []):
        if result.get("name") == "no_chinese":
            tasks.extend(result.get("tasks", []))
    seen: set[Path] = set()
    files: list[Path] = []
    for task in tasks:
        pid = next((str(task.get(key, "")) for key in JSON_PROJECT_ID_KEYS if task.get(key)), "")
        fid = next((str(task.get(key, "")) for key in JSON_FILE_ID_KEYS if task.get(key)), "")
        if not pid or not fid:
            continue
        candidates = []
        file_path = Path(fid)
        if file_path.is_absolute():
            candidates.append(file_path)
        if source_root.name == pid:
            candidates.append(source_root / file_path)
        candidates.append(source_root / pid / file_path)
        candidates.append(source_root / "ERROR-TASK" / "异常tasks" / pid / file_path)
        for path in candidates:
            if path not in seen and path.is_file():
                seen.add(path)
                files.append(path)
                break
    return sorted(files)


def split_markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    cells = [cell.strip().replace("\\|", "|") for cell in stripped.strip("|").split("|")]
    return cells if len(cells) >= 4 else None


def resolve_markdown_session_path(path_text: str, source_root: Path, task_id: str) -> Path:
    path = Path(path_text)
    candidates = []
    if path.is_absolute():
        candidates.append(path)
        repaired = Path(path_text.replace("澶辫触-", "失败-"))
        if repaired != path:
            candidates.append(repaired)
        if task_id and "ERROR-TASK" not in repaired.parts:
            try:
                task_index = repaired.parts.index(task_id)
            except ValueError:
                pass
            else:
                task_rel = Path(*repaired.parts[task_index:])
                task_parent = repaired.parents[len(repaired.parts) - task_index - 1]
                candidates.append(task_parent / "ERROR-TASK" / "异常tasks" / task_rel)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    if task_id:
        candidates.append(source_root / task_id / path)
        candidates.append(source_root / "ERROR-TASK" / "异常tasks" / task_id / path)
    candidates.append(source_root / path)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if candidates else path


def collect_files_from_markdown_report(report_path: Path, source_root: Path) -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    current_task_id = ""

    for raw_line in report_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("### "):
            heading = stripped[4:].strip()
            current_task_id = heading if heading.startswith("TASK-") else ""
            continue

        cells = split_markdown_table_row(raw_line)
        if not cells or cells[0] in MARKDOWN_HEADER_CHECK_ITEM or cells[0] == "---":
            continue

        check_item, reason, session_path_text, _line_number = cells[:4]
        if check_item not in NO_CHINESE_CHECK_TITLES or reason not in NO_CHINESE_REASONS:
            continue

        path = resolve_markdown_session_path(session_path_text, source_root, current_task_id).resolve()
        if path not in seen and path.is_file():
            seen.add(path)
            files.append(path)

    return sorted(files)


def collect_files_from_report(report_path: Path, source_root: Path | None) -> list[Path]:
    if report_path.suffix.lower() == ".md":
        return collect_files_from_markdown_report(report_path, source_root or report_path.parent)
    if source_root is None:
        raise ValueError("--source-root is required when reading check_no_chinese.json")
    return collect_files_from_json_report(report_path, source_root)


def display_path(path: Path, source_root: Path | None) -> Path | str:
    if source_root is None:
        return path
    try:
        return path.relative_to(source_root)
    except ValueError:
        return str(path)


def replace_in_file(path: Path) -> tuple[int, bool]:
    """Replace fullwidth/CJK punctuation in a JSON file.

    Returns (replacement_count, json_valid).
    """
    original = path.read_text(encoding="utf-8")
    replaced = original.translate(TRANS_TABLE)
    count = sum(1 for a, b in zip(original, replaced) if a != b)

    if count == 0:
        return 0, True

    try:
        json.loads(replaced)
    except json.JSONDecodeError as exc:
        print(f"  WARNING: result is not valid JSON after replacement: {exc}")
        return count, False

    path.write_text(replaced, encoding="utf-8")
    return count, True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replace Chinese/fullwidth punctuation in session JSON files."
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Path to check_no_chinese.json or pipeline_report_summary.md report.",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Root directory containing TASK-* project subdirectories. Required for JSON reports; optional for Markdown reports with absolute paths.",
    )
    args = parser.parse_args()

    report_path: Path = args.report.expanduser().resolve()
    source_root: Path | None = args.source_root.expanduser().resolve() if args.source_root else None

    if not report_path.is_file():
        print(f"ERROR: report not found: {report_path}", file=sys.stderr)
        return 1
    if source_root is not None and not source_root.is_dir():
        print(f"ERROR: source root not found: {source_root}", file=sys.stderr)
        return 1

    try:
        files = collect_files_from_report(report_path, source_root)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Found {len(files)} files to fix.\n")

    total_replacements = 0
    fixed = 0
    skipped = 0
    failed = 0

    for path in files:
        rel = display_path(path, source_root)
        count, valid = replace_in_file(path)
        if count == 0:
            print(f"  SKIP (no match): {rel}")
            skipped += 1
        elif valid:
            print(f"  FIXED ({count} chars): {rel}")
            fixed += 1
            total_replacements += count
        else:
            print(f"  FAIL (invalid JSON): {rel}")
            failed += 1

    print(f"\nDone. fixed={fixed}, skipped={skipped}, failed={failed}, total_replacements={total_replacements}")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
r"""m_session.py — single-file processor for a Claude Code TASK trajectory.

"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import re
import shutil
import tarfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


# ============================================================================
# 通用常量
# ============================================================================

DEFAULT_SOURCE = Path("original_sessions")
DEFAULT_OUTPUT = Path("original_sessions")
FIX_SOURCE = Path("fix_original_sessions")

REPORT_DIR_NAME = "report"
DEFAULT_FORMAT_REPORT = "json_format_report.md"
DEFAULT_REPAIR_REPORT = "json_repair_report.md"
DEFAULT_CASE_REPORT = "json_repair_case_report.md"
CLI_FORMAT_REPORT = "json_format_report.fix_original_sessions.md"
BACKUP_DIR_NAME = ".backup_before_json_repair"

SOURCE_DIR_CANDIDATES = (
    Path("original_sessions"),
    Path(".backup/original_sessions"),
)

SESSION_JSON_ALLOWED_DIR_PREFIXES = ("tool-results",)
SESSION_JSONL_ROOT_TYPES = {"queue-operation", "file-history-snapshot"}

CASE_DIFF_CONTEXT = 70

REPAIR_CASE_DESCRIPTIONS = {
    "escape-invalid-backslashes": (
        "string with invalid backslash escape",
        "JSON string contains an invalid backslash escape, parser reports `Invalid \\escape`.",
        "Replace bare backslashes with valid JSON escapes while preserving original semantics.",
    ),
    "escape-bare-quotes": (
        "string with unescaped double quote",
        "Body of a string contains an unescaped double quote so the parser closes the string early.",
        "Escape the bare double quote inside the string body as `\\\"`.",
    ),
    "root-cause-block-join": (
        "JSONL record split across physical lines",
        "A single JSONL record was broken by a real newline so structures cannot close on one line.",
        "Re-join the physical lines and rewrite real newlines inside strings as `\\n`.",
    ),
    "extract-json-values-from-nul-or-embedded-text": (
        "NUL bytes or embedded text mixed into JSONL",
        "JSONL line contains NUL bytes, hole padding, bad prefix or inline event text.",
        "Strip the invalid padding and extract whole JSON objects from valid anchors.",
    ),
    "insert-tool-result-content": (
        "tool_result missing content field",
        "`tool_result` is followed by bare text with no valid `content` field.",
        "Wrap the bare text as a `content` string field.",
    ),
    "close-truncated-tail-json": (
        "trailing JSON structure truncated",
        "Tail record was truncated so strings/arrays/objects are not closed.",
        "Append inferred closers to repair the tail.",
    ),
    "drop-plain-text-jsonl-record": (
        "plain text line mixed into JSONL",
        "Line is a plain text fragment, not a JSONL record (JSONL has no comments).",
        "Remove the line and quarantine the original into `.tmp/quarantined_jsonl_records.jsonl`.",
    ),
    "split-concatenated-json": (
        "multiple JSON objects glued on one line",
        "Multiple complete JSON values are written on one physical line causing `Extra data`.",
        "Split them into separate JSONL records by JSON decoder boundaries.",
    ),
    "remove-bom": (
        "JSONL line starts with BOM",
        "Line begins with a UTF-8 BOM so it is not a valid JSON value.",
        "Strip the leading BOM and re-parse.",
    ),
    "remove-trailing-comma": (
        "JSONL line ends with trailing comma",
        "A trailing comma on the line breaks single-line JSON parsing.",
        "Drop the trailing comma.",
    ),
    "fix-utf16-to-utf8": (
        "file is not UTF-8",
        "File uses UTF-16 or BOM encoding so UTF-8 read fails.",
        "Decode with the detected encoding, verify, then rewrite as UTF-8.",
    ),
}


# ============================================================================
# 中文清理常量
# ============================================================================

CHINESE_RE = re.compile(r"[一-鿿]")

SKIP_LANGUAGE_CHECK_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".db", ".sqlite", ".sqlite3",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".exe", ".dll", ".so", ".dylib",
    ".class", ".jar", ".o", ".obj", ".a",
}

ENGLISH_CHECK_EXCLUDED_DIRS = {".tmp", ".backup", ".git"}
ENGLISH_CHECK_EXCLUDED_FILES = {".gitignore", ".gitattributes", ".gitmodules"}

RUNTIME_NOISE_DIR_NAMES = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", ".npm", ".pnpm-store", ".yarn", ".next",
    "coverage", "dist", "build", "target", "out", "bin", "obj",
    "debug", "release", ".gradle", ".kotlin", ".dart_tool", "htmlcov",
    "cmakefiles", ".bundle", "vendor", ".cache",
}

RUNTIME_NOISE_FILE_NAMES = {
    ".coverage", "coverage.out", "cmakecache.txt", "cmake_install.cmake",
    "compile_commands.json", ".flutter-plugins", ".flutter-plugins-dependencies",
}

RUNTIME_NOISE_FILE_SUFFIXES = {
    ".pyc", ".class", ".jar", ".war", ".o", ".obj", ".exe", ".pdb",
    ".test", ".tsbuildinfo",
}

ORIGINAL_SESSIONS_DIR_NAME = "original_sessions"

# 函数 c：在 original_sessions 内容中删除的文件名关键字。
# 顺序按长度降序排列以避免被短关键字截断（如 quality-runner-darwin-arm64 必须排在 quality-runner 之前）。
KEYWORD_REMOVALS: tuple[str, ...] = (
    "merge_claude_subagents_trajectory.py",
    "codex_report_issues_verification.md",
    "prompt_requirements_verification.md",
    "quality-runner-darwin-arm64",
    "quality-runner-darwin-amd64",
    "final_validation_report.md",
    "test_effectiveness_report.md",
    "clean_original_sessions.py",
    "run_session_export.py",
    "validate_package.py",
    "validation_report.md",
    "docker_startup.log",
    "run_evaluation.py",
    "run_evaluation.log",
    "run_validate.py",
    "run_quality.py",
    "check_chinese.py",
    "quality-runner.exe",
    "codex_report.md",
    "run_tests.log",
    "quality-runner",
)
KEYWORD_REMOVAL_RE = re.compile(
    "|".join(re.escape(k) for k in KEYWORD_REMOVALS)
)

class Colors:
    OK = "\033[92m"
    WARN = "\033[93m"
    FAIL = "\033[91m"
    INFO = "\033[96m"
    RESET = "\033[0m"


MARK_OK = "[OK]"
MARK_WARN = "[WARN]"
MARK_FAIL = "[FAIL]"
MARK_SKIP = "[SKIP]"


def colored(text: str, color: str) -> str:
    return f"{color}{text}{Colors.RESET}"


# ============================================================================
# TASK 目录发现
# ============================================================================

def discover_task_dirs(root: Path) -> list[Path]:
    root = root.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if root.is_dir() and root.name.startswith("TASK-"):
        return [root]
    if not root.is_dir():
        raise NotADirectoryError(root)
    direct = sorted(
        path for path in root.iterdir() if path.is_dir() and path.name.startswith("TASK-")
    )
    if direct:
        return direct
    return sorted(path for path in root.rglob("TASK-*") if path.is_dir())


# ============================================================================
# 数据结构
# ============================================================================

@dataclass(frozen=True)
class Issue:
    path: Path
    line_no: int
    message: str


# ============================================================================
# 基础 JSON 工具
# ============================================================================

def _loads_ok(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    pos = index - 1
    while pos >= 0 and text[pos] == "\\":
        backslashes += 1
        pos -= 1
    return backslashes % 2 == 1


def _is_session_level_value(value: object) -> bool:
    return isinstance(value, dict) and (
        "parentUuid" in value
        or "sessionId" in value
        or value.get("type") in SESSION_JSONL_ROOT_TYPES
    )


def _is_session_jsonl_record(value: object) -> bool:
    return _is_session_level_value(value)


# ============================================================================
# 第一阶段：JSONL/JSON 格式检查（替代 check_session_format.py）
# ============================================================================

def _safe_archive_target(base: Path, relative_name: str) -> Path | None:
    name = relative_name.replace("\\", "/")
    if name.startswith("__MACOSX/") or "/__MACOSX/" in name or Path(name).name.startswith("._"):
        return None
    parts = Path(name).parts
    if len(parts) >= 2 and parts[0] == "opencode-sessions":
        relative = Path(*parts[1:])
    else:
        relative = Path(*parts)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return base / relative


def extract_session_archives_if_needed(source_dir: Path) -> dict[str, int]:
    if any(path.is_file() and path.suffix.lower() in {".json", ".jsonl"} for path in source_dir.rglob("*")):
        return {"archives": 0, "extracted": 0, "skipped": 0}
    archives = [*source_dir.glob("*.tar.gz"), *source_dir.glob("*.zip")]
    extracted = 0
    skipped = 0
    for archive in archives:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    target = _safe_archive_target(source_dir, info.filename)
                    if info.is_dir() or target is None or target.exists():
                        skipped += 1
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(info))
                    extracted += 1
        elif archive.name.endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as tf:
                for member in tf.getmembers():
                    target = _safe_archive_target(source_dir, member.name)
                    if not member.isfile() or target is None or target.exists():
                        skipped += 1
                        continue
                    handle = tf.extractfile(member)
                    if handle is None:
                        skipped += 1
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(handle.read())
                    extracted += 1
    return {"archives": len(archives), "extracted": extracted, "skipped": skipped}


def _is_allowed_session_json_file(path: Path, source_dir: Path) -> bool:
    if path.name.endswith(".meta.json"):
        return True
    try:
        relative = path.relative_to(source_dir)
    except ValueError:
        return True
    return any(
        part.startswith(SESSION_JSON_ALLOWED_DIR_PREFIXES)
        for part in relative.parts[:-1]
    )


def _check_source_layout(source_dir: Path) -> list[str]:
    issues: list[str] = []
    if not source_dir.is_dir():
        return issues
    for child in sorted(source_dir.iterdir()):
        if child.is_dir() and (child.name == "subagents" or child.name.startswith("tool-results")):
            issues.append(
                f"{child}:1: invalid top-level session artifact directory; "
                "subagents and tool-results must be nested under a session directory"
            )
        elif child.is_file() and child.name == "subagents.jsonl":
            issues.append(
                f"{child}:1: invalid top-level subagents.jsonl; "
                "subagent transcripts must be nested under a session directory"
            )
    return issues


def _check_json_file(path: Path, source_dir: Path | None = None) -> list[str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle)
    except json.JSONDecodeError as exc:
        return [f"{path}:{exc.lineno}: {exc.msg}"]
    except UnicodeDecodeError as exc:
        return [f"{path}: encoding error: {exc}"]
    except OSError as exc:
        return [f"{path}: {exc}"]
    if source_dir is not None and not _is_allowed_session_json_file(path, source_dir):
        return [
            f"{path}:1: unsupported session JSON file; "
            "Claude Code transcripts must be JSONL, while JSON files are only allowed "
            "for .meta.json metadata or tool-results payloads"
        ]
    return []


def _check_jsonl_file(path: Path, source_dir: Path | None = None) -> list[str]:
    issues: list[str] = []
    records = 0
    blank_reported = False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                line = line.rstrip("\n\r")
                if not line.strip():
                    if not blank_reported:
                        issues.append(f"{path}:{line_no}: blank JSONL line")
                        blank_reported = True
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    issues.append(f"{path}:{line_no}: {exc.msg}")
                    continue
                records += 1
                if source_dir is not None and not _is_session_jsonl_record(value):
                    issues.append(
                        f"{path}:{line_no}: JSONL record is not a Claude Code session event"
                    )
    except UnicodeDecodeError as exc:
        issues.append(f"{path}: encoding error: {exc}")
    except OSError as exc:
        issues.append(f"{path}: {exc}")
    if source_dir is not None and records == 0 and not blank_reported:
        issues.append(f"{path}:1: JSONL file contains no session records")
    return issues


def _check_file(path: Path, source_dir: Path | None = None) -> list[str]:
    if path.suffix.lower() == ".jsonl":
        return _check_jsonl_file(path, source_dir)
    return _check_json_file(path, source_dir)


def _source_dir_for_path(path: Path, source_dirs: list[Path]) -> Path | None:
    for source_dir in source_dirs:
        try:
            path.relative_to(source_dir)
        except ValueError:
            continue
        return source_dir
    return None


def _rel(base: Path, path_text: str) -> str:
    try:
        path_part, line_no, message = path_text.rsplit(":", 2)
        separator = ":"
        suffix = f"{line_no}:{message}"
    except ValueError:
        path_part, separator, suffix = path_text.partition(":")
    path = Path(path_part)
    try:
        rel_path = path.relative_to(base).as_posix()
    except ValueError:
        rel_path = path.as_posix()
    if rel_path == ".":
        rel_path = path.name
    return f"{rel_path}{separator}{suffix}"


def write_format_report(task_dir: Path) -> dict[str, Any]:
    """扫描 TASK/original_sessions，写入 TASK/report/json_format_report.md。

    返回 {status, checked, issues, report}
    """
    source_dir = task_dir / DEFAULT_SOURCE
    report_dir = task_dir / REPORT_DIR_NAME
    report_dir.mkdir(parents=True, exist_ok=True)

    archive_result = {"archives": 0, "extracted": 0, "skipped": 0}
    if source_dir.is_dir():
        archive_result = extract_session_archives_if_needed(source_dir)

    files: list[Path] = []
    if source_dir.is_dir():
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}:
                files.append(path)

    issues: list[str] = []
    source_dirs = [source_dir] if source_dir.is_dir() else []
    for sd in source_dirs:
        issues.extend(_check_source_layout(sd))
    for path in files:
        issues.extend(_check_file(path, _source_dir_for_path(path, source_dirs)))

    lines = [
        "# JSON Format Report",
        "",
        f"TASK: `{task_dir.name}`",
        f"CHECK_PATH: `{source_dir}`",
        f"OUTPUT: `{task_dir}`",
        f"REPORT_DIR: `{report_dir}`",
        "Mode: `claude-code-session`",
        "",
    ]
    if archive_result["archives"]:
        lines.extend([
            "## Archive Extraction",
            "",
            f"- archives: {archive_result['archives']}",
            f"- extracted: {archive_result['extracted']}",
            f"- skipped: {archive_result['skipped']}",
            "",
        ])

    report_path = report_dir / DEFAULT_FORMAT_REPORT
    report_text = ""
    base = task_dir
    if not files:
        status = "skipped"
    elif issues:
        status = "failed"
        lines.extend(["## FAIL", ""])
        for issue in issues:
            lines.append(f"- {_rel(base, issue)}")
        report_text = "\n".join(lines) + "\n"
        if report_path.is_file():
            report_text = report_path.read_text(encoding="utf-8").rstrip() + "\n\n---\n\n" + report_text
        report_path.write_text(report_text, encoding="utf-8")
    else:
        status = "passed"

    return {
        "status": status,
        "checked": len(files),
        "issues": issues,
        "report": str(report_path) if report_text else "",
        "archive_extraction": archive_result,
    }


# ============================================================================
# 第二阶段：JSONL 精准修复（替代 repair_session_format.py）
# ============================================================================

def split_concatenated_objects(line: str) -> list[str] | None:
    parts: list[str] = []
    start = 0
    depth = 0
    in_string = False
    for index, char in enumerate(line):
        if char == '"' and not _is_escaped(line, index):
            in_string = not in_string
        if in_string:
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                part = line[start: index + 1].strip()
                if part:
                    parts.append(part)
                start = index + 1
    tail = line[start:].strip()
    if tail or len(parts) <= 1:
        return None
    if all(_loads_ok(part) for part in parts):
        return parts
    return None


def _escape_quote_candidates(line: str, error_pos: int) -> list[str]:
    candidates: list[str] = []
    quote_indexes = [i for i, char in enumerate(line) if char == '"' and not _is_escaped(line, i)]
    near = sorted(quote_indexes, key=lambda i: abs(i - error_pos))[:20]
    for index in near + quote_indexes:
        if index <= 0:
            continue
        candidate = f"{line[:index]}\\{line[index:]}"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def escape_bare_quotes_by_context(line: str) -> str:
    result: list[str] = []
    in_string = False
    index = 0
    while index < len(line):
        char = line[index]
        if char != '"' or _is_escaped(line, index):
            result.append(char)
            index += 1
            continue
        if not in_string:
            in_string = True
            result.append(char)
            index += 1
            continue
        next_index = index + 1
        while next_index < len(line) and line[next_index] in " \t\r\n":
            next_index += 1
        next_char = line[next_index] if next_index < len(line) else ""
        if next_char in {":", ",", "}", "]", ""}:
            in_string = False
            result.append(char)
        else:
            result.append('\\"')
        index += 1
    return "".join(result)


def repair_line(line: str) -> tuple[list[str] | None, str]:
    stripped = line.rstrip("\n\r")
    if not stripped.strip():
        return [stripped], "blank"
    if _loads_ok(stripped):
        return [stripped], "already-valid"
    if stripped.startswith("﻿") and _loads_ok(stripped.lstrip("﻿")):
        return [stripped.lstrip("﻿")], "remove-bom"
    split = split_concatenated_objects(stripped)
    if split:
        return split, "split-concatenated-json"
    if stripped.endswith(",") and _loads_ok(stripped[:-1]):
        return [stripped[:-1]], "remove-trailing-comma"
    try:
        json.loads(stripped)
    except json.JSONDecodeError as exc:
        contextual = escape_bare_quotes_by_context(stripped)
        if contextual != stripped and _loads_ok(contextual):
            return [contextual], "escape-bare-quotes-by-context"
        for candidate in _escape_quote_candidates(stripped, exc.pos):
            if _loads_ok(candidate):
                return [candidate], "escape-unescaped-quote"
    return None, "unresolved"


def repair_line_iteratively(line: str, max_steps: int = 200) -> tuple[str | None, str]:
    current = line.rstrip("\n\r")
    methods: list[str] = []
    for _ in range(max_steps):
        try:
            json.loads(current)
            return current, "+".join(methods) if methods else "already-valid"
        except json.JSONDecodeError as exc:
            fixed, method = repair_line(current)
            if fixed and len(fixed) == 1 and fixed[0] != current:
                current = fixed[0]
                methods.append(method)
                continue
            candidates = _escape_quote_candidates(current, exc.pos)
            applied = False
            for candidate in candidates[:8]:
                if candidate == current:
                    continue
                try:
                    json.loads(candidate)
                    return candidate, "+".join(methods + ["escape-near-error-quote"])
                except json.JSONDecodeError as next_exc:
                    if next_exc.pos > exc.pos:
                        current = candidate
                        methods.append("escape-near-error-quote")
                        applied = True
                        break
            if applied:
                continue
            return None, "unresolved"
    return None, "max-steps-exceeded"


def escape_invalid_json_backslashes(text: str) -> str:
    valid = {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}
    result: list[str] = []
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                if char in valid:
                    result.append("\\")
                    result.append(char)
                else:
                    result.append("\\\\")
                    result.append(char)
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
                result.append(char)
            else:
                result.append(char)
        else:
            result.append(char)
            if char == '"':
                in_string = True
    if escape:
        result.append("\\\\")
    return "".join(result)


def extract_json_values_from_text(text: str) -> list[str]:
    cleaned = text.replace("\x00", "")
    values: list[str] = []
    decoder = json.JSONDecoder()
    anchors = ['{"parentUuid"', '{"type":"queue-operation"', '{"type":"file-history-snapshot"']
    starts: list[int] = []
    for anchor in anchors:
        pos = cleaned.find(anchor)
        while pos != -1:
            starts.append(pos)
            pos = cleaned.find(anchor, pos + 1)
    for index in sorted(set(starts)):
        try:
            value, end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if _is_session_level_value(value):
            values.append(cleaned[index: index + end])
    return values


def repair_nul_or_embedded_json_values(text: str) -> tuple[list[str] | None, str]:
    if "\x00" not in text and text.count('{"parentUuid"') <= 1 and text.count('{"type":"queue-operation"') <= 1:
        return None, "not-applicable"
    values = extract_json_values_from_text(text)
    if values and all(_loads_ok(value) for value in values):
        return values, "extract-json-values-from-nul-or-embedded-text"
    return None, "unresolved"


def repair_missing_tool_result_content(text: str) -> tuple[str | None, str]:
    marker = '"type":"tool_result",'
    pos = text.find(marker)
    if pos == -1:
        return None, "not-applicable"
    insert_pos = pos + len(marker)
    suffix = text[insert_pos:]
    tail_pos = suffix.find('}]}')
    if tail_pos == -1:
        return None, "unresolved"
    raw_content = suffix[:tail_pos].strip()
    tail = suffix[tail_pos:]
    if raw_content.startswith('"content"'):
        return None, "not-applicable"
    candidate = text[:insert_pos] + '"content":' + json.dumps(raw_content, ensure_ascii=False) + tail
    if _loads_ok(candidate):
        return candidate, "add-missing-tool-result-content"
    return None, "unresolved"


def is_plain_text_jsonl_garbage(text: str) -> bool:
    """Return True for non-empty JSONL garbage that should be dropped."""
    stripped = text.strip()
    if not stripped or "\x00" in text:
        return False
    if stripped[0] in "{[":
        return False
    return True


def close_truncated_tail_json(text: str) -> tuple[str | None, str]:
    if not text.lstrip().startswith('{"parentUuid"'):
        return None, "not-applicable"
    current = text.rstrip("\n\r")
    for suffix in [
        '"}]} ,"stop_reason":null,"stop_sequence":null,"stop_details":null,"usage":{}} ,"type":"assistant"}',
        '"}]} ,"stop_reason":null,"stop_sequence":null,"usage":{}} ,"type":"assistant"}',
        '"}]} ,"type":"assistant"}',
    ]:
        candidate = current + suffix.replace(" ", "")
        if _loads_ok(candidate):
            return candidate, "close-truncated-tail-json"

    in_string = False
    escape = False
    stack: list[str] = []
    for char in current:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]" and stack and stack[-1] == char:
            stack.pop()
    candidate = current + ('"' if in_string else "") + "".join(reversed(stack))
    if _loads_ok(candidate):
        return candidate, "close-truncated-tail-json"
    return None, "unresolved"


def repair_encoding_file(path: Path) -> tuple[str | None, str]:
    data = path.read_bytes()
    encodings = ["utf-16", "utf-16-le", "utf-16-be", "utf-8-sig"]
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            json.loads(text)
            return text, f"transcode-{encoding}-to-utf-8"
        except json.JSONDecodeError:
            continue
    return None, "encoding-unresolved"


def _verify_lines(lines: list[str]) -> list[str]:
    issues: list[str] = []
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"{line_no}: {exc.msg}")
    return issues


def rebuild_jsonl_records_by_state(lines: list[str]) -> tuple[list[str] | None, list[dict[str, Any]]]:
    text = "\n".join(lines)
    records: list[str] = []
    repairs: list[dict[str, Any]] = []
    buffer: list[str] = []
    depth = 0
    in_string = False
    escape = False
    line_no = 1
    record_start_line = 0
    record_had_newline = False
    record_had_quote_fix = False
    index = 0

    def next_nonspace(pos: int) -> str:
        pos += 1
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        return text[pos] if pos < len(text) else ""

    while index < len(text):
        char = text[index]

        if depth == 0 and not buffer:
            if char in " \t\r\n":
                if char == "\n":
                    line_no += 1
                index += 1
                continue
            if char != "{":
                return None, []
            record_start_line = line_no
            record_had_newline = False
            record_had_quote_fix = False

        if char == "\n":
            if in_string:
                buffer.append("\\n")
                record_had_newline = True
            elif depth > 0:
                record_had_newline = True
            line_no += 1
            index += 1
            continue

        if in_string:
            if escape:
                buffer.append(char)
                escape = False
            elif char == "\\":
                buffer.append(char)
                escape = True
            elif char == '"':
                following = next_nonspace(index)
                if following in {":", ",", "}", "]", ""}:
                    buffer.append(char)
                    in_string = False
                else:
                    buffer.append('\\"')
                    record_had_quote_fix = True
            else:
                buffer.append(char)
            index += 1
            continue

        buffer.append(char)
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                record = "".join(buffer).strip()
                fixed_record = record
                method_parts = []
                if record_had_newline:
                    method_parts.append("state-join-multiline-jsonl")
                if record_had_quote_fix:
                    method_parts.append("escape-bare-quotes-by-state")
                if not _loads_ok(fixed_record):
                    fixed, method = repair_line(fixed_record)
                    if not fixed or len(fixed) != 1 or not _loads_ok(fixed[0]):
                        return None, []
                    fixed_record = fixed[0]
                    method_parts.append(method)
                records.append(fixed_record)
                if method_parts:
                    repairs.append({
                        "line_no": record_start_line,
                        "end_line_no": line_no,
                        "method": "+".join(method_parts),
                    })
                buffer = []
        index += 1

    if buffer or depth != 0 or in_string:
        return None, []
    if repairs and not _verify_lines(records):
        return None, []
    if repairs:
        return records, repairs
    return None, []


def repair_reported_multiline_jsonl_records(
    lines: list[str], file_issues: list[Issue]
) -> tuple[list[str] | None, list[dict[str, Any]]]:
    max_join_lines = 200
    start_candidates = sorted(
        issue.line_no
        for issue in file_issues
        if "Unterminated string" in issue.message or "Unterminated" in issue.message
    )
    if not start_candidates:
        return None, []

    repaired = list(lines)
    offset = 0
    repairs: list[dict[str, Any]] = []
    for start_line in start_candidates:
        start = start_line - 1 + offset
        if start < 0 or start >= len(repaired) or _loads_ok(repaired[start]):
            continue
        accumulator = repaired[start]
        fixed_record: str | None = None
        end = start
        for cursor in range(start + 1, len(repaired)):
            if cursor - start > max_join_lines:
                break
            accumulator = f"{accumulator}\\n{repaired[cursor]}"
            if _loads_ok(accumulator):
                fixed_record = accumulator
                end = cursor
                break
            fixed, method = repair_line(accumulator)
            if fixed and len(fixed) == 1 and _loads_ok(fixed[0]):
                fixed_record = fixed[0]
                end = cursor
                repairs.append({
                    "line_no": start_line,
                    "end_line_no": start_line + (cursor - start),
                    "method": f"join-reported-multiline-jsonl+{method}",
                })
                break
        if fixed_record is None:
            continue
        repaired[start: end + 1] = [fixed_record]
        if not repairs or repairs[-1].get("line_no") != start_line:
            repairs.append({
                "line_no": start_line,
                "end_line_no": start_line + (end - start),
                "method": "join-reported-multiline-jsonl",
            })
        offset -= end - start

    if repairs and not _verify_lines(repaired):
        return repaired, repairs
    if repairs:
        return repaired, repairs
    return None, []


def repair_first_root_issue_block(
    lines: list[str], file_issues: list[Issue]
) -> tuple[list[str] | None, list[dict[str, Any]]]:
    root_issues = sorted(file_issues, key=lambda issue: issue.line_no)
    if not root_issues:
        return None, []
    start_line = root_issues[0].line_no
    if not (
        "Unterminated" in root_issues[0].message
        or "Expecting ',' delimiter" in root_issues[0].message
        or "Expecting ':' delimiter" in root_issues[0].message
        or "Expecting value" in root_issues[0].message
        or "Expecting property name enclosed in double quotes" in root_issues[0].message
    ):
        return None, []
    start = start_line - 1
    if start < 0 or start >= len(lines):
        return None, []

    end = len(lines)
    for cursor in range(start + 1, len(lines)):
        if not lines[cursor].startswith('{"parentUuid"'):
            continue
        try:
            json.loads(lines[cursor])
            end = cursor
            break
        except json.JSONDecodeError:
            continue

    candidate = "\\n".join(lines[start:end])
    extracted, extracted_method = repair_nul_or_embedded_json_values(candidate)
    if extracted:
        repaired = lines[:start] + extracted + lines[end:]
        remaining = _verify_lines(repaired)
        if len(remaining) < len(file_issues):
            return repaired, [{"line_no": start_line, "end_line_no": end, "method": extracted_method}]

    tool_fixed, tool_method = repair_missing_tool_result_content(candidate)
    if tool_fixed:
        repaired = lines[:start] + [tool_fixed] + lines[end:]
        remaining = _verify_lines(repaired)
        if len(remaining) < len(file_issues):
            return repaired, [{"line_no": start_line, "end_line_no": end, "method": tool_method}]

    if is_plain_text_jsonl_garbage(candidate):
        repaired = lines[:start] + lines[end:]
        remaining = _verify_lines(repaired)
        if len(remaining) < len(file_issues):
            return repaired, [{"line_no": start_line, "end_line_no": end, "method": "drop-plain-text-jsonl-record"}]

    closed, close_method = close_truncated_tail_json(candidate)
    if closed:
        repaired = lines[:start] + [closed] + lines[end:]
        remaining = _verify_lines(repaired)
        if len(remaining) < len(file_issues):
            return repaired, [{"line_no": start_line, "end_line_no": end, "method": close_method}]

    fixed, method = repair_line_iteratively(candidate)
    if fixed is None:
        return None, []
    repaired = lines[:start] + [fixed] + lines[end:]
    if _verify_lines(repaired):
        return repaired, [{"line_no": start_line, "end_line_no": end, "method": f"root-cause-block-join+{method}"}]
    return repaired, [{
        "line_no": start_line,
        "end_line_no": end,
        "method": f"root-cause-block-join+{method}+needs-next-iteration",
    }]


def quarantine_first_root_issue_block(
    task_path: Path,
    path: Path,
    lines: list[str],
    file_issues: list[Issue],
) -> tuple[list[str] | None, list[dict[str, Any]]]:
    root_issues = sorted(file_issues, key=lambda issue: issue.line_no)
    if not root_issues:
        return None, []
    if not ("Unterminated" in root_issues[0].message or "Expecting" in root_issues[0].message):
        return None, []
    start_line = root_issues[0].line_no
    start = start_line - 1
    if start < 0 or start >= len(lines):
        return None, []
    end = len(lines)
    for cursor in range(start + 1, len(lines)):
        if not lines[cursor].startswith('{"parentUuid"'):
            continue
        try:
            json.loads(lines[cursor])
            end = cursor
            break
        except json.JSONDecodeError:
            continue
    repaired = lines[:start] + lines[end:]
    remaining = _verify_lines(repaired)
    if remaining:
        return None, []
    quarantine_path = task_path / ".tmp" / "quarantined_jsonl_records.jsonl"
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    with quarantine_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "task": task_path.name,
            "file": path.relative_to(task_path).as_posix(),
            "line_no": start_line,
            "end_line_no": end,
            "reason": "unresolved root JSONL block quarantined",
            "content": "\n".join(lines[start:end]),
        }, ensure_ascii=False) + "\n")
    return repaired, [{
        "line_no": start_line,
        "end_line_no": end,
        "method": "quarantine-unresolved-root-block",
    }]


# ----------------------------------------------------------------------------
# 报告解析与重写（替代 read_report / _prepare_cli_format_report）
# ----------------------------------------------------------------------------

def _rewrite_report_issue_path(path_text: str, task_path: Path, source: Path) -> str:
    path = Path(path_text)
    if path.is_absolute():
        try:
            relative = path.relative_to(task_path)
        except ValueError:
            try:
                relative = path.relative_to(task_path / DEFAULT_SOURCE)
                relative = DEFAULT_SOURCE / relative
            except ValueError:
                relative = Path(path.name)
    else:
        relative = path

    if relative.parts and relative.parts[0] in {DEFAULT_SOURCE.name, FIX_SOURCE.name}:
        return Path(source, *relative.parts[1:]).as_posix()
    return Path(source, relative).as_posix()


def _prepare_cli_format_report(task_path: Path, source: Path) -> Path:
    """读取 report/json_format_report.md，重写路径后写到 .tmp/CLI_FORMAT_REPORT。"""
    report_path = task_path / REPORT_DIR_NAME / DEFAULT_FORMAT_REPORT
    if not report_path.is_file():
        raise FileNotFoundError(report_path)

    tmp = task_path / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / CLI_FORMAT_REPORT
    out_lines: list[str] = []
    for raw in report_path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("- "):
            payload = raw[2:]
            try:
                path_text, line_no, message = payload.rsplit(":", 2)
            except ValueError:
                out_lines.append(raw)
                continue
            fixed_path = _rewrite_report_issue_path(path_text, task_path, source)
            out_lines.append(f"- {fixed_path}:{line_no}: {message.strip()}")
        else:
            out_lines.append(raw)
    target.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return target


def read_report(task_dir: Path, report_name: str) -> list[Issue]:
    report = task_dir / ".tmp" / report_name
    issues: list[Issue] = []
    if not report.is_file():
        raise FileNotFoundError(report)
    issue_re = re.compile(r"^- (.*):(\d+): (.*)$")
    encoding_re = re.compile(r"^- (.*): encoding error: (.*)$")
    for raw in report.read_text(encoding="utf-8").splitlines():
        match = issue_re.match(raw)
        if match:
            path_text, line_no, message = match.groups()
            issues.append(Issue(task_dir / path_text, int(line_no), message.strip()))
            continue
        match = encoding_re.match(raw)
        if match:
            path_text, message = match.groups()
            issues.append(Issue(task_dir / path_text, 0, f"encoding error: {message.strip()}"))
    return issues


# ----------------------------------------------------------------------------
# 修复任务核心
# ----------------------------------------------------------------------------

def _copy_source(task_dir: Path, source: Path, output: Path) -> None:
    src = task_dir / source
    dst = task_dir / output
    if src.resolve() == dst.resolve():
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _backup_file(task_dir: Path, path: Path) -> Path:
    rel = path.relative_to(task_dir)
    backup = task_dir / BACKUP_DIR_NAME / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def append_quarantined_records(task_path: Path, file_path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    quarantine_path = task_path / ".tmp" / "quarantined_jsonl_records.jsonl"
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    rel = file_path.relative_to(task_path).as_posix()
    with quarantine_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps({
                "task": task_path.name,
                "file": rel,
                **record,
            }, ensure_ascii=False) + "\n")


def _write_repair_report(task_dir: Path, report_name: str, result: dict[str, Any]) -> Path:
    lines = [
        "# JSON Repair Report",
        "",
        f"TASK: `{Path(result['task']).name}`",
        f"Mode: `{'apply' if result['apply'] else 'dry-run'}`",
        f"Source: `{result['source']}`",
        f"Output: `{result['output']}`",
        f"In place: `{result['in_place']}`",
        "",
        "## Summary",
        "",
        f"- report_issues: {result['report_issues']}",
        f"- repairable_lines: {result['repairable_lines']}",
        f"- repaired_files: {result['repaired_files']}",
        f"- unresolved: {len(result['unresolved'])}",
        "",
        "## Repaired",
        "",
    ]
    if result["repairs"]:
        for repair in result["repairs"]:
            lines.append(f"- {repair['path']}:{repair['line_no']} `{repair['method']}`")
    else:
        lines.append("none")
    lines.extend(["", "## Unresolved", ""])
    if result["unresolved"]:
        lines.extend(f"- {item}" for item in result["unresolved"])
    else:
        lines.append("none")
    lines.extend(["", "## Backups", ""])
    if result["backups"]:
        lines.extend(f"- {item}" for item in result["backups"])
    else:
        lines.append("none")
    lines.extend(["", "## Quarantined", ""])
    if result.get("quarantined"):
        lines.extend(f"- {item}" for item in result["quarantined"])
    else:
        lines.append("none")
    report_path = task_dir / ".tmp" / report_name
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def _method_key(method: str) -> str:
    for key in REPAIR_CASE_DESCRIPTIONS:
        if key in method:
            return key
    if "escape-near-error-quote" in method or "escape-unescaped-quote" in method:
        return "escape-bare-quotes"
    return method


def _read_line(path: Path, line_no: int | str) -> str:
    if isinstance(line_no, int):
        start_line = end_line = line_no
    else:
        parts = str(line_no).split("-", 1)
        if not parts[0].isdigit():
            return ""
        start_line = int(parts[0])
        end_line = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else start_line
    if start_line < 1 or end_line < start_line or not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if start_line > len(lines):
        return ""
    return "\n".join(lines[start_line - 1: min(end_line, len(lines))])


def _read_quarantine_items(task_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    path = task_dir / ".tmp" / "quarantined_jsonl_records.jsonl"
    items: dict[tuple[str, int], dict[str, Any]] = {}
    if not path.is_file():
        return items
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        file = item.get("file")
        line_no = item.get("line_no")
        if isinstance(file, str) and isinstance(line_no, int):
            items[(file, line_no)] = item
    return items


def _merge_spans(spans: list[tuple[int, int]], text_len: int) -> list[tuple[int, int]]:
    if not spans:
        return []
    normalized = sorted((max(0, start), min(text_len, end)) for start, end in spans)
    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1] + 20:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _diff_snippet(original: str, repaired: str, context: int = CASE_DIFF_CONTEXT) -> tuple[str, str]:
    if not original or not repaired or original == repaired:
        return original[:4000], repaired[:4000]
    matcher = difflib.SequenceMatcher(a=original, b=repaired, autojunk=True)
    original_spans: list[tuple[int, int]] = []
    repaired_spans: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        original_spans.append((i1 - context, i2 + context))
        repaired_spans.append((j1 - context, j2 + context))

    def build(text: str, spans: list[tuple[int, int]]) -> str:
        parts: list[str] = []
        previous_end = 0
        for start, end in _merge_spans(spans, len(text)):
            if parts and start > previous_end:
                parts.append("\n...\n")
            elif start > 0 and not parts:
                parts.append("...\n")
            parts.append(text[start:end])
            previous_end = end
        if previous_end < len(text):
            parts.append("\n...")
        return "".join(parts) if parts else text[:4000]

    return build(original, original_spans), build(repaired, repaired_spans)


def _write_repair_case_report(
    task_dir: Path,
    result: dict[str, Any],
    report_name: str = DEFAULT_CASE_REPORT,
    append: bool = False,
) -> Path:
    repairs = result.get("repairs") or []
    quarantine_items = _read_quarantine_items(task_dir)
    source = Path(str(result.get("source", DEFAULT_SOURCE.as_posix())))
    output = Path(str(result.get("output", DEFAULT_OUTPUT.as_posix())))
    by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for repair in repairs:
        by_method[_method_key(str(repair.get("method", "")))].append(repair)

    report_path = task_dir / ".tmp" / report_name
    report_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# JSON Repair Case Report",
        "",
        f"Task ID: {task_dir.name}",
        f"Source: `{source.as_posix()}`",
        f"Output: `{output.as_posix()}`",
        f"Mode: `{'apply' if result.get('apply') else 'dry-run'}`",
        "",
        "Summary:",
        f"- report_issues: {result.get('report_issues', 0)}",
        f"- repaired_lines: {result.get('repairable_lines', 0)}",
        f"- repaired_files: {result.get('repaired_files', 0)}",
        f"- unresolved: {len(result.get('unresolved') or [])}",
        "",
    ]

    if not by_method:
        if append:
            return report_path
        lines.append("No JSON repair cases produced.")
    for number, (method_key, method_repairs) in enumerate(sorted(by_method.items()), 1):
        title, problem, solution = REPAIR_CASE_DESCRIPTIONS.get(
            method_key,
            ("Other JSON repair", "Located by JSON parser report; uncategorized repair type.", "Apply the matching strategy in the repair script."),
        )
        lines.extend([
            f"# Case {number} {title}",
            "",
            f"Task ID: {task_dir.name}",
            f"Problem: {problem}",
            f"Strategy: {solution}",
            f"Method: `{method_key}`",
            "",
        ])
        for repair in method_repairs:
            output_rel = Path(str(repair.get("path", "")))
            line_no = repair.get("line_no")
            if output_rel.parts[: len(output.parts)] == output.parts:
                source_rel = Path(*((source.parts) + output_rel.parts[len(output.parts):]))
            else:
                source_rel = output_rel
            original_line = _read_line(task_dir / source_rel, line_no)
            if not original_line and result.get("in_place"):
                original_line = _read_line(task_dir / BACKUP_DIR_NAME / source_rel, line_no)
            repaired_line = _read_line(task_dir / output_rel, line_no)
            key = (output_rel.as_posix(), int(line_no) if isinstance(line_no, int) else -1)
            quarantine = quarantine_items.get(key)
            if quarantine:
                original_line = str(quarantine.get("content", original_line))
                repaired_line = "Line removed from repaired JSONL; raw content kept in `.tmp/quarantined_jsonl_records.jsonl`."
                original_snippet, repaired_snippet = original_line, repaired_line
            else:
                original_snippet, repaired_snippet = _diff_snippet(original_line, repaired_line)
            lines.extend([
                f"File and line: {source_rel.as_posix()}:{line_no}",
                "",
                "Original:",
                "",
                "```text",
                original_snippet if original_snippet else "Original snippet not readable.",
                "```",
                "",
                "Repaired:",
                "",
                "```text",
                repaired_snippet if repaired_snippet else "Repaired snippet not readable (multi-line merge or drop).",
                "```",
                "",
            ])

    content = "\n".join(lines).rstrip() + "\n"
    if append and report_path.is_file():
        previous = report_path.read_text(encoding="utf-8").rstrip()
        content = previous + "\n\n---\n\n" + content
    report_path.write_text(content, encoding="utf-8")
    return report_path


def repair_task_format(
    task_dir: Path,
    source: Path = FIX_SOURCE,
    output: Path = DEFAULT_OUTPUT,
    format_report: str = CLI_FORMAT_REPORT,
    repair_report: str = DEFAULT_REPAIR_REPORT,
) -> dict[str, Any]:
    """对单个 TASK 执行 JSONL 精准修复（apply=True、非 in-place）。"""
    task_path = task_dir.resolve()
    source_path = task_path / source
    output_relative = output
    output_path = task_path / output_relative
    issues = read_report(task_path, format_report)

    result: dict[str, Any] = {
        "task": str(task_path),
        "apply": True,
        "source": source.as_posix(),
        "output": output_relative.as_posix(),
        "in_place": False,
        "report_issues": len(issues),
        "repairable_lines": 0,
        "repaired_files": 0,
        "repairs": [],
        "unresolved": [],
        "backups": [],
        "quarantined": [],
        "report": "",
        "case_report": "",
        "status": "passed",
    }

    if not source_path.is_dir():
        result["status"] = "skipped"
        result["unresolved"].append(f"missing source directory: {source.as_posix()}")
        result["report"] = str(_write_repair_report(task_path, repair_report, result))
        result["case_report"] = str(_write_repair_case_report(task_path, result))
        return result

    _copy_source(task_path, source, output_relative)

    by_file: dict[Path, list[Issue]] = defaultdict(list)
    for issue in issues:
        try:
            rel = issue.path.relative_to(task_path)
        except ValueError:
            result["unresolved"].append(f"outside task path: {issue.path}")
            continue
        if not rel.parts or rel.parts[0] != source.parts[0]:
            result["unresolved"].append(f"outside source: {rel.as_posix()}")
            continue
        target_rel = Path(*((output_relative.parts) + rel.parts[len(source.parts):]))
        by_file[task_path / target_rel].append(Issue(task_path / target_rel, issue.line_no, issue.message))

    for path, file_issues in sorted(by_file.items(), key=lambda item: str(item[0])):
        if any(issue.line_no == 0 and issue.message.startswith("encoding error") for issue in file_issues):
            read_path = path
            if not read_path.is_file():
                result["unresolved"].append(f"missing file: {read_path.relative_to(task_path).as_posix()}")
                continue
            fixed_text, method = repair_encoding_file(read_path)
            if fixed_text is None:
                result["unresolved"].append(f"encoding unresolved: {path.relative_to(task_path).as_posix()}")
                continue
            result["repaired_files"] += 1
            result["repairable_lines"] += 1
            result["repairs"].append({"path": path.relative_to(task_path).as_posix(), "line_no": 0, "method": method})
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fixed_text, encoding="utf-8")
            continue

        if path.suffix.lower() != ".jsonl":
            for issue in file_issues:
                result["unresolved"].append(f"non-jsonl unsupported: {path.relative_to(task_path).as_posix()}:{issue.line_no}")
            continue
        read_path = path
        if not read_path.is_file():
            result["unresolved"].append(f"missing file: {read_path.relative_to(task_path).as_posix()}")
            continue
        lines = read_path.read_text(encoding="utf-8").splitlines()

        multiline_lines, multiline_repairs = repair_first_root_issue_block(lines, file_issues)
        if multiline_lines is None:
            multiline_lines, multiline_repairs = rebuild_jsonl_records_by_state(lines)
        if multiline_lines is None:
            multiline_lines, multiline_repairs = repair_reported_multiline_jsonl_records(lines, file_issues)
        if multiline_lines is None:
            multiline_lines, multiline_repairs = quarantine_first_root_issue_block(task_path, path, lines, file_issues)
        if multiline_lines is not None:
            remaining = _verify_lines(multiline_lines)
            if not remaining:
                result["repaired_files"] += 1
                result["repairable_lines"] += len(multiline_repairs)
                for repair in multiline_repairs:
                    if repair["line_no"] == repair["end_line_no"]:
                        label = str(repair["line_no"])
                    else:
                        label = f"{repair['line_no']}-{repair['end_line_no']}"
                    result["repairs"].append({
                        "path": path.relative_to(task_path).as_posix(),
                        "line_no": label,
                        "method": repair["method"],
                    })
                    if repair["method"].startswith("quarantine-"):
                        result["quarantined"].append(f"{path.relative_to(task_path).as_posix()}: lines {label}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(multiline_lines) + "\n", encoding="utf-8")
                continue

        if any("Unterminated" in issue.message for issue in file_issues):
            result["unresolved"].append(
                f"unresolved multiline JSONL record: {path.relative_to(task_path).as_posix()} "
                "(state-machine rebuild failed; skipped slow per-line fallback)"
            )
            continue

        replacements: dict[int, list[str]] = {}
        methods: dict[int, str] = {}
        unresolved_indexes: set[int] = set()
        dropped_records: list[dict[str, Any]] = []
        for issue in file_issues:
            index = issue.line_no - 1
            if index < 0 or index >= len(lines):
                result["unresolved"].append(f"missing line: {path.relative_to(task_path).as_posix()}:{issue.line_no}")
                continue
            if "Invalid \\escape" in issue.message:
                candidate = escape_invalid_json_backslashes(lines[index])
                if _loads_ok(candidate):
                    fixed, method = [candidate], "escape-invalid-backslashes"
                else:
                    iterative, iterative_method = repair_line_iteratively(lines[index])
                    fixed, method = ([iterative], iterative_method) if iterative is not None else repair_line(lines[index])
            elif (
                "Expecting ',' delimiter" in issue.message
                or "Expecting ':' delimiter" in issue.message
                or "Expecting property name enclosed in double quotes" in issue.message
                or "Extra data" in issue.message
            ):
                iterative, iterative_method = repair_line_iteratively(lines[index])
                fixed, method = ([iterative], iterative_method) if iterative is not None else repair_line(lines[index])
            else:
                extracted, extracted_method = repair_nul_or_embedded_json_values(lines[index])
                if extracted:
                    fixed, method = extracted, extracted_method
                else:
                    tool_fixed, tool_method = repair_missing_tool_result_content(lines[index])
                    if tool_fixed:
                        fixed, method = [tool_fixed], tool_method
                    else:
                        if is_plain_text_jsonl_garbage(lines[index]):
                            fixed, method = [], "drop-plain-text-jsonl-record"
                            dropped_records.append({
                                "line_no": index + 1,
                                "reason": "plain text JSONL garbage dropped",
                                "method": method,
                                "content": lines[index],
                            })
                        else:
                            fixed, method = repair_line(lines[index])
            if fixed is None:
                unresolved_indexes.add(index)
                continue
            replacements[index] = fixed
            methods[index] = method

        if unresolved_indexes:
            quarantine_path = task_path / ".tmp" / "quarantined_jsonl_records.jsonl"
            quarantine_path.parent.mkdir(parents=True, exist_ok=True)
            rel = path.relative_to(task_path).as_posix()
            with quarantine_path.open("a", encoding="utf-8") as handle:
                for index in sorted(unresolved_indexes):
                    handle.write(json.dumps({
                        "task": task_path.name,
                        "file": rel,
                        "line_no": index + 1,
                        "reason": "unresolved single-line JSONL issue quarantined",
                        "content": lines[index] if 0 <= index < len(lines) else "",
                    }, ensure_ascii=False) + "\n")
            result["quarantined"].append(
                f"{rel}: lines {', '.join(str(i + 1) for i in sorted(unresolved_indexes))}"
            )

        if not replacements and not unresolved_indexes:
            continue
        new_lines: list[str] = []
        for index, line in enumerate(lines):
            if index in unresolved_indexes:
                continue
            new_lines.extend(replacements.get(index, [line]))
        remaining = _verify_lines(new_lines)
        if remaining:
            for item in remaining[:20]:
                result["unresolved"].append(f"postcheck: {path.relative_to(task_path).as_posix()}:{item}")
            continue

        result["repaired_files"] += 1
        result["repairable_lines"] += len(replacements)
        for index in sorted(replacements):
            result["repairs"].append({
                "path": path.relative_to(task_path).as_posix(),
                "line_no": index + 1,
                "method": methods[index],
            })
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        append_quarantined_records(task_path, path, dropped_records)

    if result["unresolved"]:
        result["status"] = "partial" if result["repairable_lines"] else "failed"
    else:
        result["status"] = "repaired" if result["repairable_lines"] else "passed"
    result["report"] = str(_write_repair_report(task_path, repair_report, result))
    result["case_report"] = str(_write_repair_case_report(task_path, result))
    return result


def _move_cli_report(path_text: str, report_dir: Path) -> str:
    path = Path(path_text)
    if not path.is_file():
        return path_text
    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / path.name
    if target.exists():
        target.unlink()
    shutil.move(str(path), str(target))
    return str(target)


def _prepare_fix_source(task_path: Path) -> Path:
    """把 TASK/original_sessions 重命名为 TASK/fix_original_sessions（如果还没重命名）。"""
    original = task_path / DEFAULT_SOURCE
    fixed = task_path / FIX_SOURCE
    if fixed.is_dir():
        return FIX_SOURCE
    if original.is_dir():
        original.rename(fixed)
        return FIX_SOURCE
    raise FileNotFoundError(f"missing source directory: {original}")


def run_repair_for_task(task_dir: Path) -> dict[str, Any]:
    """完整修复流程：准备 fix_original_sessions → 重写报告路径 → repair → 把报告 move 到 report/。"""
    source = _prepare_fix_source(task_dir)
    format_report_path = _prepare_cli_format_report(task_dir, source)
    result = repair_task_format(
        task_dir,
        source=source,
        output=DEFAULT_SOURCE,
        format_report=format_report_path.name,
        repair_report=DEFAULT_REPAIR_REPORT,
    )
    report_dir = task_dir / REPORT_DIR_NAME
    result["report"] = _move_cli_report(result["report"], report_dir)
    result["case_report"] = _move_cli_report(result["case_report"], report_dir)
    try:
        format_report_path.unlink()
    except OSError:
        pass
    return result


# ============================================================================
# 第三阶段：编码错误兜底修复
# ============================================================================

def scan_task_jsonl(task_dir: Path) -> dict[str, Any]:
    """扫描 TASK/original_sessions 下的 .jsonl，返回当前状态摘要。"""
    orig = task_dir / DEFAULT_SOURCE
    if not orig.is_dir():
        return {"status": "no_source", "errors": [], "total_files": 0, "total_lines": 0}

    errors: list[dict[str, Any]] = []
    total_files = 0
    total_lines = 0

    for jsonl_path in sorted(orig.rglob("*.jsonl")):
        total_files += 1
        try:
            with jsonl_path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    total_lines += 1
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as exc:
                        errors.append({
                            "file": jsonl_path.relative_to(orig).as_posix(),
                            "line": line_no,
                            "error": exc.msg,
                            "type": "parse",
                        })
                        break
        except UnicodeDecodeError as exc:
            errors.append({
                "file": jsonl_path.relative_to(orig).as_posix(),
                "line": 0,
                "error": f"encoding error: {exc}",
                "type": "encoding",
            })

    if total_files > 0 and total_lines == 0:
        return {"status": "all_blank", "errors": errors, "total_files": total_files, "total_lines": total_lines}
    if errors:
        return {"status": "has_errors", "errors": errors, "total_files": total_files, "total_lines": total_lines}
    return {"status": "ok", "errors": [], "total_files": total_files, "total_lines": total_lines}


def fix_encoding_errors(task_dir: Path) -> tuple[int, list[str]]:
    """对无法以 UTF-8 读取的 .jsonl 文件用 errors='replace' 兜底重编码。"""
    orig = task_dir / DEFAULT_SOURCE
    fixed = 0
    details: list[str] = []
    if not orig.is_dir():
        return 0, []

    for jsonl_path in sorted(orig.rglob("*.jsonl")):
        try:
            with jsonl_path.open("r", encoding="utf-8") as handle:
                handle.read()
            continue
        except UnicodeDecodeError:
            pass

        raw = jsonl_path.read_bytes()
        text = raw.decode("utf-8", errors="replace")

        valid = True
        bad_lines: list[int] = []
        for line_no, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    valid = False
                    bad_lines.append(line_no)
                    break

        rel = jsonl_path.relative_to(orig).as_posix()
        if valid:
            jsonl_path.write_text(text, encoding="utf-8")
            details.append(f"  {rel}: UTF-8 re-encoded with replace ({len(raw)} bytes)")
            fixed += 1
        else:
            details.append(f"  {rel}: encoding error persists (invalid JSON at lines {bad_lines})")

    return fixed, details


# ============================================================================
# 函数 a：检查并修复 JSON / JSONL（一个 TASK）
# ============================================================================

def check_and_repair_json(task_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    """函数 a：检查并修复一个 TASK 下 original_sessions 中的 JSON / JSONL。

    流程：扫描 → 写格式报告 → 重命名为 fix_original_sessions → 精准修复 →
    编码兜底 → 二次扫描验证。返回包含 status / message / 详细数据的 dict。
    """
    result: dict[str, Any] = {
        "task": task_dir.name,
        "status": "pending",
        "check_report_generated": False,
        "repair_result": None,
        "encoding_fixed": 0,
        "encoding_details": [],
        "final_scan": {},
        "message": "",
    }

    scan = scan_task_jsonl(task_dir)
    if scan["status"] == "no_source":
        result["status"] = "skipped"
        result["message"] = "missing original_sessions directory"
        return result

    if scan["status"] == "ok":
        result["status"] = "ok"
        result["message"] = f"no repair needed ({scan['total_files']} files, {scan['total_lines']} valid lines)"
        return result

    if scan["status"] == "all_blank":
        if dry_run:
            result["status"] = "dry_run"
            result["message"] = f"[DRY-RUN] {scan['total_files']} JSONL files are all blank"
            return result
        result["status"] = "data_lost"
        result["message"] = f"data lost — {scan['total_files']} JSONL files contain no records"
        return result

    if dry_run:
        result["status"] = "dry_run"
        result["message"] = f"[DRY-RUN] {len(scan['errors'])} files have issues, repair not run"
        result["initial_scan"] = scan
        return result

    # 1. 写入 format report（无论是否已存在都重新生成，以反映最新状态）
    check_out = write_format_report(task_dir)
    result["check_report_generated"] = bool(check_out["report"]) or check_out["status"] != "failed"

    # 2. 执行精准修复
    try:
        repair_out = run_repair_for_task(task_dir)
        result["repair_result"] = {
            "returncode": 0 if repair_out["status"] in {"repaired", "passed"} else 1,
            "stdout": (
                f"{repair_out['status'].upper()}: {Path(repair_out['task']).name} "
                f"files={repair_out['repaired_files']} lines={repair_out['repairable_lines']} "
                f"report={repair_out['report']} case_report={repair_out['case_report']}"
            ),
            "stderr": "",
            "detail": repair_out,
        }
    except FileNotFoundError as exc:
        result["repair_result"] = {"returncode": 2, "stdout": "", "stderr": f"missing input: {exc}"}

    # 3. 编码兜底修复
    enc_fixed, enc_details = fix_encoding_errors(task_dir)
    result["encoding_fixed"] = enc_fixed
    result["encoding_details"] = enc_details

    # 4. 最终验证
    final_scan = scan_task_jsonl(task_dir)
    result["final_scan"] = final_scan
    if final_scan["status"] == "ok":
        result["status"] = "repaired"
        result["message"] = f"repaired ({final_scan['total_files']} files, {final_scan['total_lines']} valid lines)"
    elif final_scan["status"] == "all_blank":
        result["status"] = "data_lost"
        result["message"] = f"data still lost — {final_scan['total_files']} JSONL files are all blank"
    else:
        result["status"] = "partial"
        result["message"] = f"partially repaired, {len(final_scan['errors'])} files still have issues"

    return result


# ============================================================================
# 函数 b：检查并删除中文字符（一个 TASK）
# ============================================================================

def read_text_safely(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def is_original_sessions_memory_exempt_path(root: Path, path: Path) -> bool:
    """豁免 original_sessions/<bundle>/memory[/...] 与 .../memory.jsonl。"""
    try:
        rel_parts = [part.lower() for part in path.resolve().relative_to(root).parts]
    except ValueError:
        return False
    if (
        len(rel_parts) >= 3
        and rel_parts[0] == ORIGINAL_SESSIONS_DIR_NAME
        and rel_parts[2] == "memory"
    ):
        return True
    if (
        len(rel_parts) == 3
        and rel_parts[0] == ORIGINAL_SESSIONS_DIR_NAME
        and rel_parts[2] == "memory.jsonl"
    ):
        return True
    return False


def should_skip_text_dir(dirname: str) -> bool:
    lower = dirname.lower()
    if lower in {".git", ".tmp", ".backup"}:
        return True
    if re.fullmatch(r"build-.*", lower):
        return True
    if lower in RUNTIME_NOISE_DIR_NAMES:
        return True
    if lower in ENGLISH_CHECK_EXCLUDED_DIRS:
        return True
    return False


def should_skip_text_file(path: Path) -> bool:
    lower_name = path.name.lower()
    if lower_name in ENGLISH_CHECK_EXCLUDED_FILES:
        return True
    if lower_name == "validation_report.md":
        return True
    if path.suffix.lower() in SKIP_LANGUAGE_CHECK_EXTS:
        return True
    if lower_name in RUNTIME_NOISE_FILE_NAMES:
        return True
    if path.suffix.lower() in RUNTIME_NOISE_FILE_SUFFIXES:
        return True
    return False


def read_gitignore_entries(gitignore_path: Path) -> list[str]:
    content = read_text_safely(gitignore_path)
    if content is None:
        return []
    entries: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def parse_gitignore_entry(entry: str) -> tuple[str, bool]:
    text = entry.strip()
    if not text:
        return "", False
    negated = text.startswith("!")
    if negated:
        text = text[1:].strip()
    if not text or text.startswith("#"):
        return "", negated
    return text, negated


def gitignore_pattern_matches_path(pattern: str, rel_posix_path: str) -> bool:
    pat = pattern.strip().replace("\\", "/")
    if not pat:
        return False

    anchored = pat.startswith("/")
    if anchored:
        pat = pat.lstrip("/")

    dir_only = pat.endswith("/")
    if dir_only:
        pat = pat.rstrip("/")
    if not pat:
        return False

    path_parts = rel_posix_path.split("/")
    file_name = path_parts[-1]
    dir_prefixes = ["/".join(path_parts[:idx]) for idx in range(1, len(path_parts))]

    if dir_only:
        if anchored:
            return any(fnmatch.fnmatch(prefix, pat) for prefix in dir_prefixes)
        if "/" in pat:
            return any(
                fnmatch.fnmatch(prefix, pat) or fnmatch.fnmatch(prefix, f"**/{pat}")
                for prefix in dir_prefixes
            )
        return any(
            any(fnmatch.fnmatch(seg, pat) for seg in prefix.split("/"))
            for prefix in dir_prefixes
        )

    if anchored:
        return fnmatch.fnmatch(rel_posix_path, pat)
    if "/" in pat:
        return fnmatch.fnmatch(rel_posix_path, pat) or fnmatch.fnmatch(rel_posix_path, f"**/{pat}")
    return any(fnmatch.fnmatch(seg, pat) for seg in path_parts) or fnmatch.fnmatch(file_name, pat)


def get_gitignore_scopes(root: Path) -> list[tuple[Path, list[str]]]:
    scopes: list[tuple[Path, list[str]]] = []
    seen: set[Path] = set()
    candidates = [root / ".gitignore"]
    for gitignore_path in candidates:
        gitignore_path = gitignore_path.resolve()
        if gitignore_path in seen:
            continue
        seen.add(gitignore_path)
        if not gitignore_path.is_file():
            continue
        entries = read_gitignore_entries(gitignore_path)
        if entries:
            scopes.append((gitignore_path.parent, entries))
    return scopes


def is_ignored_by_any_gitignore(root: Path, path: Path, treat_as_dir: bool = False) -> bool:
    scopes = get_gitignore_scopes(root)
    if not scopes:
        return False

    matched_scopes: list[tuple[int, str, list[str]]] = []
    for base_dir, entries in scopes:
        try:
            rel = path.relative_to(base_dir).as_posix()
        except ValueError:
            continue
        if rel in {"", "."}:
            continue
        depth = len(base_dir.parts)
        matched_scopes.append((depth, rel, entries))

    if not matched_scopes:
        return False

    matched_scopes.sort(key=lambda item: item[0])

    ignored = False
    for _, rel, entries in matched_scopes:
        rel_to_match = rel
        if treat_as_dir:
            rel_to_match = f"{rel}/.codex_ignore_probe"
        for raw_entry in entries:
            pattern, negated = parse_gitignore_entry(raw_entry)
            if not pattern:
                continue
            if gitignore_pattern_matches_path(pattern, rel_to_match):
                ignored = not negated
    return ignored


def iter_readable_text_files(start_dir: Path, root: Path) -> Iterable[tuple[Path, str]]:
    for current_root, dirs, files in os.walk(start_dir, topdown=True):
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        current_path = Path(current_root)

        pruned_dirs: list[str] = []
        for dirname in dirs:
            if should_skip_text_dir(dirname):
                continue
            dir_path = current_path / dirname
            if is_original_sessions_memory_exempt_path(root, dir_path):
                continue
            if is_ignored_by_any_gitignore(root, dir_path, treat_as_dir=True):
                continue
            pruned_dirs.append(dirname)
        dirs[:] = pruned_dirs

        for filename in files:
            path = current_path / filename
            if is_original_sessions_memory_exempt_path(root, path):
                continue
            if should_skip_text_file(path):
                continue
            if is_ignored_by_any_gitignore(root, path):
                continue
            text = read_text_safely(path)
            if text is None:
                continue
            yield path, text


def find_chinese_line_numbers(content: str) -> list[int]:
    return [idx for idx, line in enumerate(content.splitlines(), 1) if CHINESE_RE.search(line)]


def remove_chinese_chars(content: str) -> str:
    return CHINESE_RE.sub("", content)


def format_line_numbers(line_numbers: list[int]) -> str:
    if not line_numbers:
        return ""
    if len(line_numbers) <= 20:
        return ", ".join(str(n) for n in line_numbers)
    head = ", ".join(str(n) for n in line_numbers[:20])
    return f"{head} ... total {len(line_numbers)} lines"


def check_and_remove_chinese(task_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    """函数 b：检查并删除一个 TASK 下 original_sessions 内文本文件中的中文字符。

    遍历 original_sessions（豁免 memory/、二进制文件、运行时噪声、被 .gitignore 忽略的路径），
    对每个文本文件查找 [一-鿿] 中文字符，若命中则删除（dry-run 模式只报告）。
    """
    checked = 0
    found = 0
    cleaned = 0
    failed = 0
    details: list[str] = []

    orig_sessions = task_dir / ORIGINAL_SESSIONS_DIR_NAME
    if not orig_sessions.is_dir():
        return {
            "status": "skipped",
            "checked": 0, "found": 0, "cleaned": 0, "failed": 0,
            "details": [f"  {MARK_SKIP} missing {ORIGINAL_SESSIONS_DIR_NAME} directory"],
        }

    for path, content in iter_readable_text_files(orig_sessions, task_dir):
        checked += 1
        line_numbers = find_chinese_line_numbers(content)
        if line_numbers:
            found += 1
            rel = path.relative_to(task_dir).as_posix()
            formatted = format_line_numbers(line_numbers)
            details.append(f"  [FOUND] {rel} | lines: {formatted}")
            if not dry_run:
                cleaned_content = remove_chinese_chars(content)
                try:
                    path.write_text(cleaned_content, encoding="utf-8")
                    details.append(f"  [CLEANED] {rel} | chinese chars removed")
                    cleaned += 1
                except Exception as exc:
                    details.append(f"  [FAILED] {rel} | write failed: {exc}")
                    failed += 1

    if found == 0:
        status = "ok"
    elif dry_run:
        status = "found"
    elif cleaned > 0:
        status = "cleaned"
    else:
        status = "failed"

    return {
        "status": status,
        "checked": checked, "found": found,
        "cleaned": cleaned, "failed": failed,
        "details": details,
    }


# ============================================================================
# 函数 c：检查并删除 trajectory 中的文件名关键字（一个 TASK）
# ============================================================================

def find_keyword_line_numbers(content: str) -> tuple[list[int], dict[str, int]]:
    """返回（命中行号列表，关键字命中次数 dict）。"""
    line_numbers: list[int] = []
    counts: dict[str, int] = {}
    for idx, line in enumerate(content.splitlines(), 1):
        hits = KEYWORD_REMOVAL_RE.findall(line)
        if hits:
            line_numbers.append(idx)
            for hit in hits:
                counts[hit] = counts.get(hit, 0) + 1
    return line_numbers, counts


def remove_keywords(content: str) -> str:
    return KEYWORD_REMOVAL_RE.sub("", content)


def check_and_remove_keywords(task_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    """函数 c：检查并删除 trajectory 中提到的文件名关键字。

    扫描 original_sessions 内文本文件，若文件内容含有 KEYWORD_REMOVALS 中
    任何一个关键字，则将其从文件内容中删除（dry-run 模式只报告）。
    """
    checked = 0
    found = 0
    cleaned = 0
    failed = 0
    total_hits = 0
    keyword_totals: dict[str, int] = {}
    details: list[str] = []

    orig_sessions = task_dir / ORIGINAL_SESSIONS_DIR_NAME
    if not orig_sessions.is_dir():
        return {
            "status": "skipped",
            "checked": 0, "found": 0, "cleaned": 0, "failed": 0,
            "total_hits": 0, "keyword_totals": {},
            "details": [f"  {MARK_SKIP} missing {ORIGINAL_SESSIONS_DIR_NAME} directory"],
        }

    for path, content in iter_readable_text_files(orig_sessions, task_dir):
        checked += 1
        line_numbers, counts = find_keyword_line_numbers(content)
        if not line_numbers:
            continue
        found += 1
        hits_in_file = sum(counts.values())
        total_hits += hits_in_file
        for k, v in counts.items():
            keyword_totals[k] = keyword_totals.get(k, 0) + v
        rel = path.relative_to(task_dir).as_posix()
        keyword_summary = ", ".join(f"{k}={v}" for k, v in counts.items())
        details.append(
            f"  [FOUND] {rel} | lines: {format_line_numbers(line_numbers)} | hits: {keyword_summary}"
        )
        if not dry_run:
            cleaned_content = remove_keywords(content)
            try:
                path.write_text(cleaned_content, encoding="utf-8")
                details.append(f"  [CLEANED] {rel} | {hits_in_file} keyword hits removed")
                cleaned += 1
            except Exception as exc:
                details.append(f"  [FAILED] {rel} | write failed: {exc}")
                failed += 1

    if found == 0:
        status = "ok"
    elif dry_run:
        status = "found"
    elif cleaned > 0:
        status = "cleaned"
    else:
        status = "failed"

    return {
        "status": status,
        "checked": checked, "found": found,
        "cleaned": cleaned, "failed": failed,
        "total_hits": total_hits,
        "keyword_totals": keyword_totals,
        "details": details,
    }


# ============================================================================
# 主流程：处理单个 TASK + 汇总
# ============================================================================

def process_task(task_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    print(f"[{task_dir.name}] processing...")

    # ---- 函数 a：检查并修复 JSON/JSONL ----
    jsonl_result = check_and_repair_json(task_dir, dry_run=dry_run)
    if jsonl_result["status"] == "ok":
        print(colored(f"  {MARK_OK} (a) JSONL: {jsonl_result['message']}", Colors.OK))
    elif jsonl_result["status"] == "repaired":
        print(colored(f"  {MARK_OK} (a) JSONL: {jsonl_result['message']}", Colors.OK))
        if jsonl_result.get("repair_result") and jsonl_result["repair_result"].get("stdout"):
            print(f"     repair: {jsonl_result['repair_result']['stdout'][:200]}")
        if jsonl_result.get("encoding_fixed"):
            print(f"     encoding fix: {jsonl_result['encoding_fixed']} files")
            for d in jsonl_result["encoding_details"]:
                print(f"       {d}")
    elif jsonl_result["status"] == "data_lost":
        print(colored(f"  {MARK_WARN} (a) JSONL: {jsonl_result['message']}", Colors.WARN))
    elif jsonl_result["status"] == "partial":
        print(colored(f"  {MARK_WARN} (a) JSONL: {jsonl_result['message']}", Colors.WARN))
        for err in jsonl_result["final_scan"].get("errors", [])[:3]:
            print(f"     - {err['file']}:{err['line']}: {err['error']}")
    elif jsonl_result["status"] == "skipped":
        print(colored(f"  {MARK_SKIP} (a) JSONL: {jsonl_result['message']}", Colors.INFO))
    elif jsonl_result["status"] == "dry_run":
        print(colored(f"  {MARK_OK} (a) JSONL: {jsonl_result['message']}", Colors.INFO))

    # ---- 函数 b：检查并删除中文字符 ----
    chinese_result = check_and_remove_chinese(task_dir, dry_run=dry_run)
    if chinese_result["status"] == "ok":
        print(colored(
            f"  {MARK_OK} (b) chinese: checked {chinese_result['checked']} files, none with chinese",
            Colors.OK,
        ))
    elif chinese_result["status"] == "cleaned":
        print(colored(
            f"  {MARK_OK} (b) chinese: {chinese_result['found']} files had chinese, {chinese_result['cleaned']} cleaned",
            Colors.OK,
        ))
        for d in chinese_result["details"]:
            print(d)
    elif chinese_result["status"] == "found":
        print(colored(
            f"  {MARK_WARN} (b) chinese: {chinese_result['found']} files have chinese (dry-run, not cleaned)",
            Colors.WARN,
        ))
        for d in chinese_result["details"]:
            print(d)
    elif chinese_result["status"] == "failed":
        print(colored(
            f"  {MARK_FAIL} (b) chinese: {chinese_result['found']} files have chinese, all cleanup failed",
            Colors.FAIL,
        ))
        for d in chinese_result["details"]:
            print(d)
    elif chinese_result["status"] == "skipped":
        print(colored(f"  {MARK_SKIP} (b) chinese: missing original_sessions", Colors.INFO))

    if chinese_result.get("failed") and chinese_result["failed"] > 0:
        print(colored(f"  {MARK_FAIL} (b) chinese cleanup failed: {chinese_result['failed']} files", Colors.FAIL))

    # ---- 函数 c：检查并删除文件名关键字 ----
    keyword_result = check_and_remove_keywords(task_dir, dry_run=dry_run)
    if keyword_result["status"] == "ok":
        print(colored(
            f"  {MARK_OK} (c) keywords: checked {keyword_result['checked']} files, no keyword hits",
            Colors.OK,
        ))
    elif keyword_result["status"] == "cleaned":
        print(colored(
            f"  {MARK_OK} (c) keywords: {keyword_result['found']} files had keywords ({keyword_result['total_hits']} hits), {keyword_result['cleaned']} cleaned",
            Colors.OK,
        ))
        for d in keyword_result["details"]:
            print(d)
    elif keyword_result["status"] == "found":
        print(colored(
            f"  {MARK_WARN} (c) keywords: {keyword_result['found']} files have keywords ({keyword_result['total_hits']} hits, dry-run)",
            Colors.WARN,
        ))
        for d in keyword_result["details"]:
            print(d)
    elif keyword_result["status"] == "failed":
        print(colored(
            f"  {MARK_FAIL} (c) keywords: {keyword_result['found']} files have keywords, all cleanup failed",
            Colors.FAIL,
        ))
        for d in keyword_result["details"]:
            print(d)
    elif keyword_result["status"] == "skipped":
        print(colored(f"  {MARK_SKIP} (c) keywords: missing original_sessions", Colors.INFO))

    print()
    return {
        "task": task_dir.name,
        "jsonl": jsonl_result,
        "chinese": chinese_result,
        "keywords": keyword_result,
    }



def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Process a TASK trajectory: (a) check & repair JSON/JSONL, "
            "(b) check & remove chinese chars, (c) check & remove file-name keywords. "
            "Operates on <task>/original_sessions/."
        ),
    )
    parser.add_argument(
        "path", type=Path,
        help=(
            "TASK directory (e.g. /path/to/TASK-XXXX) or parent directory containing TASK-*. "
            "All cleanup actions run inside <task>/original_sessions/."
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Check only, no file is written (a/b/c only report what they would change).")
    args = parser.parse_args()

    try:
        tasks = discover_task_dirs(args.path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(colored(f"ERROR: {exc}", Colors.FAIL))
        return 1

    if not tasks:
        print(colored("No TASK-* directory found", Colors.WARN))
        return 0

    mode = "[DRY-RUN check only]" if args.dry_run else "[APPLY]"
    print(colored(f"{mode} discovered {len(tasks)} TASK directory(ies)\n", Colors.INFO))

    results: list[dict[str, Any]] = []
    for task_dir in tasks:
        results.append(process_task(task_dir, dry_run=args.dry_run))

    partial_jsonl = sum(1 for r in results if r["jsonl"]["status"] == "partial")
    lost_jsonl = sum(1 for r in results if r["jsonl"]["status"] == "data_lost")
    failed_chinese = sum(1 for r in results if r["chinese"]["status"] == "failed")
    failed_keywords = sum(1 for r in results if r["keywords"]["status"] == "failed")

    if partial_jsonl == 0 and lost_jsonl == 0 and failed_chinese == 0 and failed_keywords == 0:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

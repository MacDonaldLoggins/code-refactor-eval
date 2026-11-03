#!/usr/bin/env python3
"""Classify abnormal TASK session layouts.

This script is read-only for input data unless --copy-classified or
--move-classified is used. It scans TASK-* directories and classifies their raw
trajectory/session artifacts into broad buckets:

  normal      TASK/original_sessions exists and looks like Claude Code JSONL.
  directory_format
              TASK/original_sessions has invalid root-level layout, such as
              misplaced subagents/tool-results or root-level JSON files.
  trajectory  non-normal trajectory JSON, usually sessions/trajectory-*.json.
  develop     sessions/develop-*.json or develop JSONL, often with bugfix files.
  session     Claude Code JSONL-like data that is structurally close/repairable.
  other       no recognizable normal, trajectory, develop, or session artifact.

Usage:
  python classify_abnormal_sessions.py -r C:/path/to/tasks-root
  python classify_abnormal_sessions.py -r C:/path/to/tasks-root -o C:/path/to/report-dir
  python classify_abnormal_sessions.py -r C:/path/to/tasks-root --copy-classified
  python classify_abnormal_sessions.py -r C:/path/to/tasks-root --move-classified
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


TASK_PREFIX = "TASK-"
TASK_ID_RE = re.compile(r"^(TASK)-(\d{8})-([0-9A-Fa-f]{6})(?:-(.+))?$")
JSON_SUFFIXES = {".json", ".jsonl"}
UUID_DIR_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
SESSION_SOURCES = ("original_sessions", "sessions", "session")
SESSION_JSONL_ROOT_TYPES = {
    "queue-operation",
    "file-history-snapshot",
    "permission-mode",
    "session_meta",
    "message",
    "thinking",
    "tool_use",
    "tool_result",
}
REPORT_JSON_NAME = "abnormal_session_classification_summary.json"
REPORT_MD_NAME = "abnormal_session_classification_report.md"
COPY_MANIFEST_NAME = "classified_task_copy_manifest.json"
MOVE_MANIFEST_NAME = "classified_task_move_manifest.json"
CATEGORY_DIR_LABELS = {
    "normal": "正常",
    "directory_format": "目录格式异常",
    "trajectory": "轨迹格式异常",
    "develop": "develop格式异常",
    "session": "session内容或位置异常",
    "other": "其他异常",
}
SUBCATEGORY_DIR_LABELS = {
    "original_sessions_valid_jsonl": "original_sessions有效jsonl",
    "original_sessions_top_level_session_artifacts": "original_sessions顶层目录结构异常",
    "original_sessions_root_json_files": "original_sessions纯json文件异常",
    "original_sessions_non_uuid_root_dirs": "original_sessions顶层非UUID目录异常",
    "original_sessions_invalid_root_layout": "original_sessions根目录结构异常",
    "original_sessions_cjk_content": "original_sessions中文内容异常",
    "sessions_cjk_content": "sessions中文内容异常",
    "singular_session_dir_cjk_content": "session单数目录中文内容异常",
    "session_cjk_content": "session中文内容异常",
    "original_sessions_blank_lines_only": "original_sessions空行异常",
    "original_sessions_needs_repair": "original_sessions内容需修复",
    "sessions_session_jsonl": "sessions会话jsonl位置异常",
    "singular_session_dir_session_jsonl": "session单数目录会话jsonl异常",
    "root_session_jsonl": "根目录session_jsonl异常",
    "sessions_trajectory_json": "sessions轨迹json异常",
    "sessions_trajectory_json_parse_error": "sessions轨迹json解析异常",
    "sessions_trajectory_with_extra_files": "sessions轨迹json混合文件异常",
    "root_trajectory_json": "根目录轨迹json异常",
    "root_trajectory_json_parse_error": "根目录轨迹json解析异常",
    "singular_session_dir_trajectory": "session单数目录轨迹异常",
    "sessions_develop": "sessions_develop文件异常",
    "sessions_develop_bugfix_mix": "sessions_develop和bugfix混合异常",
    "sessions_bugfix": "sessions_bugfix文件异常",
    "root_develop": "根目录develop文件异常",
    "original_sessions_develop": "original_sessions_develop文件异常",
    "task_develop_files": "任务内develop文件异常",
    "original_sessions_trajectory_files": "original_sessions轨迹文件异常",
    "task_trajectory_files": "任务内轨迹文件异常",
    "unrecognized_session_artifacts": "未识别session产物异常",
    "project_without_session_artifacts": "缺少session产物异常",
    "unknown_directory_layout": "未知目录结构异常",
}
SUBCATEGORY_LABELS_ZH = {
    "original_sessions_valid_jsonl": "original_sessions有效jsonl",
    "original_sessions_top_level_session_artifacts": "original_sessions顶层目录结构异常",
    "original_sessions_root_json_files": "original_sessions纯json文件异常",
    "original_sessions_non_uuid_root_dirs": "original_sessions顶层非UUID目录异常",
    "original_sessions_invalid_root_layout": "original_sessions根目录结构异常",
    "original_sessions_cjk_content": "original_sessions中文内容异常",
    "session_format_valid_cjk_content": "session格式正常中文内容异常",
    "sessions_cjk_content": "sessions中文内容异常",
    "singular_session_dir_cjk_content": "session单数目录中文内容异常",
    "session_cjk_content": "session中文内容异常",
    "original_sessions_blank_lines_only": "original_sessions空行异常",
    "original_sessions_needs_repair": "original_sessions内容需修复",
    "sessions_session_jsonl": "sessions会话jsonl位置异常",
    "singular_session_dir_session_jsonl": "session单数目录会话jsonl异常",
    "root_session_jsonl": "根目录session_jsonl异常",
    "sessions_trajectory_json": "sessions轨迹json异常",
    "sessions_trajectory_json_parse_error": "sessions轨迹json解析异常",
    "sessions_trajectory_with_extra_files": "sessions轨迹json混合文件异常",
    "root_trajectory_json": "根目录轨迹json异常",
    "root_trajectory_json_parse_error": "根目录轨迹json解析异常",
    "singular_session_dir_trajectory": "session单数目录轨迹异常",
    "original_sessions_trajectory_files": "original_sessions轨迹文件异常",
    "task_trajectory_files": "任务内轨迹文件异常",
    "sessions_develop": "sessions_develop文件异常",
    "sessions_develop_bugfix_mix": "sessions_develop和bugfix混合异常",
    "sessions_bugfix": "sessions_bugfix文件异常",
    "root_develop": "根目录develop文件异常",
    "original_sessions_develop": "original_sessions_develop文件异常",
    "task_develop_files": "任务内develop文件异常",
    "unrecognized_session_artifacts": "未识别session产物异常",
    "project_without_session_artifacts": "缺少session产物异常",
    "unknown_directory_layout": "未知目录结构异常",
}
SUBCATEGORY_LABELS_EN = {key: key for key in SUBCATEGORY_LABELS_ZH}
SUBCATEGORY_LABELS_BY_STYLE = {
    "en": SUBCATEGORY_LABELS_EN,
    "zh": SUBCATEGORY_LABELS_ZH,
}
SUBCATEGORY_LABELS_ZH_REVERSE = {value: key for key, value in SUBCATEGORY_LABELS_ZH.items()}
CLASSIFICATION_OUTPUT_DIR_NAMES = set(CATEGORY_DIR_LABELS) | set(CATEGORY_DIR_LABELS.values())


@dataclass
class FileScan:
    path: str
    source: str
    suffix: str
    raw_prefix: str
    prefix: str
    root_type: str = ""
    shape: str = ""
    first_keys: list[str] = field(default_factory=list)
    blank_lines: int = 0
    jsonl_records: int = 0
    jsonl_session_records: int = 0
    cjk_lines: int = 0
    cjk_fragments: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


@dataclass
class TaskScan:
    task: str
    normalized_task: str
    task_suffix: str
    normalized_duplicate: bool
    category: str
    subcategory: str
    confidence: str
    reasons: list[str]
    repair_hint: str
    top_dirs: list[str]
    top_files: list[str]
    sources: list[str]
    file_count: int
    prefix_counts: dict[str, int]
    raw_prefix_counts: dict[str, int]
    suffix_counts: dict[str, int]
    shape_counts: dict[str, int]
    issue_counts: dict[str, int]
    examples: list[dict[str, Any]]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify abnormal TASK session layouts")
    parser.add_argument("-r", "--root", required=True, type=Path, help="TASK dir or root containing TASK-* dirs")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="Optional report directory. Writes JSON and Markdown summaries there.",
    )
    parser.add_argument(
        "--copy-classified",
        action="store_true",
        help="Copy classified non-normal TASK dirs into category/subcategory directories.",
    )
    parser.add_argument(
        "--move-classified",
        action="store_true",
        help="Move classified non-normal TASK dirs into category/subcategory directories.",
    )
    parser.add_argument(
        "--copy-output-dir",
        type=Path,
        help=(
            "Destination root for --copy-classified. Defaults to the scanned root for a "
            "batch root, or the TASK parent for a single TASK input."
        ),
    )
    parser.add_argument(
        "--move-output-dir",
        type=Path,
        help=(
            "Destination root for --move-classified. Defaults to the scanned root for a "
            "batch root, or the TASK parent for a single TASK input."
        ),
    )
    parser.add_argument(
        "--copy-existing",
        choices=("skip", "merge"),
        default="skip",
        help="How to handle an existing copied TASK directory. Default: skip.",
    )
    parser.add_argument(
        "--copy-normal",
        action="store_true",
        help="Also copy normal TASK dirs when --copy-classified is enabled.",
    )
    parser.add_argument(
        "--move-normal",
        action="store_true",
        help="Also move normal TASK dirs when --move-classified is enabled.",
    )
    parser.add_argument(
        "--subcategory-label",
        choices=("zh", "en"),
        default="zh",
        help="Subcategory display/organization language. Default: zh.",
    )
    parser.add_argument("--sample-limit", type=int, default=5, help="Examples kept per TASK in output")
    args = parser.parse_args(argv)
    if args.copy_classified and args.move_classified:
        parser.error("--copy-classified and --move-classified cannot be used together")
    return args


def discover_task_dirs(root: Path) -> list[Path]:
    root = root.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if root.is_dir() and root.name.startswith(TASK_PREFIX):
        return [root]
    if not root.is_dir():
        raise NotADirectoryError(root)
    direct = sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith(TASK_PREFIX))
    if direct:
        return direct
    return sorted(
        path
        for path in root.rglob(f"{TASK_PREFIX}*")
        if path.is_dir() and not _is_under_classification_output(path, root)
    )


def _is_under_classification_output(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part in CLASSIFICATION_OUTPUT_DIR_NAMES for part in relative.parts[:-1])


def normalize_task_name(name: str) -> tuple[str, str]:
    """Return the canonical TASK id used for matching external registries.

    Directory names may carry a suffix such as
    TASK-20260327-F38A98-session轨迹异常.  The source folder name is preserved
    in reports, but matching should use TASK-20260327-F38A98.
    """
    match = TASK_ID_RE.match(name)
    if not match:
        return name, ""
    prefix, date, task_hash, suffix = match.groups()
    return f"{prefix}-{date}-{task_hash.upper()}", suffix or ""


def _raw_prefix(path: Path) -> str:
    stem = path.name
    for suffix in (".jsonl", ".json"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if "-" in stem:
        return stem.split("-", 1)[0]
    match = re.match(r"([A-Za-z_]+)", stem)
    return match.group(1) if match else stem


def _normalized_prefix(path: Path) -> str:
    name = path.name.lower()
    stem = path.stem.lower()
    raw = _raw_prefix(path).lower()
    if "trajectory" in name or raw.startswith("trajectory"):
        return "trajectory"
    if name.startswith("develop") or raw == "develop":
        return "develop"
    if name.startswith("bugfix") or raw == "bugfix":
        return "bugfix"
    if name.endswith(".session.jsonl") or raw == "session" or stem.startswith("session"):
        return "session"
    return raw


def _is_root_data_file(path: Path) -> bool:
    name = path.name.lower()
    if path.suffix.lower() not in JSON_SUFFIXES:
        return False
    return (
        "trajectory" in name
        or name.startswith("session")
        or name.endswith(".session.jsonl")
        or name.startswith("develop")
        or name.startswith("bugfix")
    )


def _is_session_record(value: object) -> bool:
    return isinstance(value, dict) and (
        "parentUuid" in value
        or "sessionId" in value
        or "session_id" in value
        or value.get("type") in SESSION_JSONL_ROOT_TYPES
    )


def _record_cjk(scan: FileScan, text: str) -> None:
    match_iter = CJK_RE.finditer(text)
    first_match = next(match_iter, None)
    if first_match is None:
        return
    scan.cjk_lines += 1
    seen = set(scan.cjk_fragments)
    for match in (first_match,):
        fragment = match.group(0)
        if fragment in seen:
            continue
        scan.cjk_fragments.append(fragment)
        seen.add(fragment)
        if len(scan.cjk_fragments) >= 5:
            return
    for match in match_iter:
        fragment = match.group(0)
        if fragment in seen:
            continue
        scan.cjk_fragments.append(fragment)
        seen.add(fragment)
        if len(scan.cjk_fragments) >= 5:
            break


def _safe_rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _load_json_file(path: Path, base: Path, source: str) -> FileScan:
    scan = FileScan(
        path=_safe_rel(path, base),
        source=source,
        suffix=path.suffix.lower(),
        raw_prefix=_raw_prefix(path),
        prefix=_normalized_prefix(path),
    )
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            text = handle.read()
        for line in text.splitlines():
            _record_cjk(scan, line)
        value = json.loads(text)
    except UnicodeDecodeError as exc:
        scan.parse_errors.append(f"encoding error: {exc}")
        scan.shape = "encoding_error"
        return scan
    except json.JSONDecodeError as exc:
        scan.parse_errors.append(f"line {exc.lineno}: {exc.msg}")
        scan.shape = "invalid_json"
        return scan
    except OSError as exc:
        scan.parse_errors.append(str(exc))
        scan.shape = "read_error"
        return scan

    scan.root_type = type(value).__name__
    if isinstance(value, dict):
        scan.first_keys = list(value.keys())[:10]
        if "messages" in value and ("meta" in value or "info" in value):
            scan.shape = "trajectory_json"
        elif "messages" in value:
            scan.shape = "messages_json"
        elif _is_session_record(value):
            scan.shape = "session_json"
        else:
            scan.shape = "dict_json"
    elif isinstance(value, list):
        scan.shape = "list_json"
    else:
        scan.shape = f"{scan.root_type}_json"
    return scan


def _load_jsonl_file(path: Path, base: Path, source: str) -> FileScan:
    scan = FileScan(
        path=_safe_rel(path, base),
        source=source,
        suffix=path.suffix.lower(),
        raw_prefix=_raw_prefix(path),
        prefix=_normalized_prefix(path),
        root_type="jsonl",
    )
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_no, raw in enumerate(handle, 1):
                line = raw.rstrip("\r\n")
                _record_cjk(scan, line)
                if not line.strip():
                    scan.blank_lines += 1
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    if len(scan.parse_errors) < 5:
                        scan.parse_errors.append(f"line {line_no}: {exc.msg}")
                    continue
                scan.jsonl_records += 1
                if _is_session_record(value):
                    scan.jsonl_session_records += 1
                    if not scan.first_keys and isinstance(value, dict):
                        scan.first_keys = list(value.keys())[:10]
    except UnicodeDecodeError as exc:
        scan.parse_errors.append(f"encoding error: {exc}")
        scan.shape = "encoding_error"
        return scan
    except OSError as exc:
        scan.parse_errors.append(str(exc))
        scan.shape = "read_error"
        return scan

    if scan.parse_errors:
        scan.shape = "invalid_jsonl"
    elif scan.jsonl_records == 0:
        scan.shape = "empty_jsonl"
    elif scan.jsonl_session_records:
        scan.shape = "session_jsonl"
    else:
        scan.shape = "jsonl"
    return scan


def scan_file(path: Path, base: Path, source: str) -> FileScan:
    if path.suffix.lower() == ".jsonl":
        return _load_jsonl_file(path, base, source)
    return _load_json_file(path, base, source)


def _source_label_for_path(path: Path, task_dir: Path) -> str:
    try:
        relative = path.relative_to(task_dir)
    except ValueError:
        return "task_anywhere"
    if len(relative.parts) <= 1:
        return "task_root"
    return relative.parts[0]


def _is_priority_named_file(path: Path) -> bool:
    if path.suffix.lower() not in JSON_SUFFIXES:
        return False
    name = path.name.lower()
    raw = _raw_prefix(path).lower()
    return (
        "trajectory" in name
        or name.startswith("develop")
        or raw == "develop"
        or name.startswith("bugfix")
        or raw == "bugfix"
    )


def collect_candidate_files(task_dir: Path) -> tuple[list[str], list[Path], dict[Path, str]]:
    sources: list[str] = []
    files: list[Path] = []
    source_by_file: dict[Path, str] = {}

    def add_source(source: str) -> None:
        if source not in sources:
            sources.append(source)

    def add_file(path: Path, source: str) -> None:
        if path in source_by_file:
            return
        files.append(path)
        source_by_file[path] = source
        add_source(source)

    for source_name in SESSION_SOURCES:
        source_dir = task_dir / source_name
        if not source_dir.is_dir():
            continue
        add_source(source_name)
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in JSON_SUFFIXES:
                add_file(path, source_name)

    root_files = [path for path in sorted(task_dir.iterdir()) if path.is_file() and _is_root_data_file(path)]
    if root_files:
        for path in root_files:
            add_file(path, "task_root")

    for path in sorted(task_dir.rglob("*")):
        if path.is_file() and _is_priority_named_file(path):
            add_file(path, _source_label_for_path(path, task_dir))

    return sources, files, source_by_file


def _original_sessions_layout_issues(task_dir: Path) -> list[str]:
    source_dir = task_dir / "original_sessions"
    if not source_dir.is_dir():
        return []

    issues: list[str] = []
    for child in sorted(source_dir.iterdir()):
        rel = _safe_rel(child, task_dir)
        if child.is_dir():
            if child.name == "subagents" or child.name.startswith("tool-results"):
                issues.append(
                    f"{rel}: invalid top-level session artifact directory; "
                    "subagents and tool-results must be nested under a session UUID directory"
                )
            elif not UUID_DIR_RE.match(child.name):
                issues.append(
                    f"{rel}: invalid top-level directory; expected a session UUID directory"
                )
        elif child.is_file():
            if child.name == "subagents.jsonl":
                issues.append(
                    f"{rel}: invalid top-level subagents.jsonl; "
                    "subagent transcripts must be nested under a session UUID directory"
                )
            elif child.suffix.lower() == ".json":
                issues.append(
                    f"{rel}: invalid root JSON file; original_sessions root should contain "
                    "session JSONL files and session UUID directories"
                )
    return issues


def _directory_format_subcategory(layout_issues: list[str]) -> str:
    if any("root JSON file" in issue for issue in layout_issues):
        return "original_sessions_root_json_files"
    if any("session artifact directory" in issue or "subagents.jsonl" in issue for issue in layout_issues):
        return "original_sessions_top_level_session_artifacts"
    if any("invalid top-level directory" in issue for issue in layout_issues):
        return "original_sessions_non_uuid_root_dirs"
    return "original_sessions_invalid_root_layout"


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def subcategory_key(value: str) -> str:
    return SUBCATEGORY_LABELS_ZH_REVERSE.get(value, value)


def subcategory_label(value: str, label_style: str = "zh") -> str:
    key = subcategory_key(value)
    labels = SUBCATEGORY_LABELS_BY_STYLE.get(label_style, SUBCATEGORY_LABELS_BY_STYLE["zh"])
    return labels.get(key, key)


def _issue_counts(scans: list[FileScan]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for scan in scans:
        if scan.parse_errors:
            counts["parse_error_files"] += 1
        if scan.blank_lines:
            counts["jsonl_blank_line_files"] += 1
        if scan.cjk_lines:
            counts["cjk_files"] += 1
            counts["cjk_lines"] += scan.cjk_lines
    return _counter_dict(counts)


def _all_jsonl_issues_are_blank(scans: list[FileScan]) -> bool:
    relevant = [scan for scan in scans if scan.suffix == ".jsonl"]
    if not relevant:
        return False
    return all(not scan.parse_errors for scan in relevant) and any(scan.blank_lines for scan in relevant)


def _has_session_jsonl(scans: list[FileScan]) -> bool:
    return any(scan.shape == "session_jsonl" for scan in scans)


def _has_trajectory_json(scans: list[FileScan]) -> bool:
    return any(scan.prefix == "trajectory" for scan in scans)


def _trajectory_scans(scans: list[FileScan]) -> list[FileScan]:
    return [scan for scan in scans if scan.prefix == "trajectory"]


def _develop_scans(scans: list[FileScan]) -> list[FileScan]:
    return [scan for scan in scans if scan.prefix in {"develop", "bugfix"}]


def _has_cjk(scans: list[FileScan]) -> bool:
    return any(scan.cjk_lines for scan in scans)


def _cjk_file_count(scans: list[FileScan]) -> int:
    return sum(1 for scan in scans if scan.cjk_lines)


def _apply_cjk_session_subcategory(
    category: str,
    subcategory: str,
    confidence: str,
    reasons: list[str],
    repair_hint: str,
    sources: list[str],
    scans: list[FileScan],
) -> tuple[str, str, str, list[str], str]:
    if category not in {"normal", "session"} or not _has_cjk(scans):
        return category, subcategory, confidence, reasons, repair_hint

    if category == "normal":
        cjk_subcategory = "session_format_valid_cjk_content"
    elif "original_sessions" in sources:
        cjk_subcategory = "original_sessions_cjk_content"
    elif "sessions" in sources:
        cjk_subcategory = "sessions_cjk_content"
    elif "session" in sources:
        cjk_subcategory = "singular_session_dir_cjk_content"
    else:
        cjk_subcategory = "session_cjk_content"

    return (
        "session",
        cjk_subcategory,
        "high",
        reasons
        + [
            f"CJK content detected in {_cjk_file_count(scans)} session artifact file(s)",
            f"pre-CJK classification was {category}/{subcategory}",
            "session data structure was valid before CJK classification"
            if category == "normal"
            else "session data structure or location issues remain before CJK classification",
        ],
        "Run cjk_session_format.py or translate/remove Chinese/CJK content, then re-run format checks.",
    )


def _classify(
    task_dir: Path,
    sources: list[str],
    scans: list[FileScan],
) -> tuple[str, str, str, list[str], str]:
    prefixes = Counter(scan.prefix for scan in scans)
    shapes = Counter(scan.shape for scan in scans)
    reasons: list[str] = []

    has_original = "original_sessions" in sources
    has_sessions = "sessions" in sources
    has_singular_session = "session" in sources and "sessions" not in sources
    has_root = "task_root" in sources
    has_develop = prefixes["develop"] > 0
    has_bugfix = prefixes["bugfix"] > 0
    has_session_jsonl = _has_session_jsonl(scans)
    has_trajectory_json = _has_trajectory_json(scans)
    has_parse_errors = any(scan.parse_errors for scan in scans)
    trajectory_scans = _trajectory_scans(scans)
    develop_scans = _develop_scans(scans)

    def finish(
        category: str,
        subcategory: str,
        confidence: str,
        final_reasons: list[str],
        repair_hint: str,
    ) -> tuple[str, str, str, list[str], str]:
        return _apply_cjk_session_subcategory(
            category,
            subcategory,
            confidence,
            final_reasons,
            repair_hint,
            sources,
            scans,
        )

    if has_trajectory_json:
        trajectory_sources = {scan.source for scan in trajectory_scans}
        trajectory_parse_errors = any(scan.parse_errors for scan in trajectory_scans)
        if "sessions" in trajectory_sources:
            subcategory = "sessions_trajectory_json"
            reason = "found trajectory-named JSON/JSONL files under TASK/sessions"
        elif "session" in trajectory_sources:
            subcategory = "singular_session_dir_trajectory"
            reason = "found trajectory-named JSON/JSONL files under TASK/session"
        elif "task_root" in trajectory_sources:
            subcategory = "root_trajectory_json"
            reason = "found root-level trajectory-named JSON/JSONL files"
        elif "original_sessions" in trajectory_sources:
            subcategory = "original_sessions_trajectory_files"
            reason = "found trajectory-named JSON/JSONL files under TASK/original_sessions"
        else:
            subcategory = "task_trajectory_files"
            reason = "found trajectory-named JSON/JSONL files under TASK"
        if trajectory_parse_errors:
            subcategory = (
                "root_trajectory_json_parse_error"
                if subcategory == "root_trajectory_json"
                else "sessions_trajectory_json_parse_error"
                if subcategory == "sessions_trajectory_json"
                else subcategory
            )
        if len(trajectory_sources) > 1 and subcategory == "sessions_trajectory_json":
            subcategory = "sessions_trajectory_with_extra_files"
        final_reasons = [reason]
        if trajectory_parse_errors:
            final_reasons.append("one or more trajectory files do not parse as JSON")
        if len(trajectory_sources) > 1:
            final_reasons.append(f"trajectory files found in multiple sources: {', '.join(sorted(trajectory_sources))}")
        return (
            "trajectory",
            subcategory,
            "high",
            final_reasons,
            "Treat as trajectory-format source; malformed JSON files need repair before conversion.",
        )

    if develop_scans:
        develop_sources = {scan.source for scan in develop_scans}
        has_develop_file = any(scan.prefix == "develop" for scan in develop_scans)
        has_bugfix_file = any(scan.prefix == "bugfix" for scan in develop_scans)
        if "sessions" in develop_sources:
            if has_develop_file and has_bugfix_file:
                subcategory = "sessions_develop_bugfix_mix"
            elif has_bugfix_file:
                subcategory = "sessions_bugfix"
            else:
                subcategory = "sessions_develop"
            reason = "found develop/bugfix-named JSON/JSONL files under TASK/sessions"
        elif "task_root" in develop_sources:
            subcategory = "root_develop"
            reason = "found root-level develop/bugfix-named JSON/JSONL files"
        elif "original_sessions" in develop_sources:
            subcategory = "original_sessions_develop"
            reason = "found develop/bugfix-named JSON/JSONL files under TASK/original_sessions"
        else:
            subcategory = "task_develop_files"
            reason = "found develop/bugfix-named JSON/JSONL files under TASK"
        final_reasons = [reason]
        if len(develop_sources) > 1:
            final_reasons.append(f"develop/bugfix files found in multiple sources: {', '.join(sorted(develop_sources))}")
        return (
            "develop",
            subcategory,
            "high",
            final_reasons,
            "Treat as develop-format source; convert develop/bugfix files before normal session checks.",
        )

    if has_original:
        reasons.append("found TASK/original_sessions")
        layout_issues = _original_sessions_layout_issues(task_dir)
        if layout_issues:
            return (
                "directory_format",
                _directory_format_subcategory(layout_issues),
                "high",
                reasons + layout_issues[:5],
                "Fix original_sessions root layout first: keep session JSONL files at root, "
                "and move subagents/tool-results under the matching session UUID directory.",
            )
        if has_session_jsonl and not has_parse_errors and not any(scan.blank_lines for scan in scans):
            return finish(
                "normal",
                "original_sessions_valid_jsonl",
                "high",
                reasons + ["JSONL session records parse without simple format issues"],
                "No abnormal classification needed.",
            )
        if _all_jsonl_issues_are_blank(scans):
            return finish(
                "session",
                "original_sessions_blank_lines_only",
                "high",
                reasons + ["only blank JSONL lines were detected"],
                "Remove blank JSONL lines, then re-run check_session_format.py.",
            )
        return finish(
            "session",
            "original_sessions_needs_repair",
            "medium",
            reasons + ["normal source directory exists but JSONL/content issues remain"],
            "Run check_session_format.py first; if it reports JSONL issues, use the repair pipeline.",
        )

    if has_sessions and has_session_jsonl:
        reasons.append("found Claude Code session-like JSONL under TASK/sessions")
        if _all_jsonl_issues_are_blank(scans):
            reasons.append("JSONL becomes valid after dropping blank lines")
        return finish(
            "session",
            "sessions_session_jsonl",
            "high",
            reasons,
            "Move or normalize session JSONL into original_sessions; remove blank lines if present.",
        )

    if has_singular_session and has_trajectory_json:
        reasons.append("found TASK/session singular directory with trajectory files")
        return (
            "trajectory",
            "singular_session_dir_trajectory",
            "high",
            reasons,
            "Rename session to sessions or convert trajectory files from the singular directory.",
        )

    if has_singular_session and has_session_jsonl:
        reasons.append("found TASK/session singular directory with session JSONL")
        return finish(
            "session",
            "singular_session_dir_session_jsonl",
            "high",
            reasons,
            "Rename session to sessions/original_sessions and remove blank lines if present.",
        )

    if has_root and has_session_jsonl:
        reasons.append("found root-level *.session.jsonl or session-like JSONL")
        return finish(
            "session",
            "root_session_jsonl",
            "high",
            reasons,
            "Move root-level session JSONL into original_sessions and re-run check_session_format.py.",
        )

    if has_root and has_trajectory_json:
        reasons.append("found root-level trajectory*.json without TASK/sessions")
        if has_parse_errors:
            reasons.append("one or more root-level trajectory files do not parse as JSON")
        return (
            "trajectory",
            "root_trajectory_json_parse_error" if has_parse_errors else "root_trajectory_json",
            "high",
            reasons,
            "Move root-level trajectory JSON into sessions or convert it before normal checks.",
        )

    if scans:
        reasons.append("candidate JSON/JSONL files exist but do not match known session formats")
        return (
            "other",
            "unrecognized_session_artifacts",
            "medium",
            reasons,
            "Inspect candidate files manually and add a classifier if this pattern repeats.",
        )

    top_dirs = {path.name for path in task_dir.iterdir() if path.is_dir()}
    if {"docs", "pure_backend"} <= top_dirs or {"docs", "repo"} <= top_dirs or "fullstack" in top_dirs:
        subcategory = "project_without_session_artifacts"
        reasons.append("project files exist but no recognized session artifact directory/file was found")
    else:
        subcategory = "unknown_directory_layout"
        reasons.append("directory layout differs from normal, trajectory, develop, and session patterns")
    return (
        "other",
        subcategory,
        "high",
        reasons,
        "Check whether session artifacts were omitted, archived, or stored under a new path.",
    )


def scan_task(task_dir: Path, sample_limit: int) -> TaskScan:
    top_dirs = sorted(path.name for path in task_dir.iterdir() if path.is_dir())
    top_files = sorted(path.name for path in task_dir.iterdir() if path.is_file())
    sources, candidate_files, source_by_file = collect_candidate_files(task_dir)
    scans = [scan_file(path, task_dir, source_by_file[path]) for path in candidate_files]

    category, subcategory, confidence, reasons, repair_hint = _classify(task_dir, sources, scans)
    prefix_counts = Counter(scan.prefix for scan in scans)
    raw_prefix_counts = Counter(scan.raw_prefix for scan in scans)
    suffix_counts = Counter(scan.suffix for scan in scans)
    shape_counts = Counter(scan.shape for scan in scans)
    examples = [asdict(scan) for scan in scans[:sample_limit]]

    return TaskScan(
        task=task_dir.name,
        normalized_task=normalize_task_name(task_dir.name)[0],
        task_suffix=normalize_task_name(task_dir.name)[1],
        normalized_duplicate=False,
        category=category,
        subcategory=subcategory,
        confidence=confidence,
        reasons=reasons,
        repair_hint=repair_hint,
        top_dirs=top_dirs,
        top_files=top_files,
        sources=sources,
        file_count=len(scans),
        prefix_counts=_counter_dict(prefix_counts),
        raw_prefix_counts=_counter_dict(raw_prefix_counts),
        suffix_counts=_counter_dict(suffix_counts),
        shape_counts=_counter_dict(shape_counts),
        issue_counts=_issue_counts(scans),
        examples=examples,
    )


def build_summary(root: Path, tasks: list[TaskScan], subcategory_label_style: str = "zh") -> dict[str, Any]:
    normalized_counts = Counter(task.normalized_task for task in tasks)
    seen_normalized: Counter[str] = Counter()
    for task in tasks:
        seen_normalized[task.normalized_task] += 1
        task.normalized_duplicate = seen_normalized[task.normalized_task] > 1

    category_counts = Counter(task.category for task in tasks)
    subcategory_key_counts = Counter(subcategory_key(task.subcategory) for task in tasks)
    subcategory_counts = Counter(
        subcategory_label(task.subcategory, subcategory_label_style) for task in tasks
    )
    issue_counts: Counter[str] = Counter()
    for task in tasks:
        issue_counts.update(task.issue_counts)
    task_rows = []
    for task in tasks:
        row = asdict(task)
        row["subcategory_key"] = subcategory_key(task.subcategory)
        row["subcategory"] = subcategory_label(task.subcategory, subcategory_label_style)
        task_rows.append(row)
    return {
        "root": str(root.expanduser().resolve()),
        "total_tasks": len(tasks),
        "subcategory_label": subcategory_label_style,
        "category_counts": _counter_dict(category_counts),
        "subcategory_counts": _counter_dict(subcategory_counts),
        "subcategory_key_counts": _counter_dict(subcategory_key_counts),
        "normalized_task_counts": _counter_dict(normalized_counts),
        "normalized_duplicate_tasks": [
            task.task for task in tasks if normalized_counts[task.normalized_task] > 1
        ],
        "issue_counts": _counter_dict(issue_counts),
        "tasks": task_rows,
    }


def _markdown_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    lines = []
    header = rows[0]
    lines.append("| " + " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(header)) + " |")
    lines.append("| " + " | ".join("-" * widths[index] for index in range(len(header))) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)) + " |")
    return lines


def render_markdown(summary: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Abnormal Session Classification Report",
        "",
        f"- Root: `{summary['root']}`",
        f"- Total TASKs: {summary['total_tasks']}",
        "",
        "## Categories",
        "",
    ]
    category_rows = [["category", "count"]]
    category_rows.extend([category, str(count)] for category, count in summary["category_counts"].items())
    lines.extend(_markdown_table(category_rows))
    lines.extend(["", "## Subcategories", ""])
    subcategory_rows = [["subcategory", "count"]]
    subcategory_rows.extend([subcategory, str(count)] for subcategory, count in summary["subcategory_counts"].items())
    lines.extend(_markdown_table(subcategory_rows))
    if summary.get("organize"):
        operation = summary["organize"]["operation"]
        lines.extend(["", f"## {operation.title()}", ""])
        lines.append(f"- Output root: `{summary['organize']['output_root']}`")
        lines.append(f"- Manifest: `{summary['organize']['manifest']}`")
        for status, count in summary["organize"]["status_counts"].items():
            lines.append(f"- {status}: {count}")
    lines.extend(["", "## Task Details", ""])
    detail_rows = [["task", "normalized_task", "duplicate", "category", "subcategory", "subcategory_key", "files", "issues", "reason"]]
    for task in summary["tasks"]:
        issues = ",".join(f"{key}:{value}" for key, value in task["issue_counts"].items()) or "-"
        reason = "; ".join(task["reasons"])[:120]
        detail_rows.append(
            [
                task["task"],
                task["normalized_task"],
                "yes" if task["normalized_duplicate"] else "",
                task["category"],
                task["subcategory"],
                task.get("subcategory_key", task["subcategory"]),
                str(task["file_count"]),
                issues,
                reason,
            ]
        )
    lines.extend(_markdown_table(detail_rows))
    lines.append("")
    return "\n".join(lines)


def write_reports(summary: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / REPORT_JSON_NAME
    md_path = output_dir / REPORT_MD_NAME
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    return json_path, md_path


def _safe_dir_label(value: str) -> str:
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return label or "unknown"


def _category_dir_label(category: str) -> str:
    return _safe_dir_label(category)


def _subcategory_dir_label(subcategory: str, label_style: str = "zh") -> str:
    return _safe_dir_label(subcategory_label(subcategory, label_style))


def _organize_root_for(root: Path, output_dir: Path | None) -> Path:
    if output_dir is not None:
        return output_dir.expanduser().resolve()
    root = root.expanduser().resolve()
    return root.parent if root.name.startswith(TASK_PREFIX) else root


def _ensure_organize_target_safe(source: Path, target: Path, operation: str) -> None:
    source = source.resolve()
    target = target.resolve()
    if target == source:
        raise ValueError(f"{operation} target equals source TASK: {target}")
    try:
        target.relative_to(source)
    except ValueError:
        return
    raise ValueError(f"{operation} target cannot be inside source TASK: {target}")


def organize_classified_tasks(
    root: Path,
    task_dirs: list[Path],
    tasks: list[TaskScan],
    operation: str,
    output_dir: Path | None = None,
    copy_existing: str = "skip",
    include_normal: bool = False,
    subcategory_label_style: str = "zh",
) -> dict[str, Any]:
    if operation not in {"copy", "move"}:
        raise ValueError(f"unsupported organize operation: {operation}")
    organize_root = _organize_root_for(root, output_dir)
    organize_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()

    for task_dir, task in zip(task_dirs, tasks):
        category_label = _category_dir_label(task.category)
        subcategory_dir_label = _subcategory_dir_label(task.subcategory, subcategory_label_style)
        task_subcategory_key = subcategory_key(task.subcategory)
        task_subcategory_label = subcategory_label(task.subcategory, subcategory_label_style)
        destination = organize_root / category_label / subcategory_dir_label / task_dir.name
        row = {
            "task": task.task,
            "source": str(task_dir),
            "operation": operation,
            "category": task.category,
            "subcategory": task_subcategory_label,
            "subcategory_key": task_subcategory_key,
            "category_dir": category_label,
            "subcategory_dir": subcategory_dir_label,
            "destination": str(destination),
            "status": "",
            "error": "",
        }

        if task.category == "normal" and not include_normal:
            row["status"] = "skipped_normal"
            status_counts[row["status"]] += 1
            rows.append(row)
            continue

        try:
            _ensure_organize_target_safe(task_dir, destination, operation)
            existed_before = destination.exists()
            if existed_before and copy_existing == "skip":
                row["status"] = "skipped_existing"
            elif operation == "move" and existed_before:
                row["status"] = "skipped_existing"
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if operation == "copy":
                    shutil.copytree(task_dir, destination, dirs_exist_ok=copy_existing == "merge")
                    row["status"] = "merged" if existed_before else "copied"
                else:
                    shutil.move(str(task_dir), str(destination))
                    row["status"] = "moved"
        except Exception as exc:
            row["status"] = "error"
            row["error"] = str(exc)

        status_counts[row["status"]] += 1
        rows.append(row)

    manifest_name = COPY_MANIFEST_NAME if operation == "copy" else MOVE_MANIFEST_NAME
    manifest = {
        "operation": operation,
        "output_root": str(organize_root),
        "manifest": str(organize_root / manifest_name),
        "copy_existing": copy_existing,
        "include_normal": include_normal,
        "subcategory_label": subcategory_label_style,
        "status_counts": _counter_dict(status_counts),
        "tasks": rows,
    }
    manifest_path = organize_root / manifest_name
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def print_console_summary(summary: dict[str, Any]) -> None:
    print(f"ROOT: {summary['root']}")
    print(f"TOTAL_TASKS: {summary['total_tasks']}")
    print("CATEGORY_COUNTS:")
    for category, count in summary["category_counts"].items():
        print(f"  {category}: {count}")
    print("SUBCATEGORY_COUNTS:")
    for subcategory, count in summary["subcategory_counts"].items():
        print(f"  {subcategory}: {count}")
    if summary["issue_counts"]:
        print("ISSUE_COUNTS:")
        for issue, count in summary["issue_counts"].items():
            print(f"  {issue}: {count}")
    if summary.get("organize"):
        label = summary["organize"]["operation"].upper()
        print(f"{label}_ROOT: {summary['organize']['output_root']}")
        print(f"{label}_MANIFEST: {summary['organize']['manifest']}")
        print(f"{label}_SUBCATEGORY_LABEL: {summary['organize'].get('subcategory_label', summary.get('subcategory_label', 'zh'))}")
        print(f"{label}_COUNTS:")
        for status, count in summary["organize"]["status_counts"].items():
            print(f"  {status}: {count}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    task_dirs = discover_task_dirs(root)
    if not task_dirs:
        raise NotADirectoryError(f"no TASK-* directories found under {root}")

    tasks = [scan_task(task_dir, args.sample_limit) for task_dir in task_dirs]
    summary = build_summary(root, tasks, args.subcategory_label)
    if args.copy_classified:
        summary["organize"] = organize_classified_tasks(
            root,
            task_dirs,
            tasks,
            operation="copy",
            output_dir=args.copy_output_dir,
            copy_existing=args.copy_existing,
            include_normal=args.copy_normal,
            subcategory_label_style=args.subcategory_label,
        )
    if args.move_classified:
        summary["organize"] = organize_classified_tasks(
            root,
            task_dirs,
            tasks,
            operation="move",
            output_dir=args.move_output_dir,
            include_normal=args.move_normal,
            subcategory_label_style=args.subcategory_label,
        )
    print_console_summary(summary)
    if args.output_dir:
        json_path, md_path = write_reports(summary, args.output_dir)
        print(f"JSON_REPORT: {json_path}")
        print(f"MARKDOWN_REPORT: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

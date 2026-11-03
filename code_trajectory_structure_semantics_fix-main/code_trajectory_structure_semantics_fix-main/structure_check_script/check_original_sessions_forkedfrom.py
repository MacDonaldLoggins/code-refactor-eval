#!/usr/bin/env python3
"""检查 original_sessions forkedFrom 是否已被转换后的 sessions 清理覆盖。"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


REPORT_NAME = "check_original_sessions_forkedfrom.json"
REPORT_TYPE = "check_original_sessions_forkedfrom_report"
CLEANUP_REPORT_NAME = "claude_openai_forkedfrom_report.json"
ORIGINAL_SESSIONS_DIR = "original_sessions"
ORIGINAL_SESSION_SUFFIXES = {".jsonl"}
VALUE_PREVIEW_LIMIT = 500
SUCCESSFUL_INLINE_CLEANUP_STATUSES = {"processed"}
SUCCESSFUL_APPLY_CLEANUP_STATUSES = {"repaired"}


@dataclass
class ForkedFromFinding:
    project_dir: str
    file_path: str
    line: int
    json_path: str
    value_preview: str


@dataclass
class FileScanResult:
    file_path: str
    lines_checked: int = 0
    invalid_json_lines: int = 0
    findings: list[ForkedFromFinding] = field(default_factory=list)
    error: str = ""

    @property
    def all_pass(self) -> bool:
        return not self.error and not self.findings


@dataclass
class OriginalSessionsForkedFromResult:
    project_dir: str
    original_sessions_path: str
    exists: bool = True
    files_checked: int = 0
    lines_checked: int = 0
    invalid_json_lines: int = 0
    findings: list[ForkedFromFinding] = field(default_factory=list)
    file_errors: list[FileScanResult] = field(default_factory=list)
    error: str = ""
    cleanup_report_path: str = ""
    cleanup_status: str = ""
    cleanup_processed: bool = False
    cleanup_reason: str = ""

    @property
    def all_pass(self) -> bool:
        has_unprocessed_forkedfrom = bool(self.findings) and not self.cleanup_processed
        return self.exists and not self.error and not has_unprocessed_forkedfrom and not self.file_errors


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def should_scan_project_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and path.name != "__pycache__"


def is_original_jsonl_file(path: Path) -> bool:
    return path.is_file() and not path.name.startswith(".") and path.suffix.lower() in ORIGINAL_SESSION_SUFFIXES


def discover_project_dirs(root_dir: Path) -> list[Path]:
    if (root_dir / ORIGINAL_SESSIONS_DIR).exists():
        return [root_dir]
    project_dirs = sorted(
        (
            path
            for path in root_dir.iterdir()
            if should_scan_project_dir(path) and (path / ORIGINAL_SESSIONS_DIR).exists()
        ),
        key=lambda path: path.name,
    )
    return project_dirs if project_dirs else [root_dir]


def iter_original_jsonl_files(project_dir: Path) -> list[Path]:
    original_sessions_path = project_dir / ORIGINAL_SESSIONS_DIR
    if not original_sessions_path.is_dir():
        return []
    return sorted(
        (path for path in original_sessions_path.rglob("*") if is_original_jsonl_file(path)),
        key=lambda path: str(path.relative_to(project_dir)),
    )


def iter_key_hits(value: Any, target_key: str = "forkedFrom", path: str = "$") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}.{key}"
            if key == target_key:
                yield key_path, item
            yield from iter_key_hits(item, target_key=target_key, path=key_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_key_hits(item, target_key=target_key, path=f"{path}[{index}]")


def stringify_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return repr(value)


def compact_value(value: Any, *, include_value: bool = False) -> str:
    text = stringify_value(value)
    if include_value or len(text) <= VALUE_PREVIEW_LIMIT:
        return text
    omitted = len(text) - VALUE_PREVIEW_LIMIT
    return f"{text[:VALUE_PREVIEW_LIMIT]}...[truncated {omitted} chars]"


def check_file(project_dir: Path, file_path: Path, *, include_value: bool = False) -> FileScanResult:
    result = FileScanResult(file_path=str(file_path))
    try:
        with file_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                result.lines_checked += 1
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    result.invalid_json_lines += 1
                    continue
                for json_path, value in iter_key_hits(payload):
                    result.findings.append(
                        ForkedFromFinding(
                            project_dir=project_dir.name,
                            file_path=str(file_path),
                            line=line_number,
                            json_path=json_path,
                            value_preview=compact_value(value, include_value=include_value),
                        )
                    )
    except OSError as exc:
        result.error = str(exc)
    return result


def cleanup_report_candidates(project_dir: Path) -> list[Path]:
    return [
        project_dir / "report" / CLEANUP_REPORT_NAME,
        project_dir / CLEANUP_REPORT_NAME,
    ]


def _int_from_summary(summary: dict[str, Any], key: str) -> int:
    value = summary.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return 0


def inline_cleanup_report_processed(payload: dict[str, Any]) -> tuple[bool, str]:
    status = payload.get("status")
    if status not in SUCCESSFUL_INLINE_CLEANUP_STATUSES:
        return False, f"forkedFrom 清洗报告状态不是 processed: {status or '<missing>'}"
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return False, "forkedFrom 清洗报告缺少 summary"
    if _int_from_summary(summary, "missing_referenced_subagents"):
        return False, "forkedFrom 清洗报告仍有 missing_referenced_subagents"
    if _int_from_summary(summary, "matched_fork_edges") <= 0:
        return False, "forkedFrom 清洗报告没有匹配到 fork 边"
    return True, "forkedFrom 已在转换后的 sessions 中处理"


def applied_cleanup_report_processed(payload: dict[str, Any], project_id: str) -> tuple[bool, str, str]:
    if payload.get("apply") is not True:
        return False, "", "forkedFrom 清洗报告不是 apply 模式"
    results = payload.get("results")
    if not isinstance(results, list):
        return False, "", "forkedFrom 清洗报告缺少 results"
    for item in results:
        if not isinstance(item, dict) or item.get("project_id") != project_id:
            continue
        status = item.get("status")
        if status not in SUCCESSFUL_APPLY_CLEANUP_STATUSES:
            return False, str(status or ""), f"forkedFrom 清洗结果状态不是 repaired: {status or '<missing>'}"
        if item.get("error"):
            return False, str(status or ""), f"forkedFrom 清洗结果有错误: {item.get('error')}"
        return True, str(status or ""), "forkedFrom 已在转换后的 sessions 中修复"
    return False, "", f"forkedFrom 清洗报告中没有项目 {project_id}"


def read_cleanup_report_status(project_dir: Path) -> tuple[str, str, bool, str]:
    for report_path in cleanup_report_candidates(project_dir):
        if not report_path.is_file():
            continue
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            return str(report_path), "", False, f"读取 forkedFrom 清洗报告失败: {exc}"
        if not isinstance(payload, dict):
            return str(report_path), "", False, "forkedFrom 清洗报告不是 JSON 对象"
        report_type = payload.get("report_type")
        if report_type and report_type != "claude_openai_forkedfrom_report":
            return str(report_path), "", False, "forkedFrom 清洗报告 report_type 不匹配"

        status = payload.get("status")
        if isinstance(status, str):
            processed, reason = inline_cleanup_report_processed(payload)
            return str(report_path), status, processed, reason

        processed, applied_status, reason = applied_cleanup_report_processed(payload, project_dir.name)
        return str(report_path), applied_status, processed, reason

    return "", "", False, f"未找到 {CLEANUP_REPORT_NAME}"


def check_one_project(project_dir: Path, *, include_value: bool = False) -> OriginalSessionsForkedFromResult:
    original_sessions_path = project_dir / ORIGINAL_SESSIONS_DIR
    result = OriginalSessionsForkedFromResult(
        project_dir=project_dir.name,
        original_sessions_path=str(original_sessions_path),
    )
    if not original_sessions_path.exists():
        result.exists = False
        result.error = f"{ORIGINAL_SESSIONS_DIR} 目录不存在"
        return result
    if not original_sessions_path.is_dir():
        result.exists = False
        result.error = f"{ORIGINAL_SESSIONS_DIR} 不是目录"
        return result

    for file_path in iter_original_jsonl_files(project_dir):
        file_result = check_file(project_dir, file_path, include_value=include_value)
        result.files_checked += 1
        result.lines_checked += file_result.lines_checked
        result.invalid_json_lines += file_result.invalid_json_lines
        result.findings.extend(file_result.findings)
        if file_result.error:
            result.file_errors.append(file_result)

    result.findings.sort(key=lambda item: (item.file_path, item.line, item.json_path))
    result.file_errors.sort(key=lambda item: item.file_path)
    if result.findings:
        (
            result.cleanup_report_path,
            result.cleanup_status,
            result.cleanup_processed,
            result.cleanup_reason,
        ) = read_cleanup_report_status(project_dir)
    return result


def file_id_for_original_session(source_root: Path, project_id: str, file_path: Path) -> str:
    for base in (source_root / project_id, source_root):
        try:
            return file_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return file_path.name


def project_id_for_report(source_root: Path, results: list[OriginalSessionsForkedFromResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def missing_original_sessions_task(source_root: Path, result: OriginalSessionsForkedFromResult) -> dict[str, Any]:
    original_sessions_path = Path(result.original_sessions_path)
    return {
        "产物id": result.project_dir,
        "文件id": file_id_for_original_session(source_root, result.project_dir, original_sessions_path),
        "原因": result.error,
        "行号": 1,
        "role": "",
        "agent": "",
        "timestamp": "",
        "工具id": "",
        "subagent": "",
        "content 原文": "",
    }


def file_error_task(source_root: Path, project_id: str, file_result: FileScanResult) -> dict[str, Any]:
    file_path = Path(file_result.file_path)
    return {
        "产物id": project_id,
        "文件id": file_id_for_original_session(source_root, project_id, file_path),
        "原因": f"读取 original_sessions 文件失败: {file_result.error}",
        "行号": 1,
        "role": "",
        "agent": "",
        "timestamp": "",
        "工具id": "",
        "subagent": "",
        "content 原文": "",
    }


def finding_task(source_root: Path, finding: ForkedFromFinding) -> dict[str, Any]:
    file_path = Path(finding.file_path)
    return {
        "产物id": finding.project_dir,
        "文件id": file_id_for_original_session(source_root, finding.project_dir, file_path),
        "原因": f"original_sessions 原始轨迹残留 forkedFrom 字段: {finding.json_path}",
        "行号": finding.line,
        "role": "",
        "agent": "",
        "timestamp": "",
        "工具id": "",
        "subagent": "",
        "content 原文": finding.value_preview,
        "json_path": finding.json_path,
    }


def build_json_report(source_root: Path, results: list[OriginalSessionsForkedFromResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    tasks: list[dict[str, Any]] = []

    for result in results:
        if not result.exists or result.error:
            tasks.append(missing_original_sessions_task(source_root, result))
        for file_result in result.file_errors:
            tasks.append(file_error_task(source_root, result.project_dir, file_result))
        if not result.cleanup_processed:
            for finding in result.findings:
                tasks.append(finding_task(source_root, finding))

    files_with_forkedfrom = {
        finding.file_path
        for result in results
        for finding in result.findings
    }

    return {
        "report_type": REPORT_TYPE,
        "project_id": project_id_for_report(source_root, results),
        "source_root": str(source_root),
        "generated_at": generated_at_text(),
        "summary": {
            "projects": total,
            "passed": passed,
            "failed": total - passed,
            "issues": len(tasks),
            "files_checked": sum(result.files_checked for result in results),
            "lines_checked": sum(result.lines_checked for result in results),
            "invalid_json_lines": sum(result.invalid_json_lines for result in results),
            "file_errors": sum(len(result.file_errors) for result in results),
            "files_with_forkedfrom": len(files_with_forkedfrom),
            "forkedfrom_hits": sum(len(result.findings) for result in results),
            "projects_with_forkedfrom": sum(1 for result in results if result.findings),
            "projects_with_processed_cleanup": sum(
                1 for result in results if result.findings and result.cleanup_processed
            ),
            "projects_with_unprocessed_forkedfrom": sum(
                1 for result in results if result.findings and not result.cleanup_processed
            ),
        },
        "cleanup_reports": [
            {
                "project_id": result.project_dir,
                "cleanup_report_path": result.cleanup_report_path,
                "cleanup_status": result.cleanup_status,
                "cleanup_processed": result.cleanup_processed,
                "cleanup_reason": result.cleanup_reason,
                "forkedfrom_hits": len(result.findings),
            }
            for result in results
            if result.findings
        ],
        "tasks": tasks,
    }


def run(
    find_path: Path | str,
    session_json_path: Path | str = "sessions",
    output_dir: Path | str | None = None,
    *,
    include_value: bool = False,
) -> Path:
    del session_json_path

    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"scan path does not exist or is not a directory: {root_dir}")

    results: list[OriginalSessionsForkedFromResult] = []
    for project_dir in discover_project_dirs(root_dir):
        print(f"checking original_sessions forkedFrom: {project_dir.name} ... ", end="", flush=True)
        result = check_one_project(project_dir, include_value=include_value)
        print("pass" if result.all_pass else "fail")
        results.append(result)

    report_output_dir = Path(output_dir).expanduser().resolve() if output_dir is not None else root_dir
    report_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_output_dir / REPORT_NAME
    report_payload = build_json_report(root_dir, results)
    report_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    summary = report_payload["summary"]
    print(f"\nJSON report written: {report_path}")
    print(f"scanned projects: {summary['projects']}")
    print(f"files checked: {summary['files_checked']}")
    print(f"files with forkedFrom: {summary['files_with_forkedfrom']}")
    print(f"forkedFrom hits: {summary['forkedfrom_hits']}")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check original_sessions JSONL files for forkedFrom fields not covered by cleanup reports."
    )
    parser.add_argument("-r", dest="find_path", type=Path, required=True, help="Root containing TASK project directories.")
    parser.add_argument(
        "-path",
        dest="session_json_path",
        type=Path,
        default=Path("sessions"),
        help="Kept for check_pipeline.py compatibility; this check scans original_sessions.",
    )
    parser.add_argument("-o", dest="output_dir", type=Path, default=None, help=f"Report output directory. Generates {REPORT_NAME}.")
    parser.add_argument("--include-value", action="store_true", help="Keep the full forkedFrom value in task previews.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report_path = run(
            args.find_path,
            args.session_json_path,
            args.output_dir,
            include_value=args.include_value,
        )
        payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - CLI should print actionable failures.
        print(f"check_original_sessions_forkedfrom failed: {exc}", file=sys.stderr)
        return 2
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    return 1 if int(summary.get("failed", 0) or 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())

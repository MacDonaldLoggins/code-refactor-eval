#!/usr/bin/env python3
"""Claude Code 轨迹检查脚本。

参数说明：
  -r, --root
      必填。指定待检查的 Claude Code 轨迹路径，可以是单个 .json/.jsonl
      文件，也可以是包含 .json/.jsonl 文件的目录。
  -o, --output
      可选。指定 TASK 输出目录。只有发现 JSON/JSONL 格式错误时，
      脚本才会在该目录下创建 report/json_format_report.md。
      如果该错误报告已存在，本次失败检查会追加到报告末尾。
      不传时，如果 -r 是 TASK 目录，则默认使用该 TASK 目录。

使用样例：
      python check_session_format.py -r path/to/TASK
      python check_session_format.py -r path/to/original_sessions -o path/to/TASK
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "repair_scripts" else SCRIPT_DIR
for import_path in (SCRIPT_DIR, ROOT_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))


SOURCE_DIR_CANDIDATES = (
    Path("original_sessions"),
    Path(".backup/original_sessions"),
)
DEFAULT_REPORT_NAME = "json_format_report.md"
DEFAULT_SUMMARY_NAME = "json_format_summary.json"
DEFAULT_FORMAT_FAILURE_DIR_NAME = "format_failed_tasks"
REPORT_DIR_NAME = "report"
SESSION_JSON_ALLOWED_DIR_PREFIXES = ("tool-results",)
SESSION_JSONL_ROOT_TYPES = {"queue-operation", "file-history-snapshot"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查 Claude Code 轨迹中的 .json/.jsonl 格式是否可解析"
    )
    parser.add_argument("-r", "--root", required=True, type=Path, help="要检查的 Claude Code 轨迹文件或目录")
    parser.add_argument("-o", "--output", type=Path, help="TASK 输出目录；报告写入该目录下的 report 文件夹")
    parser.add_argument(
        "--format-failure-dir",
        type=Path,
        help=(
            "Directory for failed TASK folders. Relative paths are resolved under "
            f"the TASK batch root. Default: {DEFAULT_FORMAT_FAILURE_DIR_NAME}"
        ),
    )
    args = parser.parse_args(argv)
    args.target = args.root
    return args


def discover_task_dirs(target: str | Path, sources: list[Path] | None = None) -> list[Path]:
    root = Path(target).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    source_candidates = tuple(sources or SOURCE_DIR_CANDIDATES)
    if root.name.startswith("TASK-") or any((root / item).is_dir() for item in source_candidates):
        return [root]

    direct = sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("TASK-"))
    if direct:
        return direct
    return sorted(path for path in root.rglob("TASK-*") if path.is_dir())


def _source_dirs(task_dir: Path, sources: list[Path] | None = None) -> list[Path]:
    source_candidates = tuple(sources or SOURCE_DIR_CANDIDATES)
    return [task_dir / relative for relative in source_candidates if (task_dir / relative).is_dir()]


def _json_files(source_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for source_dir in source_dirs:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}:
                files.append(path)
    return files


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


def _is_session_jsonl_record(value: object) -> bool:
    return isinstance(value, dict) and (
        "parentUuid" in value
        or "sessionId" in value
        or value.get("type") in SESSION_JSONL_ROOT_TYPES
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


def _source_dir_for_path(path: Path, source_dirs: list[Path]) -> Path | None:
    for source_dir in source_dirs:
        try:
            path.relative_to(source_dir)
        except ValueError:
            continue
        return source_dir
    return None


def _infer_source_dir_from_path(path: Path) -> Path | None:
    for parent in path.parents:
        if parent.name == "original_sessions":
            return parent
    return None


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


def _rel(task_dir: Path, path_text: str) -> str:
    try:
        path_part, line_no, message = path_text.rsplit(":", 2)
        separator = ":"
        suffix = f"{line_no}:{message}"
    except ValueError:
        path_part, separator, suffix = path_text.partition(":")
    path = Path(path_part)
    try:
        rel_path = path.relative_to(task_dir).as_posix()
    except ValueError:
        rel_path = path.as_posix()
    if rel_path == ".":
        rel_path = path.name
    return f"{rel_path}{separator}{suffix}"


def _write_task_report(task_dir: Path, lines: list[str], report_name: str = DEFAULT_REPORT_NAME) -> Path:
    report_path = task_dir / ".tmp" / report_name
    report_path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    if report_path.is_file():
        text = report_path.read_text(encoding="utf-8").rstrip() + "\n\n---\n\n" + text
    report_path.write_text(text, encoding="utf-8")
    return report_path


def _single_file_report_name(path: Path, report_name: str) -> str:
    if report_name != DEFAULT_REPORT_NAME:
        return report_name
    return f"json_format_report.{path.name}.md"


def check_file_target(path: str | Path, report_name: str = DEFAULT_REPORT_NAME) -> dict[str, Any]:
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(file_path)
    if file_path.suffix.lower() not in {".json", ".jsonl"}:
        raise ValueError(f"不支持的文件类型：{file_path}")

    issues = _check_file(file_path, _infer_source_dir_from_path(file_path))
    report_lines = [
        "# JSON Format Report",
        "",
        f"TARGET: `{file_path.name}`",
        "Mode: `single-file`",
        "",
    ]
    report_path = ""
    if issues:
        report_lines.extend(["## FAIL", ""])
        for issue in issues:
            report_lines.append(f"- {_rel(file_path.parent, issue)}")
        status = "failed"
        path = file_path.parent / ".tmp" / _single_file_report_name(file_path, report_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(report_lines) + "\n"
        if path.is_file():
            text = path.read_text(encoding="utf-8").rstrip() + "\n\n---\n\n" + text
        path.write_text(text, encoding="utf-8")
        report_path = str(path)
    else:
        status = "passed"
    return {
        "task": str(file_path),
        "status": status,
        "checked": 1,
        "issues": issues,
        "report": report_path,
        "single_file": True,
    }


def check_task(
    task_dir: str | Path,
    sources: list[Path] | None = None,
    report_name: str = DEFAULT_REPORT_NAME,
) -> dict[str, Any]:
    task_path = Path(task_dir).expanduser().resolve()
    source_dirs = _source_dirs(task_path, sources)
    source_labels = [path.as_posix() for path in (sources or list(SOURCE_DIR_CANDIDATES))]
    report_lines = [
        "# JSON Format Report",
        "",
        f"TASK: `{task_path.name}`",
        f"Sources: `{', '.join(source_labels)}`",
        "",
    ]

    if not source_dirs:
        return {"task": str(task_path), "status": "skipped", "checked": 0, "issues": [], "report": ""}

    files = _json_files(source_dirs)
    if not files:
        return {"task": str(task_path), "status": "skipped", "checked": 0, "issues": [], "report": ""}

    issues: list[str] = []
    for source_dir in source_dirs:
        issues.extend(_check_source_layout(source_dir))
    for path in files:
        issues.extend(_check_file(path, _source_dir_for_path(path, source_dirs)))

    report_path = ""
    if issues:
        report_lines.extend(["## FAIL", ""])
        for issue in issues:
            report_lines.append(f"- {_rel(task_path, issue)}")
        status = "failed"
        report_path = str(_write_task_report(task_path, report_lines, report_name))
    else:
        status = "passed"

    return {"task": str(task_path), "status": status, "checked": len(files), "issues": issues, "report": report_path}


def check_target(
    target: str | Path,
    sources: list[Path] | None = None,
    report_name: str = DEFAULT_REPORT_NAME,
) -> dict[str, Any]:
    target_path = Path(target).expanduser().resolve()
    if target_path.is_file():
        result = check_file_target(target_path, report_name=report_name)
        return {
            "total": 1,
            "passed": 1 if result["status"] == "passed" else 0,
            "failed": 1 if result["status"] == "failed" else 0,
            "skipped": 0,
            "checked": int(result["checked"]),
            "results": [result],
        }
    task_dirs = discover_task_dirs(target, sources)
    results = [check_task(task_dir, sources=sources, report_name=report_name) for task_dir in task_dirs]
    passed = sum(1 for result in results if result["status"] == "passed")
    failed = sum(1 for result in results if result["status"] == "failed")
    skipped = sum(1 for result in results if result["status"] == "skipped")
    checked = sum(int(result["checked"]) for result in results)
    return {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "checked": checked,
        "results": results,
    }


def _cli_json_files(target: Path) -> list[Path]:
    if target.is_file():
        if target.suffix.lower() not in {".json", ".jsonl"}:
            raise ValueError(f"不支持的文件类型：{target}")
        return [target]
    if not target.is_dir():
        raise FileNotFoundError(target)
    return sorted(
        path
        for path in target.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}
    )


def _resolve_cli_paths(root: str | Path, output: str | Path | None = None) -> tuple[Path, Path]:
    root_path = Path(root).expanduser().resolve()
    if root_path.is_dir() and (root_path / "original_sessions").is_dir():
        check_path = root_path / "original_sessions"
        default_output = root_path
    elif root_path.is_dir() and root_path.name == "original_sessions":
        check_path = root_path
        default_output = root_path.parent
    else:
        check_path = root_path
        default_output = root_path.parent if root_path.is_file() else root_path
    output_path = Path(output).expanduser().resolve() if output else default_output
    return check_path, output_path


def check_cli_target(root: str | Path, output: str | Path | None = None) -> dict[str, Any]:
    check_path, output_path = _resolve_cli_paths(root, output)
    report_dir = output_path / REPORT_DIR_NAME
    report_dir.mkdir(parents=True, exist_ok=True)

    archive_result = {"archives": 0, "extracted": 0, "skipped": 0}
    if check_path.is_dir():
        archive_result = extract_session_archives_if_needed(check_path)
    files = _cli_json_files(check_path)
    if check_path.is_relative_to(output_path):
        base = output_path
    else:
        base = check_path.parent if check_path.is_file() else check_path
    issues: list[str] = []
    if check_path.is_file():
        inferred_source_dir = _infer_source_dir_from_path(check_path)
        source_dirs = [inferred_source_dir] if inferred_source_dir is not None else []
    else:
        source_dirs = [check_path] if check_path.is_dir() and check_path.name == "original_sessions" else []
    for source_dir in source_dirs:
        issues.extend(_check_source_layout(source_dir))
    for path in files:
        issues.extend(_check_file(path, _source_dir_for_path(path, source_dirs)))

    lines = [
        "# JSON Format Report",
        "",
        f"TARGET: `{Path(root).expanduser().resolve()}`",
        f"CHECK_PATH: `{check_path}`",
        f"OUTPUT: `{output_path}`",
        f"REPORT_DIR: `{report_dir}`",
        "Mode: `claude-code-session`",
        "",
    ]
    if archive_result["archives"]:
        lines.extend(
            [
                "## Archive Extraction",
                "",
                f"- archives: {archive_result['archives']}",
                f"- extracted: {archive_result['extracted']}",
                f"- skipped: {archive_result['skipped']}",
                "",
            ]
        )
    report_path = report_dir / DEFAULT_REPORT_NAME
    report_text = ""
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
        "target": str(Path(root).expanduser().resolve()),
        "check_path": str(check_path),
        "output": str(output_path),
        "status": status,
        "checked": len(files),
        "issues": issues,
        "report": str(report_path) if report_text else "",
        "archive_extraction": archive_result,
    }


def _direct_cli_task_dirs(root: Path) -> list[Path]:
    if not root.is_dir() or root.name.startswith("TASK-"):
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("TASK-"))


def _resolve_format_failure_dir(batch_root: Path, format_failure_dir: str | Path | None = None) -> Path:
    if format_failure_dir is None:
        return batch_root / DEFAULT_FORMAT_FAILURE_DIR_NAME
    path = Path(format_failure_dir).expanduser()
    if not path.is_absolute():
        path = batch_root / path
    return path.resolve()


def _unique_task_move_target(failure_dir: Path, task_name: str) -> Path:
    target = failure_dir / task_name
    if not target.exists():
        return target
    index = 1
    while True:
        candidate = failure_dir / f"{task_name}-{index}"
        if not candidate.exists():
            return candidate
        index += 1


def _move_failed_task_dir(task_dir: Path, failure_dir: Path) -> Path:
    task_path = task_dir.resolve()
    failure_path = failure_dir.resolve()
    if task_path.parent == failure_path:
        return task_path
    try:
        failure_path.relative_to(task_path)
    except ValueError:
        pass
    else:
        raise ValueError(f"failure directory cannot be inside failed TASK: {failure_path}")
    failure_path.mkdir(parents=True, exist_ok=True)
    target = _unique_task_move_target(failure_path, task_path.name)
    return Path(shutil.move(str(task_path), str(target))).resolve()


def _report_path_after_task_move(report: str, old_task: Path, new_task: Path) -> str:
    if not report:
        return ""
    report_path = Path(report)
    try:
        relative = report_path.relative_to(old_task)
    except ValueError:
        return report
    return str(new_task / relative)


def _write_batch_summary(summary_root: Path, rows: list[dict[str, Any]], failure_dir: Path) -> Path:
    summary_root.mkdir(parents=True, exist_ok=True)
    summary_path = summary_root / DEFAULT_SUMMARY_NAME
    payload = {
        "summary": {
            "total": len(rows),
            "passed": sum(1 for row in rows if row["status"] == "passed"),
            "failed": sum(1 for row in rows if row["status"] == "failed"),
            "skipped": sum(1 for row in rows if row["status"] == "skipped"),
            "checked": sum(int(row.get("checked") or 0) for row in rows),
            "moved_failed": sum(1 for row in rows if row.get("moved_to")),
            "failure_dir": str(failure_dir),
        },
        "tasks": rows,
    }
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary_path


def check_cli_batch_target(
    root: str | Path,
    output: str | Path | None = None,
    format_failure_dir: str | Path | None = None,
) -> dict[str, Any]:
    batch_root = Path(root).expanduser().resolve()
    task_dirs = _direct_cli_task_dirs(batch_root)
    if not task_dirs:
        raise NotADirectoryError(f"no direct TASK-* directories under {batch_root}")
    summary_root = Path(output).expanduser().resolve() if output else batch_root
    failure_dir = _resolve_format_failure_dir(batch_root, format_failure_dir)
    rows: list[dict[str, Any]] = []

    for task_dir in task_dirs:
        result = check_cli_target(task_dir, task_dir)
        row = {
            "task": str(task_dir),
            "status": result["status"],
            "checked": result["checked"],
            "issues": len(result["issues"]),
            "report": result["report"],
            "moved_to": "",
        }
        if result["status"] == "failed":
            moved_to = _move_failed_task_dir(task_dir, failure_dir)
            row["moved_to"] = str(moved_to)
            row["report"] = _report_path_after_task_move(result["report"], task_dir, moved_to)
        rows.append(row)

    summary_path = _write_batch_summary(summary_root, rows, failure_dir)
    failed = sum(1 for row in rows if row["status"] == "failed")
    passed = sum(1 for row in rows if row["status"] == "passed")
    skipped = sum(1 for row in rows if row["status"] == "skipped")
    checked = sum(int(row.get("checked") or 0) for row in rows)
    return {
        "target": str(batch_root),
        "status": "failed" if failed else "passed",
        "total": len(rows),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "checked": checked,
        "summary": str(summary_path),
        "failure_dir": str(failure_dir),
        "results": rows,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root_path = args.root.expanduser().resolve()
    try:
        if _direct_cli_task_dirs(root_path):
            result = check_cli_batch_target(args.root, args.output, args.format_failure_dir)
            print(
                f"JSON {result['status'].upper()}: tasks={result['total']} "
                f"passed={result['passed']} failed={result['failed']} skipped={result['skipped']} "
                f"checked={result['checked']} summary={result['summary']} "
                f"failure_dir={result['failure_dir']}"
            )
            for row in result["results"]:
                if row["status"] == "failed":
                    print(f"{row['task']} -> {row['moved_to']} report={row['report']}")
            return 1 if result["failed"] else 0
        result = check_cli_target(args.root, args.output)
    except (OSError, ValueError) as exc:
        print(f"处理路径无效: {exc}", file=sys.stderr)
        return 2

    print(f"JSON {result['status'].upper()}: checked={result['checked']} report={result['report']}")
    for issue in result["issues"][:20]:
        print(issue)
    if len(result["issues"]) > 20:
        print(f"... 其余 {len(result['issues']) - 20} 条省略")
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

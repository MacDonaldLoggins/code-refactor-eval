#!/usr/bin/env python3
"""检查 TASK 项目的 sessions 目录下是否存在轨迹 JSON 文件。"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_NAME = "check_sessions_folder.json"
REPORT_TYPE = "check_sessions_folder_report"
SESSION_FILE_SUFFIXES = {".json"}
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}


@dataclass
class SessionsFolderResult:
    project_dir: str
    sessions_path: str
    exists: bool = True
    trajectory_files: int = 0
    error: str = ""

    @property
    def all_pass(self) -> bool:
        return self.exists and self.trajectory_files > 0


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def should_scan_project_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and path.name not in SKIPPED_SESSION_DIR_NAMES


def is_candidate_trajectory_file(path: Path) -> bool:
    return (
        path.is_file()
        and not path.name.startswith(".")
        and not path.name.endswith(".meta.json")
        and path.suffix.lower() in SESSION_FILE_SUFFIXES
        and not any(part in SKIPPED_SESSION_DIR_NAMES for part in path.parts)
    )


def find_trajectory_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if is_candidate_trajectory_file(path) else []
    if path.is_dir():
        return sorted(
            (candidate for candidate in path.rglob("*") if is_candidate_trajectory_file(candidate)),
            key=lambda candidate: candidate.as_posix(),
        )
    return []


def check_one_project(project_dir: Path, session_path_arg: Path) -> SessionsFolderResult:
    sessions_path = project_dir / session_path_arg
    result = SessionsFolderResult(project_dir=project_dir.name, sessions_path=str(sessions_path))
    if not sessions_path.exists():
        result.exists = False
        result.error = f"{session_path_arg.as_posix()} 目录不存在"
        return result
    if not sessions_path.is_dir():
        result.exists = False
        result.error = f"{session_path_arg.as_posix()} 目录不存在"
        return result

    trajectory_files = find_trajectory_files(sessions_path)
    result.trajectory_files = len(trajectory_files)
    if result.trajectory_files == 0:
        result.error = f"{session_path_arg.as_posix()} 目录下没有轨迹 JSON 文件"
    return result


def discover_project_dirs(root_dir: Path, session_path_arg: Path) -> list[Path]:
    if (root_dir / session_path_arg).exists():
        return [root_dir]
    project_dirs = sorted((path for path in root_dir.iterdir() if should_scan_project_dir(path)), key=lambda path: path.name)
    return project_dirs if project_dirs else [root_dir]


def file_id_for_sessions(source_root: Path, project_id: str, sessions_path: Path) -> str:
    for base in (source_root / project_id, source_root):
        try:
            return sessions_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return sessions_path.name


def project_id_for_report(source_root: Path, results: list[SessionsFolderResult]) -> str:
    project_ids = sorted({result.project_dir for result in results if result.project_dir})
    if len(project_ids) == 1:
        return project_ids[0]
    return source_root.name


def build_json_report(source_root: Path, results: list[SessionsFolderResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.all_pass)
    tasks: list[dict[str, Any]] = []
    for result in results:
        if result.all_pass:
            continue
        sessions_path = Path(result.sessions_path)
        tasks.append(
            {
                "产物id": result.project_dir,
                "文件id": file_id_for_sessions(source_root, result.project_dir, sessions_path),
                "原因": result.error,
                "行号": 1,
                "role": "",
                "agent": "",
                "timestamp": "",
                "工具id": "",
                "subagent": "",
                "content 原文": "",
                "trajectory_files": result.trajectory_files,
            }
        )

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
            "missing_sessions_folder": sum(1 for result in results if not result.exists),
            "empty_sessions_folder": sum(
                1 for result in results if result.exists and result.trajectory_files == 0
            ),
        },
        "tasks": tasks,
    }


def check_sessions_folder(find_path: Path | str, session_json_path: Path | str, output_dir: Path | str) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"scan path does not exist or is not a directory: {root_dir}")

    session_relative_path = Path(session_json_path)
    results: list[SessionsFolderResult] = []
    for project_dir in discover_project_dirs(root_dir, session_relative_path):
        print(f"checking sessions folder: {project_dir.name} ... ", end="", flush=True)
        result = check_one_project(project_dir, session_relative_path)
        print("pass" if result.all_pass else "fail")
        results.append(result)

    report_output_dir = Path(output_dir).expanduser().resolve()
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
    print(f"passed: {summary['passed']}")
    print(f"failed: {summary['failed']}")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check TASK sessions folders contain trajectory JSON files.")
    parser.add_argument("-r", dest="find_path", type=Path, required=True, help="Root containing TASK project directories.")
    parser.add_argument("-path", dest="session_json_path", type=Path, default=Path("sessions"), help="Sessions directory path relative to each TASK project.")
    parser.add_argument("-o", dest="output_dir", type=Path, required=True, help=f"Report output directory. Generates {REPORT_NAME}.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        check_sessions_folder(args.find_path, args.session_json_path, args.output_dir)
    except Exception as exc:  # noqa: BLE001 - CLI should print actionable failures.
        print(f"check_sessions_folder failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

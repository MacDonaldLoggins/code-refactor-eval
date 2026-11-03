#!/usr/bin/env python3
"""Calculate original_sessions token usage and estimated USD cost.

Usage:
  python check_calculate_price.py -r /path/to/TASK
  python check_calculate_price.py -r /path/to/projects -o /path/to/report
"""
from __future__ import annotations

import argparse
import json
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


REPORT_NAME = "calculate_price.json"
REPORT_TYPE = "calculate_price_report"
ORIGINAL_SESSIONS_DIR = "original_sessions"

TOKEN_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
TOKEN_PRICE_INPUT_PER_M_USD = 5.0
TOKEN_PRICE_OUTPUT_PER_M_USD = 25.0
TOKEN_PRICE_CACHE_READ_PER_M_USD = TOKEN_PRICE_INPUT_PER_M_USD * 0.1
TOKEN_PRICE_CACHE_WRITE_PER_M_USD = TOKEN_PRICE_INPUT_PER_M_USD * 1.25
DEFAULT_COST_THRESHOLD_USD = 10.0


@dataclass
class JsonlUsageAnalysis:
    path: Path
    readable: bool = False
    usage_totals: dict[str, int] = field(default_factory=dict)
    usage_record_count: int = 0
    invalid_json_lines: int = 0


@dataclass(frozen=True)
class BundleCandidate:
    path: Path
    report_path: str
    source_type: str


@dataclass
class BundleUsageResult:
    path: str
    source_type: str
    usage_totals: dict[str, int]
    subagent_usage_totals: dict[str, int]
    session_jsonl_count: int = 0
    subagent_jsonl_count: int = 0
    usage_record_count: int = 0
    unreadable_files: int = 0
    invalid_json_lines: int = 0
    cost_usd: float = 0.0

    @property
    def token_total(self) -> int:
        return sum(self.usage_totals.values())

    @property
    def subagent_token_total(self) -> int:
        return sum(self.subagent_usage_totals.values())


@dataclass
class ProjectPriceResult:
    project_id: str
    source_root: str
    original_sessions_path: str
    threshold_usd: float
    usage_totals: dict[str, int]
    subagent_usage_totals: dict[str, int]
    bundles: list[BundleUsageResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    archives_seen: int = 0
    archives_extracted: int = 0
    archive_errors: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return calc_token_cost_usd(self.usage_totals)

    @property
    def token_total(self) -> int:
        return sum(self.usage_totals.values())

    @property
    def subagent_token_total(self) -> int:
        return sum(self.subagent_usage_totals.values())

    @property
    def session_jsonl_count(self) -> int:
        return sum(bundle.session_jsonl_count for bundle in self.bundles)

    @property
    def subagent_jsonl_count(self) -> int:
        return sum(bundle.subagent_jsonl_count for bundle in self.bundles)

    @property
    def usage_record_count(self) -> int:
        return sum(bundle.usage_record_count for bundle in self.bundles)

    @property
    def unreadable_files(self) -> int:
        return sum(bundle.unreadable_files for bundle in self.bundles)

    @property
    def invalid_json_lines(self) -> int:
        return sum(bundle.invalid_json_lines for bundle in self.bundles)

    @property
    def below_threshold(self) -> bool:
        return self.cost_usd < self.threshold_usd

    @property
    def status(self) -> str:
        if self.errors or self.archive_errors:
            return "FAIL"
        if self.below_threshold:
            return "FAIL"
        return "PASS"


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_usage_totals() -> dict[str, int]:
    return {key: 0 for key in TOKEN_USAGE_KEYS}


def add_usage_totals(dst: dict[str, int], src: dict[str, int]) -> None:
    for key in TOKEN_USAGE_KEYS:
        dst[key] = dst.get(key, 0) + src.get(key, 0)


def to_int_token_value(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return 0
    return 0


def map_usage_tokens(usage: dict[str, object]) -> dict[str, int]:
    return {
        "input_tokens": to_int_token_value(usage.get("input_tokens", 0)),
        "output_tokens": to_int_token_value(usage.get("output_tokens", 0)),
        "cache_read_tokens": to_int_token_value(usage.get("cache_read_input_tokens", 0)),
        "cache_write_tokens": to_int_token_value(usage.get("cache_creation_input_tokens", 0)),
    }


def calc_token_cost_usd(tokens: dict[str, int]) -> float:
    return (
        tokens.get("input_tokens", 0) / 1_000_000 * TOKEN_PRICE_INPUT_PER_M_USD
        + tokens.get("output_tokens", 0) / 1_000_000 * TOKEN_PRICE_OUTPUT_PER_M_USD
        + tokens.get("cache_read_tokens", 0) / 1_000_000 * TOKEN_PRICE_CACHE_READ_PER_M_USD
        + tokens.get("cache_write_tokens", 0) / 1_000_000 * TOKEN_PRICE_CACHE_WRITE_PER_M_USD
    )


def extract_usage_tokens_from_payload(payload: dict[str, object]) -> tuple[dict[str, int] | None, str | None]:
    usage: object | None = None
    message_id: str | None = None

    message = payload.get("message")
    if isinstance(message, dict):
        usage = message.get("usage")
        candidate_id = message.get("id")
        if isinstance(candidate_id, str) and candidate_id.strip():
            message_id = candidate_id.strip()
    elif "usage" in payload:
        usage = payload.get("usage")
        candidate_id = payload.get("id")
        if isinstance(candidate_id, str) and candidate_id.strip():
            message_id = candidate_id.strip()

    if not isinstance(usage, dict) or not usage:
        return None, message_id
    return map_usage_tokens(usage), message_id


def read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def analyze_jsonl_usage(jsonl_path: Path) -> JsonlUsageAnalysis:
    analysis = JsonlUsageAnalysis(
        path=jsonl_path,
        readable=False,
        usage_totals=new_usage_totals(),
    )
    content = read_text(jsonl_path)
    if content is None:
        return analysis

    analysis.readable = True
    usage_by_message_id: dict[str, dict[str, int]] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            analysis.invalid_json_lines += 1
            continue
        if not isinstance(payload, dict):
            continue

        usage_tokens, message_id = extract_usage_tokens_from_payload(payload)
        if usage_tokens is None:
            continue
        if message_id:
            usage_by_message_id[message_id] = usage_tokens
        else:
            add_usage_totals(analysis.usage_totals, usage_tokens)
            analysis.usage_record_count += 1

    for usage_tokens in usage_by_message_id.values():
        add_usage_totals(analysis.usage_totals, usage_tokens)
        analysis.usage_record_count += 1

    return analysis


def collect_first_layer_jsonl_files(bundle_dir: Path) -> list[Path]:
    if not bundle_dir.is_dir():
        return []
    files = [path for path in bundle_dir.iterdir() if path.is_file() and path.suffix.lower() == ".jsonl"]
    files.sort(key=lambda path: path.name.lower())
    return files


def analyze_bundle_token_usage(candidate: BundleCandidate) -> BundleUsageResult:
    bundle_totals = new_usage_totals()
    subagent_totals = new_usage_totals()
    result = BundleUsageResult(
        path=candidate.report_path,
        source_type=candidate.source_type,
        usage_totals=bundle_totals,
        subagent_usage_totals=subagent_totals,
    )

    for main_jsonl in collect_first_layer_jsonl_files(candidate.path):
        result.session_jsonl_count += 1
        main_analysis = analyze_jsonl_usage(main_jsonl)
        result.invalid_json_lines += main_analysis.invalid_json_lines
        if not main_analysis.readable:
            result.unreadable_files += 1
        else:
            add_usage_totals(result.usage_totals, main_analysis.usage_totals)
            result.usage_record_count += main_analysis.usage_record_count

        subagents_dir = candidate.path / main_jsonl.stem / "subagents"
        if not subagents_dir.is_dir():
            continue

        for sub_jsonl in sorted(subagents_dir.glob("*.jsonl"), key=lambda path: path.name.lower()):
            result.subagent_jsonl_count += 1
            sub_analysis = analyze_jsonl_usage(sub_jsonl)
            result.invalid_json_lines += sub_analysis.invalid_json_lines
            if not sub_analysis.readable:
                result.unreadable_files += 1
                continue
            add_usage_totals(result.usage_totals, sub_analysis.usage_totals)
            add_usage_totals(result.subagent_usage_totals, sub_analysis.usage_totals)
            result.usage_record_count += sub_analysis.usage_record_count

    result.cost_usd = calc_token_cost_usd(result.usage_totals)
    return result


def is_zip_archive(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".zip"


def safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target_path = (destination / member.filename).resolve()
            try:
                target_path.relative_to(destination_resolved)
            except ValueError as exc:
                raise ValueError(f"unsafe zip member path: {member.filename}") from exc
        archive.extractall(destination)


def relative_or_name(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


def bundle_candidates_from_extracted_archive(archive_path: Path, extract_dir: Path) -> list[BundleCandidate]:
    candidates: list[BundleCandidate] = []
    if collect_first_layer_jsonl_files(extract_dir):
        candidates.append(
            BundleCandidate(
                path=extract_dir,
                report_path=f"{archive_path.as_posix()}::.",
                source_type="zip",
            )
        )
        return candidates

    for child in sorted((path for path in extract_dir.iterdir() if path.is_dir()), key=lambda path: path.name.lower()):
        child_original_sessions = child / ORIGINAL_SESSIONS_DIR
        if collect_first_layer_jsonl_files(child_original_sessions):
            candidates.append(
                BundleCandidate(
                    path=child_original_sessions,
                    report_path=f"{archive_path.as_posix()}::{relative_or_name(child_original_sessions, extract_dir)}",
                    source_type="zip",
                )
            )
            continue
        if collect_first_layer_jsonl_files(child):
            candidates.append(
                BundleCandidate(
                    path=child,
                    report_path=f"{archive_path.as_posix()}::{relative_or_name(child, extract_dir)}",
                    source_type="zip",
                )
            )
    return candidates


def collect_bundle_candidates(
    sessions_dir: Path,
    project_result: ProjectPriceResult,
    temp_root: Path,
) -> list[BundleCandidate]:
    candidates: list[BundleCandidate] = []
    seen_paths: set[str] = set()

    def add_candidate(path: Path, report_path: str, source_type: str) -> None:
        try:
            key = path.resolve().as_posix()
        except OSError:
            key = path.absolute().as_posix()
        if key in seen_paths:
            return
        seen_paths.add(key)
        candidates.append(BundleCandidate(path=path, report_path=report_path, source_type=source_type))

    direct_jsonl_files = collect_first_layer_jsonl_files(sessions_dir)
    direct_jsonl_stems = {path.stem for path in direct_jsonl_files}
    if direct_jsonl_files:
        add_candidate(sessions_dir, sessions_dir.as_posix(), "directory")

    for child in sorted((path for path in sessions_dir.iterdir() if path.is_dir()), key=lambda path: path.name.lower()):
        if child.name.startswith("."):
            continue
        if child.name in direct_jsonl_stems:
            continue
        if collect_first_layer_jsonl_files(child):
            add_candidate(child, child.as_posix(), "directory")

    for archive_path in sorted((path for path in sessions_dir.iterdir() if is_zip_archive(path)), key=lambda path: path.name.lower()):
        project_result.archives_seen += 1
        extract_dir = temp_root / archive_path.stem
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            safe_extract_zip(archive_path, extract_dir)
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            project_result.archive_errors.append(f"{archive_path.name}: {exc}")
            continue

        archive_candidates = bundle_candidates_from_extracted_archive(archive_path, extract_dir)
        if not archive_candidates:
            project_result.archive_errors.append(f"{archive_path.name}: no first-layer jsonl files found after extraction")
            continue

        project_result.archives_extracted += 1
        for candidate in archive_candidates:
            add_candidate(candidate.path, candidate.report_path, candidate.source_type)

    return candidates


def analyze_project_price(
    project_dir: Path,
    *,
    original_sessions_name: str = ORIGINAL_SESSIONS_DIR,
    threshold_usd: float = DEFAULT_COST_THRESHOLD_USD,
    temp_base: Path | None = None,
) -> ProjectPriceResult:
    sessions_dir = project_dir / original_sessions_name
    result = ProjectPriceResult(
        project_id=project_dir.name,
        source_root=str(project_dir),
        original_sessions_path=str(sessions_dir),
        threshold_usd=threshold_usd,
        usage_totals=new_usage_totals(),
        subagent_usage_totals=new_usage_totals(),
    )

    if not sessions_dir.exists():
        result.errors.append(f"{original_sessions_name} directory does not exist")
        return result
    if not sessions_dir.is_dir():
        result.errors.append(f"{original_sessions_name} is not a directory")
        return result

    if temp_base is not None:
        temp_root = temp_base / project_dir.name
    else:
        temp_root = Path(tempfile.mkdtemp(prefix="calculate_price_"))
    candidates = collect_bundle_candidates(sessions_dir, result, temp_root)
    if not candidates:
        result.errors.append("no countable first-layer jsonl files found in original_sessions or zip archives")
        return result

    for candidate in candidates:
        bundle_result = analyze_bundle_token_usage(candidate)
        if bundle_result.session_jsonl_count == 0:
            continue
        result.bundles.append(bundle_result)
        add_usage_totals(result.usage_totals, bundle_result.usage_totals)
        add_usage_totals(result.subagent_usage_totals, bundle_result.subagent_usage_totals)

    if not result.bundles:
        result.errors.append("no jsonl files with countable bundle structure found")
    return result


def discover_project_dirs(root_dir: Path, original_sessions_name: str = ORIGINAL_SESSIONS_DIR) -> list[Path]:
    if (root_dir / original_sessions_name).exists():
        return [root_dir]
    if not root_dir.is_dir():
        return []

    project_dirs = [
        path
        for path in root_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / original_sessions_name).exists()
    ]
    project_dirs.sort(key=lambda path: path.name.lower())
    return project_dirs


def format_token_summary(result: ProjectPriceResult) -> str:
    tokens = result.usage_totals
    return (
        f"input={tokens['input_tokens']:,}, "
        f"output={tokens['output_tokens']:,}, "
        f"cache_read={tokens['cache_read_tokens']:,}, "
        f"cache_write={tokens['cache_write_tokens']:,}, "
        f"total={result.token_total:,}, "
        f"cost_usd=${result.cost_usd:,.4f}"
    )


def project_to_payload(result: ProjectPriceResult) -> dict[str, Any]:
    return {
        "project_id": result.project_id,
        "source_root": result.source_root,
        "original_sessions_path": result.original_sessions_path,
        "status": result.status,
        "threshold_usd": result.threshold_usd,
        "cost_usd": round(result.cost_usd, 6),
        "token_total": result.token_total,
        "subagent_token_total": result.subagent_token_total,
        "subagent_ratio": round(result.subagent_token_total / result.token_total * 100, 2)
        if result.token_total > 0
        else 0.0,
        "tokens": dict(result.usage_totals),
        "subagent_tokens": dict(result.subagent_usage_totals),
        "session_jsonl": result.session_jsonl_count,
        "subagent_jsonl": result.subagent_jsonl_count,
        "usage_records": result.usage_record_count,
        "unreadable_files": result.unreadable_files,
        "invalid_json_lines": result.invalid_json_lines,
        "archives_seen": result.archives_seen,
        "archives_extracted": result.archives_extracted,
        "errors": list(result.errors),
        "archive_errors": list(result.archive_errors),
        "bundles": [
            {
                "path": bundle.path,
                "source_type": bundle.source_type,
                "cost_usd": round(bundle.cost_usd, 6),
                "token_total": bundle.token_total,
                "subagent_token_total": bundle.subagent_token_total,
                "subagent_ratio": round(bundle.subagent_token_total / bundle.token_total * 100, 2)
                if bundle.token_total > 0
                else 0.0,
                "tokens": dict(bundle.usage_totals),
                "subagent_tokens": dict(bundle.subagent_usage_totals),
                "session_jsonl": bundle.session_jsonl_count,
                "subagent_jsonl": bundle.subagent_jsonl_count,
                "usage_records": bundle.usage_record_count,
                "unreadable_files": bundle.unreadable_files,
                "invalid_json_lines": bundle.invalid_json_lines,
            }
            for bundle in result.bundles
        ],
    }


def build_tasks(results: Iterable[ProjectPriceResult]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for result in results:
        for error in result.errors:
            tasks.append(
                {
                    "检查项id": "token_usage_scan",
                    "产物id": result.project_id,
                    "文件id": result.original_sessions_path,
                    "行号": 0,
                    "原因": error,
                    "content 原文": "",
                }
            )

        for error in result.archive_errors:
            tasks.append(
                {
                    "检查项id": "token_usage_archive",
                    "产物id": result.project_id,
                    "文件id": result.original_sessions_path,
                    "行号": 0,
                    "原因": error,
                    "content 原文": "",
                }
            )

        if result.below_threshold:
            tasks.append(
                {
                    "检查项id": "token_cost_threshold",
                    "产物id": result.project_id,
                    "文件id": result.original_sessions_path,
                    "行号": 0,
                    "原因": (
                        "original_sessions token 成本低于阈值: "
                        f"cost_usd=${result.cost_usd:,.4f} < threshold=${result.threshold_usd:,.2f}"
                    ),
                    "content 原文": format_token_summary(result),
                }
            )
    return tasks


def build_json_report(source_root: Path, results: list[ProjectPriceResult]) -> dict[str, Any]:
    tasks = build_tasks(results)
    total_tokens = new_usage_totals()
    total_subagent_tokens = new_usage_totals()
    for result in results:
        add_usage_totals(total_tokens, result.usage_totals)
        add_usage_totals(total_subagent_tokens, result.subagent_usage_totals)

    passed = sum(1 for result in results if result.status == "PASS")
    failed = len(results) - passed
    total_cost_usd = calc_token_cost_usd(total_tokens)
    total_token_count = sum(total_tokens.values())
    total_subagent_token_count = sum(total_subagent_tokens.values())

    return {
        "report_type": REPORT_TYPE,
        "project_id": source_root.name,
        "source_root": str(source_root),
        "generated_at": generated_at_text(),
        "summary": {
            "projects": len(results),
            "passed": passed,
            "failed": failed,
            "issues": len(tasks),
            "below_threshold_failed": sum(1 for result in results if result.below_threshold),
            "threshold_usd": results[0].threshold_usd if results else DEFAULT_COST_THRESHOLD_USD,
            "total_cost_usd": round(total_cost_usd, 6),
            "total_tokens": total_token_count,
            "total_subagent_tokens": total_subagent_token_count,
            "total_subagent_ratio": round(total_subagent_token_count / total_token_count * 100, 2)
            if total_token_count > 0
            else 0.0,
            "tokens": total_tokens,
            "subagent_tokens": total_subagent_tokens,
            "session_jsonl": sum(result.session_jsonl_count for result in results),
            "subagent_jsonl": sum(result.subagent_jsonl_count for result in results),
            "usage_records": sum(result.usage_record_count for result in results),
            "unreadable_files": sum(result.unreadable_files for result in results),
            "invalid_json_lines": sum(result.invalid_json_lines for result in results),
        },
        "projects": [project_to_payload(result) for result in results],
        "tasks": tasks,
    }


def default_report_output_dir(root_dir: Path, output_dir: Path | None) -> Path:
    if output_dir is not None:
        return output_dir.expanduser().resolve()
    return root_dir / "report"


def run(
    find_path: Path | str,
    session_json_path: Path | str = ORIGINAL_SESSIONS_DIR,
    output_dir: Path | str | None = None,
    *,
    threshold_usd: float = DEFAULT_COST_THRESHOLD_USD,
) -> Path:
    root_dir = Path(find_path).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"scan path does not exist or is not a directory: {root_dir}")

    original_sessions_name = Path(session_json_path or ORIGINAL_SESSIONS_DIR).as_posix().strip("/") or ORIGINAL_SESSIONS_DIR
    project_dirs = discover_project_dirs(root_dir, original_sessions_name)
    if not project_dirs:
        project_dirs = [root_dir]

    report_output_dir = default_report_output_dir(root_dir, Path(output_dir) if output_dir is not None else None)
    report_output_dir.mkdir(parents=True, exist_ok=True)
    temp_base = report_output_dir / ".calculate_price_tmp"

    results = [
        analyze_project_price(
            project_dir,
            original_sessions_name=original_sessions_name,
            threshold_usd=threshold_usd,
            temp_base=temp_base,
        )
        for project_dir in project_dirs
    ]
    try:
        temp_base.rmdir()
    except OSError:
        pass

    report_path = report_output_dir / REPORT_NAME
    report_path.write_text(
        json.dumps(build_json_report(root_dir, results), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统计 original_sessions token 并估算成本费用")
    parser.add_argument("-r", "--root", dest="find_path", type=Path, required=True, help="项目目录或项目父目录")
    parser.add_argument(
        "-path",
        dest="session_json_path",
        type=Path,
        default=Path(ORIGINAL_SESSIONS_DIR),
        help=f"original_sessions 相对路径，默认 {ORIGINAL_SESSIONS_DIR}",
    )
    parser.add_argument("-o", "--output", dest="output_dir", type=Path, default=None, help="报告输出目录，默认 <root>/report")
    parser.add_argument(
        "--threshold",
        dest="threshold_usd",
        type=float,
        default=DEFAULT_COST_THRESHOLD_USD,
        help=f"最低成本阈值，默认 ${DEFAULT_COST_THRESHOLD_USD:.2f}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report_path = run(
        args.find_path,
        args.session_json_path,
        args.output_dir,
        threshold_usd=args.threshold_usd,
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    summary = payload.get("summary", {})
    failed = int(summary.get("failed", 0))
    print(f"JSON report written: {report_path}")
    print(
        "summary: "
        f"projects={summary.get('projects', 0)}, "
        f"failed={failed}, "
        f"below_threshold={summary.get('below_threshold_failed', 0)}, "
        f"total_cost_usd=${float(summary.get('total_cost_usd', 0.0)):,.4f}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

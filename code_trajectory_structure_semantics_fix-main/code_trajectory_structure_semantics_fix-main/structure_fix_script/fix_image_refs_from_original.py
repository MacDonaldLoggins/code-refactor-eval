#!/usr/bin/env python3
r"""Extract `[Image #N]` images from original jsonl traces.

For `pipeline_report_summary.md` findings with reason
`session 原文包含 "[Image #N]" 关键词`, this script:

1. Reads the reported `sessions/*.json` target message.
2. Finds matching base64 image parts in the corresponding `original_sessions/*.jsonl`.
3. Writes the decoded image into the TASK `images` directory as `<session-stem>#<N>.<ext>`.

It never edits paths under `original_sessions` or `convert-sessions`.

Usage:
  python fix_image_refs_from_original.py -r D:\path\to\pipeline_report_summary.md --dry-run
  python fix_image_refs_from_original.py -r D:\path\to\pipeline_report_summary.md --apply
  python fix_image_refs_from_original.py -r D:\path\to\pipeline_report_summary.md --task-id TASK-20260427-89FDE4 --dry-run
  python fix_image_refs_from_original.py --task-dir D:\path\to\TASK-20260427-89FDE4 --dry-run

实际路径示例：
  python "C:\Users\15837\Desktop\workspace\fix\claude_code_session\检查-修复脚本\fix_image_refs_from_original.py" -r "C:\Users\15837\Desktop\workspace\data\5-21-200-wq\pipeline_report_summary.md" -o "C:\Users\15837\Desktop\workspace\data\5-21-200-wq" --dry-run
  python "C:\Users\15837\Desktop\workspace\fix\claude_code_session\检查-修复脚本\fix_image_refs_from_original.py" -r "C:\Users\15837\Desktop\workspace\data\5-21-200-wq\pipeline_report_summary.md" -o "C:\Users\15837\Desktop\workspace\data\5-21-200-wq" --apply
  python "C:\Users\15837\Desktop\workspace\fix\claude_code_session\检查-修复脚本\fix_image_refs_from_original.py" --task-dir "C:\Users\15837\Desktop\workspace\data\5-21-200-wq\TASK-20260427-89FDE4" -o "C:\Users\15837\Desktop\workspace\data\5-21-200-wq" --dry-run
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IMAGE_REASON = 'session 原文包含 "[Image #N]" 关键词'
DEFAULT_REPORT_NAME = "fix_image_refs_from_original_report.json"
FINAL_SECTION_RE = re.compile(r"^###\s+\d+\.\s+final")
ANY_SECTION_RE = re.compile(r"^###\s+")
LOCATION_RE = re.compile(r"^\s*-\s+(?P<task>TASK-[^;]+);\s+轨迹=(?P<path>.*?);\s+行号=(?P<line>\d+)\s*$")
IMAGE_REF_RE = re.compile(r"\[Image\s+#(?P<num>\d+)(?::source:\s*[^\]]+)?\]", re.I)
UUID_PREFIX_RE = re.compile(r"^\d+-(?P<id>[0-9a-fA-F-]{32,})$")
TYPED_UUID_PREFIX_RE = re.compile(r"^\d+-(?:develop|test|bugfix)-(?P<id>[0-9a-fA-F-]{32,})$")

MEDIA_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

SKIP_PATH_PARTS = {"original_sessions", "convert-sessions", "converted_sessions"}


@dataclass(frozen=True)
class ReportTarget:
    task_id: str
    session_path: Path
    line_number: int


@dataclass
class OriginalImage:
    number: int
    media_type: str
    data: str


@dataclass
class FileFixResult:
    path: Path
    original_path: Path | None = None
    images_written: int = 0
    output_paths: list[str] = field(default_factory=list)
    missing_images: list[int] = field(default_factory=list)
    error: str = ""


def parse_report(report_path: Path, task_ids: set[str] | None = None) -> list[ReportTarget]:
    lines = report_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    in_final_section = False
    in_image_reason = False
    targets: list[ReportTarget] = []

    for line in lines:
        stripped = line.strip()
        if ANY_SECTION_RE.match(stripped):
            in_final_section = bool(FINAL_SECTION_RE.match(stripped))
            in_image_reason = False
            continue
        if not in_final_section:
            continue
        if stripped.startswith("- 原因:"):
            in_image_reason = IMAGE_REASON in stripped
            continue
        if not in_image_reason:
            continue
        match = LOCATION_RE.match(line)
        if not match:
            continue
        task_id = match.group("task")
        if task_ids and task_id not in task_ids:
            continue
        targets.append(
            ReportTarget(
                task_id=task_id,
                session_path=Path(match.group("path")),
                line_number=int(match.group("line")),
            )
        )
    return targets


def iter_session_json_files(task_dir: Path) -> list[Path]:
    sessions_dir = task_dir / "sessions"
    if not sessions_dir.is_dir():
        return []
    return sorted(
        (
            path
            for path in sessions_dir.rglob("*.json")
            if path.is_file()
            and not path.name.startswith(".")
            and not path.name.endswith(".meta.json")
            and not path_has_skipped_part(path)
        ),
        key=lambda path: path.as_posix(),
    )


def targets_from_task_dir(task_dir: Path) -> list[ReportTarget]:
    task_dir = task_dir.resolve()
    task_id = task_dir.name
    targets: list[ReportTarget] = []
    for session_path in iter_session_json_files(task_dir):
        try:
            text = session_path.read_text(encoding="utf-8-sig", errors="replace")
            payload = json.loads(text)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        starts = messages_element_lines_from_text(text)
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if not IMAGE_REF_RE.search(content_text(message.get("content"))):
                continue
            line_number = starts[index] if index < len(starts) else 1
            targets.append(ReportTarget(task_id=task_id, session_path=session_path, line_number=line_number))
    return targets


def task_dir_for_path(path: Path, task_id: str) -> Path:
    parts = path.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == task_id:
            return Path(*parts[: index + 1])
    for parent in [path.parent, *path.parents]:
        if parent.name.startswith("TASK-"):
            return parent
    raise ValueError(f"cannot locate TASK directory for {path}")


def session_id_from_stem(stem: str) -> str:
    typed_match = TYPED_UUID_PREFIX_RE.match(stem)
    if typed_match:
        return typed_match.group("id")
    match = UUID_PREFIX_RE.match(stem)
    return match.group("id") if match else stem


def corresponding_original_path(session_path: Path, task_dir: Path) -> Path:
    try:
        relative = session_path.relative_to(task_dir / "sessions")
    except ValueError as exc:
        raise ValueError(f"target is not under TASK sessions directory: {session_path}") from exc

    if not relative.parts:
        raise ValueError(f"invalid sessions-relative path: {session_path}")

    first = Path(relative.parts[0])
    if len(relative.parts) == 1:
        original_id = session_id_from_stem(first.stem)
        return task_dir / "original_sessions" / f"{original_id}.jsonl"

    original_id = session_id_from_stem(first.name)
    if len(relative.parts) >= 3 and relative.parts[1] == "subagents":
        return task_dir / "original_sessions" / original_id / "subagents" / f"{Path(relative.parts[2]).stem}.jsonl"
    raise ValueError(f"unsupported sessions-relative path: {relative}")


def top_level_element_lines(text: str) -> list[int]:
    lines: list[int] = []
    depth = 0
    line = 1
    in_string = False
    escape = False
    expecting_element = True

    for char in text:
        if char == "\n":
            line += 1
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            if depth == 1 and expecting_element:
                lines.append(line)
                expecting_element = False
            in_string = True
            continue
        if char in "[{":
            if depth == 1 and expecting_element:
                lines.append(line)
                expecting_element = False
            depth += 1
            continue
        if char in "]}":
            depth -= 1
            continue
        if depth == 1 and char == ",":
            expecting_element = True
            continue
        if depth == 1 and not char.isspace() and expecting_element:
            lines.append(line)
            expecting_element = False
    return lines


def messages_element_lines_from_text(text: str) -> list[int]:
    marker = '"messages"'
    start = text.find(marker)
    if start == -1:
        return []
    array_start = text.find("[", start + len(marker))
    if array_start == -1:
        return []
    line_offset = text.count("\n", 0, array_start)
    return [line_offset + line for line in top_level_element_lines(text[array_start:])]


def message_index_for_line(starts: list[int], line_number: int) -> int | None:
    for index in range(len(starts) - 1, -1, -1):
        if starts[index] <= line_number:
            return index
    return None


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
            elif isinstance(item, str):
                texts.append(item)
        return "\n".join(texts)
    return ""


def collect_original_images(original_path: Path) -> dict[int, OriginalImage]:
    images: dict[int, OriginalImage] = {}
    with original_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue

            refs: list[int] = []
            image_parts: list[tuple[str, str]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    refs.extend(int(match.group("num")) for match in IMAGE_REF_RE.finditer(part["text"]))
                    continue
                source = part.get("source")
                if part.get("type") == "image" and isinstance(source, dict) and source.get("type") == "base64":
                    data = source.get("data")
                    if isinstance(data, str) and data.strip():
                        image_parts.append((str(source.get("media_type") or "image/png"), data))

            for number, (media_type, data) in zip(refs, image_parts):
                images.setdefault(number, OriginalImage(number=number, media_type=media_type, data=data))
    return images


def decode_base64(data: str) -> bytes:
    normalized = "".join(data.split()).replace("-", "+").replace("_", "/")
    if len(normalized) % 4:
        normalized += "=" * (4 - len(normalized) % 4)
    return base64.b64decode(normalized, validate=True)


def image_extension(media_type: str) -> str:
    return MEDIA_EXTENSIONS.get(media_type.lower(), ".bin")


def image_output_path(task_dir: Path, session_path: Path, number: int, media_type: str) -> Path:
    return task_dir / "images" / f"{session_path.stem}#{number}{image_extension(media_type)}"


def path_has_skipped_part(path: Path) -> bool:
    return any(part in SKIP_PATH_PARTS for part in path.parts)


def process_target(target: ReportTarget, *, dry_run: bool) -> FileFixResult:
    result = FileFixResult(path=target.session_path)
    try:
        if path_has_skipped_part(target.session_path):
            raise ValueError("refusing to edit original/converted trajectory path")
        if not target.session_path.is_file():
            raise FileNotFoundError(target.session_path)

        task_dir = task_dir_for_path(target.session_path, target.task_id)
        original_path = corresponding_original_path(target.session_path, task_dir)
        result.original_path = original_path
        if not original_path.is_file():
            raise FileNotFoundError(f"original trace not found: {original_path}")

        text = target.session_path.read_text(encoding="utf-8-sig", errors="replace")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("top-level JSON is not an object")
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise ValueError("missing messages array")
        starts = messages_element_lines_from_text(text)
        message_index = message_index_for_line(starts, target.line_number)
        if message_index is None or message_index >= len(messages):
            raise ValueError(f"cannot locate message for line {target.line_number}")
        message = messages[message_index]
        if not isinstance(message, dict):
            raise ValueError("located message is not an object")

        target_refs = sorted({int(match.group("num")) for match in IMAGE_REF_RE.finditer(content_text(message.get("content")))})
        if not target_refs:
            return result

        original_images = collect_original_images(original_path)
        for number in target_refs:
            image = original_images.get(number)
            if image is None:
                result.missing_images.append(number)
                continue
            output_path = image_output_path(task_dir, target.session_path, number, image.media_type)
            if not dry_run:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                image_bytes = decode_base64(image.data)
                output_path.write_bytes(image_bytes)
            result.images_written += 1
            result.output_paths.append(str(output_path))
    except (OSError, json.JSONDecodeError, ValueError, binascii.Error) as exc:
        result.error = str(exc)
    return result


def result_to_record(item: FileFixResult, *, apply: bool) -> dict[str, Any]:
    if item.error:
        operation = "error"
    elif item.missing_images:
        operation = "missing_image"
    elif item.images_written:
        operation = "written" if apply else "would_write"
    else:
        operation = "skipped_no_image_ref"
    return {
        "文件id": str(item.path),
        "原始轨迹": str(item.original_path) if item.original_path else "",
        "原因": '从 original_sessions 提取 "[Image #N]" 对应的 base64 图片到 TASK/images 目录',
        "操作结果": operation,
        "apply": apply,
        "images_written": item.images_written,
        "output_paths": item.output_paths,
        "missing_images": item.missing_images,
        "error": item.error,
    }


def resolve_output_path(output: Path | None, report: Path | None, task_dirs: list[Path]) -> Path:
    if output is not None:
        output = output.expanduser().resolve()
        if output.exists() and output.is_dir():
            return output / DEFAULT_REPORT_NAME
        if output.suffix.lower() != ".json":
            return output / DEFAULT_REPORT_NAME
        return output
    if report is not None:
        return report.expanduser().resolve().parent / DEFAULT_REPORT_NAME
    if task_dirs:
        return task_dirs[0].expanduser().resolve().parent / DEFAULT_REPORT_NAME
    return Path(DEFAULT_REPORT_NAME).resolve()


def write_report(output_path: Path, *, apply: bool, targets: list[ReportTarget], results: list[FileFixResult]) -> None:
    records = [result_to_record(item, apply=apply) for item in results]
    payload = {
        "report_type": "fix_image_refs_from_original_report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "summary": {
            "targets": len(targets),
            "results": len(results),
            "images_written": sum(item.images_written for item in results),
            "missing_image_refs": sum(len(item.missing_images) for item in results),
            "errors": sum(1 for item in results if item.error),
        },
        "results": records,
        "tasks": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_summary(results: list[FileFixResult], *, dry_run: bool) -> None:
    errors = [item for item in results if item.error]
    missing = sum(len(item.missing_images) for item in results)
    print(f"Mode: {'DRY-RUN' if dry_run else 'WRITE'}")
    print(f"Targets: {len(results)}")
    print(f"Images written: {sum(item.images_written for item in results)}")
    print(f"Missing image refs: {missing}")
    print(f"Errors: {len(errors)}")

    for item in results:
        if item.images_written:
            print(f"IMAGES {item.path} images={item.images_written}")
            if item.original_path:
                print(f"  original: {item.original_path}")
    for item in results:
        if item.missing_images:
            missing_text = ", ".join(f"#{number}" for number in item.missing_images)
            print(f"MISSING {item.path}: {missing_text}")
    for item in errors:
        print(f"ERROR {item.path}: {item.error}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 original_sessions 提取 [Image #N] 对应的 base64 图片。")
    parser.add_argument("-r", "--report", type=Path, help="pipeline_report_summary.md 路径。")
    parser.add_argument("--task-dir", action="append", type=Path, default=[], help="扫描指定 TASK 目录下所有 sessions 图片引用；可重复传入。")
    parser.add_argument("--task-id", action="append", default=[], help="只处理指定 TASK ID；可重复传入。")
    parser.add_argument("-o", "--output", type=Path, default=None, help=f"JSON 修复报告输出路径或目录；默认写入报告同目录或 TASK 父目录下的 {DEFAULT_REPORT_NAME}。")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只报告将要写出的图片，不写文件。")
    mode.add_argument("--apply", action="store_true", help="实际写出图片文件。")
    args = parser.parse_args(argv)
    if not args.report and not args.task_dir:
        parser.error("必须指定 --report 或 --task-dir。")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    task_ids = set(args.task_id) if args.task_id else None
    targets: list[ReportTarget] = []
    if args.report:
        targets.extend(parse_report(args.report, task_ids=task_ids))
    for task_dir in args.task_dir:
        if task_ids and task_dir.name not in task_ids:
            continue
        targets.extend(targets_from_task_dir(task_dir))
    results = [process_target(target, dry_run=args.dry_run) for target in targets]
    print_summary(results, dry_run=args.dry_run)
    output_path = resolve_output_path(args.output, args.report, args.task_dir)
    write_report(output_path, apply=args.apply, targets=targets, results=results)
    print(f"report: {output_path}")
    return 1 if any(item.error for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

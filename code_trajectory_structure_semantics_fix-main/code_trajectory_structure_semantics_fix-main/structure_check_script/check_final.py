#!/usr/bin/env python3
"""收尾检查项集合：metadata.json 与 sessions 文件的最终一致性校验。

当前包含的检查项：
    - cwd：检查 TASK 项目根目录下 metadata.json 必须存在 cwd 字段且为单一字符串；
           sessions 目录下的所有 JSON 轨迹文件 meta 必须包含 cwd。
    - session_meta：检查 sessions 目录下的所有 JSON 轨迹文件顶层 meta.session_meta
                    必须为合法对象，且至少包含非空 source 与非空 session_ids。
    - meta_agent_fields：检查 sessions 目录下的所有 JSON 轨迹文件顶层最终 meta
                         中是否出现 agent 或 agent_type 字段；出现时在报告中列出。
    - image_keyword：检查 sessions 目录下的所有 JSON 轨迹文件原文中是否出现
                     "[Image #N]" 编号图片占位；若 TASK/images 目录已存在对应
                     <session-stem>#N 图片文件，则视为已修复，不再报告。

新增检查项时，向 CHECK_SWITCHES 加开关、向 build_tasks 中加分支即可。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_NAME = "check_final.json"
REPORT_TYPE = "check_final_report"
METADATA_FILENAME = "metadata.json"
SESSION_FILE_SUFFIX = ".json"
SKIPPED_SESSION_DIR_NAMES = {"__pycache__", "tool-results", "fix_issues", "session_pipeline_middle"}
SESSION_TYPE_LABELS = {"develop", "test", "bugfix"}
IMAGE_KEYWORD_RE = re.compile(r"\[Image\s+#\d+\]", re.I)
IMAGE_REF_RE = re.compile(r"\[Image\s+#(?P<number>\d+)\]", re.I)
IMAGE_FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".bin")


CHECK_SWITCHES: dict[str, bool] = {
    "cwd": True,
    "session_meta": True,
    "meta_agent_fields": True,
    "image_keyword": True,
}


@dataclass
class FinalTask:
    check_id: str
    project_id: str
    file_id: str
    reason: str
    line: int = 1
    preview: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def generated_at_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def is_candidate_session_file(path: Path) -> bool:
    return (
        path.is_file()
        and not path.name.startswith(".")
        and not path.name.endswith(".meta.json")
        and path.suffix.lower() == SESSION_FILE_SUFFIX
        and not any(part in SKIPPED_SESSION_DIR_NAMES for part in path.parts)
    )


def iter_session_files(sessions_dir: Path) -> list[Path]:
    if not sessions_dir.is_dir():
        return []
    return sorted(
        (candidate for candidate in sessions_dir.rglob("*") if is_candidate_session_file(candidate)),
        key=lambda candidate: candidate.as_posix(),
    )


def file_id_relative(project_dir: Path, target: Path) -> str:
    try:
        return target.relative_to(project_dir).as_posix()
    except ValueError:
        return target.name


def image_stem_candidates(stem: str) -> list[str]:
    candidates = [stem]
    prefix = "ERROR-" if stem.startswith("ERROR-") else ""
    value = stem[len(prefix):] if prefix else stem
    original_stems: list[str] = []

    parts = value.split("-", 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[1] in SESSION_TYPE_LABELS:
        original_stems.extend([f"{parts[0]}-{parts[2]}", parts[2]])
    else:
        head, separator, rest = value.partition("-")
        if separator and head in SESSION_TYPE_LABELS:
            original_stems.append(rest)

    for original_stem in original_stems:
        candidate = f"{prefix}{original_stem}"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def session_image_exists(project_dir: Path, session_file: Path, image_number: int) -> bool:
    images_dir = project_dir / "images"
    stems = image_stem_candidates(session_file.stem)
    for extension in IMAGE_FILE_EXTENSIONS:
        for stem in stems:
            if (images_dir / f"{stem}#{image_number}{extension}").is_file():
                return True
    return False


def session_id_from_payload(path: Path, payload: Any | None) -> str:
    if isinstance(payload, dict):
        meta = payload.get("meta")
        if isinstance(meta, dict):
            session_meta = meta.get("session_meta")
            if isinstance(session_meta, dict):
                session_ids = session_meta.get("session_ids")
                if isinstance(session_ids, list):
                    for session_id in session_ids:
                        if isinstance(session_id, str) and session_id.strip():
                            return session_id.strip()

    stem = path.stem
    if stem.startswith("ERROR-"):
        stem = stem[len("ERROR-") :]
    parts = stem.split("-", 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[1] in SESSION_TYPE_LABELS:
        return parts[2]
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1]
    head, separator, rest = stem.partition("-")
    if separator and head in SESSION_TYPE_LABELS:
        return rest
    return stem


def agent_id_from_session_path(path: Path) -> str | None:
    if not any(part.lower() == "subagents" for part in path.parts):
        return None
    stem = path.stem
    return stem[len("agent-") :] if stem.startswith("agent-") else stem


def find_original_jsonl(session_id: str, input_root: Path) -> Path | None:
    candidates = [
        input_root / f"{session_id}.jsonl",
        input_root / "original_sessions" / f"{session_id}.jsonl",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for path in input_root.rglob(f"{session_id}.jsonl"):
        if path.is_file():
            return path
    return None


def find_subagent_original_jsonl(session_id: str, agent_id: str, input_root: Path) -> Path | None:
    filenames = [f"agent-{agent_id}.jsonl", f"{agent_id}.jsonl"]
    candidates = [
        base / filename
        for base in (
            input_root / session_id / "subagents",
            input_root / "original_sessions" / session_id / "subagents",
        )
        for filename in filenames
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for filename in filenames:
        for path in input_root.rglob(filename):
            if path.is_file() and path.parent.name == "subagents":
                return path
    return None


def original_session_image_numbers(original_path: Path) -> set[int] | None:
    image_numbers: set[int] = set()
    try:
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
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                refs: list[int] = []
                base64_count = 0
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if isinstance(text, str):
                        refs.extend(int(match.group("number")) for match in IMAGE_REF_RE.finditer(text))
                        continue
                    source = part.get("source")
                    if part.get("type") != "image" or not isinstance(source, dict):
                        continue
                    if source.get("type") != "base64":
                        continue
                    data = source.get("data")
                    if isinstance(data, str) and data.strip():
                        base64_count += 1
                image_numbers.update(refs[:base64_count])
    except OSError:
        return None
    return image_numbers


def original_session_image_numbers_for_session(
    project_dir: Path,
    session_file: Path,
    payload: Any | None,
    cache: dict[Path, set[int] | None],
) -> set[int] | None:
    original_sessions_dir = project_dir / "original_sessions"
    if not original_sessions_dir.is_dir():
        return None

    session_id = session_id_from_payload(session_file, payload)
    agent_id = agent_id_from_session_path(session_file)
    original_path = (
        find_subagent_original_jsonl(session_id, agent_id, original_sessions_dir)
        if agent_id
        else find_original_jsonl(session_id, original_sessions_dir)
    )
    if original_path is None:
        return None

    original_path = original_path.resolve()
    if original_path not in cache:
        cache[original_path] = original_session_image_numbers(original_path)
    return cache[original_path]


def load_json(path: Path) -> tuple[Any, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), ""
    except FileNotFoundError:
        return None, f"{path.name} 不存在"
    except json.JSONDecodeError as exc:
        return None, f"{path.name} JSON 解析失败: {exc}"
    except OSError as exc:
        return None, f"{path.name} 读取失败: {exc}"


def check_metadata_cwd(project_dir: Path) -> list[FinalTask]:
    project_id = project_dir.name
    metadata_path = project_dir / METADATA_FILENAME
    tasks: list[FinalTask] = []

    payload, load_error = load_json(metadata_path)
    if load_error:
        tasks.append(
            FinalTask(
                check_id="cwd",
                project_id=project_id,
                file_id=METADATA_FILENAME,
                reason=load_error,
            )
        )
        return tasks
    if not isinstance(payload, dict):
        tasks.append(
            FinalTask(
                check_id="cwd",
                project_id=project_id,
                file_id=METADATA_FILENAME,
                reason=f"{METADATA_FILENAME} 顶层不是对象",
            )
        )
        return tasks

    if "cwd" not in payload:
        tasks.append(
            FinalTask(
                check_id="cwd",
                project_id=project_id,
                file_id=METADATA_FILENAME,
                reason="metadata.json 缺少 cwd 字段",
            )
        )
        return tasks

    cwd_value = payload["cwd"]
    if not isinstance(cwd_value, str):
        tasks.append(
            FinalTask(
                check_id="cwd",
                project_id=project_id,
                file_id=METADATA_FILENAME,
                reason=f"metadata.json cwd 不是字符串（实际类型 {type(cwd_value).__name__}），可能包含多个值",
                preview=json.dumps(cwd_value, ensure_ascii=False)[:180],
            )
        )
        return tasks

    if not cwd_value.strip():
        tasks.append(
            FinalTask(
                check_id="cwd",
                project_id=project_id,
                file_id=METADATA_FILENAME,
                reason="metadata.json cwd 为空字符串",
            )
        )
    return tasks


def check_sessions_cwd(project_dir: Path, sessions_dir: Path) -> list[FinalTask]:
    project_id = project_dir.name
    tasks: list[FinalTask] = []
    session_files = iter_session_files(sessions_dir)
    if not session_files:
        return tasks

    for session_file in session_files:
        file_id = file_id_relative(project_dir, session_file)
        payload, load_error = load_json(session_file)
        if load_error:
            tasks.append(
                FinalTask(
                    check_id="cwd",
                    project_id=project_id,
                    file_id=file_id,
                    reason=load_error,
                )
            )
            continue
        if not isinstance(payload, dict):
            tasks.append(
                FinalTask(
                    check_id="cwd",
                    project_id=project_id,
                    file_id=file_id,
                    reason="session JSON 顶层不是对象",
                )
            )
            continue

        meta = payload.get("meta")
        if not isinstance(meta, dict):
            tasks.append(
                FinalTask(
                    check_id="cwd",
                    project_id=project_id,
                    file_id=file_id,
                    reason="session 缺少 meta 对象",
                )
            )
            continue

        if "cwd" not in meta:
            tasks.append(
                FinalTask(
                    check_id="cwd",
                    project_id=project_id,
                    file_id=file_id,
                    reason="session meta 缺少 cwd 字段",
                )
            )
            continue

        cwd_value = meta["cwd"]
        if not isinstance(cwd_value, str) or not cwd_value.strip():
            tasks.append(
                FinalTask(
                    check_id="cwd",
                    project_id=project_id,
                    file_id=file_id,
                    reason=f"session meta.cwd 非有效字符串（类型 {type(cwd_value).__name__}）",
                    preview=json.dumps(cwd_value, ensure_ascii=False)[:180],
                )
            )
    return tasks


def is_valid_session_meta_payload(session_meta: Any) -> bool:
    if not isinstance(session_meta, dict):
        return False

    source = session_meta.get("source")
    session_ids = session_meta.get("session_ids")
    if not isinstance(source, str) or not source.strip():
        return False
    if not isinstance(session_ids, list) or not session_ids:
        return False
    return all(isinstance(session_id, str) and session_id.strip() for session_id in session_ids)


def check_sessions_last_node_session_meta(project_dir: Path, sessions_dir: Path) -> list[FinalTask]:
    project_id = project_dir.name
    tasks: list[FinalTask] = []
    session_files = iter_session_files(sessions_dir)
    if not session_files:
        return tasks

    for session_file in session_files:
        file_id = file_id_relative(project_dir, session_file)
        payload, load_error = load_json(session_file)
        if load_error:
            tasks.append(
                FinalTask(
                    check_id="session_meta",
                    project_id=project_id,
                    file_id=file_id,
                    reason=load_error,
                )
            )
            continue
        if not isinstance(payload, dict):
            tasks.append(
                FinalTask(
                    check_id="session_meta",
                    project_id=project_id,
                    file_id=file_id,
                    reason="session JSON 顶层不是对象",
                )
            )
            continue

        meta = payload.get("meta")
        if not isinstance(meta, dict):
            tasks.append(
                FinalTask(
                    check_id="session_meta",
                    project_id=project_id,
                    file_id=file_id,
                    reason="最后一个节点缺少 meta 对象",
                )
            )
            continue

        session_meta = meta.get("session_meta")
        if is_valid_session_meta_payload(session_meta):
            continue

        tasks.append(
            FinalTask(
                check_id="session_meta",
                project_id=project_id,
                file_id=file_id,
                reason="最后一个节点 meta.session_meta 格式不合法",
                preview=json.dumps(session_meta, ensure_ascii=False)[:180],
            )
        )
    return tasks


def check_sessions_meta_agent_fields(project_dir: Path, sessions_dir: Path) -> list[FinalTask]:
    project_id = project_dir.name
    tasks: list[FinalTask] = []
    session_files = iter_session_files(sessions_dir)
    if not session_files:
        return tasks

    for session_file in session_files:
        file_id = file_id_relative(project_dir, session_file)
        payload, load_error = load_json(session_file)
        if load_error:
            tasks.append(
                FinalTask(
                    check_id="meta_agent_fields",
                    project_id=project_id,
                    file_id=file_id,
                    reason=load_error,
                )
            )
            continue
        if not isinstance(payload, dict):
            tasks.append(
                FinalTask(
                    check_id="meta_agent_fields",
                    project_id=project_id,
                    file_id=file_id,
                    reason="session JSON 顶层不是对象",
                )
            )
            continue

        meta = payload.get("meta")
        if not isinstance(meta, dict):
            tasks.append(
                FinalTask(
                    check_id="meta_agent_fields",
                    project_id=project_id,
                    file_id=file_id,
                    reason="session 顶层缺少 meta 对象",
                )
            )
            continue

        present_fields = [field_name for field_name in ("agent", "agent_type") if field_name in meta]
        if not present_fields:
            continue

        tasks.append(
            FinalTask(
                check_id="meta_agent_fields",
                project_id=project_id,
                file_id=file_id,
                reason=f"session 顶层 meta 出现字段: {', '.join(present_fields)}",
                preview=json.dumps({field_name: meta.get(field_name) for field_name in present_fields}, ensure_ascii=False)[:180],
            )
        )
    return tasks


def check_sessions_image_keyword(project_dir: Path, sessions_dir: Path) -> list[FinalTask]:
    project_id = project_dir.name
    tasks: list[FinalTask] = []
    session_files = iter_session_files(sessions_dir)
    if not session_files:
        return tasks

    original_image_numbers_cache: dict[Path, set[int] | None] = {}
    for session_file in session_files:
        file_id = file_id_relative(project_dir, session_file)
        try:
            raw_text = session_file.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            tasks.append(
                FinalTask(
                    check_id="image_keyword",
                    project_id=project_id,
                    file_id=file_id,
                    reason=f"{session_file.name} 不存在",
                )
            )
            continue
        except OSError as exc:
            tasks.append(
                FinalTask(
                    check_id="image_keyword",
                    project_id=project_id,
                    file_id=file_id,
                    reason=f"{session_file.name} 读取失败: {exc}",
                )
            )
            continue

        payload: Any | None = None
        checked_original = False
        available_original_images: set[int] | None = None
        for match in IMAGE_KEYWORD_RE.finditer(raw_text):
            keyword_index = match.start()
            image_match = IMAGE_REF_RE.match(match.group(0))
            image_number = int(image_match.group("number")) if image_match else None
            if image_number is not None and session_image_exists(project_dir, session_file, image_number):
                continue
            if not checked_original:
                try:
                    payload = json.loads(raw_text)
                except json.JSONDecodeError:
                    payload = None
                available_original_images = original_session_image_numbers_for_session(
                    project_dir,
                    session_file,
                    payload,
                    original_image_numbers_cache,
                )
                checked_original = True
            if image_number is not None and available_original_images is not None and image_number not in available_original_images:
                continue
            line = raw_text.count("\n", 0, keyword_index) + 1
            line_start = raw_text.rfind("\n", 0, keyword_index)
            line_end = raw_text.find("\n", keyword_index)
            snippet_start = 0 if line_start < 0 else line_start + 1
            snippet_end = len(raw_text) if line_end < 0 else line_end
            preview = raw_text[snippet_start:snippet_end].strip()[:180]

            tasks.append(
                FinalTask(
                    check_id="image_keyword",
                    project_id=project_id,
                    file_id=file_id,
                    reason='session 原文包含 "[Image #N]" 关键词',
                    line=line,
                    preview=preview,
                )
            )
    return tasks


def build_tasks(project_dir: Path, sessions_dir: Path) -> list[FinalTask]:
    tasks: list[FinalTask] = []
    if CHECK_SWITCHES.get("cwd", False):
        tasks.extend(check_metadata_cwd(project_dir))
        tasks.extend(check_sessions_cwd(project_dir, sessions_dir))
    if CHECK_SWITCHES.get("session_meta", False):
        tasks.extend(check_sessions_last_node_session_meta(project_dir, sessions_dir))
    if CHECK_SWITCHES.get("meta_agent_fields", False):
        tasks.extend(check_sessions_meta_agent_fields(project_dir, sessions_dir))
    if CHECK_SWITCHES.get("image_keyword", False):
        tasks.extend(check_sessions_image_keyword(project_dir, sessions_dir))
    return tasks


def task_to_payload(task: FinalTask) -> dict[str, Any]:
    payload = {
        "检查项id": task.check_id,
        "产物id": task.project_id,
        "文件id": task.file_id,
        "原因": task.reason,
        "行号": task.line,
        "role": "",
        "agent": "",
        "timestamp": "",
        "工具id": "",
        "subagent": "",
        "content 原文": task.preview,
    }
    if task.extra:
        payload.update(task.extra)
    return payload


def enabled_check_ids() -> list[str]:
    return [key for key, value in CHECK_SWITCHES.items() if value]


def build_json_report(project_dir: Path, tasks: list[FinalTask]) -> dict[str, Any]:
    enabled = enabled_check_ids()
    failed_per_check = {check_id: 0 for check_id in CHECK_SWITCHES}
    for task in tasks:
        if task.check_id in failed_per_check:
            failed_per_check[task.check_id] += 1
    return {
        "report_type": REPORT_TYPE,
        "project_id": project_dir.name,
        "source_root": str(project_dir),
        "generated_at": generated_at_text(),
        "summary": {
            "enabled_checks": enabled,
            "failed": len(tasks),
            "issues": len(tasks),
            "failed_per_check": failed_per_check,
        },
        "tasks": [task_to_payload(task) for task in tasks],
    }


def check_final(
    find_path: Path | str,
    session_json_path: Path | str = "sessions",
    output_dir: Path | str | None = None,
) -> Path:
    project_dir = Path(find_path).expanduser().resolve()
    if not project_dir.exists() or not project_dir.is_dir():
        raise FileNotFoundError(f"project path does not exist or is not a directory: {project_dir}")

    sessions_dir = (project_dir / Path(session_json_path or "sessions")).resolve()
    report_output_dir = Path(output_dir).expanduser().resolve() if output_dir is not None else project_dir
    report_output_dir.mkdir(parents=True, exist_ok=True)

    print(f"checking final ({','.join(enabled_check_ids()) or 'none'}): {project_dir.name} ... ", end="", flush=True)
    tasks = build_tasks(project_dir, sessions_dir)
    print("pass" if not tasks else f"fail ({len(tasks)})")

    report_path = report_output_dir / REPORT_NAME
    report_payload = build_json_report(project_dir, tasks)
    report_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\nJSON report written: {report_path}")
    print(f"failed tasks: {report_payload['summary']['failed']}")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="收尾检查 metadata.json 与 sessions 一致性。")
    parser.add_argument("-r", dest="find_path", type=Path, required=True, help="TASK 项目根目录。")
    parser.add_argument(
        "-path",
        dest="session_json_path",
        type=Path,
        default=Path("sessions"),
        help="sessions 目录相对路径，默认 sessions。",
    )
    parser.add_argument("-o", dest="output_dir", type=Path, required=True, help=f"报告输出目录。生成 {REPORT_NAME}。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        check_final(args.find_path, args.session_json_path, args.output_dir)
    except Exception as exc:
        print(f"check_final failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

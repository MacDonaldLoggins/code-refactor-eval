#!/usr/bin/env python3
"""Prepare, download, and upload TASK data in the trajectory-repair OSS layout."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import oss2
except ImportError:  # pragma: no cover - exercised only when real OSS is used
    oss2 = None

try:
    from .common import (
        PROJECT_ROOT,
        context_task_ids,
        load_context,
        load_env_file,
        require_values,
        resolve_result_path,
        write_json_atomic,
    )
except ImportError:
    from common import (  # type: ignore[no-redef]
        PROJECT_ROOT,
        context_task_ids,
        load_context,
        load_env_file,
        require_values,
        resolve_result_path,
        write_json_atomic,
    )


DEFAULT_PREFIX = "repo轨迹修复/聚合任务/"
DEFAULT_FULL_DATA_PREFIX = "repo轨迹修复/全量数据/"
DEFAULT_AGGREGATE_DATA_PREFIX = "repo轨迹修复/聚合任务/"
RETRY_DELAYS = (1, 2, 5)


class TaskAlignmentError(RuntimeError):
    """The 聚合任务 and 全量清洗成功 OSS sources cannot be aligned for one TASK.

    Raised when a single TASK cannot be assembled because the two OSS folders
    disagree (one is missing the TASK, or their session layouts cannot be
    reconciled).  It is deliberately non-fatal at the batch level: the caller
    records the TASK as ``unaligned`` and moves on to the next TASK instead of
    aborting the whole run.
    """


def is_missing_object_error(exc: BaseException) -> bool:
    """Return True when *exc* means the OSS object does not exist (404)."""
    if oss2 is not None:
        not_found = getattr(getattr(oss2, "exceptions", None), "NoSuchKey", None)
        if not_found is not None and isinstance(exc, not_found):
            return True
    return getattr(exc, "status", None) == 404 or "NoSuchKey" in exc.__class__.__name__


def normalize_prefix(prefix: str | None) -> str:
    value = (prefix or "").replace("\\", "/").lstrip("/")
    return value if not value or value.endswith("/") else value + "/"


def task_ids_from_context(context: dict[str, Any]) -> list[str]:
    ids = context_task_ids(context)
    if not ids:
        raise ValueError("OSS operation requires non-empty task_ids")
    return ids


def build_bucket(config: dict[str, str]):
    if oss2 is None:
        raise RuntimeError("missing oss2 dependency; install requirements.txt before using OSS hooks")
    endpoint = config["OSS_ENDPOINT"]
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    auth = oss2.Auth(config["OSS_ACCESS_KEY_ID"], config["OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, endpoint, config["OSS_BUCKET"])


def retry(operation, attempts: int = 3, retryable=None):
    last_error = None
    for index in range(max(1, attempts)):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - OSS SDK exposes heterogeneous exceptions
            last_error = exc
            if retryable is not None and not retryable(exc):
                raise
            if index >= min(attempts, len(RETRY_DELAYS) + 1) - 1:
                raise
            time.sleep(RETRY_DELAYS[index])
    raise last_error  # type: ignore[misc]


def safe_extract(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"unsafe path in archive: {info.filename}")
            target = (destination / Path(*relative.parts)).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError as exc:
                raise RuntimeError(f"archive path escapes destination: {info.filename}") from exc
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise RuntimeError(f"symlink in archive is not allowed: {info.filename}")
            if info.is_dir() or info.filename.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def locate_task_root(
    extracted: Path,
    task_id: str,
    required_dirs: tuple[str, ...] = ("sessions",),
) -> Path:
    def matches(path: Path) -> bool:
        return all((path / name).is_dir() for name in required_dirs)

    direct = extracted / task_id
    if matches(direct):
        return direct
    if matches(extracted):
        return extracted
    candidates = [
        path.parent
        for path in extracted.rglob(required_dirs[0])
        if path.is_dir() and matches(path.parent)
    ]
    if len(candidates) == 1:
        return candidates[0]
    required = ", ".join(required_dirs)
    raise RuntimeError(f"cannot locate one TASK root containing [{required}] in archive for {task_id}")


def task_destination(root: Path, task_id: str) -> Path:
    return root if (root / "sessions").is_dir() and root.name == task_id else root / task_id


def sanitize_session_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_.=-" else "_" for char in name).strip("._")
    return cleaned or "session"


def download_one(
    bucket: Any,
    task_id: str,
    root: Path,
    work_dir: Path,
    prefix: str,
    overwrite: bool,
    keep_zip: bool,
) -> dict[str, Any]:
    destination = task_destination(root, task_id)
    if destination.exists() and not overwrite:
        if destination.is_dir() and (destination / "sessions").is_dir():
            return {"task_id": task_id, "status": "skipped_exists", "destination": str(destination)}
        raise RuntimeError(
            f"destination exists but is not a complete TASK; pass --overwrite after checking it: {destination}"
        )

    zip_path = work_dir / "zips" / f"{task_id}.zip"
    extracted = work_dir / "extracted" / task_id
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if extracted.exists():
        shutil.rmtree(extracted)
    key = normalize_prefix(prefix) + task_id + ".zip"
    retry(lambda: bucket.get_object_to_file(key, str(zip_path)))
    safe_extract(zip_path, extracted)
    source = locate_task_root(extracted, task_id)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    session_count = sum(1 for path in (destination / "sessions").rglob("*.json"))
    if session_count == 0:
        raise RuntimeError(f"downloaded archive has no session JSON: {task_id}")
    if not keep_zip:
        zip_path.unlink(missing_ok=True)
    return {
        "task_id": task_id,
        "status": "downloaded",
        "destination": str(destination),
        "sessions": session_count,
        "key": key,
    }


def download_archive_to_destination(
    bucket: Any,
    task_id: str,
    destination: Path,
    work_dir: Path,
    prefix: str,
    required_dirs: tuple[str, ...],
    overwrite: bool,
    keep_zip: bool,
    label: str,
    alternate_required_dirs: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Download one TASK ZIP into an explicit immutable-source destination."""
    destination = destination.expanduser().resolve()
    accepted_layouts = [required_dirs]
    if alternate_required_dirs:
        accepted_layouts.append(alternate_required_dirs)
    if destination.exists() and not overwrite:
        if destination.is_dir() and any(
            all((destination / name).is_dir() for name in layout)
            for layout in accepted_layouts
        ):
            return {
                "task_id": task_id,
                "status": "skipped_exists",
                "destination": str(destination),
                "source": label,
            }
        raise RuntimeError(
            f"{label} destination exists but is incomplete; inspect it before using --overwrite: {destination}"
        )

    zip_path = work_dir / "zips" / f"{task_id}.zip"
    extracted = work_dir / "extracted" / task_id
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if extracted.exists():
        shutil.rmtree(extracted)
    key = normalize_prefix(prefix) + task_id + ".zip"
    retry(
        lambda: bucket.get_object_to_file(key, str(zip_path)),
        retryable=lambda exc: not is_missing_object_error(exc),
    )
    safe_extract(zip_path, extracted)
    selected_layout = required_dirs
    try:
        source = locate_task_root(extracted, task_id, required_dirs)
    except RuntimeError:
        if not alternate_required_dirs:
            raise
        source = locate_task_root(extracted, task_id, alternate_required_dirs)
        selected_layout = alternate_required_dirs
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    missing = [name for name in selected_layout if not (destination / name).is_dir()]
    if missing:
        raise RuntimeError(f"downloaded {label} TASK is missing directories {missing}: {task_id}")
    if not keep_zip:
        zip_path.unlink(missing_ok=True)
    return {
        "task_id": task_id,
        "status": "downloaded",
        "destination": str(destination),
        "source": label,
        "key": key,
        "files": sum(1 for path in destination.rglob("*") if path.is_file()),
    }


def build_after_task(full_clean_task: Path, aggregate_task: Path, after_task: Path) -> dict[str, Any]:
    """Create a full working TASK without mutating either OSS source.

    The full-data TASK is the authoritative base and must be preserved as a
    complete tree.  Only ``sessions`` is replaced with the repaired aggregate
    result used by subsequent pipeline stages.  This prevents task-level
    artifacts such as reports, compatibility session directories, skills,
    hidden files, and future/unknown entries from being dropped by an
    allowlist during assembly.
    """
    if not full_clean_task.is_dir():
        raise TaskAlignmentError(f"full-data TASK directory not found: {full_clean_task}")
    aggregate_sessions = aggregate_task / "sessions"
    if not aggregate_sessions.is_dir():
        raise TaskAlignmentError(
            f"聚合任务 TASK 缺少 sessions 目录，无法与全量清洗成功对齐: {aggregate_task}"
        )

    if after_task.exists():
        shutil.rmtree(after_task)
    after_task.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(full_clean_task, after_task)
    supplemented_support_files = supplement_task_support_files(
        full_clean_task,
        aggregate_task,
        after_task,
    )
    after_sessions = after_task / "sessions"
    if after_sessions.exists():
        if after_sessions.is_dir():
            shutil.rmtree(after_sessions)
        else:
            after_sessions.unlink()
    shutil.copytree(aggregate_sessions, after_sessions)
    action_sessions = sorted(
        str(path.resolve())
        for path in after_sessions.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}
    )
    if not action_sessions:
        raise TaskAlignmentError(
            f"聚合任务 TASK 没有 session JSON/JSONL，无法与全量清洗成功对齐: {aggregate_task}"
        )
    return {
        "after_task_path": str(after_task.resolve()),
        "action_task_path": str(after_task.resolve()),
        "action_sessions": action_sessions,
        "aggregate_session_count": len(action_sessions),
        "supplemented_support_files": supplemented_support_files,
    }


def normalize_full_data_task(full_data_task: Path) -> dict[str, Any]:
    """Validate a raw ``全量数据`` TASK while preserving its downloaded layout.

    Historical archives may contain ``original_sessions``, ``convert-sessions``
    and ``clean_sessions`` while newer archives may contain only ``sessions``.
    Both layouts are valid. Never synthesize or overwrite compatibility folders.
    """
    full_data_task = full_data_task.expanduser().resolve()
    required = ("original_sessions", "convert-sessions", "clean_sessions")
    if all((full_data_task / name).is_dir() for name in required):
        return {"status": "already_normalized", "source": "full_clean"}

    source_sessions = full_data_task / "sessions"
    if not source_sessions.is_dir():
        raise TaskAlignmentError(
            f"全量数据 TASK 缺少 sessions 目录，无法转换为流水线输入: {full_data_task}"
        )

    return {
        "status": "sessions_layout_preserved",
        "source": "full_data",
        "source_sessions": str(source_sessions),
        "preserved_directories": [
            name for name in required if (full_data_task / name).is_dir()
        ],
    }


def supplement_task_support_files(
    full_clean_task: Path,
    aggregate_task: Path,
    after_task: Path,
) -> list[str]:
    """Backfill legacy support entries missing from the full-data archive.

    This is only a compatibility fallback for older/incomplete source
    archives.  It is not the mechanism that selects the contents of the
    working TASK; ``build_after_task`` starts by copying that tree completely.
    """
    supplemented: list[str] = []
    for relative in (Path("repo"), Path("docs"), Path("metadata.json")):
        target = after_task / relative
        if target.exists():
            continue
        source = next(
            (
                candidate
                for candidate in (
                    full_clean_task / relative,
                    aggregate_task / relative,
                )
                if candidate.is_dir() or candidate.is_file()
            ),
            None,
        )
        if source is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        supplemented.append(relative.as_posix())
    return supplemented


def session_identifier(path: Path, task_id: str) -> str:
    value = path.stem
    for prefix in (f"{task_id}__", f"{task_id}_"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    return sanitize_session_name(value).casefold()


def complete_after_task_sessions(full_clean_task: Path, after_task: Path) -> dict[str, Any]:
    """Fill after/sessions with clean sessions missing from repaired aggregate sessions."""
    clean_sessions = (full_clean_task / "clean_sessions").resolve()
    after_sessions = (after_task / "sessions").resolve()
    if not after_sessions.is_dir():
        raise RuntimeError(f"after TASK has no sessions: {after_task}")

    task_id = after_task.name
    existing: dict[str, Path] = {}
    for path in sorted(after_sessions.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl"}:
            continue
        identifier = session_identifier(path, task_id)
        if identifier in existing and existing[identifier] != path:
            raise RuntimeError(f"duplicate aggregate session ID {identifier}: {existing[identifier]} / {path}")
        existing[identifier] = path

    if not clean_sessions.is_dir():
        return {
            "status": "completed",
            "mode": "aggregate_only_no_clean_sessions",
            "aggregate_sessions": len(existing),
            "clean_sessions": 0,
            "added": 0,
            "added_paths": [],
            "already_present": 0,
            "already_present_paths": [],
            "final_sessions": len(existing),
        }

    clean_index: dict[str, Path] = {}
    for path in sorted(clean_sessions.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl"}:
            continue
        identifier = session_identifier(path, task_id)
        if identifier in clean_index and clean_index[identifier] != path:
            raise RuntimeError(f"duplicate clean session ID {identifier}: {clean_index[identifier]} / {path}")
        clean_index[identifier] = path

    added: list[str] = []
    skipped: list[str] = []
    for identifier, source in clean_index.items():
        relative = source.relative_to(clean_sessions)
        if identifier in existing:
            skipped.append(relative.as_posix())
            continue
        target = (after_sessions / relative).resolve()
        target.relative_to(after_sessions)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        existing[identifier] = target
        added.append(relative.as_posix())
    return {
        "status": "completed",
        "mode": "complete_missing_clean_sessions",
        "aggregate_sessions": len(existing) - len(added),
        "clean_sessions": len(clean_index),
        "added": len(added),
        "added_paths": added,
        "already_present": len(skipped),
        "already_present_paths": skipped,
        "final_sessions": len(existing),
    }


def prepare_pipeline_one(
    bucket: Any,
    task_id: str,
    before_root: Path,
    after_root: Path,
    work_dir: Path,
    full_data_prefix: str,
    aggregate_data_prefix: str,
    overwrite: bool,
    keep_zip: bool,
) -> dict[str, Any]:
    """Download both new OSS sources and build the disposable Action TASK."""
    if overwrite:
        raise RuntimeError("pipeline prepare never overwrites batch_before_tasks; remove --overwrite")
    before_task = before_root / task_id
    full_clean_task = before_task / "full_clean"
    aggregate_task = before_task / "aggregate"
    try:
        full_result = download_archive_to_destination(
            bucket,
            task_id,
            full_clean_task,
            work_dir / "full_clean",
            full_data_prefix,
            ("sessions",),
            overwrite,
            keep_zip,
            "full_data",
            alternate_required_dirs=("original_sessions", "convert-sessions", "clean_sessions"),
        )
    except TaskAlignmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - OSS SDK exposes heterogeneous exceptions
        if is_missing_object_error(exc):
            raise TaskAlignmentError(
                f"全量清洗成功 OSS 目录缺少该 TASK，无法与聚合任务对齐: {task_id}"
            ) from exc
        raise
    full_layout = normalize_full_data_task(full_clean_task)
    try:
        aggregate_result = download_archive_to_destination(
            bucket,
            task_id,
            aggregate_task,
            work_dir / "aggregate",
            aggregate_data_prefix,
            ("sessions",),
            overwrite,
            keep_zip,
            "aggregate",
        )
    except TaskAlignmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - OSS SDK exposes heterogeneous exceptions
        if is_missing_object_error(exc):
            raise TaskAlignmentError(
                f"聚合任务 OSS 目录缺少该 TASK，无法与全量清洗成功对齐: {task_id}"
            ) from exc
        raise
    after_task = after_root / task_id
    assembled = build_after_task(full_clean_task, aggregate_task, after_task)
    return {
        "task_id": task_id,
        "status": "prepared",
        "before_task_path": str(before_task.resolve()),
        "full_clean_task_path": str(full_clean_task.resolve()),
        "aggregate_task_path": str(aggregate_task.resolve()),
        "full_clean_key": full_result.get("key"),
        "full_data_key": full_result.get("key"),
        "aggregate_key": aggregate_result.get("key"),
        "full_clean_status": full_result.get("status"),
        "full_layout": full_layout,
        "aggregate_status": aggregate_result.get("status"),
        **assembled,
    }


def write_tree_to_archive(archive: zipfile.ZipFile, source: Path, archive_base: Path) -> int:
    """Write one file or directory tree into *archive* under *archive_base*.

    Everything is written verbatim, including ``.git``, ``__pycache__``,
    ``*.pyc``, empty directories, and other artifacts: uploaded archives must
    preserve the complete TASK contents for downstream use.
    """
    if source.is_file():
        archive.write(source, archive_base)
        return 1
    count = 0
    paths = sorted(source.rglob("*"))
    if not paths:
        archive.writestr(archive_base.as_posix().rstrip("/") + "/", b"")
        return 0
    for path in paths:
        relative = path.relative_to(source)
        if path.is_file():
            archive.write(path, archive_base / relative)
            count += 1
            continue
        if path.is_dir():
            try:
                next(path.iterdir())
            except StopIteration:
                archive.writestr((archive_base / relative).as_posix().rstrip("/") + "/", b"")
    return count


def zip_task(task_dir: Path, zip_path: Path, archive_root: Path | str | None = None) -> int:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    archive_base = Path(archive_root) if archive_root is not None else Path(task_dir.name)
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for child in sorted(task_dir.iterdir()):
            count += write_tree_to_archive(archive, child, archive_base / child.name)
    if count == 0:
        raise RuntimeError(f"TASK has no uploadable files: {task_dir}")
    return count


SESSIONS_SUPPORT_ENTRIES = ("repo", "docs", "metadata.json")


def zip_sessions_task(task_dir: Path, zip_path: Path, task_id: str) -> tuple[int, list[str]]:
    """Zip the sessions tree plus task-level support files (repo/docs/metadata.json)."""
    sessions_dir = (task_dir / "sessions").resolve()
    if not sessions_dir.is_dir():
        raise FileNotFoundError(f"TASK sessions directory not found for upload: {sessions_dir}")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    archive_root = Path(task_id)
    included_support: list[str] = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        count = write_tree_to_archive(archive, sessions_dir, archive_root / "sessions")
        for name in SESSIONS_SUPPORT_ENTRIES:
            source = task_dir / name
            if not source.exists():
                continue
            count += write_tree_to_archive(archive, source, archive_root / name)
            included_support.append(name)
    if count == 0:
        raise RuntimeError(f"TASK has no uploadable session files: {task_dir}")
    return count, included_support


def upload_one(
    bucket: Any,
    task_id: str,
    task_dir: Path,
    work_dir: Path,
    prefix: str,
    overwrite: bool,
    keep_zip: bool,
) -> dict[str, Any]:
    zip_path = work_dir / "zips" / f"{task_id}.zip"
    count = zip_task(task_dir, zip_path)
    key = normalize_prefix(prefix) + task_id + ".zip"
    if not overwrite:
        if bucket.object_exists(key):
            return {"task_id": task_id, "status": "skipped_exists", "key": key, "files": count}
    retry(lambda: bucket.put_object_from_file(key, str(zip_path)))
    if not keep_zip:
        zip_path.unlink(missing_ok=True)
    return {"task_id": task_id, "status": "uploaded", "key": key, "files": count}


def upload_sessions_one(
    bucket: Any,
    task_id: str,
    task_dir: Path,
    work_dir: Path,
    prefix: str,
    overwrite: bool,
    keep_zip: bool,
) -> dict[str, Any]:
    zip_path = work_dir / "session_zips" / f"{task_id}.zip"
    count, included_support = zip_sessions_task(task_dir, zip_path, task_id)
    key = normalize_prefix(prefix) + task_id + ".zip"
    if not overwrite and bucket.object_exists(key):
        return {
            "task_id": task_id,
            "status": "skipped_exists",
            "key": key,
            "files": count,
            "support_files": included_support,
        }
    retry(lambda: bucket.put_object_from_file(key, str(zip_path)))
    if not keep_zip:
        zip_path.unlink(missing_ok=True)
    return {
        "task_id": task_id,
        "status": "uploaded",
        "key": key,
        "files": count,
        "support_files": included_support,
    }


def upload_task_outputs(
    bucket: Any,
    task_id: str,
    task_dir: Path,
    work_dir: Path,
    full_prefix: str,
    sessions_prefix: str,
    overwrite: bool,
    keep_zip: bool,
) -> dict[str, Any]:
    """Upload a successful TASK as separate full-task and sessions archives."""
    full = upload_one(bucket, task_id, task_dir, work_dir, full_prefix, overwrite, keep_zip)
    try:
        sessions = upload_sessions_one(
            bucket,
            task_id,
            task_dir,
            work_dir,
            sessions_prefix,
            overwrite,
            keep_zip,
        )
    except Exception:
        # The full archive may already exist remotely; surface the failure so
        # the pipeline cannot mark the TASK complete with only half an upload.
        raise
    return {
        "task_id": task_id,
        "status": "uploaded" if full["status"] == "uploaded" and sessions["status"] == "uploaded" else "skipped_exists",
        "full": full,
        "sessions": sessions,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "download", "upload"))
    parser.add_argument("--context", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--prefix", default=None)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-existing-upload", action="store_true")
    parser.add_argument("--keep-zip", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _context_path, context = load_context(args.context)
        result_path = resolve_result_path(args.result)
        config = load_env_file(args.env_file.expanduser().resolve())
        require_values(config, ("OSS_ENDPOINT", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_BUCKET"))
        root = Path(str(context["root"])).expanduser().resolve()
        task_ids = task_ids_from_context(context)
        if args.operation == "prepare":
            full_data_prefix = normalize_prefix(
                args.prefix or config.get("OSS_FULL_DATA_PREFIX") or DEFAULT_FULL_DATA_PREFIX
            )
            aggregate_data_prefix = normalize_prefix(
                config.get("OSS_AGGREGATE_DATA_PREFIX") or DEFAULT_AGGREGATE_DATA_PREFIX
            )
            prefix = full_data_prefix
            before_root = Path(str(context.get("before_root") or root / "batch_before_tasks")).expanduser().resolve()
            after_root = Path(str(context.get("after_root") or root / "batch_after_tasks")).expanduser().resolve()
        elif args.operation == "download":
            configured_prefix = config.get("OSS_DOWNLOAD_PREFIX") or config.get("OSS_PREFIX")
            prefix = normalize_prefix(args.prefix or configured_prefix or DEFAULT_PREFIX)
        else:
            full_upload_prefix = normalize_prefix(
                config.get("OSS_UPLOAD_FULL_PREFIX") or config.get("OSS_UPLOAD_PREFIX") or ""
            )
            sessions_upload_prefix = normalize_prefix(config.get("OSS_UPLOAD_SESSIONS_PREFIX") or "")
            if not full_upload_prefix or not sessions_upload_prefix:
                raise RuntimeError(
                    "upload requires OSS_UPLOAD_FULL_PREFIX and OSS_UPLOAD_SESSIONS_PREFIX"
                )
            prefix = full_upload_prefix
        work_dir = (args.work_dir or Path(str(context["run_dir"])) / "oss").expanduser().resolve()
        bucket = build_bucket(config)
        jobs = []
        for task_id in task_ids:
            if args.operation == "prepare":
                jobs.append((task_id, lambda task_id=task_id: prepare_pipeline_one(
                    bucket,
                    task_id,
                    before_root,
                    after_root,
                    work_dir,
                    full_data_prefix,
                    aggregate_data_prefix,
                    args.overwrite,
                    args.keep_zip,
                )))
            elif args.operation == "download":
                jobs.append((task_id, lambda task_id=task_id: download_one(bucket, task_id, root, work_dir, prefix, args.overwrite, args.keep_zip)))
            else:
                destination = task_destination(root, task_id)
                if not destination.is_dir():
                    raise FileNotFoundError(f"TASK directory not found for upload: {destination}")
                jobs.append((task_id, lambda task_id=task_id, destination=destination: upload_task_outputs(
                    bucket,
                    task_id,
                    destination,
                    work_dir,
                    full_upload_prefix,
                    sessions_upload_prefix,
                    not args.skip_existing_upload,
                    args.keep_zip,
                )))

        def run_job(task_id: str, job) -> dict[str, Any]:
            # Isolate every TASK: one TASK that cannot be aligned (or otherwise
            # fails) must never abort the whole batch, so the remaining TASKs
            # keep processing instead of the run getting stuck.
            try:
                return job()
            except TaskAlignmentError as exc:
                return {
                    "task_id": task_id,
                    "operation": args.operation,
                    "status": "unaligned",
                    "reason": str(exc),
                }
            except Exception as exc:  # noqa: BLE001 - surface as a per-TASK failure
                return {
                    "task_id": task_id,
                    "operation": args.operation,
                    "status": "error",
                    "error": str(exc),
                }

        results: list[dict[str, Any]] = []
        workers = max(1, args.workers)
        if workers == 1:
            for task_id, job in jobs:
                results.append(run_job(task_id, job))
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
                futures = {pool.submit(run_job, task_id, job): task_id for task_id, job in jobs}
                for future in as_completed(futures):
                    results.append(future.result())
        results.sort(key=lambda row: row["task_id"])
        unaligned = [row for row in results if row.get("status") == "unaligned"]
        payload = {
            "operation": args.operation,
            "prefix": prefix,
            "sessions_prefix": sessions_upload_prefix if args.operation == "upload" else None,
            "results": results,
            "unaligned": unaligned,
            "failed": [
                row
                for row in results
                if row.get("status")
                not in {"prepared", "downloaded", "uploaded", "skipped_exists", "unaligned"}
            ],
        }
        write_json_atomic(result_path, payload)
        print(json.dumps({
            "operation": args.operation,
            "count": len(results),
            "failed": len(payload["failed"]),
            "unaligned": len(unaligned),
        }, ensure_ascii=False))
        return 1 if payload["failed"] else 0
    except Exception as exc:
        print(f"OSS adapter failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run trajectory repair and QA with per-TASK state isolation.

Without ``--count``, TASKs come from the project-root manifest. With
``--count``, one batch claim runs at startup and the claimed IDs are persisted
for resume. After successful upload, the QA completion API is called during
``cleanup_local_tasks`` for each TASK.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from pipeline_integrations.oss_transfer import (
    build_after_task,
    complete_after_task_sessions,
    locate_task_root,
    safe_extract,
)
from pipeline_integrations.task_structure_semantics_db import (
    TaskStructureSemanticsDatabase,
    load_task_database_settings,
)
from pipeline_integrations.push_qa import push_qa_status
from pipeline_db.task_init import normalize_records
from pipeline_db.task_update import TaskStageUpdater


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "full_pipeline_config.toml"
STAGE_ORDER = (
    "claim_tasks",
    "fetch_from_oss",
    "action_semantics_by_session",
    "quality_judge",
    "aggregate_sessions",
    "runtest_pipeline_by_task",
    "structure_check_fix_by_task",
    "split_success_fail_tasks",
    "upload_results",
    "mark_qa_complete",
    "cleanup_local_tasks",
    "notify_failure",
)
HOOK_STAGES = {
    "claim_tasks", "fetch_from_oss", "upload_results",
    "mark_qa_complete", "notify_failure",
}


@dataclass
class TaskState:
    task_id: str
    task_path: str
    status: str = "pending"
    failed_stage: str = ""
    error: str = ""
    stages: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def fail(self, stage: str, error: str) -> None:
        if self.status != "failed":
            self.status = "failed"
            self.failed_stage = stage
            self.error = error
        self.stages[stage] = "failed"

    def complete_stage(self, stage: str) -> None:
        if self.status != "failed":
            self.stages[stage] = "completed"

    def mark_unaligned(self, stage: str, reason: str) -> None:
        """Terminally skip a TASK whose OSS sources cannot be aligned.

        The TASK leaves the active set without being marked ``failed`` so the
        pipeline neither blocks nor routes it to fail_task; it is recorded as a
        skipped ``unaligned`` TASK and its database sub-status says 无法对齐.
        """
        self.status = "unaligned"
        self.failed_stage = ""
        self.error = reason
        self.stages[stage] = "unaligned"


@dataclass
class CommandResult:
    item: str
    command: list[str]
    command_text: str
    log: str
    status: str
    returncode: int | None
    elapsed_seconds: float = 0.0
    attempts: int = 0
    error: str = ""


@dataclass
class StageResult:
    name: str
    status: str
    started_at: str
    finished_at: str
    elapsed_seconds: float
    items: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class ResumeSnapshot:
    """Checkpoint loaded by ``--resume``.

    The checkpoint deliberately resumes at a stage boundary.  The stage that
    was running when the process stopped is reset to ``pending``; completed
    stages before it are represented by their saved results and are not run
    again.
    """

    run_dir: Path
    context: dict[str, Any]
    states: dict[str, TaskState]
    stages: dict[str, StageResult]
    resume_index: int
    claim_requested: bool

    @property
    def has_state(self) -> bool:
        return bool(self.states)


@dataclass(frozen=True)
class RootTaskManifest:
    task_ids: list[str]
    user_id: str | None
    user_name: str
    operator_code: str | None = None

    @property
    def identity(self) -> str:
        return str(self.user_id or self.operator_code or "")


def lock_owner_pid(path: Path) -> int | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return pid if pid > 0 else None


def process_is_running(pid: int | None) -> bool:
    if pid is None:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is NOT a liveness probe on Windows: CPython maps any
        # signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT to
        # TerminateProcess(handle, sig), so os.kill(pid, 0) would kill the very
        # process being checked (with exit code 0).  Query the exit code via
        # the Win32 API instead.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_ACCESS_DENIED (5): process exists but cannot be queried.
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # handle opened but query failed; assume alive
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class PipelineLock:
    def __init__(self, path: Path, force: bool = False):
        self.path = path.expanduser().resolve()
        self.force = force
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.force and self.path.exists():
            self.path.unlink()
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            owner_pid = lock_owner_pid(self.path)
            if owner_pid is None:
                raise RuntimeError(
                    f"pipeline lock owner is unreadable: {self.path}; "
                    "verify no pipeline is running, then pass --force-unlock"
                ) from exc
            if process_is_running(owner_pid):
                raise RuntimeError(
                    f"another full pipeline is already running (PID: {owner_pid}); "
                    f"lock: {self.path}"
                ) from exc
            # Stale lock left behind by a killed/crashed run (its finally-block
            # never released it).  Reclaim it so a fresh run is not blocked.
            print(
                f"removing stale pipeline lock (owner PID {owner_pid} is not running): {self.path}",
                file=sys.stderr,
            )
            self.path.unlink()
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "started_at": datetime.now().isoformat()}))
        self.acquired = True

    def release(self) -> None:
        if self.acquired:
            try:
                # A resumed/forced run may replace an older lock.  Never let
                # the older process remove the newer process's lock on exit.
                owner_pid = lock_owner_pid(self.path)
                if owner_pid in (None, os.getpid()):
                    self.path.unlink(missing_ok=True)
            finally:
                self.acquired = False


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"pipeline config not found: {path}")
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    if payload.get("config_version", 1) != 1:
        raise ValueError("unsupported pipeline config_version; expected 1")
    return payload


def table(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config section [{name}] must be a table")
    return value


def positive_int(value: Any, name: str, default: int | None = None) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")
    return list(value)


def repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT_DIR / path).resolve()


def workspace_path(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def path_within(path: Path, anchor: Path, label: str) -> Path:
    """Resolve *path* and require it to remain inside *anchor*."""
    resolved = path.expanduser().resolve()
    anchor_resolved = anchor.expanduser().resolve()
    try:
        resolved.relative_to(anchor_resolved)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside {anchor_resolved}: {resolved}") from exc
    return resolved


def validate_workspace_roots(
    root: Path,
    roots: dict[str, Path],
) -> dict[str, Path]:
    """Keep all pipeline buckets under --root and mutually isolated.

    In particular, an accidental overlap between ``batch_before_tasks`` and
    ``batch_after_tasks`` would make the supposedly immutable OSS snapshot
    writable by later stages, so reject it before any work starts.
    """
    root = root.expanduser().resolve()
    checked = {name: path_within(path, root, name) for name, path in roots.items()}
    entries = list(checked.items())
    for index, (left_name, left) in enumerate(entries):
        for right_name, right in entries[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError(
                    f"pipeline directories must not overlap: {left_name}={left}, "
                    f"{right_name}={right}"
                )
    return checked


def parse_name_set(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def validate_claim_task_ids(raw: Any) -> list[str]:
    """Validate task IDs returned by the claim hook before using them as paths."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("claim result task_ids must be a non-empty array")
    task_ids: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("claim result task_ids must contain only strings")
        task_id = item.strip()
        candidate = Path(task_id)
        if (
            not task_id
            or candidate.is_absolute()
            or bool(candidate.drive)
            or candidate.name != task_id
            or task_id in {".", ".."}
        ):
            raise ValueError(f"unsafe claim task_id: {item!r}")
        if task_id not in seen:
            seen.add(task_id)
            task_ids.append(task_id)
    if not task_ids:
        raise ValueError("claim result task_ids must contain at least one unique ID")
    return task_ids


def load_root_task_ids(root: Path, *, required: bool = False) -> list[str] | None:
    """Read TASK IDs from ``tasks.txt`` or its legacy filename."""
    task_file = root / "tasks.txt"
    legacy_task_file = root / "taskid.txt"
    available_task_files = [
        path for path in (task_file, legacy_task_file) if path.is_file()
    ]
    if not available_task_files:
        if required:
            raise FileNotFoundError(
                f"tasks.txt not found in project root: {root}"
            )
        return None

    def parse_task_file(path: Path) -> list[str]:
        raw_task_ids: list[str] = []
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line:
                raw_task_ids.extend(
                    item for item in re.split(r"[,;\s]+", line) if item
                )
        return validate_claim_task_ids(raw_task_ids)

    task_ids = parse_task_file(available_task_files[0])
    for alternate in available_task_files[1:]:
        if parse_task_file(alternate) != task_ids:
            raise ValueError(
                f"tasks.txt and taskid.txt contain different TASK batches: {root}"
            )
    return task_ids


def load_operator_manifest(
    root: Path,
    *,
    required: bool = False,
) -> RootTaskManifest | None:
    """Read the operator identity while accepting both supported key styles."""
    operator_file = root / "operator.json"
    if not operator_file.is_file():
        if required:
            raise FileNotFoundError(
                f"operator.json not found in project root: {root}"
            )
        return None

    operator = _read_json_object(operator_file, "operator manifest")
    user_id = str(operator.get("user_id") or operator.get("operator_id") or "").strip()
    operator_code = str(operator.get("operator_code") or "").strip()
    user_name = str(operator.get("user_name") or operator.get("operator_name") or "").strip()
    missing = [
        name
        for name, value in (
            ("user_id/operator_code", user_id or operator_code),
            ("user_name/operator_name", user_name),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"operator.json missing required value(s): {', '.join(missing)}"
        )
    return RootTaskManifest([], user_id or None, user_name, operator_code or None)


def load_root_task_manifest(root: Path) -> RootTaskManifest | None:
    """Read the authoritative local TASK batch and operator identity."""
    task_ids = load_root_task_ids(root)
    operator = load_operator_manifest(root)
    if task_ids is None and operator is None:
        return None
    if task_ids is None or operator is None:
        raise FileNotFoundError(
            "tasks.txt and operator.json must both exist for local mode"
        )
    return RootTaskManifest(
        task_ids,
        operator.user_id,
        operator.user_name,
        operator.operator_code,
    )


def write_root_task_ids(root: Path, task_ids: list[str]) -> Path:
    """Atomically persist platform-claimed TASK IDs for restart/resume."""
    validated = validate_claim_task_ids(task_ids)
    path = root / "tasks.txt"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(f"{task_id}\n" for task_id in validated), encoding="utf-8")
    temporary.replace(path)
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-r",
        "--root",
        type=Path,
        default=ROOT_DIR,
        help="流水线工作区根目录；默认使用 full_pipeline.py 所在目录。",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--task", action="append", default=[], help="本地/恢复模式下指定 TASK-ID；可重复。")
    parser.add_argument("--user-id", help="平台 QA 用户 ID。")
    parser.add_argument(
        "--count",
        "--task-count",
        dest="task_count",
        type=int,
        help="从平台领取的 TASK 数量；指定后启用平台领取模式。",
    )
    parser.add_argument(
        "--from-batch-before",
        action="store_true",
        help="从 --root 下的 batch_before_tasks 本地 TASK 目录或 TASK-ID.zip 开始处理，跳过平台领取和 OSS 下载。",
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "自动续跑最近一次未完成运行；配合 --run-dir 可指定运行目录。"
            "已完成阶段不会重复执行，中断阶段会从头重试。"
        ),
    )
    parser.add_argument(
        "--local-action-input-root",
        type=Path,
        help="本地模拟：从该 TASK/父目录读取待做 Action 的 sessions，完全绕过 OSS fetch Hook。",
    )
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--force-unlock", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--session-glob")
    parser.add_argument("--action-session-workers", type=int)
    parser.add_argument("--runtest-task-workers", type=int)
    parser.add_argument("--structure-task-workers", type=int)
    parser.add_argument("--judge-session-workers", type=int)
    parser.add_argument("--action-entry", type=Path)
    parser.add_argument("--runtest-entry", type=Path)
    parser.add_argument("--structure-entry", type=Path)
    parser.add_argument("--judge-entry", type=Path)
    parser.add_argument("--skip", default="", help="跳过指定阶段，逗号分隔。")
    parser.add_argument("--continue-on-error", action="store_true", help="保留失败 TASK，同时继续其它 TASK。")
    parser.add_argument("--dry-run", action="store_true", help="生成执行计划，不启动子进程。")
    parser.add_argument("--list-stages", action="store_true")
    return parser.parse_args(argv)


def load_env_file(path: Path, env: dict[str, str]) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env.setdefault(key, value)


def command_text(command: Iterable[str]) -> str:
    return subprocess.list2cmdline(list(command))


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def artifact_key(path: Path, anchor: Path) -> str:
    relative = path.resolve().relative_to(anchor.resolve()).as_posix()
    readable = re.sub(r"[^A-Za-z0-9._-]+", "__", relative).strip("._-")
    digest = hashlib.sha1(relative.encode("utf-8")).hexdigest()[:8]
    return f"{readable[:100]}__{digest}"


def discover_tasks(root: Path, selected: list[str], task_glob: str) -> list[Path]:
    if selected:
        candidates = []
        for item in selected:
            candidate = Path(item).expanduser()
            if not candidate.is_absolute():
                candidate = root if (root / "sessions").is_dir() and item == root.name else root / item
            candidates.append(candidate.resolve())
    elif (root / "sessions").is_dir():
        candidates = [root]
    else:
        candidates = sorted(path.resolve() for path in root.glob(task_glob) if path.is_dir())
    unique = list(dict.fromkeys(candidates))
    if not unique:
        raise FileNotFoundError(f"no TASK directories found under: {root}")
    return unique


def task_path(root: Path, task_id: str) -> Path:
    return root if (root / "sessions").is_dir() and root.name == task_id else root / task_id


def discover_sessions(task: Path, pattern: str) -> list[Path]:
    sessions = sorted(path.resolve() for path in (task / "sessions").glob(pattern) if path.is_file())
    if not sessions:
        raise FileNotFoundError(f"no sessions matched {pattern!r} under {task / 'sessions'}")
    return sessions


def execute_command(
    item: str,
    command: list[str],
    log_path: Path,
    env: dict[str, str],
    dry_run: bool,
    timeout_seconds: int,
    retries: int,
) -> CommandResult:
    if dry_run:
        return CommandResult(item, command, command_text(command), str(log_path), "planned", None)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    last_status = "error"
    last_code: int | None = 127
    last_error = ""
    attempts = max(1, retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n=== attempt {attempt}/{attempts} ===\n{command_text(command)}\n")
                completed = subprocess.run(
                    command,
                    check=False,
                    cwd=str(ROOT_DIR),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                )
            last_code = completed.returncode
            last_status = "completed" if completed.returncode == 0 else "failed"
            if last_status == "completed":
                break
        except subprocess.TimeoutExpired as exc:
            last_status = "timeout"
            last_code = 124
            last_error = f"command timed out after {timeout_seconds}s: {exc}"
        except OSError as exc:
            last_status = "error"
            last_code = 127
            last_error = str(exc)
        if attempt < attempts:
            time.sleep(min(2 * attempt, 10))
    return CommandResult(
        item, command, command_text(command), str(log_path), last_status, last_code,
        round(time.perf_counter() - started, 3), attempt, last_error,
    )


def execute_parallel_stage(
    name: str,
    jobs: list[tuple[str, list[str], Path]],
    workers: int,
    env: dict[str, str],
    dry_run: bool,
    timeout_seconds: int,
    retries: int,
) -> StageResult:
    started_wall = datetime.now().isoformat(timespec="seconds")
    started = time.perf_counter()
    if dry_run:
        results = [execute_command(item, command, log, env, True, timeout_seconds, retries) for item, command, log in jobs]
    else:
        results: list[CommandResult] = []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
            futures = {
                pool.submit(execute_command, item, command, log, env, False, timeout_seconds, retries): item
                for item, command, log in jobs
            }
            for future in as_completed(futures):
                results.append(future.result())
    results.sort(key=lambda row: row.item)
    failures = sum(result.status in {"failed", "error", "timeout"} for result in results)
    return StageResult(
        name=name,
        status="failed" if failures else ("planned" if dry_run else "completed"),
        started_at=started_wall,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        elapsed_seconds=round(time.perf_counter() - started, 3),
        items=[asdict(result) for result in results],
        summary={"total": len(results), "completed": len(results) - failures, "planned": sum(r.status == "planned" for r in results), "failed": failures},
    )


def skipped_stage(name: str, reason: str, items: list[str] | None = None) -> StageResult:
    now = datetime.now().isoformat(timespec="seconds")
    rows = [{"item": item, "status": "skipped", "reason": reason} for item in items or []]
    return StageResult(name, "skipped", now, now, 0.0, rows, {"total": len(rows), "skipped": len(rows)}, reason)


def hook_config(config: dict[str, Any], name: str) -> dict[str, Any]:
    hooks = table(config, "hooks")
    value = hooks.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config section [hooks.{name}] must be a table")
    return value


def format_hook_command(command: list[str], values: dict[str, str]) -> list[str]:
    try:
        return [token.format_map(values) for token in command]
    except KeyError as exc:
        raise ValueError(f"unknown hook command placeholder: {exc.args[0]}") from exc


def run_hook_stage(
    name: str,
    config: dict[str, Any],
    context: dict[str, Any],
    context_path: Path,
    run_dir: Path,
    base_env: dict[str, str],
    dry_run: bool,
    scope: str | None = None,
) -> tuple[StageResult, dict[str, Any] | None]:
    settings = hook_config(config, name)
    items = context.get("task_ids") or []
    if not settings.get("enabled", False):
        return skipped_stage(name, "hook disabled in full_pipeline_config.toml", [str(item) for item in items]), None
    command = string_list(settings.get("command"), f"hooks.{name}.command")
    if not command:
        raise ValueError(f"enabled hook has no command: hooks.{name}")
    hook_dir = run_dir / "hooks" / name / (scope or "batch")
    result_path = hook_dir / "result.json"
    log_path = hook_dir / "hook.log"
    values = {
        "python": str(context.get("python") or sys.executable), "repo": str(ROOT_DIR),
        "root": str(context["root"]), "run_dir": str(run_dir), "context": str(context_path),
        "result": str(result_path), "env_file": str(context.get("env_file") or ""),
        "user_id": str(context.get("user_id") or ""),
        "user_name": str(context.get("user_name") or ""),
        "task_count": str(context.get("task_count") or ""),
        "grades": str(context.get("grades_file") or ""),
    }
    rendered = format_hook_command(command, values)
    env = dict(base_env)
    env.update({
        "PIPELINE_STAGE": name, "PIPELINE_CONTEXT_PATH": str(context_path),
        "PIPELINE_HOOK_RESULT_PATH": str(result_path), "PIPELINE_TASK_ROOT": str(context["root"]),
        "PIPELINE_RUN_DIR": str(run_dir),
        "PIPELINE_USER_ID": str(context.get("user_id") or ""),
        "PIPELINE_USER_NAME": str(context.get("user_name") or ""),
        "PIPELINE_TASK_COUNT": str(context.get("task_count") or ""),
        "PIPELINE_GRADES_PATH": str(context.get("grades_file") or ""),
        "PIPELINE_ENV_FILE": str(context.get("env_file") or ""),
    })
    stage = execute_parallel_stage(
        name, [(scope or "batch", rendered, log_path)], 1, env, dry_run,
        positive_int(settings.get("timeout_seconds", 600), f"hooks.{name}.timeout_seconds"),
        # Claiming is not safely retryable: the first request may have
        # succeeded server-side even when the client observed an error.
        0 if name == "claim_tasks" else max(0, int(settings.get("retries", 1))),
    )
    if dry_run or not result_path.is_file():
        return stage, None
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"hook result must be a JSON object: {result_path}")
    stage.summary["result_path"] = str(result_path)
    return stage, payload


def write_context(path: Path, context: dict[str, Any]) -> None:
    serializable = dict(context)
    serializable["root"] = str(serializable["root"])
    serializable["run_dir"] = str(serializable["run_dir"])
    write_json_atomic(path, serializable)


RESUMABLE_REPORT_STATUSES = {"running", "interrupted", "failed"}
RESUME_STAGE_ALIASES = {"pre_upload_gate": "upload_results"}


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return payload


def find_resume_run(run_parent: Path, requested: Path | None = None) -> Path:
    """Find an explicit or newest unfinished pipeline run."""
    if requested is not None:
        run_dir = requested.expanduser().resolve()
        if not (run_dir / "full_pipeline_report.json").is_file():
            raise FileNotFoundError(f"resume report not found: {run_dir / 'full_pipeline_report.json'}")
        return run_dir
    candidates: list[Path] = []
    if run_parent.is_dir():
        for candidate in run_parent.iterdir():
            report = candidate / "full_pipeline_report.json"
            if not candidate.is_dir() or not report.is_file():
                continue
            try:
                payload = _read_json_object(report, "pipeline report")
            except ValueError:
                continue
            if payload.get("status") in RESUMABLE_REPORT_STATUSES:
                candidates.append(candidate)
    if not candidates:
        raise FileNotFoundError(
            f"no unfinished pipeline run found under {run_parent}; pass --run-dir to choose one"
        )
    return max(candidates, key=lambda path: (path / "full_pipeline_report.json").stat().st_mtime_ns)


def _stage_result_from_payload(payload: Any, expected_name: str) -> StageResult:
    if not isinstance(payload, dict) or payload.get("name") != expected_name:
        raise ValueError(f"invalid checkpoint stage entry for {expected_name}")
    return StageResult(
        name=expected_name,
        status=str(payload.get("status") or "unknown"),
        started_at=str(payload.get("started_at") or ""),
        finished_at=str(payload.get("finished_at") or ""),
        elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
        items=list(payload.get("items") or []),
        summary=dict(payload.get("summary") or {}),
        reason=str(payload.get("reason") or ""),
    )


def _stage_index(name: str) -> int | None:
    canonical = RESUME_STAGE_ALIASES.get(name, name)
    try:
        return STAGE_ORDER.index(canonical)
    except ValueError:
        return None


def load_resume_snapshot(run_dir: Path) -> ResumeSnapshot:
    """Load a run and rewind its in-flight stage to a safe boundary."""
    run_dir = run_dir.expanduser().resolve()
    report_path = run_dir / "full_pipeline_report.json"
    context_path = run_dir / "pipeline_context.json"
    report = _read_json_object(report_path, "pipeline report")
    if report.get("status") not in RESUMABLE_REPORT_STATUSES:
        raise ValueError(
            f"run is not resumable (status={report.get('status')!r}): {run_dir}"
        )
    context = (
        _read_json_object(context_path, "pipeline context")
        if context_path.is_file()
        else {}
    )
    raw_states = context.get("task_states") or report.get("task_states") or {}
    if not isinstance(raw_states, dict):
        raise ValueError(f"task_states must be an object in checkpoint: {run_dir}")
    states: dict[str, TaskState] = {}
    for task_id, raw_state in raw_states.items():
        if not isinstance(raw_state, dict):
            raise ValueError(f"invalid task state for {task_id}: {run_dir}")
        states[str(task_id)] = TaskState(
            task_id=str(raw_state.get("task_id") or task_id),
            task_path=str(raw_state.get("task_path") or ""),
            status=str(raw_state.get("status") or "pending"),
            failed_stage=str(raw_state.get("failed_stage") or ""),
            error=str(raw_state.get("error") or ""),
            stages=dict(raw_state.get("stages") or {}),
            artifacts=dict(raw_state.get("artifacts") or {}),
        )

    raw_stages = report.get("stages") or []
    if not isinstance(raw_stages, list):
        raise ValueError(f"stages must be an array in checkpoint: {run_dir}")
    stage_entries: dict[str, StageResult] = {}
    for raw_stage in raw_stages:
        if not isinstance(raw_stage, dict):
            continue
        name = str(raw_stage.get("name") or "")
        if name in STAGE_ORDER and name not in stage_entries:
            stage_entries[name] = _stage_result_from_payload(raw_stage, name)

    completed_prefix: list[str] = []
    for name in STAGE_ORDER:
        stage = stage_entries.get(name)
        if stage is None or stage.status not in {"completed", "skipped"}:
            break
        completed_prefix.append(name)
    resume_index = len(completed_prefix)
    for state in states.values():
        failed_index = _stage_index(state.failed_stage)
        if failed_index is not None:
            resume_index = min(resume_index, failed_index)
    if resume_index >= len(STAGE_ORDER):
        resume_index = len(STAGE_ORDER) - 1

    root = Path(str(context.get("root") or run_dir.parent)).expanduser().resolve()
    split_index = STAGE_ORDER.index("split_success_fail_tasks")
    qa_complete_index = STAGE_ORDER.index("mark_qa_complete")
    resume_stage_name = STAGE_ORDER[resume_index]
    resume_stage_completed_items = {
        str(item.get("item"))
        for item in (stage_entries.get(resume_stage_name).items if stage_entries.get(resume_stage_name) else [])
        if isinstance(item, dict) and item.get("status") == "completed" and item.get("item")
    }
    for state in states.values():
        if state.status == "unaligned":
            # A terminally skipped 无法对齐 TASK must not be revived on resume.
            continue
        state.stages = {
            name: value
            for name, value in state.stages.items()
            if _stage_index(name) is None or (_stage_index(name) < resume_index)
        }
        state.artifacts = {
            name: value
            for name, value in state.artifacts.items()
            if _stage_index(name) is None or (_stage_index(name) < resume_index)
        }
        if resume_index <= STAGE_ORDER.index("fetch_from_oss"):
            state.task_path = str(root / state.task_id)
        elif resume_index <= split_index:
            fetch_artifact = state.artifacts.get("fetch_from_oss")
            after_task_path = (
                fetch_artifact.get("after_task_path")
                if isinstance(fetch_artifact, dict)
                else None
            )
            if after_task_path:
                state.task_path = str(Path(str(after_task_path)).expanduser().resolve())
            else:
                state.task_path = str(root / state.task_id)
        if resume_index <= split_index:
            state.artifacts.pop("task_bucket", None)
            state.artifacts.pop("bucket_task_path", None)

        failed_index = _stage_index(state.failed_stage)
        if failed_index is not None and failed_index < resume_index:
            continue
        if (
            resume_stage_name == "cleanup_local_tasks"
            and state.task_id in resume_stage_completed_items
        ):
            state.stages[resume_stage_name] = "completed"
            state.status = "completed"
            state.failed_stage = ""
            state.error = ""
            continue
        if resume_index > qa_complete_index:
            state.status = "completed"
        else:
            state.status = "pending"
            state.failed_stage = ""
            state.error = ""

    resume_options = context.get("resume_options")
    if not isinstance(resume_options, dict):
        resume_options = {}
    raw_claim_requested = context.get("claim_requested")
    if not isinstance(raw_claim_requested, bool):
        raw_claim_requested = resume_options.get("claim_requested")
    if not isinstance(raw_claim_requested, bool):
        claim_stage = stage_entries.get("claim_tasks")
        raw_claim_requested = bool(
            claim_stage is not None
            and claim_stage.status == "completed"
            and context.get("claimed_task_ids")
        )

    return ResumeSnapshot(
        run_dir=run_dir,
        context=context,
        states=states,
        stages={name: stage_entries[name] for name in completed_prefix[:resume_index]},
        resume_index=resume_index,
        claim_requested=raw_claim_requested,
    )


def apply_resume_options(args: argparse.Namespace, snapshot: ResumeSnapshot) -> None:
    """Restore the original task-selection options for a foolproof resume."""
    if any(
        (
            args.task,
            args.user_id is not None,
            args.task_count is not None,
            args.from_batch_before,
            args.local_action_input_root is not None,
            bool(args.skip),
        )
    ):
        raise ValueError(
            "--resume cannot be combined with task-selection options; "
            "stop the old run and start a new run to add TASKs"
        )
    options = snapshot.context.get("resume_options")
    if not isinstance(options, dict):
        return
    args.task = [str(item) for item in options.get("task", [])]
    args.user_id = options.get("user_id")
    args.task_count = options.get("task_count")
    args.from_batch_before = bool(options.get("from_batch_before", False))
    local_root = options.get("local_action_input_root")
    args.local_action_input_root = Path(str(local_root)) if local_root else None
    args.skip = ",".join(str(item) for item in options.get("skip", []))
    args.continue_on_error = bool(options.get("continue_on_error", False))
    args.session_glob = options.get("session_glob") or args.session_glob
    for name in (
        "action_session_workers",
        "runtest_task_workers",
        "structure_task_workers",
        "judge_session_workers",
        "action_entry",
        "runtest_entry",
        "structure_entry",
        "judge_entry",
        "python",
        "lock_file",
    ):
        value = options.get(name)
        if value is not None:
            setattr(
                args,
                name,
                Path(str(value)) if name.endswith(("_entry", "lock_file")) else value,
            )


def active_states(states: dict[str, TaskState]) -> list[TaskState]:
    return [state for state in states.values() if state.status == "pending"]


def overlay_prepared_sessions(
    full_task: Path,
    action_task: Path,
    replacements: list[dict[str, Any]],
    *,
    skip_missing: bool = False,
    skipped_missing: list[str] | None = None,
) -> list[str]:
    """Replace repaired sessions while preserving all other sessions.

    Some semantic-repair steps intentionally exclude a session (for example,
    when the model classifies it as off-topic).  In that case the disposable
    Action copy may no longer contain the source file.  Callers that pass
    ``skip_missing=True`` leave the original full-TASK session untouched and
    receive its relative path through ``skipped_missing``.
    """
    full_sessions = (full_task / "sessions").resolve()
    action_sessions = (action_task / "sessions").resolve()
    if not full_sessions.is_dir():
        raise FileNotFoundError(f"full TASK sessions directory not found: {full_sessions}")
    if not action_sessions.is_dir():
        raise FileNotFoundError(f"action TASK sessions directory not found: {action_sessions}")
    written: list[str] = []
    for replacement in replacements:
        relative = PurePosixPath(str(replacement.get("relative_path") or ""))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe replacement relative path: {relative}")
        source = (action_sessions / Path(*relative.parts)).resolve()
        target = (full_sessions / Path(*relative.parts)).resolve()
        try:
            source.relative_to(action_sessions)
            target.relative_to(full_sessions)
        except ValueError as exc:
            raise ValueError(f"replacement path escapes sessions root: {relative}") from exc
        if not source.is_file():
            if not skip_missing:
                raise FileNotFoundError(f"prepared repaired session not found: {source}")
            if skipped_missing is not None:
                skipped_missing.append(relative.as_posix())
            continue
        if not target.is_file():
            raise FileNotFoundError(f"original session to replace not found: {target}")
        temporary = target.with_name(target.name + ".pipeline-replace.tmp")
        shutil.copy2(source, temporary)
        temporary.replace(target)
        written.append(relative.as_posix())
    return written


def prepare_local_action_inputs(
    states: dict[str, TaskState],
    input_root: Path,
    session_glob: str,
    dry_run: bool,
) -> StageResult:
    """Build fetch-stage metadata from local prepared sessions without OSS."""
    started = datetime.now().isoformat(timespec="seconds")
    input_root = input_root.expanduser().resolve()
    items: list[dict[str, Any]] = []
    for state in active_states(states):
        candidate = input_root if (input_root / "sessions").is_dir() and input_root.name == state.task_id else input_root / state.task_id
        try:
            sessions_root = (candidate / "sessions").resolve()
            sessions = [
                session
                for session in discover_sessions(candidate, session_glob)
                if "ua_excluded" not in session.relative_to(sessions_root).parts
            ]
            if not sessions:
                raise FileNotFoundError(
                    f"no non-excluded sessions matched {session_glob!r} under {sessions_root}"
                )
            full_sessions = (Path(state.task_path).resolve() / "sessions").resolve()
            replacements: list[dict[str, str]] = []
            action_sessions: list[str] = []
            for session in sessions:
                relative = session.relative_to(candidate.resolve() / "sessions")
                target = (full_sessions / relative).resolve()
                target.relative_to(full_sessions)
                if not target.is_file():
                    raise FileNotFoundError(f"original session to replace not found: {target}")
                action_sessions.append(str(session.resolve()))
                replacements.append({"session_id": session.stem, "relative_path": relative.as_posix(), "source": "local"})
            state.artifacts["fetch_from_oss"] = {
                "operation": "local-prepare",
                "status": "planned" if dry_run else "prepared",
                "full_task_path": state.task_path,
                "action_task_path": str(candidate.resolve()),
                "action_sessions": action_sessions,
                "replacements": replacements,
                "replacement_count": len(replacements),
            }
            if dry_run:
                state.stages["fetch_from_oss"] = "planned"
                status = "planned"
            else:
                state.complete_stage("fetch_from_oss")
                status = "completed"
            items.append({"item": state.task_id, "status": status, "action_sessions": len(action_sessions), "source": "local"})
        except Exception as exc:
            state.fail("fetch_from_oss", str(exc))
            items.append({"item": state.task_id, "status": "failed", "error": str(exc), "source": "local"})
    failed = sum(item["status"] == "failed" for item in items)
    completed = sum(item["status"] == "completed" for item in items)
    planned = sum(item["status"] == "planned" for item in items)
    status = "failed" if failed and not completed else ("partial" if failed else ("planned" if planned else "completed"))
    return StageResult(
        "fetch_from_oss",
        status,
        started,
        datetime.now().isoformat(timespec="seconds"),
        0.0,
        items,
        {"tasks_total": len(items), "tasks_completed": completed, "tasks_failed": failed, "tasks_planned": planned, "source": "local"},
    )


def discover_local_before_task_ids(
    before_root: Path,
    selected: list[str],
    task_glob: str,
) -> list[str]:
    """Discover TASK IDs from local directories or ``TASK-ID.zip`` inputs."""
    if selected:
        return validate_claim_task_ids(selected)
    before_root = before_root.expanduser().resolve()
    candidates = [
        path.name
        for path in before_root.glob(task_glob)
        if path.is_dir()
    ]
    candidates.extend(
        path.stem
        for path in before_root.glob(f"{task_glob}.zip")
        if path.is_file()
    )
    if not candidates:
        raise FileNotFoundError(
            f"no local TASK directories or ZIPs matching {task_glob!r} under {before_root}"
        )
    return validate_claim_task_ids(sorted(dict.fromkeys(candidates)))


def _copy_local_before_task(source: Path, after_task: Path) -> list[str]:
    """Copy a local TASK source to the disposable after-task directory."""
    if after_task.exists():
        shutil.rmtree(after_task)
    after_task.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, after_task)
    return sorted(
        str(path.resolve())
        for path in (after_task / "sessions").rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}
    )


def prepare_local_before_inputs(
    states: dict[str, TaskState],
    before_root: Path,
    after_root: Path,
    run_dir: Path,
    dry_run: bool,
) -> StageResult:
    """Prepare local ``batch_before_tasks`` directories or TASK ZIPs.

    A directory may already use the canonical ``full_clean``/``aggregate``
    layout, or it may simply be a complete TASK containing ``sessions``. A
    ``TASK-ID.zip`` is safely extracted and treated as the latter form.
    """
    started = datetime.now().isoformat(timespec="seconds")
    before_root = before_root.expanduser().resolve()
    after_root = after_root.expanduser().resolve()
    items: list[dict[str, Any]] = []
    for state in active_states(states):
        task_id = state.task_id
        source_dir = before_root / task_id
        source_zip = before_root / f"{task_id}.zip"
        after_task = after_root / task_id
        try:
            source: Path
            source_kind: str
            if source_dir.is_dir():
                source = source_dir
                source_kind = "directory"
            elif source_zip.is_file():
                extracted = run_dir / "local_before" / "extracted" / task_id
                if extracted.exists():
                    shutil.rmtree(extracted)
                safe_extract(source_zip, extracted)
                source = locate_task_root(extracted, task_id)
                source_kind = "zip"
            else:
                raise FileNotFoundError(
                    f"local TASK input not found under {before_root}: {task_id} or {task_id}.zip"
                )

            full_clean = source / "full_clean"
            aggregate = source / "aggregate"
            canonical = (
                (full_clean / "original_sessions").is_dir()
                and (full_clean / "convert-sessions").is_dir()
                and (full_clean / "clean_sessions").is_dir()
                and (aggregate / "sessions").is_dir()
            )
            if canonical:
                assembled = build_after_task(full_clean, aggregate, after_task)
                artifact = {
                    "operation": "local-before",
                    "status": "planned" if dry_run else "prepared",
                    "source_kind": source_kind,
                    "before_task_path": str(source.resolve()),
                    "full_clean_task_path": str(full_clean.resolve()),
                    "aggregate_task_path": str(aggregate.resolve()),
                    **assembled,
                }
            else:
                if not (source / "sessions").is_dir():
                    raise RuntimeError(
                        f"local TASK input must contain sessions or canonical full_clean/aggregate directories: {source}"
                    )
                action_sessions = _copy_local_before_task(source, after_task)
                if not action_sessions:
                    raise RuntimeError(f"local TASK has no session JSON/JSONL: {source}")
                artifact = {
                    "operation": "local-before",
                    "status": "planned" if dry_run else "prepared",
                    "source_kind": source_kind,
                    "before_task_path": str(source.resolve()),
                    "after_task_path": str(after_task.resolve()),
                    "action_task_path": str(after_task.resolve()),
                    "action_sessions": action_sessions,
                    "aggregate_session_count": len(action_sessions),
                }
            state.artifacts["fetch_from_oss"] = artifact
            state.task_path = str(after_task.resolve())
            state.stages["fetch_from_oss"] = "planned" if dry_run else "completed"
            items.append({
                "item": task_id,
                "status": "planned" if dry_run else "completed",
                "source": source_kind,
                "path": str(source.resolve()),
            })
        except Exception as exc:
            state.fail("fetch_from_oss", str(exc))
            items.append({"item": task_id, "status": "failed", "error": str(exc)})
    failed = sum(item["status"] == "failed" for item in items)
    completed = sum(item["status"] == "completed" for item in items)
    planned = sum(item["status"] == "planned" for item in items)
    status = "failed" if failed and not completed else (
        "partial" if failed else ("planned" if planned else "completed")
    )
    return StageResult(
        "fetch_from_oss",
        status,
        started,
        datetime.now().isoformat(timespec="seconds"),
        0.0,
        items,
        {
            "tasks_total": len(items),
            "tasks_completed": completed,
            "tasks_failed": failed,
            "tasks_planned": planned,
            "source": "local-before",
        },
    )


def scoped_context(base: dict[str, Any], state: TaskState) -> dict[str, Any]:
    context = dict(base)
    context["task_ids"] = [state.task_id]
    context["task_paths"] = [state.task_path]
    return context


def validate_fetch_artifact_paths(
    artifact: dict[str, Any],
    task_id: str,
    before_root: Path,
    after_root: Path,
) -> dict[str, Any]:
    """Validate paths returned by the fetch hook before using them.

    A Hook is an external process.  Its metadata must not be allowed to point
    Action or aggregation at an arbitrary path outside the pipeline buckets.
    The canonical layout is deliberately strict: one TASK ID maps to exactly
    ``before/TASK-ID/{full_clean,aggregate}`` and ``after/TASK-ID``.
    """
    if not isinstance(artifact, dict):
        return artifact
    expected = {
        "full_clean_task_path": before_root / task_id / "full_clean",
        "aggregate_task_path": before_root / task_id / "aggregate",
        "after_task_path": after_root / task_id,
        "action_task_path": after_root / task_id,
    }
    checked = dict(artifact)
    for key, expected_path in expected.items():
        raw = artifact.get(key)
        if raw in (None, ""):
            continue
        actual = Path(str(raw)).expanduser().resolve()
        expected_resolved = expected_path.expanduser().resolve()
        path_within(actual, expected_resolved.parent, key)
        if actual != expected_resolved:
            raise ValueError(
                f"{key} must be {expected_resolved} for {task_id}, got {actual}"
            )
        checked[key] = str(actual)
    return checked


def validate_online_fetch_artifact(
    artifact: dict[str, Any],
    task_id: str,
    before_root: Path,
    after_root: Path,
) -> dict[str, Any]:
    """Require a complete, on-disk preparation result for claimed TASKs."""
    checked = validate_fetch_artifact_paths(artifact, task_id, before_root, after_root)
    required = (
        "full_clean_task_path",
        "aggregate_task_path",
        "after_task_path",
        "action_task_path",
    )
    missing = [key for key in required if not checked.get(key)]
    if missing:
        raise ValueError("OSS fetch result is missing required path(s): " + ", ".join(missing))
    if checked["action_task_path"] != checked["after_task_path"]:
        raise ValueError("action_task_path must equal after_task_path for claimed TASKs")

    full_clean = Path(checked["full_clean_task_path"])
    aggregate = Path(checked["aggregate_task_path"])
    after = Path(checked["after_task_path"])
    required_dirs = [full_clean, aggregate / "sessions", after / "sessions"]
    missing_dirs = [str(path) for path in required_dirs if not path.is_dir()]
    if missing_dirs:
        raise ValueError("OSS fetch result is missing required directory(ies): " + ", ".join(missing_dirs))

    raw_sessions = checked.get("action_sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise ValueError("OSS fetch result must contain non-empty action_sessions")
    action_sessions_root = (after / "sessions").resolve()
    action_sessions: list[str] = []
    seen_sessions: set[str] = set()
    for raw in raw_sessions:
        session = Path(str(raw)).expanduser().resolve()
        try:
            session.relative_to(action_sessions_root)
        except ValueError as exc:
            raise ValueError(f"action session escapes after/sessions: {session}") from exc
        if not session.is_file() or session.suffix.lower() not in {".json", ".jsonl"}:
            raise ValueError(f"invalid prepared action session: {session}")
        normalized = str(session)
        if normalized in seen_sessions:
            raise ValueError(f"duplicate prepared action session: {session}")
        seen_sessions.add(normalized)
        action_sessions.append(normalized)
    available_sessions = {
        str(path.resolve())
        for path in action_sessions_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}
    }
    if seen_sessions != available_sessions:
        missing = sorted(available_sessions - seen_sessions)
        extra = sorted(seen_sessions - available_sessions)
        raise ValueError(
            "action_sessions must cover exactly after/sessions"
            + (f"; missing={missing}" if missing else "")
            + (f"; extra={extra}" if extra else "")
        )
    checked["action_sessions"] = sorted(action_sessions)
    return checked


def stage_from_task_results(
    name: str,
    results: list[dict[str, Any]],
    states: dict[str, TaskState],
    item_to_task: dict[str, str],
) -> StageResult:
    failed_tasks: set[str] = set()
    completed_tasks: set[str] = set()
    for item in results:
        task_id = item_to_task.get(str(item.get("item", "")), str(item.get("task_id", "")))
        status = item.get("status")
        if status in {"failed", "error", "timeout"}:
            failed_tasks.add(task_id)
            if task_id in states:
                states[task_id].fail(name, str(item.get("error") or f"{name} failed"))
        elif status == "completed":
            completed_tasks.add(task_id)
            if task_id in states:
                states[task_id].complete_stage(name)
    if failed_tasks and completed_tasks:
        status = "partial"
    elif failed_tasks:
        status = "failed"
    else:
        status = "completed"
    return StageResult(
        name=name,
        status=status,
        started_at=datetime.now().isoformat(timespec="seconds"),
        finished_at=datetime.now().isoformat(timespec="seconds"),
        elapsed_seconds=0.0,
        items=results,
        summary={"tasks_total": len(item_to_task), "tasks_completed": len(completed_tasks), "tasks_failed": len(failed_tasks)},
    )


def merge_jsonl_files(paths: list[Path], output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    seen: set[tuple[Any, Any]] = set()
    count = 0
    with output.open("w", encoding="utf-8") as target:
        for path in paths:
            with path.open(encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    key = (record.get("task_id"), record.get("session_id") or record.get("id"))
                    if key in seen:
                        raise RuntimeError(f"duplicate quality record {key} in {path}:{line_number}")
                    seen.add(key)
                    target.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += 1
    return count


def has_completed_action_report(report_root: Path) -> bool:
    reports = sorted(
        report_root.glob("*/action_semantics_fix_report.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for report in reports:
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
            summary = payload.get("summary") or {}
            return (
                int(summary.get("returncode") or 0) == 0
                and int(summary.get("failed") or 0) == 0
                and int(summary.get("steps_recorded") or 0) == int(summary.get("steps_selected") or 0)
            )
        except (OSError, ValueError, TypeError):
            continue
    return False


def prune_runs(parent: Path, current: Path, keep: int) -> None:
    if keep < 1 or not parent.is_dir():
        return
    candidates = sorted(
        (path for path in parent.iterdir() if path.is_dir() and path != current and (path / "full_pipeline_report.json").is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    old_slots = max(0, keep - 1)
    for old in candidates[old_slots:]:
        shutil.rmtree(old)


def copy_task_to_bucket(source: Path, bucket_root: Path, task_id: str) -> Path:
    source = source.expanduser().resolve()
    bucket_root = bucket_root.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"TASK source directory not found: {source}")
    target = (bucket_root / task_id).resolve()
    target.relative_to(bucket_root)
    bucket_root.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".pipeline-copy.tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, temporary)
    temporary.replace(target)
    return target


def split_task_buckets(
    states: dict[str, TaskState],
    success_root: Path,
    fail_root: Path,
    dry_run: bool,
) -> StageResult:
    """Copy final after-TASKs to success/fail buckets without touching sources."""
    started = datetime.now().isoformat(timespec="seconds")
    items: list[dict[str, Any]] = []
    for state in states.values():
        if state.status == "unaligned":
            # Never copied anywhere: the TASK was skipped before assembly.
            items.append({"item": state.task_id, "status": "skipped", "reason": "unaligned"})
            continue
        bucket = fail_root if state.status == "failed" else success_root
        bucket_name = "fail_task" if state.status == "failed" else "success_task"
        if dry_run:
            items.append({"item": state.task_id, "status": "planned", "bucket": bucket_name})
            continue
        try:
            target = copy_task_to_bucket(Path(state.task_path), bucket, state.task_id)
            state.artifacts["task_bucket"] = bucket_name
            state.artifacts["bucket_task_path"] = str(target)
            state.task_path = str(target)
            if state.status == "pending":
                state.complete_stage("split_success_fail_tasks")
            items.append({
                "item": state.task_id,
                "status": "completed",
                "bucket": bucket_name,
                "path": str(target),
            })
        except Exception as exc:
            state.fail("split_success_fail_tasks", str(exc))
            items.append({"item": state.task_id, "status": "failed", "bucket": bucket_name, "error": str(exc)})
    failed = sum(item["status"] == "failed" for item in items)
    completed = sum(item["status"] == "completed" for item in items)
    planned = sum(item["status"] == "planned" for item in items)
    status = "failed" if failed and not completed else ("partial" if failed else ("planned" if planned else "completed"))
    return StageResult(
        "split_success_fail_tasks",
        status,
        started,
        datetime.now().isoformat(timespec="seconds"),
        0.0,
        items,
        {"tasks_total": len(items), "tasks_completed": completed, "tasks_failed": failed, "tasks_planned": planned},
    )


def relocate_failed_task_buckets(
    states: dict[str, TaskState],
    success_root: Path,
    fail_root: Path,
) -> None:
    """Move any later-failed TASK out of success_task before upload."""
    success_root = success_root.expanduser().resolve()
    fail_root = fail_root.expanduser().resolve()
    for state in states.values():
        if state.status != "failed":
            continue
        source = Path(state.task_path).expanduser().resolve()
        if not source.is_dir():
            continue
        expected_target = (fail_root / state.task_id).resolve()
        target = expected_target if source == expected_target else copy_task_to_bucket(source, fail_root, state.task_id)
        success_target = (success_root / state.task_id).resolve()
        if success_target != source and success_target.is_dir():
            shutil.rmtree(success_target)
        state.task_path = str(target)
        state.artifacts["task_bucket"] = "fail_task"
        state.artifacts["bucket_task_path"] = str(target)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_stages:
        for index, name in enumerate(STAGE_ORDER, 1):
            print(f"{index:02d}\t{name}\t{'hook' if name in HOOK_STAGES else 'local'}")
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)
    general = table(config, "general")
    action_cfg = table(config, "action")
    runtest_cfg = table(config, "runtest")
    runtest_enabled = bool(runtest_cfg.get("enabled", False))
    structure_cfg = table(config, "structure")
    judge_cfg = table(config, "judge")
    database_cfg = table(config, "database")
    judge_enabled = bool(judge_cfg.get("enabled", False))
    paths_cfg = table(config, "paths")
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"pipeline root not found: {root}")
    pipeline_roots = validate_workspace_roots(root, {
        "before_tasks": workspace_path(root, paths_cfg.get("before_tasks", "batch_before_tasks")),
        "after_tasks": workspace_path(root, paths_cfg.get("after_tasks", "batch_after_tasks")),
        "success_tasks": workspace_path(root, paths_cfg.get("success_tasks", "success_task")),
        "fail_tasks": workspace_path(root, paths_cfg.get("fail_tasks", "fail_task")),
    })
    before_tasks_root = pipeline_roots["before_tasks"]
    after_tasks_root = pipeline_roots["after_tasks"]
    success_tasks_root = pipeline_roots["success_tasks"]
    fail_tasks_root = pipeline_roots["fail_tasks"]
    explicit_task_selection = bool(args.task or args.user_id is not None)
    if args.task_count is not None and args.task:
        raise ValueError("--count cannot be combined with --task")
    run_parent = root / str(general.get("run_subdir", "pipeline_runs"))
    resume_snapshot: ResumeSnapshot | None = None
    if args.resume:
        if args.dry_run:
            raise ValueError("--resume cannot be combined with --dry-run")
        resume_dir = find_resume_run(run_parent, args.run_dir)
        resume_snapshot = load_resume_snapshot(resume_dir)
        checkpoint_root = Path(str(resume_snapshot.context.get("root") or root)).resolve()
        if checkpoint_root != root:
            raise ValueError(
                f"resume checkpoint belongs to a different root: {checkpoint_root}"
            )
        checkpoint_config = resume_snapshot.context.get("config_path")
        if checkpoint_config and Path(str(checkpoint_config)).resolve() != config_path:
            raise ValueError(
                f"resume checkpoint uses a different config: {checkpoint_config}"
            )
        apply_resume_options(args, resume_snapshot)
        args.run_dir = resume_dir

    platform_mode = (
        resume_snapshot.claim_requested
        if resume_snapshot is not None
        else args.task_count is not None
    )
    root_task_manifest: RootTaskManifest | None = None
    operator_manifest: RootTaskManifest | None = None
    resume_persisted_task_ids: list[str] | None = None
    if platform_mode:
        if args.from_batch_before or args.local_action_input_root:
            raise ValueError(
                "--count platform mode cannot use local input modes"
            )
        operator_manifest = load_operator_manifest(root, required=True)
        assert operator_manifest is not None
        if args.user_id and args.user_id != operator_manifest.identity:
            raise ValueError(
                "--user-id does not match operator.json"
            )
        args.user_id = operator_manifest.identity
        if resume_snapshot is not None:
            checkpoint_user_id = str(
                resume_snapshot.context.get("user_id") or ""
            ).strip()
            if checkpoint_user_id and checkpoint_user_id != operator_manifest.identity:
                raise ValueError(
                    "operator.json identity changed after the run started; start a new run"
                )
            checkpoint_task_ids = (
                list(resume_snapshot.states)
                or [
                    str(item)
                    for item in (
                        resume_snapshot.context.get("claimed_task_ids")
                        or resume_snapshot.context.get("task_ids")
                        or []
                    )
                ]
            )
            resume_persisted_task_ids = load_root_task_ids(root)
            if (
                checkpoint_task_ids
                and resume_persisted_task_ids
                and checkpoint_task_ids != resume_persisted_task_ids
            ):
                raise ValueError(
                    "tasks.txt changed after platform claim; start a new run"
                )
    else:
        root_task_manifest = load_root_task_manifest(root)
    if root_task_manifest is not None:
        if explicit_task_selection and resume_snapshot is None:
            raise ValueError(
                "tasks.txt/operator.json are authoritative in local mode; "
                "do not also pass --task or --user-id"
            )
        if resume_snapshot is not None:
            checkpoint_task_ids = (
                list(resume_snapshot.states)
                or [str(item) for item in resume_snapshot.context.get("task_ids", [])]
            )
            if checkpoint_task_ids and checkpoint_task_ids != root_task_manifest.task_ids:
                raise ValueError(
                    "tasks.txt changed after the run started; start a new run for the new TASK batch"
                )
            checkpoint_user_id = str(
                resume_snapshot.context.get("user_id") or ""
            ).strip()
            checkpoint_user_name = str(
                resume_snapshot.context.get("user_name") or ""
            ).strip()
            if checkpoint_user_id and checkpoint_user_id != root_task_manifest.identity:
                raise ValueError(
                    "operator.json user_id changed after the run started; start a new run"
                )
            if checkpoint_user_name and checkpoint_user_name != root_task_manifest.user_name:
                raise ValueError(
                    "operator.json user_name changed after the run started; start a new run"
                )
        args.task = list(root_task_manifest.task_ids)
        args.user_id = root_task_manifest.identity
        args.task_count = None
    skip = parse_name_set(args.skip)
    # QA completion is performed per TASK during cleanup_local_tasks.
    skip.add("mark_qa_complete")
    if not platform_mode:
        skip.add("claim_tasks")
    if platform_mode and "claim_tasks" in skip:
        raise ValueError("--count platform mode cannot skip claim_tasks")
    unknown = skip - set(STAGE_ORDER)
    if unknown:
        raise ValueError(f"unknown pipeline stage(s): {', '.join(sorted(unknown))}")
    if args.from_batch_before:
        if args.local_action_input_root:
            raise ValueError("--from-batch-before cannot be combined with --local-action-input-root")
        if "fetch_from_oss" in skip:
            raise ValueError("--from-batch-before uses the fetch_from_oss stage for local preparation; do not skip it")
        if args.task_count is not None:
            raise ValueError("--count is only valid when claiming TASKs from the platform")
        skip.add("claim_tasks")
        if not args.user_id:
            skip.add("mark_qa_complete")

    if args.task:
        args.task = validate_claim_task_ids(args.task)

    claim_hook_enabled = (
        "claim_tasks" not in skip
        and bool(hook_config(config, "claim_tasks").get("enabled", False))
    )
    fetch_hook_enabled = (
        "fetch_from_oss" not in skip
        and bool(hook_config(config, "fetch_from_oss").get("enabled", False))
    )
    if claim_hook_enabled:
        if not args.user_id:
            raise ValueError("enabled claim_tasks requires --user-id")
        task_count = positive_int(args.task_count, "task_count")
        if not fetch_hook_enabled and not args.local_action_input_root:
            raise ValueError("platform claim mode requires enabled fetch_from_oss")
    elif args.user_id and "claim_tasks" not in skip:
        raise ValueError(
            "--user-id requires enabled claim_tasks; omit --user-id for local TASK processing"
        )
    elif args.task_count is not None and "claim_tasks" not in skip:
        raise ValueError(
            "--count requires enabled claim_tasks; omit --count for local TASK processing"
        )
    else:
        task_count = None
    claim_requested = (
        resume_snapshot.claim_requested
        if resume_snapshot is not None
        else claim_hook_enabled
    )
    selected_operator = root_task_manifest or operator_manifest
    operator_user_name = (
        selected_operator.user_name
        if selected_operator is not None
        else str((resume_snapshot.context if resume_snapshot else {}).get("user_name") or "")
    )
    operator_code = (
        str(selected_operator.operator_code or "")
        if selected_operator is not None
        else str((resume_snapshot.context if resume_snapshot else {}).get("operator_code") or "")
    )
    operator_database_id = (
        selected_operator.user_id
        if selected_operator is not None
        else None
    )

    task_glob = str(general.get("task_glob", "TASK-*")).strip()
    session_glob = args.session_glob or str(general.get("session_glob", "**/*.json")).strip()
    action_session_workers = positive_int(args.action_session_workers or general.get("action_session_workers", 4), "action_session_workers")
    runtest_task_workers = (
        positive_int(
            args.runtest_task_workers or general.get("runtest_task_workers", 4),
            "runtest_task_workers",
        )
        if runtest_enabled
        else 1
    )
    structure_task_workers = positive_int(args.structure_task_workers or general.get("structure_task_workers", 4), "structure_task_workers")
    judge_session_workers = (
        positive_int(
            args.judge_session_workers or general.get("judge_session_workers", 4),
            "judge_session_workers",
        )
        if judge_enabled
        else 1
    )
    action_workers = positive_int(action_cfg.get("workers", 1), "action.workers")
    runtest_workers = (
        positive_int(runtest_cfg.get("workers", 1), "runtest.workers")
        if runtest_enabled
        else 1
    )
    structure_check_workers = positive_int(structure_cfg.get("check_workers", 20), "structure.check_workers")
    structure_fix_workers = positive_int(structure_cfg.get("fix_workers", 8), "structure.fix_workers")
    action_timeout = positive_int(action_cfg.get("timeout_seconds", 3600), "action.timeout_seconds")
    runtest_timeout = (
        positive_int(runtest_cfg.get("timeout_seconds", 43200), "runtest.timeout_seconds")
        if runtest_enabled
        else 43200
    )
    structure_timeout = positive_int(structure_cfg.get("timeout_seconds", 3600), "structure.timeout_seconds")
    judge_timeout = (
        positive_int(judge_cfg.get("timeout_seconds", 7200), "judge.timeout_seconds")
        if judge_enabled
        else 7200
    )
    command_retries = max(0, int(general.get("command_retries", 1)))
    retain_runs = max(0, int(general.get("retain_runs", 20)))
    completion_requires_upload = bool(general.get("completion_requires_upload", False))
    cleanup_task_dirs = bool(general.get("cleanup_task_dirs_on_success", False))
    qa_status_timeout = positive_int(
        general.get("qa_status_timeout_seconds", 60),
        "general.qa_status_timeout_seconds",
    )
    qa_status_retries = max(0, int(general.get("qa_status_retries", 3)))

    action_entry = args.action_entry.expanduser().resolve() if args.action_entry else repo_path(action_cfg.get("entry", "action_semantics_fix.py"))
    runtest_entry = args.runtest_entry.expanduser().resolve() if args.runtest_entry else repo_path(runtest_cfg.get("entry", "action_semantics_fix_script/runtest_pipeline.py"))
    structure_entry = args.structure_entry.expanduser().resolve() if args.structure_entry else repo_path(structure_cfg.get("entry", "structure_check_fix.py"))
    judge_entry = args.judge_entry.expanduser().resolve() if args.judge_entry else repo_path(judge_cfg.get("entry", "action_semantics_check_script/seven_dimension_quality/detector/batch_judge_tasks.py"))
    required_entries = [("action", action_entry), ("structure", structure_entry)]
    if runtest_enabled and "runtest_pipeline_by_task" not in skip:
        required_entries.append(("runtest", runtest_entry))
    if judge_enabled:
        required_entries.append(("judge", judge_entry))
    for name, path in required_entries:
        if not path.is_file():
            raise FileNotFoundError(f"{name} entry not found: {path}")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = args.run_dir.expanduser().resolve() if args.run_dir else run_parent / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "full_pipeline_report.json"
    context_path = run_dir / "pipeline_context.json"
    grades_file = run_dir / "quality" / "grades.jsonl"
    env_file = repo_path(general.get("env_file", ".env"))
    env = dict(os.environ)
    load_env_file(env_file, env)
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    database_settings = (
        None if args.dry_run else load_task_database_settings(database_cfg, env)
    )
    task_db = TaskStructureSemanticsDatabase(database_settings) if database_settings else None
    stage_updater = TaskStageUpdater(task_db, args.dry_run)

    lock_path = args.lock_file.expanduser().resolve() if args.lock_file else root / ".pipeline_state" / "full_pipeline.lock"
    if resume_snapshot is not None and lock_path.exists() and not args.force_unlock:
        owner_pid = lock_owner_pid(lock_path)
        if owner_pid is None:
            raise RuntimeError(
                f"resume lock owner is unreadable: {lock_path}; "
                "verify no pipeline is running, then pass --force-unlock"
            )
        if process_is_running(owner_pid):
            raise RuntimeError(
                f"cannot resume while full_pipeline.py is still running (PID: {owner_pid})"
            )
        lock_path.unlink(missing_ok=True)
    lock = PipelineLock(lock_path, args.force_unlock)
    lock.acquire()
    stages: list[StageResult] = []
    states: dict[str, TaskState] = (
        dict(resume_snapshot.states) if resume_snapshot is not None else {}
    )
    resumed_stages = dict(resume_snapshot.stages) if resume_snapshot is not None else {}
    started_at = datetime.now().isoformat(timespec="seconds")
    context: dict[str, Any] = (
        dict(resume_snapshot.context) if resume_snapshot is not None else {}
    )
    context.update({
        "root": root,
        "config_path": str(config_path),
        "run_dir": run_dir,
        "python": args.python,
        "env_file": str(env_file),
        "user_id": args.user_id,
        "user_name": operator_user_name,
        "operator_code": operator_code,
        "task_source": (
            "platform-claim"
            if platform_mode
            else ("root-manifest" if root_task_manifest is not None else "pipeline-selection")
        ),
        "task_count": task_count,
        "task_ids": list(states) if states else list(args.task),
        "before_root": str(before_tasks_root),
        "after_root": str(after_tasks_root),
        "success_root": str(success_tasks_root),
        "fail_root": str(fail_tasks_root),
        "task_paths": [state.task_path for state in states.values()],
        "grades_file": str(grades_file),
        "claim_requested": claim_requested,
        "resume_options": {
            "task": list(args.task),
            "user_id": args.user_id,
            "user_name": operator_user_name,
            "operator_code": operator_code,
            "task_count": task_count,
            "from_batch_before": args.from_batch_before,
            "local_action_input_root": (
                str(args.local_action_input_root.resolve())
                if args.local_action_input_root
                else None
            ),
            "skip": sorted(skip),
            "continue_on_error": args.continue_on_error,
            "session_glob": args.session_glob,
            "action_session_workers": args.action_session_workers,
            "runtest_task_workers": args.runtest_task_workers,
            "structure_task_workers": args.structure_task_workers,
            "judge_session_workers": args.judge_session_workers,
            "action_entry": str(args.action_entry.resolve()) if args.action_entry else None,
            "runtest_entry": str(args.runtest_entry.resolve()) if args.runtest_entry else None,
            "structure_entry": str(args.structure_entry.resolve()) if args.structure_entry else None,
            "judge_entry": str(args.judge_entry.resolve()) if args.judge_entry else None,
            "python": args.python,
            "lock_file": str(args.lock_file.resolve()) if args.lock_file else None,
            "claim_requested": claim_requested,
        },
    })
    context.setdefault("claimed_task_ids", [])
    context.setdefault("hook_results", {})
    if resume_snapshot is not None:
        context["resume_count"] = int(context.get("resume_count") or 0) + 1
        context["resumed_from_stage"] = STAGE_ORDER[resume_snapshot.resume_index]
        print(
            f"resuming run {run_dir} from stage "
            f"{resume_snapshot.resume_index + 1}/{len(STAGE_ORDER)} "
            f"{STAGE_ORDER[resume_snapshot.resume_index]}",
            flush=True,
        )

    def resumed_stage(name: str) -> StageResult | None:
        saved = resumed_stages.get(name)
        if saved is None:
            return None
        return StageResult(
            name=saved.name,
            status=saved.status,
            started_at=saved.started_at,
            finished_at=saved.finished_at,
            elapsed_seconds=saved.elapsed_seconds,
            items=list(saved.items),
            summary={**saved.summary, "resumed_from_checkpoint": True},
            reason=saved.reason,
        )

    def task_summary() -> dict[str, Any]:
        counts = {
            status: sum(state.status == status for state in states.values())
            for status in ("pending", "planned", "completed", "failed", "unaligned")
        }
        return {
            "total": len(states),
            **counts,
            "failed_task_ids": [
                state.task_id for state in states.values() if state.status == "failed"
            ],
        }

    def save_report(status: str, error: str = "") -> None:
        write_context(context_path, {**context, "task_states": {key: asdict(value) for key, value in states.items()}})
        write_json_atomic(report_path, {
            "script": Path(__file__).name, "config_path": str(config_path), "started_at": started_at,
            "updated_at": datetime.now().isoformat(timespec="seconds"), "status": status,
            "dry_run": args.dry_run, "continue_on_error": args.continue_on_error,
            "root": str(root), "run_dir": str(run_dir), "claimed_task_ids": context.get("claimed_task_ids", []),
            "task_ids": list(states), "task_paths": context.get("task_paths", []),
            "grades_file": str(grades_file), "stage_order": list(STAGE_ORDER),
            "task_summary": task_summary(),
            "task_states": {key: asdict(value) for key, value in states.items()},
            "stages": [asdict(stage) for stage in stages], "error": error,
        })

    def handle_optional_database_error(exc: Exception) -> bool:
        if claim_requested or args.dry_run:
            return False
        if task_db is not None:
            try:
                task_db.close()
            except Exception:
                pass
        warning = (
            "database unavailable; continuing and retrying "
            f"task_structure_semantics updates at the next write: {exc}"
        )
        context["database_warning"] = warning
        print(f"warning: {warning}", file=sys.stderr, flush=True)
        save_report("running")
        return True

    stage_updater.on_error = handle_optional_database_error

    def record_stage(stage: StageResult) -> None:
        stages.append(stage)
        save_report("running")
        print(f"[{len(stages)}/{len(STAGE_ORDER)}] {stage.name}: {stage.status}", flush=True)

    def current_context() -> dict[str, Any]:
        active = active_states(states)
        context["task_ids"] = [state.task_id for state in active]
        context["task_paths"] = [state.task_path for state in active]
        return context

    def db_active_task_ids() -> list[str]:
        return [state.task_id for state in active_states(states)]

    def run_database_write(operation) -> None:
        if task_db is None or args.dry_run:
            return
        try:
            operation(task_db)
        except Exception as exc:
            if handle_optional_database_error(exc):
                return
            raise

    def db_mark_claimed(task_ids: list[str]) -> None:
        if task_ids:
            run_database_write(lambda database: database.mark_claimed(task_ids))

    def db_initialize_claimed_tasks(claim_result: dict[str, Any] | None) -> None:
        if task_db is None or args.dry_run:
            return
        raw_tasks = claim_result.get("tasks") if isinstance(claim_result, dict) else None
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise RuntimeError("claim response did not include task details for database initialization")
        records = normalize_records(raw_tasks, str(args.user_id or ""), "已领取")
        run_database_write(lambda database: database.initialize_claimed_tasks(records))

    def db_mark_downloaded(task_ids: list[str]) -> None:
        if task_ids:
            run_database_write(lambda database: database.mark_downloaded(task_ids))

    def db_mark_running(task_ids: list[str]) -> None:
        if task_ids:
            run_database_write(lambda database: database.mark_running(task_ids))

    def db_mark_completed(task_ids: list[str]) -> None:
        if task_ids:
            run_database_write(lambda database: database.mark_completed(task_ids))

    def db_mark_failed(task_ids: list[str]) -> None:
        if task_ids:
            run_database_write(lambda database: database.mark_failed(task_ids))

    def db_mark_finished(task_ids: list[str]) -> None:
        if task_ids:
            run_database_write(lambda database: database.mark_finished(task_ids))

    def db_stage_flag(column: str, value: str, task_ids: list[str] | None = None) -> None:
        run_database_write(
            lambda database: database.mark_stage_flag(task_ids or db_active_task_ids(), column, value)
        )

    def update_qa_status_for_cleanup(state: TaskState) -> dict[str, Any]:
        """Mark one successfully uploaded TASK as QA-complete before deletion."""
        result, returncode = push_qa_status(
            {
                "user_id": context.get("user_id"),
                "task_ids": [state.task_id],
            },
            env,
            qa_status_timeout,
            qa_status_retries,
        )
        if returncode:
            raise RuntimeError(
                f"QA completion update failed for {state.task_id}: {result}"
            )
        return result

    def run_task_hooks(name: str) -> StageResult:
        active = active_states(states)
        if not active:
            return skipped_stage(name, "no active TASKs")
        results: list[dict[str, Any]] = []
        for state in active:
            scoped = scoped_context(context, state)
            if name == "upload_results":
                success_root = Path(str(context["success_root"])).resolve()
                scoped["root"] = str(success_root)
                scoped["task_paths"] = [str((success_root / state.task_id).resolve())]
            scoped_path = run_dir / "contexts" / name / f"{state.task_id}.json"
            write_context(scoped_path, {**scoped, "task_states": {state.task_id: asdict(state)}})
            try:
                stage, payload = run_hook_stage(name, config, scoped, scoped_path, run_dir, env, args.dry_run, state.task_id)
                result_item = {
                    "item": state.task_id,
                    "status": stage.status,
                    "returncode": (stage.items[0].get("returncode") if stage.items else None),
                    "error": stage.reason or (stage.items[0].get("error") if stage.items else ""),
                }
                if payload is not None:
                    result_item["result"] = payload
                    artifact_payload = payload
                    payload_rows = payload.get("results")
                    if isinstance(payload_rows, list):
                        matching_rows = [
                            row for row in payload_rows
                            if isinstance(row, dict) and str(row.get("task_id")) == state.task_id
                        ]
                        if len(matching_rows) == 1:
                            artifact_payload = matching_rows[0]
                    if (
                        name == "fetch_from_oss"
                        and isinstance(artifact_payload, dict)
                        and str(artifact_payload.get("status")) == "unaligned"
                    ):
                        # The two OSS sources could not be aligned. Mark the
                        # TASK 无法对齐 in the database and skip it without
                        # failing, so the pipeline keeps processing other TASKs.
                        reason = str(
                            artifact_payload.get("reason")
                            or "聚合任务与全量清洗成功无法对齐"
                        )
                        state.artifacts[name] = artifact_payload
                        state.mark_unaligned(name, reason)
                        stage_updater.mark_unaligned([state.task_id])
                        result_item["status"] = "unaligned"
                        result_item["error"] = reason
                        results.append(result_item)
                        continue
                    if (
                        name == "fetch_from_oss"
                        and isinstance(artifact_payload, dict)
                        and (
                            claim_requested
                            or any(
                                artifact_payload.get(key)
                                for key in (
                                    "full_clean_task_path",
                                    "aggregate_task_path",
                                    "after_task_path",
                                )
                            )
                        )
                    ):
                        before_root = Path(str(context["before_root"]))
                        after_root = Path(str(context["after_root"]))
                        artifact_payload = (
                            validate_online_fetch_artifact(
                                artifact_payload,
                                state.task_id,
                                before_root,
                                after_root,
                            )
                            if claim_requested
                            else validate_fetch_artifact_paths(
                                artifact_payload,
                                state.task_id,
                                before_root,
                                after_root,
                            )
                        )
                    state.artifacts[name] = artifact_payload
                    if name == "fetch_from_oss" and isinstance(artifact_payload, dict):
                        after_task_path = artifact_payload.get("after_task_path")
                        if after_task_path:
                            after_root = Path(str(context["after_root"])).resolve()
                            candidate = Path(str(after_task_path)).expanduser().resolve()
                            candidate.relative_to(after_root)
                            state.task_path = str(candidate)
                results.append(result_item)
                if stage.status in {"failed", "error", "timeout"}:
                    state.fail(name, str(result_item["error"] or f"{name} failed"))
                elif stage.status == "completed":
                    state.complete_stage(name)
            except Exception as exc:
                state.fail(name, str(exc))
                results.append({"item": state.task_id, "status": "failed", "returncode": 1, "error": str(exc)})
        failed = sum(row["status"] in {"failed", "error", "timeout"} for row in results)
        completed = sum(row["status"] == "completed" for row in results)
        skipped = sum(row["status"] == "skipped" for row in results)
        planned = sum(row["status"] == "planned" for row in results)
        if skipped == len(results):
            status = "skipped"
        elif planned == len(results):
            status = "planned"
        else:
            status = "partial" if failed and completed else ("failed" if failed else "completed")
        return StageResult(name, status, datetime.now().isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"), 0.0, results, {"tasks_total": len(results), "tasks_completed": completed, "tasks_failed": failed, "tasks_skipped": skipped, "tasks_planned": planned})

    def run_local_stage(
        name: str,
        jobs: list[tuple[str, list[str], Path]],
        item_to_task: dict[str, str],
        workers: int,
        timeout: int,
        resumed_items: list[str] | None = None,
    ) -> StageResult:
        resumed_items = resumed_items or []
        if not jobs and not resumed_items:
            return skipped_stage(name, "no active TASKs")
        if jobs:
            stage = execute_parallel_stage(name, jobs, workers, env, args.dry_run, timeout, command_retries)
        else:
            now = datetime.now().isoformat(timespec="seconds")
            stage = StageResult(name, "completed", now, now, 0.0)
        stage.items.extend({
            "item": item,
            "command": [],
            "command_text": "",
            "log": "",
            "status": "completed",
            "returncode": 0,
            "elapsed_seconds": 0.0,
            "attempts": 0,
            "error": "",
            "resumed": True,
        } for item in resumed_items)
        stage.items.sort(key=lambda item: item["item"])
        task_failed: set[str] = set()
        task_ok: set[str] = set()
        for item in stage.items:
            task_id = item_to_task.get(item["item"], item["item"])
            if item["status"] in {"failed", "error", "timeout"}:
                task_failed.add(task_id)
                if task_id in states:
                    states[task_id].fail(name, str(item.get("error") or f"{name} failed"))
            elif item["status"] == "completed":
                task_ok.add(task_id)
        # A TASK succeeds at a multi-session stage only when all its jobs succeeded.
        for task_id in task_ok - task_failed:
            states[task_id].complete_stage(name)
        stage.status = "partial" if task_failed and task_ok else ("failed" if task_failed else stage.status)
        stage.summary.update({
            "total": len(stage.items),
            "completed": sum(item["status"] == "completed" for item in stage.items),
            "failed": sum(item["status"] in {"failed", "error", "timeout"} for item in stage.items),
            "resumed": len(resumed_items),
            "tasks_completed": len(task_ok - task_failed),
            "tasks_failed": len(task_failed),
        })
        return stage

    try:
        # Establish a resumable checkpoint before the first external call.
        save_report("running")
        # Database health check disabled: the first real write will connect.
        # The original ensure_connected() pre-check caused spurious
        # "Lost connection during query" on some pymysql/server combinations
        # despite the connection working fine in real operations.
        # if task_db is not None and not args.dry_run:
        #     try:
        #         task_db.ensure_connected()
        #     except Exception as exc:
        #         if claim_requested:
        #             raise
        #         task_db.close()
        #         task_db = None
        #         stage_updater = TaskStageUpdater(None, args.dry_run)
        #         warning = (
        #             "database unavailable; continuing without "
        #             f"task_structure_semantics updates: {exc}"
        #         )
        #         context["database_warning"] = warning
        #         print(f"warning: {warning}", file=sys.stderr, flush=True)
        #         save_report("running")

        # Claim remains one batch API call; all following records are per TASK.
        resumed_claim = resumed_stage("claim_tasks")
        if resumed_claim is not None:
            claim_stage, claim_result = resumed_claim, None
        elif "claim_tasks" in skip:
            reason = "skipped by --from-batch-before" if args.from_batch_before else "skipped by --skip"
            claim_stage, claim_result = skipped_stage("claim_tasks", reason), None
        elif resume_snapshot is not None and claim_requested and not args.dry_run:
            # Claim is not safely retryable. Recover a completed call from its
            # durable hook result, or stop rather than potentially claim twice.
            claim_result_path = run_dir / "hooks" / "claim_tasks" / "batch" / "result.json"
            if claim_result_path.is_file():
                claim_result = _read_json_object(claim_result_path, "claim hook result")
                validate_claim_task_ids(claim_result.get("task_ids"))
                now = datetime.now().isoformat(timespec="seconds")
                claim_stage = StageResult(
                    "claim_tasks",
                    "completed",
                    now,
                    now,
                    0.0,
                    summary={
                        "result_path": str(claim_result_path),
                        "recovered_from_result": True,
                    },
                )
            else:
                raise RuntimeError(
                    "the previous platform claim outcome is unknown; refusing to claim "
                    "again automatically. Verify the platform assignment and restore the "
                    "claim result or start a new run"
                )
        else:
            write_context(context_path, context)
            claim_stage, claim_result = run_hook_stage("claim_tasks", config, context, context_path, run_dir, env, args.dry_run)
        if claim_requested and not args.dry_run and resumed_claim is None:
            # A claim failure is a control-plane failure. Continuing here could
            # process TASKs that the platform never assigned to this worker.
            if claim_stage.status != "completed":
                claim_error_ids: list[str] = []
                raw_claim_ids = (
                    claim_result.get("task_ids")
                    if isinstance(claim_result, dict)
                    else args.task
                )
                if not raw_claim_ids:
                    raw_claim_ids = args.task
                try:
                    claim_error_ids = validate_claim_task_ids(raw_claim_ids)
                except ValueError:
                    # A count-only claim failure does not identify a TASK;
                    # there is no safe row to update in that case.
                    claim_error_ids = []
                stage_updater.mark_result(
                    "claim_tasks", claim_stage.status, claim_error_ids
                )
                record_stage(claim_stage)
                save_report("failed", f"claim failed: {claim_stage.reason or claim_stage.status}")
                return 1
            try:
                claimed_task_ids = validate_claim_task_ids(
                    claim_result.get("task_ids") if isinstance(claim_result, dict) else None
                )
            except ValueError as exc:
                save_report("failed", f"invalid claim result: {exc}")
                return 1
            context["claimed_task_ids"] = claimed_task_ids
            context["task_ids"] = claimed_task_ids
            write_root_task_ids(root, claimed_task_ids)
        elif claim_result and claim_result.get("task_ids"):
            context["claimed_task_ids"] = list(claim_result["task_ids"])
            context["task_ids"] = list(claim_result["task_ids"])
        elif resume_snapshot is not None and resumed_claim is not None:
            resumed_task_ids = (
                list(states)
                or validate_claim_task_ids(
                    context.get("claimed_task_ids")
                    or context.get("task_ids")
                    or resume_persisted_task_ids
                )
            )
            context["claimed_task_ids"] = resumed_task_ids
            context["task_ids"] = resumed_task_ids
            if not args.dry_run:
                write_root_task_ids(root, resumed_task_ids)
        elif args.from_batch_before:
            context["task_ids"] = discover_local_before_task_ids(
                before_tasks_root,
                list(args.task),
                task_glob,
            )
        elif not context.get("task_ids"):
            # Local discovery is allowed only when claim is disabled, skipped,
            # or being planned by --dry-run.
            context["task_ids"] = [task.name for task in discover_tasks(root, [], task_glob)]
        record_stage(claim_stage)

        task_ids: list[str] = []
        selected_paths: dict[str, Path] = {}
        for raw_item in context.get("task_ids") or []:
            raw = str(raw_item)
            candidate = Path(raw).expanduser()
            if candidate.is_absolute():
                task_id = candidate.name
                selected_paths[task_id] = candidate.resolve()
            else:
                task_id = raw
            if task_id not in task_ids:
                task_ids.append(task_id)
        for task_id in task_ids:
            if task_id not in states:
                states[task_id] = TaskState(
                    task_id,
                    str(selected_paths.get(task_id, task_path(root, task_id))),
                )
        context["claimed_task_ids"] = context.get("claimed_task_ids") or task_ids
        context["task_ids"] = task_ids
        context["task_paths"] = [states[task_id].task_path for task_id in task_ids]
        save_report("running")
        if not claim_requested and resumed_claim is None:
            stage_updater.ensure_tasks(
                task_ids,
                operator_id=operator_database_id,
                operator_code=operator_code or None,
                operator_name=operator_user_name,
            )
        if claim_requested and resumed_claim is None:
            db_initialize_claimed_tasks(claim_result)
            stage_updater.set_status(
                list(context["claimed_task_ids"]), "claim_tasks", "success"
            )

        resumed_fetch = resumed_stage("fetch_from_oss")
        for hook_name in ("fetch_from_oss",):
            if resumed_fetch is not None:
                stage = resumed_fetch
            elif hook_name == "fetch_from_oss" and args.from_batch_before:
                stage = prepare_local_before_inputs(
                    states,
                    before_tasks_root,
                    after_tasks_root,
                    run_dir,
                    args.dry_run,
                )
            elif hook_name == "fetch_from_oss" and args.local_action_input_root:
                stage = prepare_local_action_inputs(
                    states,
                    args.local_action_input_root,
                    session_glob,
                    args.dry_run,
                )
            elif hook_name in skip:
                stage = skipped_stage(hook_name, "skipped by --skip")
            else:
                stage = run_task_hooks(hook_name)
            record_stage(stage)
            if hook_name == "fetch_from_oss" and resumed_fetch is None:
                fetch_completed = [
                    str(item.get("item"))
                    for item in stage.items
                    if item.get("status") == "completed" and item.get("item")
                ]
                fetch_failed = [
                    str(item.get("item"))
                    for item in stage.items
                    if item.get("status") in {"failed", "error", "timeout"} and item.get("item")
                ]
                stage_updater.mark_fetch_result(fetch_completed, success=True)
                stage_updater.mark_fetch_result(fetch_failed, success=False)
            for state in ([] if resumed_fetch is not None else active_states(states)):
                if claim_requested and not args.dry_run:
                    artifact = state.artifacts.get("fetch_from_oss")
                    if not isinstance(artifact, dict) or not artifact.get("after_task_path"):
                        state.fail(
                            hook_name,
                            "claimed TASK fetch did not produce a validated after_task_path",
                        )
                        continue
                if not Path(state.task_path).is_dir() or not (Path(state.task_path) / "sessions").is_dir():
                    state.fail(hook_name, f"TASK sessions directory not found: {state.task_path}")
            if resumed_fetch is None:
                db_mark_downloaded([
                    state.task_id
                    for state in active_states(states)
                    if state.stages.get("fetch_from_oss") == "completed"
                ])

        # Action: one command per session, grouped back into TASK states.
        action_jobs: list[tuple[str, list[str], Path]] = []
        action_map: dict[str, str] = {}
        action_resumed: list[str] = []
        resumed_action = resumed_stage("action_semantics_by_session")
        for state in ([] if resumed_action is not None else active_states(states)):
            fetch_artifact = state.artifacts.get("fetch_from_oss", {})
            action_task = Path(str(fetch_artifact.get("action_task_path") or state.task_path)).resolve()
            try:
                prepared_sessions = fetch_artifact.get("action_sessions")
                if isinstance(prepared_sessions, list) and prepared_sessions:
                    action_sessions_root = (action_task / "sessions").resolve()
                    sessions = []
                    for raw_session in prepared_sessions:
                        session = Path(str(raw_session)).resolve()
                        session.relative_to(action_sessions_root)
                        if not session.is_file():
                            raise FileNotFoundError(f"prepared action session not found: {session}")
                        sessions.append(session)
                    sessions.sort()
                else:
                    sessions = discover_sessions(action_task, session_glob)
            except Exception as exc:
                state.fail("action_semantics_by_session", str(exc))
                continue
            for session in sessions:
                relative = session.relative_to(action_task / "sessions")
                key = artifact_key(session, action_task / "sessions")
                item = f"{state.task_id}::{relative.as_posix()}"
                action_map[item] = state.task_id
                report_root = run_dir / "action_semantics" / state.task_id / key
                if not args.dry_run and has_completed_action_report(report_root):
                    action_resumed.append(item)
                    continue
                action_jobs.append((item, [
                    args.python, str(action_entry), "-r", str(action_task), "--session-file", str(session),
                    "--report-dir", str(report_root),
                    "--task-workers", "1", "--workers", str(action_workers), "--quiet",
                    "--continue-on-error" if args.continue_on_error else "--stop-on-error",
                    *string_list(action_cfg.get("extra_args"), "action.extra_args"),
                ], run_dir / "logs" / "action_semantics" / state.task_id / f"{key}.log"))
        action_stage_task_ids = db_active_task_ids()
        if resumed_action is not None:
            action_stage = resumed_action
        elif "action_semantics_by_session" in skip:
            db_stage_flag("is_semantics", "否", action_stage_task_ids)
            action_stage = skipped_stage("action_semantics_by_session", "skipped by --skip")
        else:
            db_mark_running(action_stage_task_ids)
            db_stage_flag("is_semantics", "是", action_stage_task_ids)
            action_stage = run_local_stage(
                "action_semantics_by_session",
                action_jobs,
                action_map,
                action_session_workers,
                action_timeout,
                action_resumed,
            )
        record_stage(action_stage)
        stage_updater.mark_states("action_semantics_by_session", states)

        judge_jobs: list[tuple[str, list[str], Path]] = []
        judge_map: dict[str, str] = {}
        judge_outputs: dict[str, Path] = {}
        expected_sessions: dict[str, int] = {}
        resumed_judge = resumed_stage("quality_judge")
        if resumed_judge is not None:
            judge_outputs = {
                state.task_id: run_dir / "quality" / state.task_id / "grades.jsonl"
                for state in states.values()
            }
        judge_stage_task_ids = [] if resumed_judge is not None else db_active_task_ids()
        if resumed_judge is None and judge_enabled and "quality_judge" not in skip:
            db_stage_flag("is_detector", "是", judge_stage_task_ids)
            for state in ([] if resumed_judge is not None else active_states(states)):
                fetch_artifact = state.artifacts.get("fetch_from_oss", {})
                judge_task = Path(
                    str(fetch_artifact.get("action_task_path") or state.task_path)
                ).resolve()
                try:
                    expected_sessions[state.task_id] = len(
                        discover_sessions(judge_task, session_glob)
                    )
                except Exception as exc:
                    state.fail("quality_judge", str(exc))
                    continue
                quality_dir = run_dir / "quality" / state.task_id
                output = quality_dir / "grades.jsonl"
                judge_outputs[state.task_id] = output
                judge_map[state.task_id] = state.task_id
                judge_jobs.append((state.task_id, [
                    args.python, str(judge_entry), "--tasks-root", str(root), "--task", str(judge_task),
                    "--glob", session_glob,
                    "--parts-dir", str(quality_dir / "parts"), "--logs-dir", str(quality_dir / "logs"),
                    "--merged-out", str(output), "--lock-file", str(quality_dir / "batch_judge_tasks.lock"),
                    "--detect-session-workers", str(judge_session_workers),
                    *string_list(judge_cfg.get("extra_args"), "judge.extra_args"),
                ], run_dir / "logs" / "quality_judge" / f"{state.task_id}.log"))
        if resumed_judge is not None:
            judge_stage = resumed_judge
        elif "quality_judge" in skip:
            db_stage_flag("is_detector", "否", judge_stage_task_ids)
            judge_stage = skipped_stage("quality_judge", "skipped by --skip")
        elif not judge_enabled:
            db_stage_flag("is_detector", "否", judge_stage_task_ids)
            judge_stage = skipped_stage("quality_judge", "judge.enabled=false")
        else:
            judge_stage = run_local_stage(
                "quality_judge",
                judge_jobs,
                judge_map,
                judge_session_workers,
                judge_timeout,
            )
            for item in judge_stage.items:
                task_id = item["item"]
                output = judge_outputs.get(task_id)
                if item["status"] == "completed" and output is not None:
                    records = [
                        line
                        for line in output.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ] if output.is_file() else []
                    if len(records) != expected_sessions.get(task_id, -1):
                        item["status"] = "failed"
                        item["returncode"] = 1
                        item["error"] = (
                            f"quality record count {len(records)} != expected sessions "
                            f"{expected_sessions.get(task_id)}"
                        )
                        states[task_id].fail("quality_judge", item["error"])
            judge_stage.summary["record_count_checked"] = True
            mismatch_count = sum(item["status"] == "failed" for item in judge_stage.items)
            if mismatch_count:
                completed_count = sum(item["status"] == "completed" for item in judge_stage.items)
                judge_stage.status = "partial" if completed_count else "failed"
                judge_stage.summary["failed"] = mismatch_count
                judge_stage.summary["completed"] = completed_count
        record_stage(judge_stage)
        stage_updater.mark_states("quality_judge", states)

        resumed_aggregate = resumed_stage("aggregate_sessions")
        if resumed_aggregate is not None:
            aggregate_stage = resumed_aggregate
        elif "aggregate_sessions" in skip:
            aggregate_stage = skipped_stage("aggregate_sessions", "skipped by --skip")
        else:
            aggregate_started = datetime.now().isoformat(timespec="seconds")
            aggregate_items: list[dict[str, Any]] = []
            for state in active_states(states):
                artifact = state.artifacts.get("fetch_from_oss", {})
                full_clean_task_path = artifact.get("full_clean_task_path")
                after_task_path = artifact.get("after_task_path")
                if full_clean_task_path and after_task_path:
                    if args.dry_run:
                        clean_sessions_available = (
                            Path(str(full_clean_task_path)).resolve() / "clean_sessions"
                        ).is_dir()
                        state.stages["aggregate_sessions"] = "planned"
                        aggregate_items.append({
                            "item": state.task_id,
                            "status": "planned",
                            "mode": (
                                "complete_missing_clean_sessions"
                                if clean_sessions_available
                                else "aggregate_only_no_clean_sessions"
                            ),
                        })
                        continue
                    try:
                        completion = complete_after_task_sessions(
                            Path(str(full_clean_task_path)).resolve(),
                            Path(str(after_task_path)).resolve(),
                        )
                        state.complete_stage("aggregate_sessions")
                        aggregate_items.append({
                            "item": state.task_id,
                            "status": "completed",
                            "mode": "complete_missing_clean_sessions",
                            **completion,
                        })
                    except Exception as exc:
                        state.fail("aggregate_sessions", str(exc))
                        aggregate_items.append({"item": state.task_id, "status": "failed", "error": str(exc)})
                    continue
                action_task_path = artifact.get("action_task_path")
                replacements = artifact.get("replacements")
                if not action_task_path or not isinstance(replacements, list):
                    if claim_requested:
                        state.fail(
                            "aggregate_sessions",
                            "OSS fetch did not return full_clean_task_path and after_task_path",
                        )
                        aggregate_items.append({
                            "item": state.task_id,
                            "status": "failed",
                            "error": "missing new OSS preparation metadata",
                        })
                        continue
                    aggregate_items.append({
                        "item": state.task_id,
                        "status": "skipped",
                        "reason": "no prepared repair-success sessions; action ran on the full TASK",
                    })
                    continue
                if args.dry_run:
                    state.stages["aggregate_sessions"] = "planned"
                    aggregate_items.append({
                        "item": state.task_id,
                        "status": "planned",
                        "would_replace": len(replacements),
                    })
                    continue
                try:
                    skipped_missing: list[str] = []
                    written = overlay_prepared_sessions(
                        Path(state.task_path).resolve(),
                        Path(str(action_task_path)).resolve(),
                        replacements,
                        skip_missing=True,
                        skipped_missing=skipped_missing,
                    )
                    state.complete_stage("aggregate_sessions")
                    aggregate_items.append({
                        "item": state.task_id,
                        "status": "completed",
                        "requested": len(replacements),
                        "replaced": len(written),
                        "skipped_missing": len(skipped_missing),
                        "skipped_missing_paths": skipped_missing,
                        "relative_paths": written,
                    })
                except Exception as exc:
                    state.fail("aggregate_sessions", str(exc))
                    aggregate_items.append({"item": state.task_id, "status": "failed", "error": str(exc)})
            aggregate_failed = sum(item["status"] == "failed" for item in aggregate_items)
            aggregate_completed = sum(item["status"] == "completed" for item in aggregate_items)
            aggregate_skipped = sum(item["status"] == "skipped" for item in aggregate_items)
            aggregate_planned = sum(item["status"] == "planned" for item in aggregate_items)
            if aggregate_skipped == len(aggregate_items):
                aggregate_status = "skipped"
            elif aggregate_planned == len(aggregate_items):
                aggregate_status = "planned"
            elif aggregate_failed and aggregate_completed:
                aggregate_status = "partial"
            elif aggregate_failed:
                aggregate_status = "failed"
            else:
                aggregate_status = "completed"
            aggregate_stage = StageResult(
                "aggregate_sessions",
                aggregate_status,
                aggregate_started,
                datetime.now().isoformat(timespec="seconds"),
                0.0,
                aggregate_items,
                {
                    "tasks_total": len(aggregate_items),
                    "tasks_completed": aggregate_completed,
                    "tasks_failed": aggregate_failed,
                    "tasks_skipped": aggregate_skipped,
                    "tasks_planned": aggregate_planned,
                },
            )
        record_stage(aggregate_stage)
        stage_updater.mark_states("aggregate_sessions", states)

        runtest_jobs: list[tuple[str, list[str], Path]] = []
        runtest_map: dict[str, str] = {}
        resumed_runtest = resumed_stage("runtest_pipeline_by_task")
        if resumed_runtest is None and runtest_enabled and "runtest_pipeline_by_task" not in skip:
            for state in active_states(states):
                runtest_map[state.task_id] = state.task_id
                runtest_jobs.append((state.task_id, [
                    args.python,
                    str(runtest_entry),
                    "chain",
                    "--root",
                    state.task_path,
                    "--python",
                    args.python,
                    "--workers",
                    str(runtest_workers),
                    "--failed-root",
                    str(run_dir / "runtest_failed" / state.task_id),
                    *string_list(runtest_cfg.get("extra_args"), "runtest.extra_args"),
                ], run_dir / "logs" / "runtest" / f"{state.task_id}.log"))
        if resumed_runtest is not None:
            runtest_stage = resumed_runtest
        elif "runtest_pipeline_by_task" in skip:
            runtest_stage = skipped_stage(
                "runtest_pipeline_by_task",
                "skipped by --skip",
            )
        elif not runtest_enabled:
            runtest_stage = skipped_stage(
                "runtest_pipeline_by_task",
                "runtest.enabled=false",
            )
        else:
            runtest_stage = run_local_stage(
                "runtest_pipeline_by_task",
                runtest_jobs,
                runtest_map,
                runtest_task_workers,
                runtest_timeout,
            )
        record_stage(runtest_stage)
        stage_updater.mark_states("runtest_pipeline_by_task", states)

        structure_jobs: list[tuple[str, list[str], Path]] = []
        structure_map: dict[str, str] = {}
        resumed_structure = resumed_stage("structure_check_fix_by_task")
        for state in ([] if resumed_structure is not None else active_states(states)):
            item = state.task_id
            structure_map[item] = state.task_id
            structure_jobs.append((item, [
                args.python, str(structure_entry), "--check_fix", "-r", state.task_path,
                "--workers", str(structure_check_workers), "--fix-workers", str(structure_fix_workers),
                *string_list(structure_cfg.get("extra_args"), "structure.extra_args"),
            ], run_dir / "logs" / "structure" / f"{state.task_id}.log"))
        structure_stage_task_ids = [] if resumed_structure is not None else db_active_task_ids()
        if resumed_structure is not None:
            structure_stage = resumed_structure
        elif "structure_check_fix_by_task" in skip:
            db_stage_flag("is_structure", "否", structure_stage_task_ids)
            structure_stage = skipped_stage("structure_check_fix_by_task", "skipped by --skip")
        else:
            db_stage_flag("is_structure", "是", structure_stage_task_ids)
            structure_stage = run_local_stage("structure_check_fix_by_task", structure_jobs, structure_map, structure_task_workers, structure_timeout)
        record_stage(structure_stage)
        stage_updater.mark_states("structure_check_fix_by_task", states)

        resumed_split = resumed_stage("split_success_fail_tasks")
        if resumed_split is not None:
            split_stage = resumed_split
        elif "split_success_fail_tasks" in skip:
            split_stage = skipped_stage("split_success_fail_tasks", "skipped by --skip")
        else:
            split_stage = split_task_buckets(
                states,
                success_tasks_root,
                fail_tasks_root,
                args.dry_run,
            )
        record_stage(split_stage)
        stage_updater.mark_split_items(split_stage, states)

        if judge_enabled and not args.dry_run:
            successful_grade_files = [
                judge_outputs[state.task_id]
                for state in states.values()
                if state.status == "pending" and state.task_id in judge_outputs
            ]
            if successful_grade_files:
                merge_jsonl_files(successful_grade_files, grades_file)
        if not args.dry_run:
            relocate_failed_task_buckets(states, success_tasks_root, fail_tasks_root)

        if not args.dry_run:
            for state in active_states(states):
                fetch_artifact = state.artifacts.get("fetch_from_oss", {})
                required_stages = []
                if claim_requested:
                    required_stages.append("fetch_from_oss")
                required_stages.append("action_semantics_by_session")
                if judge_enabled:
                    required_stages.append("quality_judge")
                if (
                    claim_requested
                    or (
                        fetch_artifact.get("action_task_path")
                        and isinstance(fetch_artifact.get("replacements"), list)
                    )
                ):
                    required_stages.append("aggregate_sessions")
                if runtest_enabled:
                    required_stages.append("runtest_pipeline_by_task")
                required_stages.extend([
                    "structure_check_fix_by_task",
                    "split_success_fail_tasks",
                ])
                incomplete = [
                    stage_name
                    for stage_name in required_stages
                    if state.stages.get(stage_name) != "completed"
                ]
                if incomplete:
                    state.fail(
                        "pre_upload_gate",
                        "required stage(s) did not complete: " + ", ".join(incomplete),
                    )

        resumed_upload = resumed_stage("upload_results")
        upload_stage = (
            resumed_upload
            if resumed_upload is not None
            else (
                skipped_stage("upload_results", "skipped by --skip")
                if "upload_results" in skip
                else run_task_hooks("upload_results")
            )
        )
        record_stage(upload_stage)
        for state in list(states.values()):
            if state.status == "failed":
                continue
            if (completion_requires_upload or claim_requested) and state.stages.get("upload_results") != "completed":
                state.fail("upload_results", "upload is required before completion but did not complete")
        stage_updater.mark_states("upload_results", states)

        resumed_complete = resumed_stage("mark_qa_complete")
        complete_stage = (
            resumed_complete
            if resumed_complete is not None
            else (
                skipped_stage("mark_qa_complete", "skipped by --skip")
                if "mark_qa_complete" in skip
                else run_task_hooks("mark_qa_complete")
            )
        )
        record_stage(complete_stage)
        for state in states.values():
            if state.status == "pending":
                state.status = "planned" if args.dry_run else "completed"

        # A TASK that fails the upload barrier must not remain in the success bucket.
        if not args.dry_run:
            relocate_failed_task_buckets(states, success_tasks_root, fail_tasks_root)

        resumed_cleanup = resumed_stage("cleanup_local_tasks")
        if resumed_cleanup is not None:
            cleanup_stage = resumed_cleanup
        elif "cleanup_local_tasks" in skip or not cleanup_task_dirs:
            cleanup_stage = skipped_stage(
                "cleanup_local_tasks",
                "skipped by --skip" if "cleanup_local_tasks" in skip else "cleanup_task_dirs_on_success=false",
            )
        else:
            cleanup_items: list[dict[str, Any]] = []
            for state in states.values():
                if state.stages.get("cleanup_local_tasks") == "completed":
                    cleanup_items.append({
                        "item": state.task_id,
                        "status": "completed",
                        "resumed": True,
                    })
                    continue
                path = Path(state.task_path).resolve()
                if state.status != "completed" or state.stages.get("upload_results") != "completed":
                    cleanup_items.append({"item": state.task_id, "status": "skipped", "reason": "TASK not completed and uploaded"})
                    continue
                try:
                    path.relative_to(root)
                    if path == root:
                        raise RuntimeError("refusing to delete a single-TASK pipeline root")
                    qa_result = update_qa_status_for_cleanup(state)
                    shutil.rmtree(path)
                    state.stages["cleanup_local_tasks"] = "completed"
                    cleanup_items.append({
                        "item": state.task_id,
                        "status": "completed",
                        "path": str(path),
                        "qa_result": qa_result,
                    })
                except Exception as exc:
                    state.fail("cleanup_local_tasks", str(exc))
                    cleanup_items.append({"item": state.task_id, "status": "failed", "error": str(exc)})
            cleanup_failed = sum(item["status"] == "failed" for item in cleanup_items)
            cleanup_completed = sum(item["status"] == "completed" for item in cleanup_items)
            cleanup_stage = StageResult(
                "cleanup_local_tasks",
                "partial" if cleanup_failed and cleanup_completed else ("failed" if cleanup_failed else "completed"),
                datetime.now().isoformat(timespec="seconds"),
                datetime.now().isoformat(timespec="seconds"),
                0.0,
                cleanup_items,
                {"completed": cleanup_completed, "failed": cleanup_failed},
            )
        record_stage(cleanup_stage)
        stage_updater.mark_items(cleanup_stage, states)

        if not args.dry_run:
            # Cleanup can itself fail after a successful upload; keep the
            # bucket invariant true for that case as well.
            relocate_failed_task_buckets(states, success_tasks_root, fail_tasks_root)

        failed_ids = [state.task_id for state in states.values() if state.status == "failed"]
        context["task_ids"] = failed_ids
        resumed_notify = resumed_stage("notify_failure")
        if resumed_notify is not None:
            notify_stage = resumed_notify
        elif failed_ids and "notify_failure" not in skip:
            write_context(
                context_path,
                {**context, "task_states": {key: asdict(value) for key, value in states.items()}},
            )
            notify_stage, _ = run_hook_stage("notify_failure", config, context, context_path, run_dir, env, args.dry_run)
        else:
            notify_stage = skipped_stage("notify_failure", "no failed TASKs" if not failed_ids else "skipped by --skip")
        record_stage(notify_stage)
        stage_updater.mark_result("notify_failure", notify_stage.status, failed_ids)

        if not args.dry_run and task_db is not None:
            terminal_ids = [state.task_id for state in states.values() if state.status != "pending"]
            db_mark_finished(terminal_ids)
            db_mark_completed([state.task_id for state in states.values() if state.status == "completed"])
            db_mark_failed(failed_ids)

        pipeline_errors: list[str] = []
        if not args.dry_run:
            unrouted_failed_ids = [
                state.task_id
                for state in states.values()
                if state.status == "failed"
                and (
                    state.artifacts.get("task_bucket") != "fail_task"
                    or not state.artifacts.get("bucket_task_path")
                    or not Path(str(state.artifacts.get("bucket_task_path") or "")).is_dir()
                )
            ]
            if unrouted_failed_ids:
                pipeline_errors.append(
                    "failed TASK(s) were not routed to fail_task: "
                    + ", ".join(unrouted_failed_ids)
                )
            if notify_stage.status in {"failed", "error", "timeout", "partial"}:
                pipeline_errors.append("notify_failure did not complete")

        final_status = (
            "planned"
            if args.dry_run
            else ("failed" if pipeline_errors else "completed")
        )
        save_report(final_status, "; ".join(pipeline_errors))
        prune_runs(run_parent, run_dir, retain_runs)
        print(f"report: {report_path}")
        return 1 if pipeline_errors else 0
    except KeyboardInterrupt:
        save_report("interrupted", "KeyboardInterrupt")
        return 130
    except Exception as exc:
        save_report("failed", str(exc))
        print(f"pipeline failed: {exc}", file=sys.stderr)
        print(f"report: {report_path}", file=sys.stderr)
        return 1
    finally:
        if task_db is not None:
            task_db.close()
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

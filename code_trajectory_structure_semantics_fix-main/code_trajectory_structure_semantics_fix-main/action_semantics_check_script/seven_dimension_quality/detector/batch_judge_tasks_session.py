#!/usr/bin/env python3
from __future__ import annotations

"""Batch-run judge_trajectory_pro_v2.py for TASK-* session files, then merge JSONL outputs.

Default usage:
  python3 batch_judge_tasks.py

The default task root is ./target-issues-tasks. For each TASK-* directory, this script enumerates
session files under sessions/, runs judge_trajectory_pro_v2.py per session, writes one part JSONL
per session under results/parts/<TASK>/, then merges the part JSONL files into
results/target_issues_grades.jsonl.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import glob
import json
import os
import subprocess
import sys
from pathlib import Path
try:
    import fcntl  # type: ignore
except ImportError:  # Windows
    fcntl = None


DETECTOR_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DETECTOR_ROOT.parent.parent
DEFAULT_TASK_ROOT = PROJECT_ROOT / "target-issues-tasks"
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "source-issues-json" / "detector-results"
DEFAULT_PARTS_DIR = DEFAULT_RESULTS_DIR / "parts"
DEFAULT_LOGS_DIR = DEFAULT_RESULTS_DIR / "logs"
DEFAULT_MERGED_OUT = DEFAULT_RESULTS_DIR / "grades.jsonl"
DEFAULT_LOCK_FILE = DEFAULT_RESULTS_DIR / "batch_judge_tasks.lock"
FIXED_PROMPTS = PROJECT_ROOT / "judge_prompts_deep"
FIXED_MODEL = "gpt-5.5"
FIXED_JUDGE_WORKERS = 1
FIXED_SESSION_WORKERS = 4
DEFAULT_BASE_URL = "https://ai.mindflow.com.cn/v1"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
API_KEY_ENV_NAMES = ("MODEL_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY_2")
BASE_URL_ENV_NAMES = ("MODEL_BASE_URL", "OPENAI_BASE_URL")


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks-root", default=str(DEFAULT_TASK_ROOT),
                    help="directory containing task subdirectories")
    ap.add_argument("--task", action="append", default=[],
                    help="specific TASK id or path to run; may be repeated. Default: all matching --task-glob under --tasks-root")
    ap.add_argument("--task-glob", default="TASK-*",
                    help="glob pattern for task directories under --tasks-root (default: TASK-*)")
    ap.add_argument("--glob", default="**/*.json",
                    help="glob passed to judge under each TASK-*/sessions directory (default: **/*.json)")
    ap.add_argument("--judge", default=str(DETECTOR_ROOT / "judge_trajectory_pro_v2.py"))
    ap.add_argument("--parts-dir", default=str(DEFAULT_PARTS_DIR))
    ap.add_argument("--logs-dir", default=str(DEFAULT_LOGS_DIR))
    ap.add_argument("--merged-out", default=str(DEFAULT_MERGED_OUT))
    ap.add_argument("--lock-file", default=str(DEFAULT_LOCK_FILE))
    ap.add_argument("--python", default=sys.executable or "python3")

    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--sample-all", dest="sample_all", action="store_true", default=True,
                    help="apply --samples to ALL dimensions (default)")
    ap.add_argument("--no-sample-all", dest="sample_all", action="store_false",
                    help="sample only the default subjective dims")
    ap.add_argument("--budget", type=int, default=None)
    ap.add_argument("--seg-budget", type=int, default=None)
    ap.add_argument("--map-workers", type=int, default=4)
    ap.add_argument("--detect-session-workers", type=int, default=FIXED_SESSION_WORKERS,
                    help="session files graded concurrently by this detector batch script")
    ap.add_argument("--strong-base-url")
    ap.add_argument("--strong-api-key")
    ap.add_argument("--weak-base-url")
    ap.add_argument("--weak-api-key")
    ap.add_argument("--max-retries", type=int, default=None)
    ap.add_argument("--raw-fit-frac", type=float, default=None)
    ap.add_argument("--rehydrate-frac", type=float, default=None)
    ap.add_argument("--no-prefilter", action="store_true")
    ap.add_argument("--no-weak-summary", action="store_true")

    ap.add_argument("--dry-run", action="store_true", help="pass --dry-run to judge; no JSONL part files are written")
    ap.add_argument("--overwrite", action="store_true", help="restart per-task judge outputs instead of resuming them")
    ap.add_argument("--merge-only", action="store_true", help="only merge existing part JSONL files")
    ap.add_argument("--keep-going", action="store_true", help="continue running later tasks if one task fails")
    return ap.parse_args()


def task_dirs(tasks_root, selected, task_glob="TASK-*"):
    root = Path(tasks_root).resolve()
    if selected:
        dirs = []
        for item in selected:
            p = Path(item)
            if not p.is_absolute():
                p = root / item
            dirs.append(p.resolve())
    else:
        dirs = sorted(p.resolve() for p in root.glob(task_glob) if p.is_dir())
    return dirs


def sessions_dir(task_dir):
    return Path(task_dir) / "sessions"


def flat_session_file(task_dir: Path) -> Path | None:
    """Return <task_dir>/<task_dir.name>.json if it exists (flat layout)."""
    p = Path(task_dir) / f"{Path(task_dir).name}.json"
    return p if p.is_file() else None


def has_inputs(input_dir, pattern):
    return any(not Path(p).is_dir() for p in glob.glob(str(Path(input_dir) / pattern), recursive=True))


def session_files(input_dir, pattern):
    return sorted(
        Path(p).resolve()
        for p in glob.glob(str(Path(input_dir) / pattern), recursive=True)
        if not Path(p).is_dir()
    )


def session_input_anchor(task_dir: Path) -> Path:
    """Return the directory (or file parent) used as the anchor for relative paths."""
    flat = flat_session_file(task_dir)
    if flat is not None:
        return Path(task_dir)
    return sessions_dir(task_dir)


def session_part_path(parts_dir, task_dir, session_path):
    rel = session_path.resolve().relative_to(session_input_anchor(task_dir).resolve())
    return parts_dir / task_dir.name / rel.with_suffix(".jsonl")


def session_log_path(logs_dir, task_dir, session_path):
    rel = session_path.resolve().relative_to(session_input_anchor(task_dir).resolve())
    return logs_dir / task_dir.name / rel.with_suffix(".log")


def judge_cmd(args, input_path, out_path):
    cmd = [
        args.python,
        args.judge,
        "--in", str(input_path),
        "--prompts", str(FIXED_PROMPTS),
        "--samples", str(args.samples),
        "--workers", str(FIXED_JUDGE_WORKERS),
        "--map-workers", str(args.map_workers),
        "--strong-model", FIXED_MODEL,
        "--weak-model", FIXED_MODEL,
    ]
    if not args.dry_run:
        cmd += ["--out", str(out_path)]
    if args.sample_all:
        cmd.append("--sample-all")
    else:
        cmd.append("--no-sample-all")
    if args.overwrite:
        cmd.append("--overwrite")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.no_prefilter:
        cmd.append("--no-prefilter")
    if args.no_weak_summary:
        cmd.append("--no-weak-summary")
    optional = [
        ("--budget", args.budget),
        ("--seg-budget", args.seg_budget),
        ("--strong-base-url", args.strong_base_url),
        ("--strong-api-key", args.strong_api_key),
        ("--weak-base-url", args.weak_base_url),
        ("--weak-api-key", args.weak_api_key),
        ("--max-retries", args.max_retries),
        ("--raw-fit-frac", args.raw_fit_frac),
        ("--rehydrate-frac", args.rehydrate_frac),
    ]
    for flag, value in optional:
        if value is not None:
            cmd += [flag, str(value)]
    return cmd


def load_env_file(path):
    values = {}
    p = Path(path)
    if not p.exists():
        return values
    with p.open(encoding="utf-8") as fin:
        for raw in fin:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    return values


def judge_env():
    env = os.environ.copy()
    for key, value in load_env_file(DEFAULT_ENV_FILE).items():
        env.setdefault(key, value)
    for key in API_KEY_ENV_NAMES:
        if env.get(key):
            env["OPENAI_API_KEY"] = env[key]
            break
    for key in BASE_URL_ENV_NAMES:
        if env.get(key):
            env["OPENAI_BASE_URL"] = env[key]
            break
    else:
        if DEFAULT_BASE_URL:
            env["OPENAI_BASE_URL"] = DEFAULT_BASE_URL
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def require_api_key(args, env):
    if not env.get("OPENAI_API_KEY") and not args.strong_api_key:
        raise SystemExit(
            "missing API key: set MODEL_API_KEY, OPENAI_API_KEY, or CODEX_API_KEY_2 "
            f"in the shell or in {DEFAULT_ENV_FILE}"
        )


def count_jsonl_records(path):
    p = Path(path)
    if not p.exists():
        return 0
    with p.open(encoding="utf-8") as fin:
        return sum(1 for line in fin if line.strip())


def run_judge(cmd, env):
    proc = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    had_session_errors = False
    assert proc.stdout is not None
    for line in proc.stdout:
        if "[error] session" in line or "AuthenticationError" in line:
            had_session_errors = True
        print(line, end="")
    return proc.wait(), had_session_errors


def run_judge_to_log(cmd, env, log_path):
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    had_session_errors = False
    assert proc.stdout is not None
    with Path(log_path).open("w", encoding="utf-8") as log:
        for line in proc.stdout:
            if "[error] session" in line or "AuthenticationError" in line:
                had_session_errors = True
            log.write(line)
            log.flush()
    return proc.wait(), had_session_errors


def _pid_running(pid):
    """Windows-safe liveness probe.

    os.kill(pid, 0) must NOT be used on Windows: CPython maps any signal other
    than CTRL_C_EVENT/CTRL_BREAK_EVENT to TerminateProcess(handle, sig), which
    would kill the very process being checked."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
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


def acquire_run_lock(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        for attempt in range(2):
            try:
                handle = p.open("x", encoding="utf-8")
                break
            except FileExistsError:
                if attempt:
                    raise SystemExit(f"another batch_judge_tasks.py run is already active; lock file: {p}")
                try:
                    owner = int(p.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    owner = None
                if owner is None or _pid_running(owner):
                    raise SystemExit(f"another batch_judge_tasks.py run is already active; lock file: {p}")
                print(f"[warn] removing stale lock (owner PID {owner} is not running): {p}", file=sys.stderr)
                try:
                    p.unlink()
                except OSError:
                    pass
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
    handle = p.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another batch_judge_tasks.py run is already active; lock file: {p}")
    handle.write(str(os.getpid()))
    handle.truncate()
    handle.flush()
    return handle


def collect_session_jobs(tasks, args):
    jobs = []
    errors = []
    for task_dir in tasks:
        if not task_dir.is_dir():
            errors.append(f"task directory does not exist: {task_dir}")
            continue
        flat = flat_session_file(task_dir)
        if flat is not None:
            files = [flat]
        else:
            input_dir = sessions_dir(task_dir)
            if not input_dir.is_dir():
                errors.append(f"no sessions directory under {task_dir}")
                continue
            files = session_files(input_dir, args.glob)
        if not files:
            errors.append(f"no inputs matched {args.glob} under {task_dir}")
            continue
        jobs.extend((task_dir, session_path) for session_path in files)
    return jobs, errors


def run_session(task_dir, session_path, args, env, parts_dir, logs_dir):
    if not task_dir.is_dir():
        return None, f"task directory does not exist: {task_dir}"
    flat = flat_session_file(task_dir)
    if flat is not None:
        input_anchor = task_dir
    else:
        input_anchor = sessions_dir(task_dir)
        if not input_anchor.is_dir():
            return None, f"no sessions directory under {task_dir}"
    if not session_path.is_file():
        return None, f"session file does not exist: {session_path}"

    rel = session_path.resolve().relative_to(input_anchor.resolve())
    out_path = session_part_path(parts_dir, task_dir, session_path)
    log_path = session_log_path(logs_dir, task_dir, session_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run] {task_dir.name}/{rel} -> {out_path} (log: {log_path})", file=sys.stderr)
    returncode, had_session_errors = run_judge_to_log(judge_cmd(args, session_path, out_path), env, log_path)
    record_count = count_jsonl_records(out_path)
    if returncode != 0 or had_session_errors or (not args.dry_run and record_count == 0):
        details = []
        if returncode != 0:
            details.append(f"exit code {returncode}")
        if had_session_errors:
            details.append("session errors were reported")
        if not args.dry_run and record_count == 0:
            details.append("0 records written")
        detail = "; ".join(details) if details else "unknown error"
        return None, f"judge failed for {task_dir.name}/{rel} ({detail}); see {log_path}"
    print(f"[done] {task_dir.name}/{rel}: {record_count} records", file=sys.stderr)
    return out_path, None


def merge_jsonl(part_files, merged_out):
    out = Path(merged_out)
    out.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    seen = set()
    duplicates = 0
    with out.open("w", encoding="utf-8") as fout:
        for part in part_files:
            with Path(part).open(encoding="utf-8") as fin:
                for lineno, line in enumerate(fin, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise SystemExit(f"invalid JSON in {part}:{lineno}: {exc}") from exc
                    key = (rec.get("task_id"), rec.get("session_id") or rec.get("id"))
                    if key in seen:
                        duplicates += 1
                    seen.add(key)
                    fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    total += 1
    return total, duplicates


def main():
    args = parse_args()
    lock_handle = acquire_run_lock(args.lock_file)
    try:
        env = judge_env()
        if not args.merge_only and not args.dry_run:
            require_api_key(args, env)
        parts_dir = Path(args.parts_dir).resolve()
        logs_dir = Path(args.logs_dir).resolve()
        parts_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        tasks = task_dirs(args.tasks_root, args.task, task_glob=args.task_glob)
        if not tasks and not args.merge_only:
            raise SystemExit(f"no TASK-* directories found under {Path(args.tasks_root).resolve()}")

        part_files = []
        if not args.merge_only:
            session_jobs, collection_errors = collect_session_jobs(tasks, args)
            if collection_errors:
                for err in collection_errors:
                    if args.keep_going:
                        print(f"[warn] {err}", file=sys.stderr)
                    else:
                        raise SystemExit(err)
            if not session_jobs:
                raise SystemExit(f"no session inputs matched {args.glob} under selected tasks")
            session_workers = max(1, int(args.detect_session_workers or FIXED_SESSION_WORKERS))
            print(
                f"[parallel] session workers={session_workers}; per-session judge workers={FIXED_JUDGE_WORKERS}; "
                f"sessions={len(session_jobs)}; prompts={FIXED_PROMPTS}",
                file=sys.stderr,
            )
            with ThreadPoolExecutor(max_workers=session_workers) as pool:
                futures = {
                    pool.submit(run_session, task_dir, session_path, args, env, parts_dir, logs_dir): (
                        task_dir,
                        session_path,
                    )
                    for task_dir, session_path in session_jobs
                }
                for future in as_completed(futures):
                    out_path, err = future.result()
                    if err:
                        if args.keep_going:
                            print(f"[warn] {err}", file=sys.stderr)
                            continue
                        raise SystemExit(err)
                    if out_path is not None:
                        part_files.append(out_path)

        if args.merge_only:
            part_files = sorted(p for p in parts_dir.rglob("*.jsonl") if p.is_file())
        else:
            part_files = sorted(p for p in part_files if p.is_file())
        if args.dry_run:
            print("[done] dry-run completed; merge skipped because judge wrote no JSONL files.", file=sys.stderr)
            return
        if not part_files:
            raise SystemExit(f"no part JSONL files found in {parts_dir}")

        total, duplicates = merge_jsonl(part_files, args.merged_out)
        print(f"[merge] {len(part_files)} part files, {total} records -> {Path(args.merged_out).resolve()}",
              file=sys.stderr)
        if duplicates:
            print(f"[warn] duplicate task/session keys in merged output: {duplicates}", file=sys.stderr)
    finally:
        lock_handle.close()
        if fcntl is None:
            try:
                Path(args.lock_file).unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()

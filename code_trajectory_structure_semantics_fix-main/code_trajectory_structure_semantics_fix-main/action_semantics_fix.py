#!/usr/bin/env python3
"""Orchestrate the action-semantics trajectory repair scripts.

The scheduler applies repairs by default. Pass ``--dry-run`` to only print the
commands and write a plan report. The default schedule is task-major, so each
TASK runs through all selected steps independently while multiple TASKs can run
concurrently. Pass ``--schedule stage-major`` to restore the old per-step
barrier. Use ``--resume`` to skip task-step pairs already completed in a prior
report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent
ACTION_FIX_DIR = ROOT_DIR / "action_semantics_fix_script"
CONFIG_ENV_VAR = "ACTION_SEMANTICS_FIX_CONFIG_PATH"
DEFAULT_CONFIG_PATH = ROOT_DIR / "action_semantics_fix_config.toml"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)).expanduser().resolve()


@dataclass(frozen=True)
class FixStep:
    name: str
    script_name: str
    description: str
    log_description: str
    requires_llm: bool = False


FIX_STEPS: tuple[FixStep, ...] = (
    FixStep(
        "remove_meaningless_dialogues",
        "remove_meaningless_dialogues.py",
        "删除独立的无意义对话轮次",
        "删除无意义对话",
        requires_llm=True,
    ),
    FixStep(
        "modify_assistant_content",
        "modify_assistant_content.py",
        "精简末尾 assistant 内容，删除防御性和过程性表述",
        "精简末尾助手内容",
        requires_llm=True,
    ),
    FixStep(
        "fix_missing_summary_nodes",
        "fix_missing_summary_nodes.py",
        "分类并修复缺少终态总结的末 assistant",
        "补齐缺失终态总结",
        requires_llm=True,
    ),
    FixStep(
        "clean_last_assistant",
        "clean_last_assistant.py",
        "清理 mixed 终态回复，只保留真实完成总结",
        "清理末尾混合回复",
        requires_llm=True,
    ),
    FixStep(
        "ua_detect_and_repair",
        "ua_detect_and_repair.py",
        "检测并修复 user-assistant 对应关系及终答幻觉",
        "修复问答对应幻觉",
        requires_llm=True,
    ),
)

STEP_BY_NAME = {step.name: step for step in FIX_STEPS}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as config_file:
            payload = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML config: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"config root must be a TOML table: {path}")
    version = payload.get("config_version", 1)
    if version != 1:
        raise ValueError(f"unsupported config_version={version!r}; expected 1")
    return payload


def config_table(parent: dict[str, Any], name: str) -> dict[str, Any]:
    value = parent.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config section [{name}] must be a table")
    return value


def config_bool(table: dict[str, Any], key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"config value {key} must be true or false")
    return value


def config_int(table: dict[str, Any], key: str, default: int, *, minimum: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"config value {key} must be an integer >= {minimum}")
    return value


def config_text(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config value {key} must be a non-empty string")
    return value.strip()


def configured_path(table: dict[str, Any], key: str, default: str) -> Path:
    path = Path(config_text(table, key, default)).expanduser()
    return path if path.is_absolute() else ROOT_DIR / path


CONFIG = load_config()
GENERAL_CONFIG = config_table(CONFIG, "general")
FIX_CONFIG = config_table(CONFIG, "fix")
FIX_SCRIPT_CONFIG = config_table(FIX_CONFIG, "scripts")
LLM_CONFIG = config_table(CONFIG, "llm")

unknown_fix_names = set(FIX_SCRIPT_CONFIG) - set(STEP_BY_NAME)
if unknown_fix_names:
    raise ValueError(f"unknown action semantics script(s) in config: {', '.join(sorted(unknown_fix_names))}")

FIX_ENABLED = config_bool(FIX_CONFIG, "enabled", True)
FIX_SCRIPT_SWITCHES = {
    step.name: config_bool(FIX_SCRIPT_CONFIG, step.name, True)
    for step in FIX_STEPS
}

DEFAULT_SESSION_PATH = config_text(GENERAL_CONFIG, "session_path", "sessions")
DEFAULT_REPORT_SUBDIR = Path(
    config_text(GENERAL_CONFIG, "report_subdir", "report/action_semantics_fix")
)
DEFAULT_TASK_WORKERS = config_int(GENERAL_CONFIG, "task_workers", 4, minimum=1)
DEFAULT_WORKERS = config_int(GENERAL_CONFIG, "workers", 1, minimum=1)
DEFAULT_STOP_ON_ERROR = config_bool(GENERAL_CONFIG, "stop_on_error", True)
DEFAULT_NO_LLM = config_bool(GENERAL_CONFIG, "no_llm", False)
DEFAULT_TASK_GLOB = config_text(GENERAL_CONFIG, "task_glob", "TASK-*")
DEFAULT_MEANINGLESS_HEURISTIC_ONLY = config_bool(
    GENERAL_CONFIG, "meaningless_heuristic_only", False
)
DEFAULT_MAX_FILES = config_int(GENERAL_CONFIG, "max_files", 0, minimum=0)
DEFAULT_ENV_PATH = configured_path(LLM_CONFIG, "env_file", ".env")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统一调度 action_semantics_fix_script 下的轨迹语义修复脚本。"
    )
    parser.add_argument(
        "-r",
        "--root",
        type=Path,
        help="包含 TASK-* 的根目录，或单个 TASK-* 目录。",
    )
    parser.add_argument(
        "-path",
        "--session-path",
        default=DEFAULT_SESSION_PATH,
        help=f"每个 TASK 下的 session 相对路径，默认 {DEFAULT_SESSION_PATH}。",
    )
    parser.add_argument(
        "-i",
        "--session-file",
        type=Path,
        help="只处理指定的一个 session JSON；文件必须位于所选 TASK 的 session 目录下。",
    )
    parser.add_argument(
        "-o",
        "--report-dir",
        type=Path,
        help="报告根目录；默认 <root>/report/action_semantics_fix。",
    )
    parser.add_argument("--python", default=sys.executable, help="运行子脚本的 Python。")
    parser.add_argument("--list", action="store_true", help="列出步骤并退出。")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        default=True,
        help="实际执行子脚本（默认行为）。",
    )
    mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="仅打印命令，不启动子脚本。",
    )

    selection = parser.add_argument_group("step selection")
    selection.add_argument("--only", default="", help="只运行指定步骤，逗号分隔。")
    selection.add_argument("--skip", default="", help="跳过指定步骤，逗号分隔。")
    llm_mode = selection.add_mutually_exclusive_group()
    llm_mode.add_argument(
        "--no-llm",
        dest="no_llm",
        action="store_true",
        default=DEFAULT_NO_LLM,
        help=(
            "不运行必须依赖 LLM 的总结脚本；remove_meaningless_dialogues "
            "改用 heuristic-only，compact LLM 辅助关闭。"
        ),
    )
    llm_mode.add_argument(
        "--with-llm",
        dest="no_llm",
        action="store_false",
        help="启用配置中打开的 LLM 步骤。",
    )

    runtime = parser.add_argument_group("runtime options")
    runtime.add_argument(
        "--task-workers",
        type=int,
        default=DEFAULT_TASK_WORKERS,
        help=f"同一步骤并行处理的 TASK 数，默认 {DEFAULT_TASK_WORKERS}。",
    )
    runtime.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"单个 TASK 内部传给子脚本的 worker 数，默认 {DEFAULT_WORKERS}。",
    )
    runtime.add_argument(
        "--schedule",
        choices=("task-major", "stage-major"),
        default="task-major",
        help=(
            "Scheduling mode. task-major runs each TASK through all selected "
            "steps independently; stage-major keeps the old per-step barrier."
        ),
    )
    runtime.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default="",
        metavar="REPORT",
        help=(
            "Skip task-step pairs already completed in a previous scheduler "
            "report. With no value, use the newest complete report under the "
            "report root; otherwise pass a run directory or report JSON."
        ),
    )
    errors = runtime.add_mutually_exclusive_group()
    errors.add_argument(
        "--continue-on-error",
        dest="continue_on_error",
        action="store_true",
        default=not DEFAULT_STOP_ON_ERROR,
        help="步骤失败后继续。",
    )
    errors.add_argument(
        "--stop-on-error",
        dest="continue_on_error",
        action="store_false",
        help="首个步骤失败时停止。",
    )
    runtime.add_argument("--quiet", action="store_true", help="减少调度器和 compact 输出。")
    runtime.add_argument("--filter", default="", help="clean_last_assistant 路径过滤条件。")
    runtime.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help="限制支持该参数的 LLM 清理步骤处理文件数；0 表示不限制。",
    )
    runtime.add_argument(
        "--task-glob",
        default=DEFAULT_TASK_GLOB,
        help=f"TASK directory glob, default: {DEFAULT_TASK_GLOB}.",
    )
    meaningless_mode = runtime.add_mutually_exclusive_group()
    meaningless_mode.add_argument(
        "--meaningless-heuristic-only",
        dest="meaningless_heuristic_only",
        action="store_true",
        default=DEFAULT_MEANINGLESS_HEURISTIC_ONLY,
        help="remove_meaningless_dialogues 只用本地启发式，不调用模型。",
    )
    meaningless_mode.add_argument(
        "--meaningless-model",
        dest="meaningless_heuristic_only",
        action="store_false",
        help="remove_meaningless_dialogues 使用模型判定。",
    )
    runtime.add_argument(
        "--check-report",
        type=Path,
        help="显式提供 fix_missing_summary_nodes 报告给 clean_last_assistant。",
    )
    runtime.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"加载到子进程环境的 dotenv；默认 {DEFAULT_ENV_PATH}。",
    )
    return parser.parse_args(argv)


def parse_name_set(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def selected_steps(args: argparse.Namespace) -> list[FixStep]:
    only = parse_name_set(args.only)
    skip = parse_name_set(args.skip)
    unknown = (only | skip) - set(STEP_BY_NAME)
    if unknown:
        valid = ", ".join(STEP_BY_NAME)
        raise SystemExit(f"unknown step(s): {', '.join(sorted(unknown))}; valid: {valid}")

    if not FIX_ENABLED:
        return []

    if only:
        chosen = [
            step for step in FIX_STEPS
            if FIX_SCRIPT_SWITCHES[step.name] and step.name in only
        ]
    else:
        chosen = [
            step for step in FIX_STEPS
            if FIX_SCRIPT_SWITCHES[step.name]
        ]

    chosen = [step for step in chosen if step.name not in skip]
    if args.no_llm:
        chosen = [
            step
            for step in chosen
            if not step.requires_llm or step.name == "remove_meaningless_dialogues"
        ]
    return chosen


def require_scripts(steps: list[FixStep]) -> None:
    missing = [ACTION_FIX_DIR / step.script_name for step in steps if not (ACTION_FIX_DIR / step.script_name).is_file()]
    if missing:
        raise FileNotFoundError("missing action semantics scripts:\n" + "\n".join(str(path) for path in missing))


def load_env_file(path: Path, env: dict[str, str]) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env.setdefault(key, value)


def build_env(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    env_file = args.env_file.expanduser().resolve()
    load_env_file(env_file, env)
    env[CONFIG_ENV_VAR] = str(CONFIG_PATH)
    # ua_detect_and_repair 没有 worker CLI；由统筹器把并发预算显式传给它。
    worker_budget = str(max(1, args.workers))
    env["UA_TASK_WORKERS"] = worker_budget
    env["UA_ROUND_WORKERS"] = "1"
    env["UA_LLM_CONCURRENCY"] = worker_budget
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def session_input_paths(root: Path, session_path: str) -> list[Path]:
    direct = root / session_path
    if direct.is_dir():
        return [direct]
    paths = [
        task_dir / session_path
        for task_dir in sorted(root.glob("TASK-*"))
        if task_dir.is_dir() and (task_dir / session_path).is_dir()
    ]
    return paths or [root]


def discover_task_roots(root: Path, session_path: str, task_glob: str) -> list[Path]:
    """Resolve -r to one TASK directory or a stable batch of TASK directories."""
    direct = root / session_path
    if direct.is_dir():
        return [root]
    return [
        task_dir
        for task_dir in sorted(root.glob(task_glob))
        if task_dir.is_dir() and (task_dir / session_path).is_dir()
    ]


def select_session_task(
    session_file: Path,
    task_roots: list[Path],
    session_path: str,
) -> tuple[Path, Path]:
    """Resolve one session file and return its owning TASK directory."""
    resolved = session_file.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"session file not found: {resolved}")

    owners: list[Path] = []
    for task_root in task_roots:
        session_root = (task_root / session_path).resolve()
        try:
            resolved.relative_to(session_root)
        except ValueError:
            continue
        owners.append(task_root)
    if len(owners) != 1:
        raise ValueError(
            f"session file must belong to exactly one selected TASK/{session_path}: {resolved}"
        )
    return owners[0], resolved


def task_name(task_root: Path) -> str:
    return task_root.name or "TASK-root"


def task_log_path(task_root: Path, run_id: str) -> Path:
    return ROOT_DIR / "log" / task_name(task_root) / run_id / "action_semantics_fix.log"


def append_task_log(path: Path, step: FixStep, success: bool | str) -> None:
    """Append one intentionally minimal per-TASK result line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    status = success if isinstance(success, str) else str(success).lower()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{step.script_name} {status} {step.log_description}\n")


def resolve_resume_report(raw_resume: str, report_root: Path) -> Path | None:
    if not raw_resume:
        return None

    if raw_resume == "latest":
        candidates = [
            path
            for path in report_root.glob("*/action_semantics_fix_report.json")
            if path.is_file()
        ]
        if not candidates:
            raise FileNotFoundError(f"no scheduler report found under: {report_root}")
        return max(candidates, key=lambda path: path.stat().st_mtime_ns)

    resume_path = Path(raw_resume).expanduser()
    if resume_path.is_dir():
        resume_path = resume_path / "action_semantics_fix_report.json"
    if not resume_path.is_file():
        raise FileNotFoundError(f"resume report not found: {resume_path}")
    return resume_path.resolve()


def load_resume_completed(
    report_path: Path,
    task_roots: list[Path],
    steps: list[FixStep],
) -> tuple[dict[str, Any], dict[str, dict[str, dict[str, Any]]]]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    task_names = {task_name(path) for path in task_roots}
    task_roots_by_name = {task_name(path): path.resolve() for path in task_roots}
    step_names = {step.name for step in steps}

    completed: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in task_names}
    for stage_record in payload.get("steps", []):
        if not isinstance(stage_record, dict):
            continue
        stage_step = stage_record.get("step")
        if stage_step not in step_names:
            continue
        for record in stage_record.get("tasks", []):
            if not isinstance(record, dict) or record.get("status") != "completed":
                continue
            task = record.get("task")
            if task not in task_names:
                continue
            recorded_root = record.get("task_root")
            if isinstance(recorded_root, str) and recorded_root:
                try:
                    if Path(recorded_root).resolve() != task_roots_by_name[task]:
                        continue
                except OSError:
                    continue
            completed.setdefault(task, {})[stage_step] = record

    return payload, completed


def resumed_step_record(
    *,
    resume_report: Path | None,
    resume_completed: dict[str, dict[str, dict[str, Any]]],
    task_path: Path,
    step: FixStep,
    index: int,
) -> dict[str, Any] | None:
    if resume_report is None:
        return None
    prior = resume_completed.get(task_name(task_path), {}).get(step.name)
    if prior is None:
        return None

    record = dict(prior)
    record["index"] = index
    record["task"] = task_name(task_path)
    record["task_root"] = str(task_path)
    record["step"] = step.name
    record["script"] = step.script_name
    record["status"] = "completed"
    record["returncode"] = 0
    record["elapsed_seconds"] = 0.0
    record["resumed"] = True
    record["resumed_from_report"] = str(resume_report)
    if "generated_check_report" in prior:
        record["generated_check_report"] = prior["generated_check_report"]
    return record


def prime_resume_state(
    *,
    resume_completed: dict[str, dict[str, dict[str, Any]]],
    resume_report: Path | None,
    state_by_task: dict[Path, dict[str, Any]],
) -> None:
    if resume_report is None:
        return

    for task_path, state in state_by_task.items():
        prior = resume_completed.get(task_name(task_path), {}).get("fix_missing_summary_nodes")
        if not prior:
            continue
        generated = prior.get("generated_check_report")
        check_report: Path | None = None
        if isinstance(generated, str) and generated:
            candidate = Path(generated)
            if candidate.is_file():
                check_report = candidate
        if check_report is None:
            check_report = newest_report(
                resume_report.parent / task_name(task_path),
                "fix_missing_summary_nodes_*.json",
            )
        if check_report is not None and check_report.is_file():
            state.setdefault("check_report", check_report)


def add_workers(command: list[str], flag: str, workers: int | None) -> None:
    if workers is not None:
        command.extend([flag, str(workers)])


def step_report_path(run_dir: Path, name: str) -> Path:
    return run_dir / f"{name}.json"


def newest_report(directory: Path, pattern: str) -> Path | None:
    matches = [path for path in directory.glob(pattern) if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime_ns)


def build_step_command(
    step: FixStep,
    args: argparse.Namespace,
    task_root: Path,
    run_dir: Path,
    state: dict[str, Any],
) -> list[str]:
    script = ACTION_FIX_DIR / step.script_name
    command = [args.python, str(script)]
    report = step_report_path(run_dir, step.name)

    if step.name == "remove_meaningless_dialogues":
        if args.session_file:
            command.append(str(args.session_file))
        else:
            command.extend(str(path) for path in session_input_paths(task_root, args.session_path))
        command.extend(["--apply", "--report", str(report)])
        if args.meaningless_heuristic_only or args.no_llm:
            command.append("--heuristic-only")
        if args.max_files > 0:
            command.extend(["--limit", str(args.max_files)])
        add_workers(command, "--workers", args.workers)
        if args.quiet:
            command.append("--no-progress")
        return command

    if step.name == "modify_assistant_content":
        command.extend(["-r", str(task_root), "-report", str(run_dir)])
        if args.session_file:
            command.extend(["-i", str(args.session_file)])
        add_workers(command, "-w", args.workers)
        return command

    if step.name == "fix_missing_summary_nodes":
        command.extend(["-r", str(task_root), "-report", str(run_dir)])
        if args.session_file:
            command.extend(["-i", str(args.session_file)])
        add_workers(command, "-w", args.workers)
        return command

    if step.name == "clean_last_assistant":
        command.extend(["--base-dir", str(task_root), "--report", str(report)])
        if args.session_file:
            command.extend(["--input", str(args.session_file)])
        add_workers(command, "--workers", args.workers)
        if args.filter:
            command.extend(["--filter", args.filter])
        if args.max_files > 0:
            command.extend(["--max-files", str(args.max_files)])
        check_report = state.get("check_report")
        if isinstance(check_report, Path) and check_report.is_file():
            command.extend(["--check-report", str(check_report)])
        return command

    if step.name == "ua_detect_and_repair":
        command.extend(["-r", str(task_root), "-report", str(run_dir)])
        if args.session_file:
            command.extend(["-i", str(args.session_file)])
        return command

    raise ValueError(f"unsupported step: {step.name}")


def command_text(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def run_task_step(
    *,
    step_index: int,
    step: FixStep,
    args: argparse.Namespace,
    task_root: Path,
    task_run_dir: Path,
    state: dict[str, Any],
    env: dict[str, str],
    apply_mode: bool,
) -> dict[str, Any]:
    command = build_step_command(step, args, task_root, task_run_dir, state)
    record: dict[str, Any] = {
        "index": step_index,
        "task": task_name(task_root),
        "task_root": str(task_root),
        "step": step.name,
        "script": step.script_name,
        "command": command,
        "command_text": command_text(command),
        "status": "planned",
        "returncode": None,
        "elapsed_seconds": 0.0,
    }
    if not apply_mode:
        return record

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            check=False,
            cwd=str(task_root),
            env=env,
        )
        record["returncode"] = completed.returncode
        record["status"] = "completed" if completed.returncode == 0 else "failed"
    except OSError as exc:
        record["returncode"] = 127
        record["status"] = "error"
        record["error"] = str(exc)
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)

    if step.name == "fix_missing_summary_nodes":
        generated = newest_report(task_run_dir, "fix_missing_summary_nodes_*.json")
        if generated is not None:
            state["check_report"] = generated
            record["generated_check_report"] = str(generated)
    return record


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def list_steps() -> None:
    for index, step in enumerate(FIX_STEPS, start=1):
        configured = FIX_ENABLED and FIX_SCRIPT_SWITCHES[step.name]
        enabled = "on" if configured else "off"
        llm = "llm" if step.requires_llm else "local"
        print(
            f"{index:02d}\t{step.name}\t{enabled}\t{llm}\t"
            f"{step.script_name}\t{step.description}"
        )


def task_step_failed(record: dict[str, Any]) -> bool:
    return record.get("status") in {"failed", "error"}


def run_task_pipeline(
    *,
    steps: list[FixStep],
    args: argparse.Namespace,
    task_path: Path,
    task_run_dir: Path,
    task_log: Path,
    state: dict[str, Any],
    env: dict[str, str],
    resume_report: Path | None,
    resume_completed: dict[str, dict[str, dict[str, Any]]],
    apply_mode: bool,
) -> dict[str, Any]:
    """Run all selected steps for one TASK in order."""
    task_run_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    stopped_pipeline = False
    stopped_after: str | None = None

    for index, step in enumerate(steps, start=1):
        resumed = resumed_step_record(
            resume_report=resume_report,
            resume_completed=resume_completed,
            task_path=task_path,
            step=step,
            index=index,
        )
        if resumed is not None:
            records.append(resumed)
            if apply_mode:
                append_task_log(task_log, step, "resumed")
            if not args.quiet:
                print(
                    f"  [{task_name(task_path)}] [{index}/{len(steps)}] "
                    f"{step.name} resumed from {resumed.get('resumed_from_report')}",
                    flush=True,
                )
            continue

        command: list[str] | None = None
        try:
            command = build_step_command(step, args, task_path, task_run_dir, state)
            if not args.quiet:
                print(
                    f"  [{task_name(task_path)}] [{index}/{len(steps)}] "
                    f"{command_text(command)}",
                    flush=True,
                )
            record = run_task_step(
                step_index=index,
                step=step,
                args=args,
                task_root=task_path,
                task_run_dir=task_run_dir,
                state=state,
                env=env,
                apply_mode=apply_mode,
            )
        except Exception as exc:  # defensive: keep other TASKs moving
            record = {
                "index": index,
                "task": task_name(task_path),
                "task_root": str(task_path),
                "step": step.name,
                "script": step.script_name,
                "command": command or [],
                "command_text": command_text(command) if command else "",
                "status": "error",
                "returncode": 127,
                "elapsed_seconds": 0.0,
                "error": str(exc),
            }

        records.append(record)
        if apply_mode:
            append_task_log(task_log, step, record.get("status") == "completed")

        if not args.quiet:
            print(
                f"  [{task_name(task_path)}] {step.name} "
                f"{record.get('status')} rc={record.get('returncode')} "
                f"elapsed={record.get('elapsed_seconds', 0.0):.3f}s",
                flush=True,
            )

        if task_step_failed(record) and not args.continue_on_error:
            record["stopped_task_pipeline"] = True
            stopped_pipeline = True
            stopped_after = step.name
            break

    return {
        "task": task_name(task_path),
        "task_root": str(task_path),
        "records": records,
        "stopped_pipeline": stopped_pipeline,
        "stopped_after": stopped_after,
    }


def aggregate_task_major_results(
    *,
    steps: list[FixStep],
    task_results: list[dict[str, Any]],
    apply_mode: bool,
    task_count: int,
) -> list[dict[str, Any]]:
    records_by_step: dict[str, list[dict[str, Any]]] = {
        step.name: [] for step in steps
    }
    for task_result in sorted(task_results, key=lambda item: item.get("task", "")):
        for record in task_result.get("records", []):
            step_name = record.get("step")
            if isinstance(step_name, str):
                records_by_step.setdefault(step_name, []).append(record)

    results: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        stage_results = sorted(
            records_by_step.get(step.name, []),
            key=lambda item: item.get("task", ""),
        )
        if not stage_results:
            continue
        stage_failed = sum(
            1 for item in stage_results
            if item.get("status") in {"failed", "error"}
        )
        stage_summary = {
            "tasks": len(stage_results),
            "completed": sum(1 for item in stage_results if item.get("status") == "completed"),
            "resumed": sum(1 for item in stage_results if item.get("resumed")),
            "failed": stage_failed,
            "planned": sum(1 for item in stage_results if item.get("status") == "planned"),
        }
        if len(stage_results) < task_count:
            stage_summary["not_attempted"] = task_count - len(stage_results)
        results.append({
            "index": index,
            "step": step.name,
            "script": step.script_name,
            "status": "failed" if stage_failed else ("planned" if not apply_mode else "completed"),
            "tasks": stage_results,
            "summary": stage_summary,
        })
    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        list_steps()
        return 0
    if args.root is None:
        raise SystemExit("error: -r/--root is required unless --list is used")
    if args.task_workers < 1:
        raise SystemExit("error: --task-workers must be a positive integer")
    if args.workers is not None and args.workers < 1:
        raise SystemExit("error: --workers must be a positive integer")
    if args.max_files < 0:
        raise SystemExit("error: --max-files must be >= 0")

    task_root = args.root.expanduser().resolve()
    if not task_root.is_dir():
        raise FileNotFoundError(f"TASK root not found: {task_root}")
    task_roots = discover_task_roots(task_root, args.session_path, args.task_glob)
    if not task_roots:
        raise FileNotFoundError(
            f"no TASK directories with {args.session_path!r} found under: {task_root}"
        )
    if args.session_file:
        owner, session_file = select_session_task(
            args.session_file,
            task_roots,
            args.session_path,
        )
        task_roots = [owner]
        args.session_file = session_file

    steps = selected_steps(args)
    if not steps:
        raise SystemExit("error: no action semantics steps selected")
    require_scripts(steps)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_root = (
        args.report_dir.expanduser().resolve()
        if args.report_dir
        else task_root / DEFAULT_REPORT_SUBDIR
    )
    run_dir = report_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    overall_report = run_dir / "action_semantics_fix_report.json"

    resume_report = resolve_resume_report(args.resume, report_root)
    resume_payload: dict[str, Any] | None = None
    resume_completed: dict[str, dict[str, dict[str, Any]]] = {}
    if resume_report is not None:
        resume_payload, resume_completed = load_resume_completed(
            resume_report,
            task_roots,
            steps,
        )

    task_run_dirs = {
        task_path: run_dir / task_name(task_path)
        for task_path in task_roots
    }
    task_log_paths = {
        task_path: task_log_path(task_path, run_id)
        for task_path in task_roots
    }

    state_by_task: dict[Path, dict[str, Any]] = {
        task_path: {} for task_path in task_roots
    }
    if args.check_report:
        explicit = args.check_report.expanduser().resolve()
        if not explicit.is_file():
            raise FileNotFoundError(f"check report not found: {explicit}")
        for state in state_by_task.values():
            state["check_report"] = explicit
    prime_resume_state(
        resume_completed=resume_completed,
        resume_report=resume_report,
        state_by_task=state_by_task,
    )

    env = build_env(args)
    apply_mode = bool(args.apply)
    results: list[dict[str, Any]] = []
    overall_rc = 0
    started_at = datetime.now().isoformat(timespec="seconds")

    if not args.quiet:
        print("=" * 72)
        print("[action semantics fix scheduler]")
        print(f"  root       = {task_root}")
        print(f"  tasks      = {len(task_roots)}")
        print(f"  session    = {args.session_file if args.session_file else 'all'}")
        print(f"  schedule   = {args.schedule}")
        print(f"  resume     = {resume_report if resume_report is not None else 'off'}")
        print(f"  task_workers = {args.task_workers}")
        print(f"  mode       = {'apply' if apply_mode else 'dry-run/plan'}")
        print(f"  reports    = {run_dir}")
        print(f"  logs       = {ROOT_DIR / 'log'}")
        print(f"  steps      = {len(steps)}")
        print("=" * 72)

    if args.schedule == "task-major":
        if not args.quiet:
            print(
                f"[task-major] {len(task_roots)} TASKs x {len(steps)} steps",
                flush=True,
            )
        max_task_workers = min(args.task_workers, len(task_roots))
        task_pipeline_results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_task_workers) as executor:
            futures = {}
            for task_path in task_roots:
                futures[executor.submit(
                    run_task_pipeline,
                    steps=steps,
                    args=args,
                    task_path=task_path,
                    task_run_dir=task_run_dirs[task_path],
                    task_log=task_log_paths[task_path],
                    state=state_by_task[task_path],
                    env=env,
                    resume_report=resume_report,
                    resume_completed=resume_completed,
                    apply_mode=apply_mode,
                )] = task_path

            for future in as_completed(futures):
                task_path = futures[future]
                try:
                    task_result = future.result()
                except Exception as exc:  # defensive: a worker must not abort the batch
                    task_result = {
                        "task": task_name(task_path),
                        "task_root": str(task_path),
                        "records": [{
                            "index": 0,
                            "task": task_name(task_path),
                            "task_root": str(task_path),
                            "step": "task_pipeline",
                            "script": Path(__file__).name,
                            "status": "error",
                            "returncode": 127,
                            "elapsed_seconds": 0.0,
                            "error": str(exc),
                        }],
                        "stopped_pipeline": True,
                        "stopped_after": "task_pipeline",
                        "error": str(exc),
                    }
                task_pipeline_results.append(task_result)
                for record in task_result.get("records", []):
                    returncode = record.get("returncode")
                    if returncode and overall_rc == 0:
                        overall_rc = int(returncode)
        results = aggregate_task_major_results(
            steps=steps,
            task_results=task_pipeline_results,
            apply_mode=apply_mode,
            task_count=len(task_roots),
        )
    else:
        for index, step in enumerate(steps, start=1):
            if not args.quiet:
                print(f"[{index}/{len(steps)}] {step.name} ({len(task_roots)} TASKs)")

            stage_results: list[dict[str, Any]] = []
            max_task_workers = min(args.task_workers, len(task_roots))
            with ThreadPoolExecutor(max_workers=max_task_workers) as executor:
                futures = {}
                for task_path in task_roots:
                    resumed = resumed_step_record(
                        resume_report=resume_report,
                        resume_completed=resume_completed,
                        task_path=task_path,
                        step=step,
                        index=index,
                    )
                    if resumed is not None:
                        stage_results.append(resumed)
                        if apply_mode:
                            append_task_log(
                                task_log_paths[task_path],
                                step,
                                "resumed",
                            )
                        if not args.quiet:
                            print(
                                f"  [{task_name(task_path)}] {step.name} resumed "
                                f"from {resumed.get('resumed_from_report')}",
                                flush=True,
                            )
                        continue

                    task_run_dir = task_run_dirs[task_path]
                    task_run_dir.mkdir(parents=True, exist_ok=True)
                    command = build_step_command(
                        step,
                        args,
                        task_path,
                        task_run_dir,
                        state_by_task[task_path],
                    )
                    if not args.quiet:
                        print(
                            f"  [{task_name(task_path)}] {command_text(command)}",
                            flush=True,
                        )
                    futures[executor.submit(
                        run_task_step,
                        step_index=index,
                        step=step,
                        args=args,
                        task_root=task_path,
                        task_run_dir=task_run_dir,
                        state=state_by_task[task_path],
                        env=env,
                        apply_mode=apply_mode,
                    )] = task_path

                for future in as_completed(futures):
                    task_path = futures[future]
                    try:
                        record = future.result()
                    except Exception as exc:  # defensive: a worker must not abort the stage
                        record = {
                            "index": index,
                            "task": task_name(task_path),
                            "task_root": str(task_path),
                            "step": step.name,
                            "script": step.script_name,
                            "status": "error",
                            "returncode": 127,
                            "elapsed_seconds": 0.0,
                            "error": str(exc),
                        }
                    stage_results.append(record)

                    if apply_mode:
                        append_task_log(
                            task_log_paths[task_path],
                            step,
                            record.get("status") == "completed",
                        )
                    if not args.quiet:
                        print(
                            f"  [{task_name(task_path)}] {record.get('status')} "
                            f"rc={record.get('returncode')} "
                            f"elapsed={record.get('elapsed_seconds', 0.0):.3f}s",
                            flush=True,
                        )
                    returncode = record.get("returncode")
                    if returncode and overall_rc == 0:
                        overall_rc = int(returncode)

            stage_results.sort(key=lambda item: item.get("task", ""))
            stage_failed = sum(
                1 for item in stage_results
                if item.get("status") in {"failed", "error"}
            )
            stage_record = {
                "index": index,
                "step": step.name,
                "script": step.script_name,
                "status": "failed" if stage_failed else ("planned" if not apply_mode else "completed"),
                "tasks": stage_results,
                "summary": {
                    "tasks": len(stage_results),
                    "completed": sum(1 for item in stage_results if item.get("status") == "completed"),
                    "resumed": sum(1 for item in stage_results if item.get("resumed")),
                    "failed": stage_failed,
                    "planned": sum(1 for item in stage_results if item.get("status") == "planned"),
                },
            }
            results.append(stage_record)

            if stage_failed and not args.continue_on_error:
                stage_record["stopped_pipeline"] = True
                break

    payload = {
        "script": Path(__file__).name,
        "config_path": str(CONFIG_PATH),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": started_at,
        "mode": "apply" if apply_mode else "dry-run",
        "root": str(task_root),
        "task_roots": [str(path) for path in task_roots],
        "session_path": args.session_path,
        "session_file": str(args.session_file) if args.session_file else None,
        "report_dir": str(run_dir),
        "resume_from": str(resume_report) if resume_report is not None else None,
        "resume_source_summary": (
            resume_payload.get("summary") if resume_payload is not None else None
        ),
        "log_paths": {
            task_name(path): str(log_path)
            for path, log_path in task_log_paths.items()
        },
        "selected_steps": [step.name for step in steps],
        "settings": {
            "schedule": args.schedule,
            "resume": str(resume_report) if resume_report is not None else "",
            "task_workers": args.task_workers,
            "workers": args.workers,
            "task_glob": args.task_glob,
            "continue_on_error": args.continue_on_error,
            "no_llm": args.no_llm,
            "meaningless_heuristic_only": args.meaningless_heuristic_only or args.no_llm,
        },
        "summary": {
            "steps_selected": len(steps),
            "steps_recorded": len(results),
            "tasks_discovered": len(task_roots),
            "task_steps_recorded": sum(len(result.get("tasks", [])) for result in results),
            "completed": sum(
                1 for result in results for item in result.get("tasks", [])
                if item.get("status") == "completed"
            ),
            "resumed": sum(
                1 for result in results for item in result.get("tasks", [])
                if item.get("resumed")
            ),
            "failed": sum(
                1 for result in results for item in result.get("tasks", [])
                if item.get("status") in {"failed", "error"}
            ),
            "planned": sum(
                1 for result in results for item in result.get("tasks", [])
                if item.get("status") == "planned"
            ),
            "not_attempted": sum(
                result.get("summary", {}).get("not_attempted", 0)
                for result in results
            ),
            "returncode": overall_rc,
        },
        "steps": results,
    }
    write_json_atomic(overall_report, payload)
    print(f"report: {overall_report}")
    return overall_rc


if __name__ == "__main__":
    raise SystemExit(main())

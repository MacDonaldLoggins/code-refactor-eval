#!/usr/bin/env python3
"""Unified trajectory structure check/fix scheduler."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent
CHECK_DIR = ROOT_DIR / "structure_check_script"
FIX_DIR = ROOT_DIR / "structure_fix_script"
CHECK_ENTRY = CHECK_DIR / "run_pipeline_with_summary.py"
CHECK_PIPELINE = CHECK_DIR / "check_pipeline.py"
CONFIG_ENV_VAR = "CHECK_FIX_CONFIG_PATH"
DEFAULT_CONFIG_PATH = ROOT_DIR / "check_fix_config.toml"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)).expanduser().resolve()


SOURCE_STATE_CHOICES = ("sessions", "convert", "auto")
SUMMARY_FREE_STEPS = {
    "encoding",
    "broken_json",
    "malformed",
    "mojibake",
    "remove_task_tools",
    "add_codegraph_tool_definition",
    "fix_corrupted_content",
    "clean_empty_assistant",
    "dedup_consecutive_assistant",
    "fix_compact_alignment",
    "assistant_scene_modify",
    "openai_pairing_restore",
    "invalid_tool_arguments",
    "subagent_002",
    "sensitive_keywords",
    "remove_internal_model_field",
}


@dataclass(frozen=True)
class FixStep:
    name: str
    script_name: str
    description: str


FIX_STEPS: tuple[FixStep, ...] = (
    FixStep("encoding", "fix_encoding.py", "fix JSON encoding before other repairs"),
    FixStep("broken_json", "fix_broken_json_from_source.py", "repair broken JSON syntax from source"),
    FixStep("malformed", "fix_malformed_from_source.py", "repair malformed message nodes from source"),
    FixStep("mojibake", "fix_mojibake.py", "fix mojibake text in session JSON"),
    FixStep("remove_task_tools", "remove_task_tools.py", "remove Task* tool calls and paired tool results"),
    FixStep("add_codegraph_tool_definition", "add_codegraph_tool_definition_session.py", "add missing codegraph tool definitions"),
    FixStep("fix_corrupted_content", "fix_corrupted_content.py", "remove corrupted assistant messages"),
    FixStep("clean_empty_assistant", "clean_empty_assistant.py", "remove empty assistant messages"),
    FixStep("dedup_consecutive_assistant", "dedup_consecutive_assistant.py", "deduplicate consecutive assistant messages"),
    FixStep("fix_compact_alignment", "fix_compact_alignment.py", "repair compact subagent alignment"),
    FixStep("assistant_scene_modify", "fix_assistant_scence_modify.py", "repair missing assistant before continuation user"),
    FixStep("missing_first_user", "fix_missing_first_user.py", "补齐缺失 first user"),
    FixStep("openai_pairing_restore", "fix_openai_pairing_restore.py", "restore tool call/result pairing"),
    FixStep("invalid_tool_arguments", "fix_invalid_tool_arguments.py", "repair invalid tool arguments JSON"),
    FixStep("subagent_002", "fix_subagent_002.py", "remove stale subagent metadata mappings"),
    FixStep("rule50", "fix_openai_rule50_reorder_results.py", "修复 OpenAI rule 50 tool result 顺序"),
    FixStep("chinese_chars", "fix_chinese_chars.py", "替换中文/CJK 字符"),
    FixStep("short_first_user", "fix_short_first_user.py", "扩展过短 first user"),
    FixStep("double_user", "fix_double_user.py", "修复连续 user 节点"),
    FixStep("subagent_003", "fix_subagent_003.py", "删除孤立 SUBAGENT-003 文件"),
    FixStep(
        "sensitive_keywords",
        "fix_sensitive_keywords.py",
        "replace allowed Claude/Anthropic model references outside excluded fields",
    ),
    FixStep(
        "remove_internal_model_field",
        "remove_internal_model_field.py",
        "remove model=internal-model outside excluded scopes, including assistant tool-call arguments",
    ),
)

CHECK_SCRIPT_NAMES = (
    "calculate_price",
    "sessions_folder",
    "original_sessions_structure",
    "original_sessions_forkedfrom",
    "basic_acceptance",
    "json_structure",
    "openai_chat_completions",
    "tools_call_result",
    "subagent",
    "final_assistant_main",
    "session_node_order",
    "session_system_prompt",
    "tool_definitions",
    "skill_commands",
    "sensitive_keywords",
    "no_chinese",
    "forbidden_commands",
    "completed_state",
    "final",
)


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


def config_int(table: dict[str, Any], key: str, default: int, *, minimum: int = 1) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"config value {key} must be an integer >= {minimum}")
    return value


def config_text(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config value {key} must be a non-empty string")
    return value.strip()


CONFIG = load_config()
GENERAL_CONFIG = config_table(CONFIG, "general")
CHECK_CONFIG = config_table(CONFIG, "check")
FIX_CONFIG = config_table(CONFIG, "fix")
CHECK_SCRIPT_CONFIG = config_table(CHECK_CONFIG, "scripts")
FIX_SCRIPT_CONFIG = config_table(FIX_CONFIG, "scripts")

unknown_check_names = set(CHECK_SCRIPT_CONFIG) - set(CHECK_SCRIPT_NAMES)
if unknown_check_names:
    raise ValueError(f"unknown check script(s) in config: {', '.join(sorted(unknown_check_names))}")
unknown_fix_names = set(FIX_SCRIPT_CONFIG) - {step.name for step in FIX_STEPS}
if unknown_fix_names:
    raise ValueError(f"unknown fix script(s) in config: {', '.join(sorted(unknown_fix_names))}")

CHECK_ENABLED = config_bool(CHECK_CONFIG, "enabled", True)
FIX_ENABLED = config_bool(FIX_CONFIG, "enabled", True)
CHECK_SCRIPT_SWITCHES = {
    name: config_bool(CHECK_SCRIPT_CONFIG, name, True)
    for name in CHECK_SCRIPT_NAMES
}
FIX_SCRIPT_SWITCHES = {
    step.name: config_bool(FIX_SCRIPT_CONFIG, step.name, True)
    for step in FIX_STEPS
}

DEFAULT_SESSION_PATH = config_text(GENERAL_CONFIG, "session_path", "sessions")
DEFAULT_REPORT_SUBDIR = config_text(GENERAL_CONFIG, "report_subdir", "report")
DEFAULT_CHECK_WORKERS = config_int(GENERAL_CONFIG, "check_workers", 20)
DEFAULT_TASK_GLOB = config_text(GENERAL_CONFIG, "task_glob", "TASK-*")
DEFAULT_FIX_WORKERS = config_int(GENERAL_CONFIG, "fix_workers", 8)
DEFAULT_COMPACT_LLM = config_text(GENERAL_CONFIG, "compact_llm", "off")
DEFAULT_SOURCE_STATE = config_text(GENERAL_CONFIG, "source_state", "convert")
DEFAULT_MIN_FIRST_USER_CHARS = config_int(GENERAL_CONFIG, "min_first_user_chars", 30)
DEFAULT_PRICE_ERROR_MOVE = config_bool(GENERAL_CONFIG, "price_error_move", False)
DEFAULT_STOP_ON_FIX_ERROR = config_bool(GENERAL_CONFIG, "stop_on_fix_error", False)

if DEFAULT_SOURCE_STATE not in SOURCE_STATE_CHOICES:
    allowed_states = ", ".join(SOURCE_STATE_CHOICES)
    raise ValueError(f"source_state must be one of: {allowed_states}")
if DEFAULT_COMPACT_LLM not in {"off", "misfiled", "backup", "both"}:
    raise ValueError("compact_llm must be one of: off, misfiled, backup, both")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Unified trajectory scheduler. Choose exactly one of --check, --fix, "
            "or --check_fix."
        )
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", dest="mode", action="store_const", const="check", help="Run validation only.")
    modes.add_argument("--fix", dest="mode", action="store_const", const="fix", help="Run repair only.")
    modes.add_argument(
        "--check_fix",
        "--check-fix",
        dest="mode",
        action="store_const",
        const="check_fix",
        help="Run check -> fix -> check and keep before/after summaries.",
    )

    parser.add_argument("-r", "--root", type=Path, default=None, help="TASK root. Required for execution modes.")
    parser.add_argument(
        "-path",
        "--session-path",
        default=None,
        help=f"Session path under each TASK. Default: {DEFAULT_SESSION_PATH}",
    )
    parser.add_argument("-o", "--output-dir", type=Path, help="Report/output root. Default: --root.")
    parser.add_argument("--python", default=sys.executable, help="Python executable used for child scripts.")
    parser.add_argument("--list", action="store_true", help="List checks/fixes for the selected mode and exit.")

    check_group = parser.add_argument_group("check options")
    check_group.add_argument("--workers", type=int, default=DEFAULT_CHECK_WORKERS, help="Project-level check workers.")
    check_group.add_argument("--check-only", default="", help="Only run selected checks, comma-separated.")
    check_group.add_argument("--check-skip", default="", help="Skip selected checks, comma-separated.")
    check_group.add_argument(
        "--summary-output",
        type=Path,
        help="Summary path for --check. --check_fix always uses before/after names.",
    )
    check_group.add_argument("--summary-format", choices=("md", "json", "csv"), help="Check summary format.")
    check_group.add_argument("--report-subdir", default=DEFAULT_REPORT_SUBDIR, help="Per-TASK check report subdirectory.")
    price_group = check_group.add_mutually_exclusive_group()
    price_group.add_argument(
        "--allow-price-error-move",
        dest="price_error_move",
        action="store_true",
        default=DEFAULT_PRICE_ERROR_MOVE,
        help="Allow calculate_price to move abnormal TASKs.",
    )
    price_group.add_argument(
        "--no-price-error-move",
        dest="price_error_move",
        action="store_false",
        help="Only report calculate_price errors; do not move TASKs.",
    )

    fix_group = parser.add_argument_group("fix options")
    fix_group.add_argument("--summary", type=Path, help="Existing check summary used by --fix.")
    fix_group.add_argument(
        "--min-chars",
        type=int,
        default=DEFAULT_MIN_FIRST_USER_CHARS,
        help="Minimum first-user length for repair.",
    )
    fix_group.add_argument("--fix-only", default="", help="Only run selected fix steps, comma-separated.")
    fix_group.add_argument("--fix-skip", default="", help="Skip selected fix steps, comma-separated.")
    fix_group.add_argument("--task-glob", default=DEFAULT_TASK_GLOB, help=f"TASK glob for add_codegraph, default: {DEFAULT_TASK_GLOB}")
    fix_group.add_argument("--fix-workers", type=int, default=DEFAULT_FIX_WORKERS, help=f"Workers for fix steps that support concurrency, default: {DEFAULT_FIX_WORKERS}")
    fix_group.add_argument("--mapping-root", type=Path, help="Optional mapping root for compact alignment")
    fix_group.add_argument("--compact-llm", choices=("off", "misfiled", "backup", "both"), default=DEFAULT_COMPACT_LLM, help="LLM mode for fix_compact_alignment")
    fix_group.add_argument("--quiet", action="store_true", help="Reduce verbose output for structure fixes")
    stop_group = fix_group.add_mutually_exclusive_group()
    stop_group.add_argument(
        "--stop-on-fix-error",
        dest="stop_on_fix_error",
        action="store_true",
        default=DEFAULT_STOP_ON_FIX_ERROR,
        help="Stop on the first failed fix step.",
    )
    stop_group.add_argument(
        "--no-stop-on-fix-error",
        dest="stop_on_fix_error",
        action="store_false",
        help="Continue remaining fix steps after an error.",
    )
    fix_group.add_argument(
        "--pairing-source",
        "--repair-source",
        dest="pairing_source",
        type=Path,
        help=(
            "Optional source root for pairing/argument restoration. "
            "Default: use -r and read each TASK's convert-sessions directory."
        ),
    )
    fix_group.add_argument(
        "--source-state",
        "--pairing-source-state",
        "--repair-source-state",
        dest="source_state",
        choices=SOURCE_STATE_CHOICES,
        default=DEFAULT_SOURCE_STATE,
        help="Source layout under --pairing-source. Default: convert.",
    )
    fix_group.add_argument(
        "--require-pairing-source",
        action="store_true",
        help="Fail instead of skipping when no pairing source can be found.",
    )

    args = parser.parse_args(argv)
    if args.price_error_move is None:
        args.price_error_move = DEFAULT_PRICE_ERROR_MOVE
    if args.stop_on_fix_error is None:
        args.stop_on_fix_error = DEFAULT_STOP_ON_FIX_ERROR
    if args.mode is None and not args.list:
        parser.error("one mode is required: --check, --fix, or --check_fix")
    if args.mode is not None and not args.list and args.root is None:
        parser.error("-r/--root is required for --check, --fix, and --check_fix")
    if args.fix_workers < 1:
        parser.error("--fix-workers must be a positive integer")
    return args


def parse_name_set(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def require_check_files() -> None:
    missing = [path for path in (CHECK_ENTRY, CHECK_PIPELINE) if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing check files:\n" + "\n".join(str(path) for path in missing))


def selected_fix_steps(args: argparse.Namespace) -> list[FixStep]:
    only = parse_name_set(args.fix_only)
    skip = parse_name_set(args.fix_skip)
    known = {step.name for step in FIX_STEPS}
    unknown = (only | skip) - known
    if unknown:
        valid = ", ".join(sorted(known))
        raise SystemExit(f"unknown fix step(s): {', '.join(sorted(unknown))}; valid: {valid}")
    if not FIX_ENABLED:
        return []
    return [
        step
        for step in FIX_STEPS
        if FIX_SCRIPT_SWITCHES[step.name]
        and (not only or step.name in only)
        and step.name not in skip
    ]


def require_fix_files(steps: list[FixStep]) -> None:
    missing = [FIX_DIR / step.script_name for step in steps if not (FIX_DIR / step.script_name).is_file()]
    if missing:
        raise FileNotFoundError("missing fix scripts:\n" + "\n".join(str(path) for path in missing))


def list_checks(python: str) -> int:
    require_check_files()
    env = dict(os.environ)
    env[CONFIG_ENV_VAR] = str(CONFIG_PATH)
    return subprocess.run([python, str(CHECK_PIPELINE), "--list"], check=False, env=env).returncode


def list_fixes() -> None:
    for step in FIX_STEPS:
        enabled = FIX_ENABLED and FIX_SCRIPT_SWITCHES[step.name]
        state = "on" if enabled else "off"
        print(f"{step.name}\t{state}\t{step.script_name}\t{step.description}")


def handle_list(args: argparse.Namespace) -> int:
    rc = 0
    if args.mode in {None, "check", "check_fix"}:
        print("[checks]", flush=True)
        rc = list_checks(args.python)
    if args.mode in {None, "fix", "check_fix"}:
        print("[fixes]", flush=True)
        list_fixes()
    return rc


def build_check_command(
    args: argparse.Namespace,
    task_root: Path,
    output_dir: Path,
    summary_output: Path,
) -> list[str]:
    command = [
        args.python,
        str(CHECK_ENTRY),
        "-r",
        str(task_root),
        "-path",
        str(args.session_path),
        "-o",
        str(output_dir),
        "--workers",
        str(args.workers),
        "--report-subdir",
        args.report_subdir,
        "--summary-output",
        str(summary_output),
    ]
    if args.check_only:
        command.extend(["--only", args.check_only])
    if args.check_skip:
        command.extend(["--skip", args.check_skip])
    if args.summary_format:
        command.extend(["--summary-format", args.summary_format])
    if not args.price_error_move:
        command.append("--no-price-error-move")
    return command


def run_check_stage(
    args: argparse.Namespace,
    task_root: Path,
    output_dir: Path,
    summary_output: Path,
    label: str,
) -> int:
    require_check_files()
    command = build_check_command(args, task_root, output_dir, summary_output)
    print(f"[{label}] {' '.join(command)}", flush=True)
    env = dict(os.environ)
    env[CONFIG_ENV_VAR] = str(CONFIG_PATH)
    result = subprocess.run(command, check=False, env=env)
    print(f"[{label}] exit={result.returncode}", flush=True)
    return result.returncode


def load_env_file(path: Path, env: dict[str, str]) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value


def build_env() -> dict[str, str]:
    env = dict(os.environ)
    load_env_file(ROOT_DIR / ".env", env)
    env[CONFIG_ENV_VAR] = str(CONFIG_PATH)
    return env


def resolve_pairing_source(args: argparse.Namespace, task_root: Path) -> Path | None:
    if args.pairing_source:
        source = args.pairing_source.expanduser().resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"pairing source not found: {source}")
        return source
    return task_root


def choose_fix_summary(args: argparse.Namespace, output_dir: Path) -> Path:
    if args.summary:
        return args.summary.expanduser().resolve()
    candidates = (
        output_dir / "pipeline_report_summary.md",
        output_dir / "pipeline_report_summary_before.md",
        output_dir / "pipeline_report_summary_finalcheck.md",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def build_fix_step_command(
    step: FixStep,
    args: argparse.Namespace,
    summary: Path,
    task_root: Path,
    output_dir: Path,
) -> list[str] | None:
    script = FIX_DIR / step.script_name
    session_path = str(args.session_path)
    if step.name == "encoding":
        return [args.python, str(script), str(task_root), "--session-path", session_path]
    if step.name == "broken_json":
        source = resolve_pairing_source(args, task_root)
        if source is None and args.require_pairing_source:
            raise FileNotFoundError("pairing source not found for broken_json")
        command = [args.python, str(script), str(task_root)]
        if source is not None:
            command.extend(["--source", str(source)])
        command.extend(["-path", session_path, "--source-state", args.source_state])
        command.extend(["-o", str(output_dir / "fix_broken_json_report.json"), "--apply"])
        return command
    if step.name == "malformed":
        source = resolve_pairing_source(args, task_root)
        if source is None and args.require_pairing_source:
            raise FileNotFoundError("pairing source not found for malformed")
        command = [args.python, str(script), str(task_root)]
        if source is not None:
            command.extend(["--source", str(source)])
        command.extend(
            [
                "--source-state",
                args.source_state,
                "-path",
                session_path,
                "-o",
                str(output_dir / "fix_malformed_report.json"),
                "--apply",
            ]
        )
        return command
    if step.name == "mojibake":
        return [args.python, str(script), "-r", str(task_root), "-i", session_path]
    if step.name == "remove_task_tools":
        return [args.python, str(script), "-r", str(task_root), "--session-path", session_path]
    if step.name == "add_codegraph_tool_definition":
        return [args.python, str(script), str(task_root), "--task-glob", args.task_glob]
    if step.name == "fix_corrupted_content":
        return [
            args.python,
            str(script),
            "-r",
            str(task_root),
            "-i",
            args.session_path,
            "-o",
            str(output_dir / "fix_corrupted_content_report.json"),
        ]
    if step.name == "clean_empty_assistant":
        return [
            args.python,
            str(script),
            "-r",
            str(task_root),
            "-i",
            args.session_path,
            "-o",
            str(output_dir / "clean_empty_assistant_report.json"),
        ]
    if step.name == "dedup_consecutive_assistant":
        return [
            args.python,
            str(script),
            "-r",
            str(task_root),
            "-i",
            args.session_path,
            "-report",
            str(output_dir / "dedup_consecutive_assistant_report.json"),
            "--workers",
            str(args.fix_workers),
        ]
    if step.name == "fix_compact_alignment":
        command = [
            args.python,
            str(script),
            "-r",
            str(task_root),
            "-report",
            str(output_dir / "fix_compact_alignment_report.json"),
            "-workers",
            str(args.fix_workers),
            "--enable-llm",
            args.compact_llm,
        ]
        if args.mapping_root:
            command.extend(["--mapping-root", str(args.mapping_root.expanduser().resolve())])
        if args.quiet:
            command.append("--quiet")
        return command
    if step.name == "assistant_scene_modify":
        return [
            args.python,
            str(script),
            "-r",
            str(task_root),
            "-i",
            str(task_root),
        ]
    if step.name == "missing_first_user":
        return [
            args.python,
            str(script),
            "-r",
            str(summary),
            "--output",
            str(output_dir / "fix_missing_first_user_report.json"),
            "--apply",
        ]
    if step.name == "openai_pairing_restore":
        source = resolve_pairing_source(args, task_root)
        if source is None:
            message = "pairing source not found; skipping openai_pairing_restore"
            if args.require_pairing_source:
                raise FileNotFoundError(message)
            print(f"[fix skip] {message}", flush=True)
            return None
        return [
            args.python,
            str(script),
            str(task_root),
            "--source",
            str(source),
            "--source-state",
            args.source_state,
            "-path",
            session_path,
        ]
    if step.name == "invalid_tool_arguments":
        source = resolve_pairing_source(args, task_root) or task_root
        if not source.is_dir():
            raise FileNotFoundError(f"tool arguments source not found: {source}")
        return [
            args.python,
            str(script),
            str(task_root),
            "--source",
            str(source),
            "--source-state",
            args.source_state,
            "-path",
            session_path,
            "-o",
            str(output_dir / "fix_invalid_tool_arguments_report.json"),
            "--apply",
        ]
    if step.name == "subagent_002":
        return [args.python, str(script), "-r", str(task_root), "-path", session_path, "--apply"]
    if step.name == "rule50":
        return [
            args.python,
            str(script),
            "-r",
            str(summary),
            "-o",
            str(output_dir / "fix_openai_rule50_reorder_results_report.json"),
            "--apply",
        ]
    if step.name == "chinese_chars":
        return [args.python, str(script), "--report", str(summary), "--source-root", str(task_root)]
    if step.name == "short_first_user":
        return [
            args.python,
            str(script),
            "-r",
            str(summary),
            "--min-chars",
            str(args.min_chars),
            "--output",
            str(output_dir / "expand_report.json"),
            "--apply",
        ]
    if step.name == "double_user":
        return [
            args.python,
            str(script),
            "-r",
            str(summary),
            "--output",
            str(output_dir / "fix_double_user_report.json"),
            "--apply",
        ]
    if step.name == "subagent_003":
        return [args.python, str(script), "--summary", str(summary), "-o", str(output_dir), "--apply"]
    if step.name == "sensitive_keywords":
        return [
            args.python,
            str(script),
            "-r",
            str(task_root),
            "--report",
            str(output_dir / "fix_sensitive_keywords_report.json"),
            "--apply",
        ]
    if step.name == "remove_internal_model_field":
        return [
            args.python,
            str(script),
            "-r",
            str(task_root),
            "--report",
            str(output_dir / "remove_internal_model_field_report.json"),
            "--apply",
        ]
    raise ValueError(step.name)


def run_fix_stage(
    args: argparse.Namespace,
    task_root: Path,
    output_dir: Path,
    summary: Path,
) -> int:
    steps = selected_fix_steps(args)
    require_fix_files(steps)
    if any(step.name not in SUMMARY_FREE_STEPS for step in steps) and not summary.is_file():
        raise FileNotFoundError(f"summary report not found: {summary}")

    output_dir.mkdir(parents=True, exist_ok=True)
    env = build_env()
    overall_rc = 0
    for index, step in enumerate(steps, start=1):
        command = build_fix_step_command(step, args, summary, task_root, output_dir)
        if command is None:
            continue
        print(f"[fix {index}/{len(steps)}] {step.name}: {' '.join(command)}", flush=True)
        started = time.perf_counter()
        result = subprocess.run(command, check=False, cwd=str(task_root), env=env)
        elapsed = time.perf_counter() - started
        print(f"[fix {index}/{len(steps)}] {step.name} exit={result.returncode} elapsed={elapsed:.2f}s", flush=True)
        if result.returncode != 0:
            overall_rc = result.returncode
            if args.stop_on_fix_error:
                break
    return overall_rc


def resolve_runtime_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.root is None:
        raise ValueError("-r/--root is required")
    task_root = args.root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else task_root
    args.session_path = args.session_path or DEFAULT_SESSION_PATH
    if not task_root.is_dir():
        raise FileNotFoundError(f"TASK root not found: {task_root}")
    return task_root, output_dir


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        return handle_list(args)

    task_root, output_dir = resolve_runtime_paths(args)
    if args.mode == "check":
        summary = (
            args.summary_output.expanduser().resolve()
            if args.summary_output
            else output_dir / "pipeline_report_summary.md"
        )
        return run_check_stage(args, task_root, output_dir, summary, "check")

    if args.mode == "fix":
        return run_fix_stage(args, task_root, output_dir, choose_fix_summary(args, output_dir))

    before_summary = output_dir / "pipeline_report_summary_before.md"
    after_summary = output_dir / "pipeline_report_summary_after.md"
    before_rc = run_check_stage(args, task_root, output_dir, before_summary, "check-before")
    if before_rc != 0:
        return before_rc

    fix_rc = run_fix_stage(args, task_root, output_dir, before_summary)
    if fix_rc != 0 and args.stop_on_fix_error:
        return fix_rc

    after_rc = run_check_stage(args, task_root, output_dir, after_summary, "check-after")
    return after_rc or fix_rc


if __name__ == "__main__":
    raise SystemExit(main())

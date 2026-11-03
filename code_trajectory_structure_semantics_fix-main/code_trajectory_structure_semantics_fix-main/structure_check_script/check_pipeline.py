#!/usr/bin/env python3
r"""批量校验 pipeline。

使用示例：
    py C:\Users\15837\Desktop\workspace\new3\claude_code_session\check\check_pipeline.py -r C:\Users\15837\Desktop\workspace\new3\claude_code_session\conversion-clear-project -o C:\Users\15837\Desktop\workspace\new3\claude_code_session\conversion-clear-project 

参数说明：
    -r      执行路径，遍历该目录下的每个 TASK 子目录。
    -path   每个 TASK 子目录中的 session 文件或 session 目录相对路径。
            未来结构为 TASK-xxx/sessions/<main>.json 时，这里传 sessions。
    -o      报告输出目录。每个项目默认写入各自 report/check_pipeline.json。
    --workers
            多项目时按项目级并发执行；同一项目内的检查脚本仍串行执行。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
import contextlib
import threading
import time
import tomllib
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Union


@dataclass
class IssueLocation:
    check: str = ""
    project: str = ""
    session: str = ""
    line: str = ""
    preview: str = ""
    reason: str = ""

try:
    import check_basic_acceptance as basic_acceptance_module
    from check_basic_acceptance import run as run_basic_acceptance
    from check_completed_state import completed_state
    from check_final import check_final
    from check_final_assistant_main import final_assistant_main
    from check_forbidden_commands import check_forbidden_commands
    from check_json_structure import json_structure
    from check_no_chinese import check_no_chinese as run_no_chinese
    from check_openaI_chat_completions import openaI_chat_completions
    from check_original_sessions_forkedfrom import run as run_original_sessions_forkedfrom
    from check_original_sessions_structure import run as run_original_sessions_structure
    from check_calculate_price import run as run_calculate_price
    from check_session_node_order import session_node_order
    from check_session_system_prompt import session_system_prompt
    from check_sessions_folder import check_sessions_folder
    from check_sensitive_keywords import check_sensitive_keywords
    from check_skill_commands import check_skill_commands
    from check_subagent import check_subagent
    from check_tool_definitions import tool_usage
    from check_tools_call_result import check_tools_call_result
except ImportError:
    import check.check_basic_acceptance as basic_acceptance_module
    from check.check_basic_acceptance import run as run_basic_acceptance
    from check.check_completed_state import completed_state
    from check.check_final import check_final
    from check.check_final_assistant_main import final_assistant_main
    from check.check_forbidden_commands import check_forbidden_commands
    from check.check_json_structure import json_structure
    from check.check_no_chinese import check_no_chinese as run_no_chinese
    from check.check_openaI_chat_completions import openaI_chat_completions
    from check.check_original_sessions_forkedfrom import run as run_original_sessions_forkedfrom
    from check.check_original_sessions_structure import run as run_original_sessions_structure
    from check.check_calculate_price import run as run_calculate_price
    from check.check_session_node_order import session_node_order
    from check.check_session_system_prompt import session_system_prompt
    from check.check_sessions_folder import check_sessions_folder
    from check.check_sensitive_keywords import check_sensitive_keywords
    from check.check_skill_commands import check_skill_commands
    from check.check_subagent import check_subagent
    from check.check_tool_definitions import tool_usage
    from check.check_tools_call_result import check_tools_call_result


CONFIG_ENV_VAR = "CHECK_FIX_CONFIG_PATH"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "check_fix_config.toml"


def load_check_fix_config() -> dict[str, Any]:
    configured = os.environ.get(CONFIG_ENV_VAR)
    config_path = Path(configured).expanduser().resolve() if configured else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("rb") as config_file:
            payload = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML config: {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"config root must be a TOML table: {config_path}")
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


CHECK_FIX_CONFIG = load_check_fix_config()
GENERAL_CONFIG = config_table(CHECK_FIX_CONFIG, "general")
CHECK_CONFIG = config_table(CHECK_FIX_CONFIG, "check")
CHECK_SCRIPT_CONFIG = config_table(CHECK_CONFIG, "scripts")


JSON_REPORT_NAME = "check_pipeline.json"
PARENT_JSON_REPORT_NAME = "check_pipeline_parent.json"
TOOL_DEFINITIONS_PREVIEW_LIMIT = 5
SESSION_TYPE_LABELS = ("develop", "test", "bugfix")
ORIGINAL_SESSIONS_DIR = "original_sessions"
ERROR_TASK_DIR_NAME = "ERROR-TASK"
PRICE_ERROR_DEST_SUBDIR = "error-price"
PRICE_ERROR_MOVE_REPORT_NAME = "move_calculate_price_errors_report.json"
PRICE_ERROR_MOVE_SWITCH = config_bool(GENERAL_CONFIG, "price_error_move", False)
# pipeline 总开关。关闭后不执行任何子脚本，只生成跳过报告。
PIPELINE_SWITCH = config_bool(CHECK_CONFIG, "enabled", True)

# 子脚本报告保留开关。默认关闭：子脚本报告会被读取进 pipeline 总 JSON 后删除，
# 最终只保留 check_pipeline.json。
# 如需排查单个检查项的原始 JSON 报告，改为 True。
KEEP_CHILD_REPORTS = config_bool(CHECK_CONFIG, "keep_child_reports", False)

# 这些检查项的原始 JSON 报告独立保留在 check_pipeline.json 同目录中，
# pipeline 只记录摘要和 report_path，不再内嵌完整报告内容。
EXTERNAL_CHILD_REPORT_NAMES = {"forbidden_commands"}
IGNORED_BASIC_ACCEPTANCE_TURNS_REASON_RE = re.compile(r"^main user 对话轮次为 \d+，不满足 > 4$")

# 子脚本默认开关会被仓库根目录 check_fix_config.toml 覆盖。
SCRIPT_SWITCHES: dict[str, bool] = {
    "calculate_price": True,
    "sessions_folder": True,
    "original_sessions_structure": True,
    "original_sessions_forkedfrom": False,
    "basic_acceptance": True,
    "json_structure": True,
    "openai_chat_completions": True,
    "tools_call_result": True,
    "subagent": True,
    "final_assistant_main": True,
    "session_node_order": True,
    "session_system_prompt": True,
    "tool_definitions": True,
    "skill_commands": True,
    "sensitive_keywords": True,
    "no_chinese": True,
    "forbidden_commands": True,
    # 该项会调用外部 OpenAI-compatible 模型服务。
    "completed_state": False,
    # 收尾检查项（metadata.json / sessions 一致性等）
    "final": True,
}

unknown_script_names = set(CHECK_SCRIPT_CONFIG) - set(SCRIPT_SWITCHES)
if unknown_script_names:
    raise ValueError(f"unknown check script(s) in config: {', '.join(sorted(unknown_script_names))}")
for script_name in SCRIPT_SWITCHES:
    SCRIPT_SWITCHES[script_name] = config_bool(
        CHECK_SCRIPT_CONFIG,
        script_name,
        SCRIPT_SWITCHES[script_name],
    )


def apply_check_fix_config_to_child_modules(config: dict[str, Any]) -> None:
    """Propagate code-level default switches to child modules used by this pipeline."""
    _ = config

    def module_for(func: Callable[..., Any]) -> Any:
        return sys.modules.get(func.__module__)

    basic_module = module_for(run_basic_acceptance)
    if basic_module is not None and hasattr(basic_module, "BASIC_ACCEPTANCE_SWITCH"):
        setattr(basic_module, "BASIC_ACCEPTANCE_SWITCH", bool(SCRIPT_SWITCHES.get("basic_acceptance", True)))

    openai_module = module_for(openaI_chat_completions)
    if openai_module is not None and hasattr(openai_module, "OPENAI_CHAT_COMPLETIONS_SWITCH"):
        setattr(openai_module, "OPENAI_CHAT_COMPLETIONS_SWITCH", bool(SCRIPT_SWITCHES.get("openai_chat_completions", True)))

    completed_module = module_for(completed_state)
    if completed_module is not None and hasattr(completed_module, "is_model_open"):
        setattr(completed_module, "is_model_open", bool(SCRIPT_SWITCHES.get("completed_state", False)))


apply_check_fix_config_to_child_modules(CHECK_FIX_CONFIG)

SHA256_MODE_WITHIN_TASK_ONLY = getattr(basic_acceptance_module, "SHA256_MODE_WITHIN_TASK_ONLY", "within_task_only")
SHA256_MODE_CROSS_TASK_ONLY = getattr(basic_acceptance_module, "SHA256_MODE_CROSS_TASK_ONLY", "cross_task_only")
SHA256_CLI_MODE_OFF = getattr(basic_acceptance_module, "SHA256_CLI_MODE_OFF", "off")
SHA256_CLI_MODE_FALSE = getattr(basic_acceptance_module, "SHA256_CLI_MODE_FALSE", "false")
SHA256_DUPLICATE_CHECK_MODES = set(
    getattr(
        basic_acceptance_module,
        "SHA256_DUPLICATE_CHECK_MODES",
        {SHA256_MODE_WITHIN_TASK_ONLY, SHA256_MODE_CROSS_TASK_ONLY, False},
    )
)
SHA256_CLI_MODE_CHOICES = tuple(
    getattr(
        basic_acceptance_module,
        "SHA256_CLI_MODE_CHOICES",
        (
            SHA256_MODE_CROSS_TASK_ONLY,
            SHA256_MODE_WITHIN_TASK_ONLY,
            SHA256_CLI_MODE_OFF,
            SHA256_CLI_MODE_FALSE,
        ),
    )
)
# SHA-256 重复检查模式默认从 check_basic_acceptance.py 读取。
# 如需调整默认模式，修改 check_basic_acceptance.py 里的 SHA256_DUPLICATE_CHECK_MODE。
# within_task_only: 仅同一个 TASK 内部 session 重复记为 basic_acceptance 异常；不做跨 TASK 汇总检查。
# cross_task_only: 同一个 TASK 内部重复不记异常；仅在父级报告做跨 TASK 重复检查。
# False: 完全关闭 SHA-256 计算、记录和重复检查。
SHA256_DUPLICATE_CHECK_MODE = getattr(
    basic_acceptance_module,
    "SHA256_DUPLICATE_CHECK_MODE",
    SHA256_MODE_CROSS_TASK_ONLY,
)
# 兼容旧开关名。实际逻辑由 SHA256_DUPLICATE_CHECK_MODE 决定。
SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH = SHA256_DUPLICATE_CHECK_MODE == SHA256_MODE_WITHIN_TASK_ONLY


CheckCallable = Callable[[Union[Path, str], Union[Path, str], Union[Path, str]], Path]


@dataclass(frozen=True)
class CheckSpec:
    name: str
    script: str
    title: str
    func: CheckCallable
    description: str


def normalize_sha256_duplicate_check_mode(mode: str | bool) -> str | bool:
    module_normalize = getattr(basic_acceptance_module, "normalize_sha256_duplicate_check_mode", None)
    if callable(module_normalize):
        return module_normalize(mode)

    if mode is False:
        return False
    if isinstance(mode, str):
        normalized = mode.strip().lower()
        if normalized in {SHA256_CLI_MODE_OFF, SHA256_CLI_MODE_FALSE}:
            return False
        if normalized in {SHA256_MODE_WITHIN_TASK_ONLY, SHA256_MODE_CROSS_TASK_ONLY}:
            return normalized

    allowed = ", ".join(SHA256_CLI_MODE_CHOICES)
    raise ValueError(f"invalid SHA-256 duplicate check mode: {mode}; expected one of: {allowed}")


def set_sha256_duplicate_check_mode(mode: str | bool) -> None:
    global SHA256_DUPLICATE_CHECK_MODE
    global SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH

    mode = normalize_sha256_duplicate_check_mode(mode)
    SHA256_DUPLICATE_CHECK_MODE = mode
    SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH = mode == SHA256_MODE_WITHIN_TASK_ONLY


def sha256_duplicate_check_enabled() -> bool:
    set_sha256_duplicate_check_mode(SHA256_DUPLICATE_CHECK_MODE)
    return SHA256_DUPLICATE_CHECK_MODE is not False


def sha256_within_project_duplicate_enabled() -> bool:
    set_sha256_duplicate_check_mode(SHA256_DUPLICATE_CHECK_MODE)
    return SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH


def sha256_cross_project_duplicate_enabled() -> bool:
    set_sha256_duplicate_check_mode(SHA256_DUPLICATE_CHECK_MODE)
    return sha256_duplicate_check_enabled() and SHA256_DUPLICATE_CHECK_MODE == SHA256_MODE_CROSS_TASK_ONLY


def configure_basic_acceptance_sha256_switches() -> None:
    set_sha256_duplicate_check_mode(SHA256_DUPLICATE_CHECK_MODE)
    module = sys.modules.get(run_basic_acceptance.__module__)
    if module is None:
        return
    if hasattr(module, "set_sha256_duplicate_check_mode"):
        module.set_sha256_duplicate_check_mode(SHA256_DUPLICATE_CHECK_MODE)
        return
    if hasattr(module, "SHA256_DUPLICATE_CHECK_MODE"):
        setattr(module, "SHA256_DUPLICATE_CHECK_MODE", SHA256_DUPLICATE_CHECK_MODE)
    if hasattr(module, "SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH"):
        setattr(
            module,
            "SHA256_WITHIN_PROJECT_DUPLICATE_SWITCH",
            sha256_within_project_duplicate_enabled(),
        )


def run_basic_acceptance_with_switches(
    find_path: Union[Path, str],
    session_json_path: Union[Path, str],
    output_dir: Union[Path, str],
) -> Path:
    configure_basic_acceptance_sha256_switches()
    return run_basic_acceptance(find_path, session_json_path, output_dir)


def run_calculate_price_check(
    find_path: Union[Path, str],
    session_json_path: Union[Path, str],
    output_dir: Union[Path, str],
) -> Path:
    # 价格检查始终面向原始 Claude Code 轨迹，不受主 pipeline 的 -path sessions 影响。
    _ = session_json_path
    return run_calculate_price(find_path, ORIGINAL_SESSIONS_DIR, output_dir)


@dataclass
class PipelineResult:
    name: str
    script: str
    title: str
    enabled: bool
    status: str
    report_path: str = ""
    log_path: str = ""
    failed_count: int | None = None
    duration_seconds: float = 0.0
    message: str = ""
    issue_locations: list[IssueLocation] = field(default_factory=list)
    child_report_type: str = ""
    child_summary: dict[str, Any] = field(default_factory=dict)
    child_tasks: list[dict[str, Any]] = field(default_factory=list)
    child_report: dict[str, Any] | None = None


CHECK_SPECS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "calculate_price",
        "check_calculate_price.py",
        "original_sessions token 成本",
        run_calculate_price_check,
        "统计 original_sessions token 用量和估算成本；低于阈值的 TASK 视为异常。",
    ),
    CheckSpec(
        "sessions_folder",
        "check_sessions_folder.py",
        "sessions 轨迹文件",
        check_sessions_folder,
        "检查每个 TASK 项目的 sessions 目录是否存在且包含轨迹 JSON 文件。",
    ),
    CheckSpec(
        "original_sessions_structure",
        "check_original_sessions_structure.py",
        "original_sessions 目录结构",
        run_original_sessions_structure,
        "检查 original_sessions 目录下的子文件夹是否为 UUID 格式。",
    ),
    CheckSpec(
        "original_sessions_forkedfrom",
        "check_original_sessions_forkedfrom.py",
        "original_sessions forkedFrom 字段",
        run_original_sessions_forkedfrom,
        "检查 original_sessions 原始 JSONL 轨迹任意层级是否残留 forkedFrom 字段。",
    ),
    CheckSpec(
        "basic_acceptance",
        "check_basic_acceptance.py",
        "基础验收标准",
        run_basic_acceptance_with_switches,
        "JSON 格式、轮次、首个用户内容、首节点 system、SHA-256。",
    ),
    CheckSpec(
        "json_structure",
        "check_json_structure.py",
        "JSON 结构与内容",
        json_structure,
        "检查 messages、tools、meta 顶层结构，消息 content/reasoning_content 非空规则和时间戳顺序。",
    ),
    CheckSpec(
        "openai_chat_completions",
        "check_openaI_chat_completions.py",
        "OpenAI 规范检查",
        openaI_chat_completions,
        "OpenAI Chat Completions 消息数组与规则项检查。",
    ),
    CheckSpec(
        "tools_call_result",
        "check_tools_call_result.py",
        "工具调用/工具响应对齐",
        check_tools_call_result,
        "主 session 与同级 subagent 的 tool call/tool response 配对检查。",
    ),
    CheckSpec(
        "subagent",
        "check_subagent.py",
        "subagent 来源映射",
        check_subagent,
        "检查 subagent 在 metadata 中的调用来源映射。",
    ),
    CheckSpec(
        "final_assistant_main",
        "check_final_assistant_main.py",
        "最终 assistant main",
        final_assistant_main,
        "检查主轨迹最后一个节点是否为 role=assistant 且 agent=main。",
    ),
    CheckSpec(
        "session_node_order",
        "check_session_node_order.py",
        "session 节点顺序",
        session_node_order,
        "检查主轨迹 session 节点顺序。",
    ),
    CheckSpec(
        "session_system_prompt",
        "check_session_system_prompt.py",
        "system 提示词节点",
        session_system_prompt,
        "检查 sessions 所有轨迹文件首节点是否为 system 且 system 节点唯一。",
    ),
    CheckSpec(
        "tool_definitions",
        "check_tool_definitions.py",
        "工具定义与格式",
        tool_usage,
        "检查工具调用是否能在当前文件顶层 tools 定义中找到并符合 schema。",
    ),
    CheckSpec(
        "skill_commands",
        "check_skill_commands.py",
        "skill 命令标签",
        check_skill_commands,
        "检查 <command-name>/<command-message>/<command-args> 标签是否闭合、唯一且 command-name 合法。",
    ),
    CheckSpec(
        "sensitive_keywords",
        "check_sensitive_keywords.py",
        "敏感关键字",
        check_sensitive_keywords,
        "按脱敏脚本口径检查模型名、model/modelID 字段、企业文本、API key、中文路径和通用脚本名是否仍有残留。",
    ),
    CheckSpec(
        "no_chinese",
        "check_no_chinese.py",
        "中文/CJK 内容检查",
        run_no_chinese,
        "按 check_no_chinese.py 逻辑检查 sessions 轨迹中是否仍有中文/CJK 内容。",
    ),
    CheckSpec(
        "forbidden_commands",
        "check_forbidden_commands.py",
        "slash 命令",
        check_forbidden_commands,
        "检查 /help、/clear、/compact、/model 等禁止 slash 命令是否仍有残留。",
    ),
    CheckSpec(
        "completed_state",
        "check_completed_state.py",
        "完成态模型判断",
        completed_state,
        "使用 model_config.py 中的模型配置按 complete/partial_complete/not_complete 三态判断完成态。",
    ),
    CheckSpec(
        "final",
        "check_final.py",
        "收尾检查",
        check_final,
        "metadata.json 必须存在 cwd 字符串字段；sessions 下所有轨迹 meta 必须包含 cwd。",
    ),
)


ALIASES: dict[str, str] = {}
for spec in CHECK_SPECS:
    ALIASES[spec.name] = spec.name
    ALIASES[spec.script] = spec.name
    ALIASES[spec.script.removesuffix(".py")] = spec.name


def parse_name_set(raw: str | None) -> set[str]:
    if not raw:
        return set()
    names: set[str] = set()
    unknown: list[str] = []
    for item in raw.split(","):
        key = item.strip()
        if not key:
            continue
        normalized = ALIASES.get(key)
        if normalized is None:
            unknown.append(key)
        else:
            names.add(normalized)
    if unknown:
        valid = ", ".join(spec.name for spec in CHECK_SPECS)
        raise ValueError(f"未知脚本名称: {', '.join(unknown)}。可用名称: {valid}")
    return names


def selected_specs(only: set[str], skip: set[str]) -> list[tuple[CheckSpec, bool, str]]:
    rows: list[tuple[CheckSpec, bool, str]] = []
    for spec in CHECK_SPECS:
        enabled = bool(SCRIPT_SWITCHES.get(spec.name, False))
        reason = "已开启" if enabled else "代码开关关闭"
        if only and spec.name not in only:
            enabled = False
            reason = "--only 未选择"
        if spec.name in skip:
            enabled = False
            reason = "--skip 跳过"
        if not PIPELINE_SWITCH:
            enabled = False
            reason = "pipeline 总开关关闭"
        rows.append((spec, enabled, reason))
    return rows


def load_child_report(report_path: Path) -> dict[str, Any] | None:
    if not report_path.exists():
        return None
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def int_from_summary(summary: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = summary.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def parse_failed_count(child_report: dict[str, Any] | None) -> int | None:
    if not child_report:
        return None
    summary = child_report.get("summary")
    if not isinstance(summary, dict):
        return None
    if child_report.get("report_type") == "check_no_chinese_report":
        issues = int_from_summary(summary, "issues", "tasks")
        if issues is not None:
            return issues
    failed = int_from_summary(summary, "failed", "failed_count")
    if failed is not None:
        return failed
    issues = int_from_summary(summary, "issues", "tasks")
    if issues is not None:
        return issues
    tasks = child_report.get("tasks")
    if isinstance(tasks, list):
        return len(tasks)
    return None


def task_text(value: Any, *, limit: int = 180) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def child_tasks_from_report(child_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not child_report:
        return []
    tasks = child_report.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def is_ignored_basic_acceptance_task(task: dict[str, Any]) -> bool:
    reason = task.get("原因")
    if not isinstance(reason, str):
        return False
    normalized_reason = " ".join(reason.split())
    return IGNORED_BASIC_ACCEPTANCE_TURNS_REASON_RE.fullmatch(normalized_reason) is not None


def failed_file_keys_from_tasks(tasks: list[dict[str, Any]]) -> set[tuple[str, str]]:
    failed_files: set[tuple[str, str]] = set()
    for task in tasks:
        failed_files.add(
            (
                task_text(task.get("产物id")),
                task_text(task.get("文件id")),
            )
        )
    return failed_files


def normalize_child_report_for_pipeline(spec_name: str, child_report: dict[str, Any] | None) -> dict[str, Any] | None:
    if (
        spec_name != "basic_acceptance"
        or not child_report
        or child_report.get("report_type") != "check_basic_acceptance_report"
    ):
        return child_report

    original_tasks = child_tasks_from_report(child_report)
    if not original_tasks:
        return child_report

    filtered_tasks = [
        task for task in original_tasks
        if not is_ignored_basic_acceptance_task(task)
    ]
    if len(filtered_tasks) == len(original_tasks):
        return child_report

    normalized_report = dict(child_report)
    normalized_report["tasks"] = filtered_tasks

    summary = normalized_report.get("summary")
    if isinstance(summary, dict):
        normalized_summary = dict(summary)
        total_files = int_from_summary(normalized_summary, "files")
        failed_files = len(failed_file_keys_from_tasks(filtered_tasks))
        normalized_summary["failed"] = failed_files
        normalized_summary["failed_count"] = failed_files
        normalized_summary["issues"] = len(filtered_tasks)
        normalized_summary["turns_failed"] = 0
        if total_files is not None:
            normalized_summary["passed"] = max(0, total_files - failed_files)
        normalized_report["summary"] = normalized_summary

    return normalized_report


def issue_locations_from_tasks(title: str, tasks: list[dict[str, Any]]) -> list[IssueLocation]:
    issues: list[IssueLocation] = []
    for task in tasks:
        issues.append(
            IssueLocation(
                check=title,
                project=task_text(task.get("产物id")),
                session=task_text(task.get("文件id")),
                line=task_text(task.get("行号")),
                preview=task_text(task.get("content 原文")),
                reason=task_text(task.get("原因")),
            )
        )
    return issues


def preview_limit_for_result(item: PipelineResult) -> int | None:
    if item.name == "tool_definitions":
        return TOOL_DEFINITIONS_PREVIEW_LIMIT
    return None


def preview_items(items: list[Any], limit: int | None) -> list[Any]:
    if limit is None:
        return list(items)
    return items[: max(0, limit)]


def compact_child_report(
    child_report: dict[str, Any] | None,
    limit: int | None,
    *,
    include_tasks: bool = True,
) -> dict[str, Any] | None:
    if not child_report:
        return None
    compact = dict(child_report)
    tasks = child_report.get("tasks")
    if isinstance(tasks, list) and not include_tasks:
        compact["tasks_total"] = len(tasks)
        compact["tasks_preview_limit"] = 0
        compact["tasks_omitted"] = len(tasks)
        compact["tasks"] = []
    elif isinstance(tasks, list) and limit is not None:
        compact["tasks_total"] = len(tasks)
        compact["tasks_preview_limit"] = limit
        compact["tasks_omitted"] = max(0, len(tasks) - limit)
        compact["tasks"] = preview_items(tasks, limit)
    return compact


def write_log(log_path: Path, stdout_text: str, stderr_text: str) -> None:
    parts = [
        "# stdout",
        stdout_text.rstrip() or "(empty)",
        "",
        "# stderr",
        stderr_text.rstrip() or "(empty)",
        "",
    ]
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(parts), encoding="utf-8")
    except OSError:
        return


def cleanup_child_reports(report_path: Path, output_dir: Path) -> tuple[list[str], list[str]]:
    output_root = output_dir.expanduser().resolve()
    candidates = [report_path]

    deleted: list[str] = []
    errors: list[str] = []
    for candidate in candidates:
        candidate_path = candidate.expanduser().resolve()
        if candidate_path.name == JSON_REPORT_NAME:
            continue
        try:
            candidate_path.relative_to(output_root)
        except ValueError:
            errors.append(f"跳过非报告目录文件: {candidate_path}")
            continue
        if not candidate_path.exists():
            continue
        try:
            candidate_path.unlink()
            deleted.append(str(candidate_path))
        except OSError as exc:
            errors.append(f"删除失败 {candidate_path}: {exc}")
    return deleted, errors


def cleanup_legacy_pipeline_logs(output_dir: Path) -> None:
    legacy_log_dir = output_dir.expanduser().resolve() / "pipeline_logs"
    if legacy_log_dir.exists() and legacy_log_dir.is_dir():
        shutil.rmtree(legacy_log_dir)


def should_externalize_child_report(check_name: str) -> bool:
    return check_name in EXTERNAL_CHILD_REPORT_NAMES


def run_one(
    spec: CheckSpec,
    *,
    find_path: Path,
    session_json_path: Path,
    output_dir: Path,
    log_dir: Path,
) -> PipelineResult:
    start = time.perf_counter()
    log_path = log_dir / f"{spec.name}.log"
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            report_path = Path(spec.func(find_path, session_json_path, output_dir)).expanduser().resolve()
        duration = time.perf_counter() - start
        write_log(log_path, stdout_buffer.getvalue(), stderr_buffer.getvalue())
        child_report = load_child_report(report_path)
        child_report = normalize_child_report_for_pipeline(spec.name, child_report)
        child_summary = child_report.get("summary", {}) if child_report else {}
        if not isinstance(child_summary, dict):
            child_summary = {}
        child_tasks = child_tasks_from_report(child_report)
        failed_count = parse_failed_count(child_report)
        issue_locations = issue_locations_from_tasks(spec.title, child_tasks)
        report_path_text = str(report_path)
        cleanup_errors: list[str] = []
        external_report = should_externalize_child_report(spec.name)
        if external_report and child_report is None and not report_path.exists():
            failed_count = 0
            report_path_text = ""
        if failed_count is None:
            status = "UNKNOWN"
            message = "已执行，未能从 JSON 报告中解析失败数。"
        elif failed_count == 0:
            status = "PASS"
            if external_report and not report_path_text:
                message = "已执行，未发现问题，未生成独立报告。"
            else:
                message = "已执行，失败数为 0。"
        else:
            status = "FAIL"
            message = f"已执行，失败数为 {failed_count}。"
        if external_report and report_path_text:
            message += " 原始报告已独立保留。"
        if not KEEP_CHILD_REPORTS and not external_report:
            deleted, cleanup_errors = cleanup_child_reports(report_path, output_dir)
            if deleted:
                report_path_text = ""
        if cleanup_errors:
            message += " " + "；".join(cleanup_errors)
        return PipelineResult(
            name=spec.name,
            script=spec.script,
            title=spec.title,
            enabled=True,
            status=status,
            report_path=report_path_text,
            log_path=str(log_path.resolve()),
            failed_count=failed_count,
            duration_seconds=duration,
            message=message,
            issue_locations=[] if external_report else issue_locations,
            child_report_type=task_text(child_report.get("report_type")) if child_report else "",
            child_summary=child_summary,
            child_tasks=[] if external_report else child_tasks,
            child_report=None if external_report else child_report,
        )
    except Exception as exc:
        duration = time.perf_counter() - start
        error_text = stderr_buffer.getvalue()
        error_text += "\n" + traceback.format_exc()
        write_log(log_path, stdout_buffer.getvalue(), error_text)
        return PipelineResult(
            name=spec.name,
            script=spec.script,
            title=spec.title,
            enabled=True,
            status="ERROR",
            log_path=str(log_path.resolve()),
            duration_seconds=duration,
            message=f"{type(exc).__name__}: {exc}",
        )


def visible_child_tasks(item: PipelineResult) -> list[dict[str, Any]]:
    return preview_items(item.child_tasks, preview_limit_for_result(item))


def visible_issue_locations(item: PipelineResult) -> list[IssueLocation]:
    return preview_items(item.issue_locations, preview_limit_for_result(item))


def flattened_tasks(results: list[PipelineResult], *, apply_preview: bool = False) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for item in results:
        source_tasks = visible_child_tasks(item) if apply_preview else item.child_tasks
        for task in source_tasks:
            merged = {
                "检查项id": item.name,
                "检查项": item.title,
                "脚本": item.script,
                "报告类型": item.child_report_type,
            }
            merged.update(task)
            tasks.append(merged)
    return tasks


def build_result_record(item: PipelineResult) -> dict[str, Any]:
    task_limit = preview_limit_for_result(item)
    shown_tasks = visible_child_tasks(item)
    shown_issues = visible_issue_locations(item)
    return {
        "name": item.name,
        "script": item.script,
        "title": item.title,
        "enabled": item.enabled,
        "status": item.status,
        "report_path": item.report_path,
        "log_path": item.log_path,
        "failed_count": item.failed_count,
        "duration_seconds": round(item.duration_seconds, 3),
        "message": item.message,
        "report_type": item.child_report_type,
        "summary": item.child_summary,
        "tasks_total": len(item.child_tasks),
        "tasks_preview_limit": task_limit,
        "tasks_omitted": max(0, len(item.child_tasks) - len(shown_tasks)),
        "tasks": shown_tasks,
        "issue_locations_total": len(item.issue_locations),
        "issue_locations_omitted": max(0, len(item.issue_locations) - len(shown_issues)),
        "issue_locations": [
            {
                "check": issue.check,
                "project": issue.project,
                "session": issue.session,
                "line": issue.line,
                "preview": issue.preview,
                "reason": issue.reason,
            }
            for issue in shown_issues
        ],
        "report": compact_child_report(item.child_report, task_limit),
    }


def write_json_report(
    path: Path,
    *,
    find_path: Path,
    session_json_path: Path,
    output_dir: Path,
    results: list[PipelineResult],
) -> None:
    total = len(results)
    passed = sum(1 for item in results if item.status == "PASS")
    failed = sum(1 for item in results if item.status in {"FAIL", "ERROR", "UNKNOWN"})
    skipped = sum(1 for item in results if item.status == "SKIPPED")
    all_tasks = flattened_tasks(results)
    called_scripts = [f"{item.script}/{item.title}" for item in results if item.enabled]
    non_pass_results = [
        item
        for item in results
        if (item.status != "PASS" or item.name == "basic_acceptance")
        and not should_externalize_child_report(item.name)
    ]
    payload = {
        "report_type": "check_pipeline_report",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": str(find_path),
        "session_path": str(session_json_path),
        "output_dir": str(output_dir),
        "summary": {
            "checks": total,
            "called_scripts": len(called_scripts),
            "called_scripts_list": "；".join(called_scripts),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "tasks": len(all_tasks),
            "sha256_duplicate_check_mode": SHA256_DUPLICATE_CHECK_MODE,
            "sha256_enabled": sha256_duplicate_check_enabled(),
            "sha256_within_project_duplicate_enabled": sha256_within_project_duplicate_enabled(),
            "tool_definitions_task_preview_limit": TOOL_DEFINITIONS_PREVIEW_LIMIT,
            "child_failed_total": sum(item.failed_count or 0 for item in results),
            "pass_results_omitted": passed,
        },
        "results": [
            build_result_record(item)
            for item in non_pass_results
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_pipeline_once(
    root_dir: Path,
    session_path: Path,
    report_output_dir: Path,
    *,
    only: set[str] | None = None,
    skip: set[str] | None = None,
    sha256_mode: str | bool | None = None,
) -> Path:
    if sha256_mode is not None:
        set_sha256_duplicate_check_mode(sha256_mode)

    resolved_session_path = resolve_session_path_arg(root_dir, session_path)
    if resolved_session_path != session_path:
        print(
            f"[pipeline] session path resolved: {session_path.as_posix()} -> {resolved_session_path.as_posix()}",
            flush=True,
        )
        session_path = resolved_session_path

    report_output_dir.mkdir(parents=True, exist_ok=True)
    cleanup_legacy_pipeline_logs(report_output_dir)

    only = only or set()
    skip = skip or set()

    results: list[PipelineResult] = []
    log_dir = report_output_dir / "pipeline_logs"
    if log_dir.exists():
        shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        for spec, enabled, reason in selected_specs(only, skip):
            if not enabled:
                print(f"[pipeline] {spec.script} 已跳过：{reason}", flush=True)
                results.append(
                    PipelineResult(
                        name=spec.name,
                        script=spec.script,
                        title=spec.title,
                        enabled=False,
                        status="SKIPPED",
                        message=reason,
                    )
                )
                continue
            print(f"[pipeline] 开始 {spec.script}：{spec.title}", flush=True)
            result = run_one(
                spec,
                find_path=root_dir,
                session_json_path=session_path,
                output_dir=report_output_dir,
                log_dir=log_dir,
            )
            result.log_path = ""
            failed_text = "未知" if result.failed_count is None else str(result.failed_count)
            print(
                f"[pipeline] {spec.script} 已完成：{result.status}，失败数={failed_text}，耗时={result.duration_seconds:.2f}s",
                flush=True,
            )
            results.append(result)
            if spec.name == "sessions_folder" and result.status != "PASS":
                print(
                    "[pipeline] sessions 目录检查未通过，当前项目跳过后续校验。",
                    flush=True,
                )
                break
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)

    json_path = report_output_dir / JSON_REPORT_NAME
    write_json_report(
        json_path,
        find_path=root_dir,
        session_json_path=session_path,
        output_dir=report_output_dir,
        results=results,
    )
    cleanup_legacy_pipeline_logs(report_output_dir)
    print(f"\npipeline json: {json_path}")
    return json_path


def load_project_payload(project_json_path: Path) -> dict[str, Any]:
    try:
        project_payload = json.loads(project_json_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return project_payload if isinstance(project_payload, dict) else {}


def run_project_pipeline(
    *,
    index: int,
    project_count: int,
    project_dir: Path,
    root_dir: Path,
    report_output_dir: Path,
    session_path: Path,
    only: set[str] | None,
    skip: set[str] | None,
    sha256_mode: str | bool,
) -> tuple[int, dict[str, Any]]:
    set_sha256_duplicate_check_mode(sha256_mode)
    report_dir = project_report_dir(project_dir, root_dir, report_output_dir, project_count)
    print(f"\n[pipeline] 项目 {index}/{project_count} 开始：{project_dir.name}", flush=True)
    project_json_path = run_pipeline_once(
        project_dir,
        session_path,
        report_dir,
        only=only,
        skip=skip,
        sha256_mode=sha256_mode,
    )
    project_payload = load_project_payload(project_json_path)
    project_summary = project_payload.get("summary", {}) if isinstance(project_payload, dict) else {}
    project_report = {
        "project_id": project_dir.name,
        "source_root": str(project_dir),
        "report_path": str(project_json_path),
        "summary": project_summary if isinstance(project_summary, dict) else {},
        "report": project_payload,
    }
    print(f"[pipeline] 项目 {project_dir.name} 已完成，报告：{project_json_path}", flush=True)
    return index, project_report


def has_session_target(project_dir: Path, session_path: Path) -> bool:
    target = project_dir / session_path
    return target.exists() or any(candidate.exists() for candidate in typed_session_target_candidates(target))


def is_typed_session_stem(stem: str) -> bool:
    value = stem[len("ERROR-"):] if stem.startswith("ERROR-") else stem
    parts = value.split("-", 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[1] in SESSION_TYPE_LABELS:
        return True
    prefix, separator, _rest = value.partition("-")
    return bool(separator and prefix in SESSION_TYPE_LABELS)


def typed_session_target_candidates(target: Path) -> list[Path]:
    suffix = target.suffix
    if suffix and suffix.lower() != ".json":
        return []

    stem = target.stem if suffix else target.name
    if not stem or is_typed_session_stem(stem):
        return []

    prefix = "ERROR-" if stem.startswith("ERROR-") else ""
    value = stem[len(prefix):] if prefix else stem
    head, separator, rest = value.partition("-")
    if separator and head.isdigit() and rest:
        typed_stems = [f"{prefix}{head}-{label}-{rest}" for label in SESSION_TYPE_LABELS]
    else:
        typed_stems = [f"{prefix}{label}-{value}" for label in SESSION_TYPE_LABELS]

    if suffix:
        return [target.with_name(f"{typed_stem}{suffix}") for typed_stem in typed_stems]
    return [target.with_name(typed_stem) for typed_stem in typed_stems]


def resolve_session_path_arg(project_dir: Path, session_path: Path) -> Path:
    target = project_dir / session_path
    if target.exists():
        return session_path

    existing = [candidate for candidate in typed_session_target_candidates(target) if candidate.exists()]
    if len(existing) != 1:
        return session_path
    try:
        return existing[0].relative_to(project_dir)
    except ValueError:
        return existing[0]


def discover_project_dirs(
    root_dir: Path,
    session_path: Path,
    *,
    include_missing_session_targets: bool = False,
) -> list[Path]:
    if has_session_target(root_dir, session_path):
        return [root_dir]
    project_dirs = sorted(
        (path for path in root_dir.iterdir() if path.is_dir() and path.name.startswith("TASK-")),
        key=lambda path: path.name,
    )
    if include_missing_session_targets:
        return project_dirs
    return [path for path in project_dirs if has_session_target(path, session_path)]


def will_run_sessions_folder_check(only: set[str] | None, skip: set[str] | None) -> bool:
    only = only or set()
    skip = skip or set()
    if not PIPELINE_SWITCH or not SCRIPT_SWITCHES.get("sessions_folder", False):
        return False
    if "sessions_folder" in skip:
        return False
    return not only or "sessions_folder" in only


def project_report_dir(project_dir: Path, root_dir: Path, output_dir: Path, project_count: int) -> Path:
    if output_dir == root_dir or output_dir == project_dir:
        return project_dir / "report"
    if project_count > 1:
        return output_dir / project_dir.name / "report"
    return output_dir


def sha256_records_from_basic_acceptance_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    child_report = result.get("report")
    if isinstance(child_report, dict):
        records = child_report.get("sha256_records")
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]

    summary = result.get("summary")
    if isinstance(summary, dict):
        records = summary.get("sha256_records")
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]

    return []


def cross_project_sha256_check(project_reports: list[dict[str, Any]]) -> dict[str, Any]:
    """跨项目 SHA-256 重复检测。只报告项目之间的重复，项目内部重复忽略。"""
    from collections import defaultdict

    sha256_map: dict[str, list[dict[str, str]]] = defaultdict(list)

    for project in project_reports:
        project_id = project.get("project_id", "")
        pipeline_report = project.get("report", {})
        for result in pipeline_report.get("results", []):
            if result.get("name") != "basic_acceptance":
                continue
            records = sha256_records_from_basic_acceptance_result(result)
            for record in records:
                sha = record.get("sha256", "")
                if sha:
                    record_project_id = str(record.get("project_dir") or project_id)
                    sha256_map[sha].append(
                        {
                            "project_id": record_project_id,
                            "session_path": str(record.get("session_path", "")),
                        }
                    )

    duplicate_groups: list[dict[str, Any]] = []
    for sha, sessions in sha256_map.items():
        projects = {s["project_id"] for s in sessions}
        if len(projects) > 1:
            duplicate_groups.append(
                {
                    "sha256": sha,
                    "sessions": sessions,
                }
            )

    return {
        "enabled": True,
        "mode": SHA256_DUPLICATE_CHECK_MODE,
        "duplicate_groups": len(duplicate_groups),
        "records": duplicate_groups,
    }


def disabled_cross_project_sha256_result() -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": SHA256_DUPLICATE_CHECK_MODE,
        "duplicate_groups": 0,
        "records": [],
    }


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def unique_price_error_destination(dest_root: Path, project_id: str) -> Path | None:
    destination = dest_root / project_id
    if not destination.exists():
        return destination
    for index in range(1, 10000):
        candidate = dest_root / f"{project_id}__price_{index}"
        if not candidate.exists():
            return candidate
    return None


def project_failed_calculate_price(project_report: dict[str, Any]) -> bool:
    pipeline_report = project_report.get("report")
    if not isinstance(pipeline_report, dict):
        return False
    for result in pipeline_report.get("results", []):
        if not isinstance(result, dict) or result.get("name") != "calculate_price":
            continue
        return result.get("status") == "FAIL"
    return False


def move_price_error_tasks(
    root_dir: Path,
    output_dir: Path,
    project_reports: list[dict[str, Any]],
    *,
    apply: bool = True,
) -> dict[str, Any]:
    dest_root = (root_dir / ERROR_TASK_DIR_NAME / PRICE_ERROR_DEST_SUBDIR).resolve()
    records: list[dict[str, Any]] = []

    for project in project_reports:
        project_id = str(project.get("project_id") or "")
        if not project_id or not project_failed_calculate_price(project):
            continue

        source_text = str(project.get("source_root") or root_dir / project_id)
        source_dir = Path(source_text).expanduser().resolve()
        destination = unique_price_error_destination(dest_root, project_id)
        status = "planned" if apply else "would_move"
        error = ""

        if destination is None:
            status = "error"
            error = "could not find a free destination suffix"
        elif not source_dir.exists():
            status = "missing_source"
            error = "source directory does not exist"
        elif not source_dir.is_dir():
            status = "error"
            error = "source path is not a directory"
        elif not path_is_relative_to(source_dir, root_dir):
            status = "error"
            error = "source directory is outside pipeline root"
        elif path_is_relative_to(dest_root, source_dir):
            status = "error"
            error = "destination root is inside the source directory"
        elif source_dir == destination.resolve():
            status = "error"
            error = "source and destination are the same directory"
        elif apply:
            try:
                dest_root.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_dir), str(destination))
                status = "moved"
            except OSError as exc:
                status = "error"
                error = str(exc)

        records.append(
            {
                "project_id": project_id,
                "source_dir": str(source_dir),
                "destination_dir": str(destination) if destination else "",
                "status": status,
                "error": error,
                "apply": apply,
            }
        )

    summary = {
        "targets": len(records),
        "moved": sum(1 for item in records if item["status"] == "moved"),
        "would_move": sum(1 for item in records if item["status"] == "would_move"),
        "missing_source": sum(1 for item in records if item["status"] == "missing_source"),
        "errors": sum(1 for item in records if item["status"] == "error"),
    }
    report = {
        "report_type": "move_calculate_price_errors_report",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root_dir),
        "destination_root": str(dest_root),
        "apply": apply,
        "summary": summary,
        "results": records,
        "tasks": [
            {
                "检查项id": "calculate_price",
                "产物id": item["project_id"],
                "文件id": item["source_dir"],
                "行号": 0,
                "原因": "calculate_price failed",
                "content 原文": item["destination_dir"],
                "操作结果": item["status"],
                "error": item["error"],
            }
            for item in records
        ],
    }
    report_path = output_dir / PRICE_ERROR_MOVE_REPORT_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def write_parent_json_report(
    path: Path,
    *,
    root_dir: Path,
    session_path: Path,
    output_dir: Path,
    project_reports: list[dict[str, Any]],
    cross_project_sha256: dict[str, Any] | None = None,
    price_error_move: dict[str, Any] | None = None,
) -> None:
    checks = sum(int(item.get("summary", {}).get("checks", 0)) for item in project_reports)
    passed = sum(int(item.get("summary", {}).get("passed", 0)) for item in project_reports)
    failed = sum(int(item.get("summary", {}).get("failed", 0)) for item in project_reports)
    skipped = sum(int(item.get("summary", {}).get("skipped", 0)) for item in project_reports)
    tasks = sum(int(item.get("summary", {}).get("tasks", 0)) for item in project_reports)
    cross_sha256 = cross_project_sha256 or {}
    price_move = price_error_move or {}
    price_move_summary = price_move.get("summary", {}) if isinstance(price_move.get("summary"), dict) else {}
    payload: dict[str, Any] = {
        "report_type": "check_pipeline_parent_report",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": str(root_dir),
        "session_path": str(session_path),
        "output_dir": str(output_dir),
        "summary": {
            "projects": len(project_reports),
            "checks": checks,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "tasks": tasks,
            "sha256_duplicate_check_mode": SHA256_DUPLICATE_CHECK_MODE,
            "sha256_enabled": sha256_duplicate_check_enabled(),
            "sha256_within_project_duplicate_enabled": sha256_within_project_duplicate_enabled(),
            "cross_project_sha256_enabled": bool(cross_sha256.get("enabled")),
            "cross_project_sha256_groups": cross_sha256.get("duplicate_groups", 0),
            "price_error_move_enabled": bool(price_move.get("enabled")),
            "price_error_move_targets": price_move_summary.get("targets", 0),
            "price_error_moved": price_move_summary.get("moved", 0),
            "price_error_move_errors": price_move_summary.get("errors", 0),
        },
        "cross_project_sha256": cross_sha256,
        "price_error_move": price_move,
        "projects": project_reports,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def check_pipeline(
    find_path: Path | str,
    session_json_path: Path | str = "sessions",
    output_dir: Path | str | None = None,
    *,
    only: set[str] | None = None,
    skip: set[str] | None = None,
    workers: int = 1,
    sha256_mode: str | bool | None = None,
    move_price_errors: bool = True,
) -> Path:
    if sha256_mode is not None:
        set_sha256_duplicate_check_mode(sha256_mode)
    else:
        set_sha256_duplicate_check_mode(SHA256_DUPLICATE_CHECK_MODE)

    root_dir = Path(find_path).expanduser().resolve()
    session_path = Path(session_json_path or "sessions")
    report_output_dir = Path(output_dir).expanduser().resolve() if output_dir is not None else root_dir
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"find path does not exist or is not a directory: {root_dir}")
    if workers < 1:
        raise ValueError("workers must be >= 1")

    project_dirs = discover_project_dirs(
        root_dir,
        session_path,
        include_missing_session_targets=will_run_sessions_folder_check(only, skip),
    )
    if not project_dirs:
        raise FileNotFoundError(f"no project sessions found under {root_dir} with path {session_path}")

    project_count = len(project_dirs)
    cpu_count = os.cpu_count() or 1
    if workers > cpu_count:
        print(f"[pipeline] workers({workers}) 超过 CPU 核心数({cpu_count})，自动限制为 {cpu_count}", flush=True)
        workers = cpu_count
    start_time = time.perf_counter()

    if len(project_dirs) == 1 and project_dirs[0] == root_dir and report_output_dir != root_dir:
        return run_pipeline_once(
            root_dir,
            session_path,
            report_output_dir,
            only=only,
            skip=skip,
            sha256_mode=SHA256_DUPLICATE_CHECK_MODE,
        )
        elapsed = time.perf_counter() - start_time
        print(f"[pipeline] 共完成 {project_count} 个项目，总耗时 {elapsed:.2f}s")

    project_reports_by_index: dict[int, dict[str, Any]] = {}
    worker_count = min(workers, project_count)
    if worker_count == 1:
        for index, project_dir in enumerate(project_dirs, start=1):
            result_index, project_report = run_project_pipeline(
                index=index,
                project_count=project_count,
                project_dir=project_dir,
                root_dir=root_dir,
                report_output_dir=report_output_dir,
                session_path=session_path,
                only=only,
                skip=skip,
                sha256_mode=SHA256_DUPLICATE_CHECK_MODE,
            )
            project_reports_by_index[result_index] = project_report
    else:
        print(f"[pipeline] 使用项目级多进程并发：workers={worker_count}", flush=True)
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    run_project_pipeline,
                    index=index,
                    project_count=project_count,
                    project_dir=project_dir,
                    root_dir=root_dir,
                    report_output_dir=report_output_dir,
                    session_path=session_path,
                    only=only,
                    skip=skip,
                    sha256_mode=SHA256_DUPLICATE_CHECK_MODE,
                )
                for index, project_dir in enumerate(project_dirs, start=1)
            ]
            for future in as_completed(futures):
                result_index, project_report = future.result()
                project_reports_by_index[result_index] = project_report

    project_reports = [
        project_reports_by_index[index]
        for index in sorted(project_reports_by_index)
    ]

    if sha256_cross_project_duplicate_enabled():
        cross_sha256 = cross_project_sha256_check(project_reports)
        if cross_sha256.get("duplicate_groups"):
            print(
                f"[pipeline] 发现跨项目 SHA-256 重复组: {cross_sha256['duplicate_groups']} 组",
                flush=True,
            )
    else:
        cross_sha256 = disabled_cross_project_sha256_result()
        if SHA256_DUPLICATE_CHECK_MODE is False:
            print("[pipeline] SHA-256 模式为 False，跳过跨项目重复检查。", flush=True)
        else:
            print("[pipeline] SHA-256 模式为 within_task_only，跳过跨项目重复检查。", flush=True)

    if PRICE_ERROR_MOVE_SWITCH and move_price_errors:
        price_error_move = move_price_error_tasks(
            root_dir,
            report_output_dir,
            project_reports,
            apply=True,
        )
        price_move_summary = price_error_move.get("summary", {})
        print(
            "[pipeline] calculate_price 异常 TASK 移动："
            f"targets={price_move_summary.get('targets', 0)} "
            f"moved={price_move_summary.get('moved', 0)} "
            f"errors={price_move_summary.get('errors', 0)}",
            flush=True,
        )
    else:
        price_error_move = {
            "enabled": False,
            "summary": {"targets": 0, "moved": 0, "would_move": 0, "missing_source": 0, "errors": 0},
        }

    parent_json_path = report_output_dir / PARENT_JSON_REPORT_NAME
    write_parent_json_report(
        parent_json_path,
        root_dir=root_dir,
        session_path=session_path,
        output_dir=report_output_dir,
        project_reports=project_reports,
        cross_project_sha256=cross_sha256,
        price_error_move=price_error_move,
    )

    elapsed = time.perf_counter() - start_time
    print("\npipeline projects completed.")
    print(f"pipeline parent json: {parent_json_path}")
    print(f"[pipeline] 共完成 {project_count} 个项目，总耗时 {elapsed:.2f}s")
    if project_reports:
        return parent_json_path
    return report_output_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="选择性运行 check 下的全部校验脚本。")
    parser.add_argument("-r", dest="find_path", type=Path, required=False, help="执行路径。")
    parser.add_argument("-path", dest="session_json_path", type=Path, default=Path("sessions"), help="session 文件或目录相对路径，默认 sessions。")
    parser.add_argument("-o", dest="output_dir", type=Path, required=False, help="报告输出目录。省略时默认使用 -r。")
    parser.add_argument("--only", default="", help="只运行指定脚本，多个名称用英文逗号分隔。")
    parser.add_argument("--skip", default="", help="跳过指定脚本，多个名称用英文逗号分隔。")
    parser.add_argument("--workers", type=int, default=1, help="多项目时的项目级多进程并发数，默认 1。")
    parser.add_argument(
        "--sha256-mode",
        choices=SHA256_CLI_MODE_CHOICES,
        default=None,
        help=(
            "SHA-256 重复检查模式：within_task_only 仅检查单个 TASK 内部 session 重复；"
            "cross_task_only 仅检查 TASK 与 TASK 之间重复；off/false 完全关闭 SHA-256。"
            "省略时使用 check_basic_acceptance.py 中的默认值。"
        ),
    )
    parser.add_argument("--no-price-error-move", action="store_true", help="只记录 calculate_price 异常，不移动 TASK。")
    parser.add_argument("--list", action="store_true", help="列出可用脚本名称后退出。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        for spec in CHECK_SPECS:
            state = "on" if SCRIPT_SWITCHES.get(spec.name, False) else "off"
            print(f"{spec.name}\t{state}\t{spec.script}\t{spec.title}")
        return 0

    missing = [name for name in ("find_path",) if getattr(args, name) is None]
    if missing:
        raise SystemExit(f"缺少必要参数: {', '.join(missing)}。可先使用 --list 查看脚本名称。")

    only = parse_name_set(args.only)
    skip = parse_name_set(args.skip)
    if args.workers < 1:
        raise SystemExit("--workers 必须大于等于 1。")
    check_pipeline(
        args.find_path,
        args.session_json_path,
        args.output_dir,
        only=only,
        skip=skip,
        workers=args.workers,
        sha256_mode=args.sha256_mode,
        move_price_errors=not args.no_price_error_move,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

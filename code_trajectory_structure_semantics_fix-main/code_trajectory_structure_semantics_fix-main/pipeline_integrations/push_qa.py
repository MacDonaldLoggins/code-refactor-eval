#!/usr/bin/env python3
"""Push trajectory-repair tasks to the platform as QA-complete.

The request is sent to ``/api/traj-repair/api/qa/update-status`` with the
following payload::

    {
        "qa_user_id": "...",
        "task_ids": ["TASK-..."],
        "qa_status": "质检完成"
    }

The script can consume the normal pipeline context or be used directly with
``--user-id`` and one or more ``--task`` arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

if __package__:
    from . import platform_qa
    from .common import PROJECT_ROOT, context_task_ids, load_context, load_env_file, write_json_atomic
else:
    import platform_qa  # type: ignore[no-redef]
    from common import (  # type: ignore[no-redef]
        PROJECT_ROOT,
        context_task_ids,
        load_context,
        load_env_file,
        write_json_atomic,
    )


UPDATE_STATUS_PATH = "/api/traj-repair/api/qa/update-status"
QA_STATUS = "质检完成"
DEFAULT_OPERATOR_FILE = PROJECT_ROOT / "operator.json"


def task_ids_from_args(values: list[str]) -> list[str]:
    """Normalize repeated task arguments while preserving their order."""
    context: dict[str, Any] = {
        "task_ids": [item for value in values for item in value.split(",")]
    }
    return context_task_ids(context)


def load_operator_id(path: Path) -> str:
    """Read the QA user ID from the project's operator file."""
    operator_path = path.expanduser().resolve()
    with operator_path.open("r", encoding="utf-8") as handle:
        operator = json.load(handle)
    if not isinstance(operator, dict):
        raise ValueError(f"operator.json must be a JSON object: {operator_path}")
    operator_id = str(operator.get("operator_id") or "").strip()
    if not operator_id:
        raise ValueError(f"operator.json missing required operator_id: {operator_path}")
    return operator_id


def build_context(args: argparse.Namespace) -> dict[str, Any]:
    """Load pipeline context and apply any explicit CLI overrides."""
    if args.context or os.environ.get("PIPELINE_CONTEXT_PATH"):
        _context_path, context = load_context(args.context)
    else:
        context = {}

    if args.user_id:
        context["user_id"] = args.user_id
    elif not context.get("user_id") and os.environ.get("PIPELINE_USER_ID"):
        context["user_id"] = os.environ["PIPELINE_USER_ID"]
    elif not context.get("user_id"):
        context["user_id"] = load_operator_id(args.operator_file)

    if args.task_ids:
        context["task_ids"] = task_ids_from_args(args.task_ids)
    return context


def push_qa_status(
    context: dict[str, Any],
    config: dict[str, str],
    timeout: int,
    retries: int,
    sender: Callable[[str, dict[str, str], dict[str, Any], int, int], dict[str, Any]] = platform_qa.post_json,
) -> tuple[dict[str, Any], int]:
    """Update all task IDs in ``context`` to the fixed QA status."""
    # Reuse the tested platform adapter for authentication, validation,
    # retries, response handling, and partial-failure reporting.
    if UPDATE_STATUS_PATH != platform_qa.UPDATE_PATH:
        raise RuntimeError("platform QA update path is out of sync")
    result, returncode = platform_qa.run_operation(
        "mark-complete",
        context,
        config,
        timeout,
        retries,
        sender=sender,
    )
    if result.get("qa_status") != QA_STATUS:
        raise RuntimeError(f"unexpected QA status in platform result: {result.get('qa_status')}")
    return result, returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context",
        type=Path,
        help="流水线上下文 JSON；未传入时读取 PIPELINE_CONTEXT_PATH",
    )
    parser.add_argument(
        "--user-id",
        help="QA 平台 qa_user_id；未传入时读取上下文、PIPELINE_USER_ID 或 operator.json",
    )
    parser.add_argument(
        "--operator-file",
        type=Path,
        default=DEFAULT_OPERATOR_FILE,
        help="读取 operator_id 的文件，默认使用项目根目录 operator.json",
    )
    parser.add_argument(
        "--task",
        "--task-id",
        dest="task_ids",
        action="append",
        default=[],
        help="TASK ID，可重复传入，也支持逗号分隔",
    )
    parser.add_argument("--result", type=Path, help="可选的结果 JSON 输出路径")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = build_context(args)
        config = load_env_file(args.env_file.expanduser().resolve())
        if not config.get("TRAJ_REPAIR_BASE_URL"):
            raise RuntimeError("missing required configuration: TRAJ_REPAIR_BASE_URL")
        platform_qa.platform_api_key(config)

        result, returncode = push_qa_status(
            context,
            config,
            max(1, args.timeout),
            max(0, args.retries),
        )

        result_path = args.result
        if result_path is None and os.environ.get("PIPELINE_HOOK_RESULT_PATH"):
            result_path = Path(os.environ["PIPELINE_HOOK_RESULT_PATH"])
        if result_path is not None:
            write_json_atomic(result_path.expanduser().resolve(), result)

        print(json.dumps({key: value for key, value in result.items() if key != "failed"}, ensure_ascii=False))
        if returncode:
            print(f"QA status update had failed items: {result.get('failed')}", file=sys.stderr)
        return returncode
    except Exception as exc:
        print(f"push QA failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

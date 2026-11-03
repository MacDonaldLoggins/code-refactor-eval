#!/usr/bin/env python3
"""Initialize task_structure_semantics rows from QA-platform claim results.

The online path reuses ``pipeline_integrations.platform_qa`` for the claim
request, captures the request metadata and raw response, and then upserts the
claimed TASKs into the MySQL ledger.  ``--response-file`` is provided for
replaying a saved platform response such as ``DB/init.json`` without making a
second claim request.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline_integrations import platform_qa
from pipeline_integrations.common import load_env_file, write_json_atomic
from pipeline_integrations.task_structure_semantics_db import (
    TaskStructureSemanticsDatabase,
    load_task_database_settings,
    now_string,
)


DEFAULT_CONFIG = REPO_ROOT / "full_pipeline_config.toml"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_REPORT = REPO_ROOT / "DB" / "task_structure_semantics_init_report.json"
CLAIM_SUB_STATUS = "claim_tasks"
CLAIM_SUB_STATUS_NAME = "从 QA 平台认领任务"


def load_toml(path: Path) -> dict[str, Any]:
    with path.expanduser().resolve().open("rb") as handle:
        value = tomllib.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"config must be a TOML table: {path}")
    return value


def task_ids_from_args(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        task_id = str(raw).strip()
        if not task_id:
            continue
        if task_id in seen:
            continue
        seen.add(task_id)
        result.append(task_id)
    return result


def build_context(args: argparse.Namespace) -> dict[str, Any]:
    task_ids = task_ids_from_args(args.task)
    if not task_ids and args.task_count is None and not args.response_file:
        raise ValueError("请提供 --task-count 或至少一个 --task")
    if args.task_count is not None:
        if isinstance(args.task_count, bool) or args.task_count < 1:
            raise ValueError("--task-count 必须是正整数")
    return {
        "user_id": args.user_id,
        "task_ids": task_ids,
        "task_count": args.task_count,
    }


def load_response(path: Path) -> dict[str, Any]:
    with path.expanduser().resolve().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"平台响应必须是 JSON 对象: {path}")
    return payload


def extract_tasks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    raw_tasks = data.get("tasks") if isinstance(data, dict) else payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("平台响应缺少 data.tasks 或 tasks 数组")
    tasks = [task for task in raw_tasks if isinstance(task, dict)]
    if not tasks:
        raise ValueError("平台响应 data.tasks 为空")
    return tasks


def normalize_records(
    tasks: list[dict[str, Any]],
    fallback_user_id: str,
    task_status: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in tasks:
        task_id = str(task.get("task_id") or "").strip()
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        # The API currently returns qa_by; qa_user_id is accepted for saved
        # responses and future API variants.
        operator_id = task.get("qa_user_id") or task.get("qa_by") or fallback_user_id
        operator_code = task.get("qa_by_name") or ""
        records.append(
            {
                "task_id": task_id,
                "operator_id": str(operator_id).strip() if operator_id else None,
                "operator_code": str(operator_code).strip(),
                "operator_name": "",
                "task_status": task_status,
                "task_sub_status": CLAIM_SUB_STATUS,
                "task_sub_status_name": CLAIM_SUB_STATUS_NAME,
            }
        )
    if not records:
        raise ValueError("平台响应中没有可写入的 task_id")
    return records


def redacted_headers(headers: dict[str, str]) -> dict[str, str]:
    result = dict(headers)
    for name in ("X-API-Key", "Authorization"):
        if name in result:
            result[name] = "<redacted>"
    return result


def claim_from_platform(
    context: dict[str, Any],
    env: dict[str, str],
    timeout: int,
    retries: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    capture: dict[str, Any] = {}

    def sender(
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        request_timeout: int,
        request_retries: int,
    ) -> dict[str, Any]:
        capture["request"] = {
            "method": "POST",
            "url": url,
            "headers": redacted_headers(headers),
            "body": body,
        }
        response = platform_qa.post_json(
            url,
            headers,
            body,
            request_timeout,
            request_retries,
        )
        capture["response"] = response
        return response

    config = dict(env)
    result, returncode = platform_qa.run_operation(
        "claim",
        context,
        config,
        timeout,
        retries,
        sender=sender,
    )
    if returncode:
        raise RuntimeError(f"platform claim failed with return code {returncode}")
    response = capture.get("response")
    if not isinstance(response, dict):
        raise RuntimeError("platform claim returned no captured response")
    request = capture.get("request") or {}
    return result, request, response


def initialize(args: argparse.Namespace) -> dict[str, Any]:
    context = build_context(args)
    timestamp = now_string()
    request: dict[str, Any] | None = None

    if args.response_file:
        response = load_response(args.response_file)
        claim_result = {
            "operation": "claim",
            "source": "response-file",
            "response_file": str(args.response_file.expanduser().resolve()),
        }
    else:
        env = load_env_file(args.env_file.expanduser().resolve())
        if not env.get("TRAJ_REPAIR_BASE_URL"):
            raise ValueError(".env 缺少 TRAJ_REPAIR_BASE_URL")
        platform_qa.platform_api_key(env)
        claim_result, request, response = claim_from_platform(
            context,
            env,
            max(1, args.timeout),
            max(0, args.retries),
        )

    tasks = extract_tasks(response)
    records = normalize_records(tasks, str(args.user_id or ""), args.task_status)
    report: dict[str, Any] = {
        "script": Path(__file__).stem,
        "created_at": timestamp,
        "claim_result": claim_result,
        "request": request,
        "response": response,
        "records": records,
        "database": {
            "config": str(args.config.expanduser().resolve()),
            "table": "task_structure_semantics",
            "status": "planned" if args.dry_run else "pending",
        },
    }

    if args.dry_run:
        report["database"]["status"] = "dry-run"
        report["database"]["written_count"] = 0
        return report

    config = load_toml(args.config)
    database_config = config.get("database")
    if not isinstance(database_config, dict):
        raise ValueError("配置缺少 [database] 段")
    env = load_env_file(args.env_file.expanduser().resolve()) if args.env_file.exists() else {}
    settings = load_task_database_settings(database_config, env)
    if settings is None:
        raise ValueError("[database].enabled 必须为 true，才能写入 task_structure_semantics")

    with TaskStructureSemanticsDatabase(settings) as database:
        count = database.initialize_claimed_tasks(
            records,
            downloaded_at=timestamp,
            task_status=args.task_status,
        )
    report["database"]["status"] = "written"
    report["database"]["written_count"] = count
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="认领 QA 任务并初始化 task_structure_semantics 金标结构语义表。"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--user-id", required=False, help="QA 平台 qa_user_id")
    parser.add_argument("--task-count", type=int, help="按数量从 QA 平台认领")
    parser.add_argument("--task", action="append", default=[], help="按指定 task_id 认领，可重复")
    parser.add_argument("--response-file", type=Path, help="使用保存的 QA 响应，例如 DB/init.json")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--task-status", default="已领取", help="写入 task_status 的流水线状态")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true", help="只调用/读取响应并生成报告，不写数据库")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.response_file and not args.user_id:
            raise ValueError("在线认领必须提供 --user-id")
        report = initialize(args)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(args.report, report)
        print(json.dumps({
            "written_count": report["database"].get("written_count", 0),
            "report": str(args.report.resolve()),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"task_init failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

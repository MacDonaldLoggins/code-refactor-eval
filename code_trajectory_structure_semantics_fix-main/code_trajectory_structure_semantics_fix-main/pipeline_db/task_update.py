#!/usr/bin/env python3
"""Update the current TASK pipeline stage in ``task_structure_semantics``.

The pipeline imports :class:`TaskStageUpdater` so all ``success``/``error``
formatting and database writes live in one place.  The CLI is also useful for
manually correcting a TASK row after an interrupted run.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline_integrations.common import load_env_file  # noqa: E402
from pipeline_integrations.task_structure_semantics_db import (  # noqa: E402
    TaskStructureSemanticsDatabase,
    load_task_database_settings,
)


STAGE_DISPLAY_NAMES = {
    "claim_tasks": "从 QA 平台认领任务",
    "fetch_from_oss": "从 OSS 下载并组装 TASK 数据",
    "action_semantics_by_session": "动作语义修复",
    "quality_judge": "质量评估",
    "aggregate_sessions": "聚合 TASK sessions",
    "runtest_pipeline_by_task": "运行测试流水线",
    "structure_check_fix_by_task": "结构检查与修复",
    "split_success_fail_tasks": "拆分成功和失败任务",
    "upload_results": "上传结果",
    "mark_qa_complete": "标记质检完成",
    "cleanup_local_tasks": "清理本地任务",
    "notify_failure": "发送失败通知",
}
OUTCOMES = {"success", "error"}
FAILED_STAGE_STATUSES = {"failed", "error", "timeout"}


def stage_status(stage_name: str, outcome: str) -> tuple[str, str]:
    """Build the database values for one stage outcome."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unsupported stage outcome: {outcome}")
    return (
        f"{stage_name}-{outcome}",
        f"{STAGE_DISPLAY_NAMES.get(stage_name, stage_name)}-{outcome}",
    )


class TaskStageUpdater:
    """Write per-TASK stage outcomes through the task database integration."""

    def __init__(
        self,
        database: TaskStructureSemanticsDatabase | None,
        dry_run: bool = False,
        on_error: Callable[[Exception], bool] | None = None,
    ) -> None:
        self.database = database
        self.dry_run = dry_run
        self.on_error = on_error

    def _write(self, operation) -> None:
        if self.database is None or self.dry_run:
            return
        try:
            operation(self.database)
        except Exception as exc:
            if self.on_error is not None and self.on_error(exc):
                return
            raise

    def set_status(
        self,
        task_ids: Iterable[str],
        stage_name: str,
        outcome: str,
    ) -> None:
        ids = [str(task_id).strip() for task_id in task_ids if str(task_id).strip()]
        if self.database is None or self.dry_run or not ids:
            return
        status, status_name = stage_status(stage_name, outcome)
        self._write(lambda database: database.set_task_sub_status(ids, status, status_name))

    def ensure_tasks(
        self,
        task_ids: Iterable[str],
        *,
        operator_id: str | None = None,
        operator_code: str | None = None,
        operator_name: str | None = None,
    ) -> None:
        """Ensure local/replay TASKs have rows before stage updates."""
        ids = [str(task_id).strip() for task_id in task_ids if str(task_id).strip()]
        if self.database is None or self.dry_run or not ids:
            return
        self._write(
            lambda database: database.ensure_task_rows(
                ids,
                operator_id=operator_id,
                operator_code=operator_code,
                operator_name=operator_name,
            )
        )

    def mark_result(
        self,
        stage_name: str,
        stage_status_value: str,
        task_ids: Iterable[str],
    ) -> None:
        if stage_status_value == "completed":
            self.set_status(task_ids, stage_name, "success")
        elif stage_status_value in FAILED_STAGE_STATUSES:
            self.set_status(task_ids, stage_name, "error")

    def mark_states(self, stage_name: str, states: Mapping[str, Any]) -> None:
        success_ids = [
            str(state.task_id)
            for state in states.values()
            if state.stages.get(stage_name) == "completed"
        ]
        error_ids = [
            str(state.task_id)
            for state in states.values()
            if state.stages.get(stage_name) == "failed"
        ]
        self.set_status(success_ids, stage_name, "success")
        self.set_status(error_ids, stage_name, "error")

    def mark_items(self, stage: Any, states: Mapping[str, Any]) -> None:
        stage_name = str(stage.name)
        state_ids = set(states)
        success_ids = [
            str(item.get("item"))
            for item in stage.items
            if item.get("status") == "completed"
            and str(item.get("item")) in state_ids
        ]
        error_ids = [
            str(item.get("item"))
            for item in stage.items
            if item.get("status") in FAILED_STAGE_STATUSES
            and str(item.get("item")) in state_ids
        ]
        self.set_status(success_ids, stage_name, "success")
        self.set_status(error_ids, stage_name, "error")

    def mark_split_items(self, stage: Any, states: Mapping[str, Any]) -> None:
        """Record split failures and successful routing to ``success_task``.

        A completed copy to ``fail_task`` is a successful routing operation,
        not a successful TASK outcome. Leave its earlier failure sub-status
        intact so the database continues to expose the real failed stage.
        """
        stage_name = str(stage.name)
        state_ids = set(states)
        success_ids = [
            str(item.get("item"))
            for item in stage.items
            if item.get("status") == "completed"
            and item.get("bucket") == "success_task"
            and str(item.get("item")) in state_ids
        ]
        error_ids = [
            str(item.get("item"))
            for item in stage.items
            if item.get("status") in FAILED_STAGE_STATUSES
            and str(item.get("item")) in state_ids
        ]
        self.set_status(success_ids, stage_name, "success")
        self.set_status(error_ids, stage_name, "error")

    def mark_fetch_result(self, task_ids: Iterable[str], success: bool) -> None:
        self.set_status(
            task_ids,
            "fetch_from_oss",
            "success" if success else "error",
        )

    def mark_unaligned(self, task_ids: Iterable[str]) -> None:
        """Record TASKs whose 聚合任务/全量清洗成功 OSS sources cannot align."""
        ids = [str(task_id).strip() for task_id in task_ids if str(task_id).strip()]
        if self.database is None or self.dry_run or not ids:
            return
        self._write(
            lambda database: database.set_task_sub_status(
                ids, "无法对齐", "聚合任务与全量清洗成功无法对齐"
            )
        )


def load_config(path: Path) -> dict[str, Any]:
    with path.expanduser().resolve().open("rb") as handle:
        config = tomllib.load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"config must be a TOML table: {path}")
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="更新 task_structure_semantics 的当前流水线阶段状态。"
    )
    parser.add_argument("--task", action="append", required=True, help="TASK ID，可重复传入")
    parser.add_argument("--stage", required=True, help="流水线阶段名")
    parser.add_argument("--outcome", choices=sorted(OUTCOMES), required=True)
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "full_pipeline_config.toml")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--dry-run", action="store_true", help="只显示将更新的值，不写数据库")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    status, status_name = stage_status(args.stage, args.outcome)
    if args.dry_run:
        print({"task_ids": args.task, "task_sub_status": status, "task_sub_status_name": status_name})
        return 0

    config = load_config(args.config)
    database_config = config.get("database")
    if not isinstance(database_config, dict):
        raise ValueError("配置缺少 [database] 段")
    env = load_env_file(args.env_file.expanduser().resolve())
    settings = load_task_database_settings(database_config, env)
    if settings is None:
        raise ValueError("[database].enabled 必须为 true")
    with TaskStructureSemanticsDatabase(settings) as database:
        updater = TaskStageUpdater(database)
        updater.ensure_tasks(args.task)
        updater.set_status(args.task, args.stage, args.outcome)
    print({"task_ids": args.task, "task_sub_status": status, "task_sub_status_name": status_name})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

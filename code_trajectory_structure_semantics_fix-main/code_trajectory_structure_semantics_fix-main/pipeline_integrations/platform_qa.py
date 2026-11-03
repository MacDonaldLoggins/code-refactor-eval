#!/usr/bin/env python3
"""Claim trajectory QA tasks and advance their platform QA status."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import requests

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


CLAIM_PATH = "/api/traj-repair/api/qa/claim"
UPDATE_PATH = "/api/traj-repair/api/qa/update-status"
VALID_QA_STATUSES = {"质检中", "质检完成"}
RETRY_DELAYS = (1, 2, 5)
API_KEY_ENV_NAMES = ("DISPATCH_WRITE_API_KEY", "TRAJ_REPAIR_API_KEY")


def platform_api_key(config: dict[str, str]) -> str:
    """Return the platform write key, accepting the deployment name and legacy alias."""
    for name in API_KEY_ENV_NAMES:
        value = str(config.get(name) or "").strip()
        if value:
            return value
    raise RuntimeError(
        "missing required configuration: DISPATCH_WRITE_API_KEY or TRAJ_REPAIR_API_KEY"
    )


def build_headers(config: dict[str, str]) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-API-Key": platform_api_key(config),
    }


def build_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    delays = RETRY_DELAYS[: max(0, retries)]
    for attempt in range(len(delays) + 1):
        try:
            response = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException:
            if attempt >= len(delays):
                raise
            time.sleep(delays[attempt])
            continue

        if response.status_code >= 500 and attempt < len(delays):
            time.sleep(delays[attempt])
            continue
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"platform response is not JSON: HTTP {response.status_code} {response.text[:200]}"
            ) from exc
        if response.status_code != 200:
            raise RuntimeError(f"platform request failed: HTTP {response.status_code} {payload}")
        if payload.get("code") not in (None, 200):
            raise RuntimeError(f"platform business request failed: {payload}")
        return payload
    raise AssertionError("unreachable")


def claim_body(context: dict[str, Any]) -> dict[str, Any]:
    qa_user_id = str(context.get("user_id") or "").strip()
    if not qa_user_id:
        raise ValueError("claim requires user_id/qa_user_id")
    body: dict[str, Any] = {"qa_user_id": qa_user_id}
    task_ids = context_task_ids(context)
    if task_ids:
        body["task_ids"] = task_ids
        return body

    task_count = context.get("task_count")
    if isinstance(task_count, bool) or not isinstance(task_count, int) or task_count < 1:
        raise ValueError("claim requires a positive task_count when task_ids are absent")
    body["count"] = task_count
    return body


def update_body(context: dict[str, Any], qa_status: str) -> dict[str, Any]:
    if qa_status not in VALID_QA_STATUSES:
        raise ValueError(f"unsupported qa_status: {qa_status}")
    qa_user_id = str(context.get("user_id") or "").strip()
    if not qa_user_id:
        raise ValueError("status update requires user_id/qa_user_id")
    task_ids = context_task_ids(context)
    if not task_ids:
        raise ValueError("status update requires non-empty task_ids")
    return {"qa_user_id": qa_user_id, "task_ids": task_ids, "qa_status": qa_status}


def run_operation(
    operation: str,
    context: dict[str, Any],
    config: dict[str, str],
    timeout: int,
    retries: int,
    sender: Callable[[str, dict[str, str], dict[str, Any], int, int], dict[str, Any]] = post_json,
) -> tuple[dict[str, Any], int]:
    headers = build_headers(config)
    if operation == "claim":
        body = claim_body(context)
        payload = sender(
            build_url(config["TRAJ_REPAIR_BASE_URL"], CLAIM_PATH),
            headers,
            body,
            timeout,
            # A claim is not safely retryable: the platform may have assigned
            # TASKs even when the client did not receive a response.
            0,
        )
        data = payload.get("data") or {}
        tasks = data.get("tasks") or []
        task_ids = [str(item.get("task_id") or "").strip() for item in tasks if isinstance(item, dict)]
        task_ids = [task_id for task_id in task_ids if task_id]
        if not task_ids:
            raise RuntimeError("claim succeeded but returned no task IDs")
        return {
            "operation": operation,
            "task_ids": task_ids,
            "tasks": tasks,
            "claimed_count": int(data.get("claimed_count") or len(task_ids)),
            "requested_count": data.get("requested_count"),
            "matched_count": data.get("matched_count"),
        }, 0

    qa_status = "质检中" if operation == "mark-in-progress" else "质检完成"
    body = update_body(context, qa_status)
    payload = sender(
        build_url(config["TRAJ_REPAIR_BASE_URL"], UPDATE_PATH),
        headers,
        body,
        timeout,
        retries,
    )
    data = payload.get("data") or {}
    failed = data.get("failed") or []
    updated_tasks = data.get("updated_tasks") or []
    result = {
        "operation": operation,
        "qa_status": qa_status,
        "requested": int(data.get("requested") or len(body["task_ids"])),
        "updated": int(data.get("updated") or len(updated_tasks)),
        "updated_task_ids": [
            str(item.get("task_id")) for item in updated_tasks if isinstance(item, dict) and item.get("task_id")
        ],
        "failed": failed,
    }
    return result, 1 if failed or result["updated"] != len(body["task_ids"]) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("claim", "mark-in-progress", "mark-complete"))
    parser.add_argument("--context", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _context_path, context = load_context(args.context)
        result_path = resolve_result_path(args.result)
        config = load_env_file(args.env_file.expanduser().resolve())
        require_values(config, ("TRAJ_REPAIR_BASE_URL",))
        platform_api_key(config)
        result, returncode = run_operation(
            args.operation,
            context,
            config,
            max(1, args.timeout),
            max(0, args.retries),
        )
        write_json_atomic(result_path, result)
        print(json.dumps({key: value for key, value in result.items() if key != "failed"}, ensure_ascii=False))
        if returncode:
            print(f"platform status update had failed items: {result.get('failed')}", file=sys.stderr)
        return returncode
    except Exception as exc:
        print(f"platform QA adapter failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""读取 pipeline_report_summary_finalcheck.md 中的异常 Task，为其 session 文件
末尾添加 role='assistant', agent='main' 的总结消息。

用法:
    python3 fix_add_summary_anchor.py \
        --report /path/to/pipeline_report_summary_finalcheck.md \
        --source-root /path/to/异常tasks

    python3 fix_add_summary_anchor.py \
        --report /path/to/pipeline_report_summary_finalcheck.md \
        --source-root /path/to/异常tasks \
        --skip-llm
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_PROFILE = "LongCat"
LONGCAT_PROFILE = "LongCat"
MINDFLOW_PROFILE = "MindFlow_deepseek"


def load_env_file(path: Path) -> None:
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
        os.environ[key] = value


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise ValueError(f"Missing required model config env var: {name}")


def model_profile() -> str:
    return os.environ.get("CLAUDE_OPENAI_MODEL_PROFILE") or DEFAULT_PROFILE


def model_profile_config() -> tuple[str, str, str]:
    profile = model_profile()
    normalized = profile.lower().strip()
    if normalized == LONGCAT_PROFILE.lower():
        return required_env("M_MODEL_NAME"), required_env("M_MODEL_BASE_URL").rstrip("/"), required_env("M_MODEL_API_KEY")
    if normalized in {MINDFLOW_PROFILE.lower(), "mindflow"}:
        return required_env("MODEL_NAME"), required_env("MODEL_BASE_URL").rstrip("/"), required_env("MODEL_API_KEY")
    raise ValueError(
        f"Unknown model profile {profile!r}; set CLAUDE_OPENAI_MODEL_PROFILE "
        f"to {LONGCAT_PROFILE!r} or {MINDFLOW_PROFILE!r}"
    )


def model_name() -> str:
    return model_profile_config()[0]


def model_base_url() -> str:
    return model_profile_config()[1]


def model_api_key() -> str:
    return model_profile_config()[2]


load_env_file(ENV_FILE_PATH)


SYSTEM_PROMPT = """You are a conversation summarizer. Read the full AI coding assistant session (user questions and assistant responses) and produce a concise but comprehensive English summary.

Requirements:
- Cover the user's original request, key decisions, implementation approach, problems encountered, and final outcome
- No more than 300 words
- Objective factual statements only, no evaluation
- Output summary text directly with no extra formatting"""


def _request_chat_completion(url: str, headers: dict, payload: dict, timeout: int) -> str:
    import requests as req

    response = req.post(url, headers=headers, json=payload, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(
            f"model API returned {response.status_code}: {response.text[:500]}"
        )
    result = response.json()
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"model response has no choices: {json.dumps(result, ensure_ascii=False)[:500]}")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if not content:
        raise RuntimeError(f"model response message has no content: {json.dumps(message, ensure_ascii=False)[:500]}")
    return content


def compact_text(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2 - 15
    return text[:half] + "\n\n...[truncated]...\n\n" + text[-half:]


def messages_to_summary_input(messages: list[dict]) -> str:
    lines: list[str] = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        if role == "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
            content = "\n".join(texts)
        if not isinstance(content, str):
            content = str(content)

        label = {"user": "User", "assistant": "Assistant", "tool": "Tool"}.get(role, role)
        text = compact_text(content.strip(), limit=800)
        if text:
            lines.append(f"[{i}] {label}: {text}")
    return "\n".join(lines)


def generate_summary(messages: list[dict]) -> str:
    conversation_text = messages_to_summary_input(messages)

    prompt = f"""Please summarize the following AI coding assistant session:

{conversation_text}

Output a concise English summary (no more than 300 words)."""

    payload = {
        "model": model_name(),
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    request_url = f"{model_base_url()}/chat/completions"
    headers = {
        "Authorization": f"Bearer {model_api_key()}",
        "Content-Type": "application/json",
    }

    print(f"  [LLM] Requesting summary from {model_name()}...", file=sys.stderr)
    started = time.monotonic()
    summary = _request_chat_completion(request_url, headers, payload, timeout=120)
    elapsed = time.monotonic() - started
    print(f"  [LLM] Done in {elapsed:.1f}s, length={len(summary)}", file=sys.stderr)
    return summary.strip()


def _last_timestamp(messages: list[dict]) -> str:
    for msg in reversed(messages):
        meta = msg.get("_metadata")
        if isinstance(meta, dict):
            ts = meta.get("timestamp")
            if isinstance(ts, str) and ts:
                return ts
    return datetime.now(timezone.utc).isoformat()


def _is_summary_anchor(msg: dict) -> bool:
    """Check if a message is already a summary anchor.

    A summary anchor has only {role, agent, content, _metadata} with no
    tokens/reasoning_content keys — regular conversation messages carry
    those extra fields.
    """
    if msg.get("role") != "assistant" or msg.get("agent") != "main":
        return False
    if "reasoning_content" in msg or msg.get("tool_calls"):
        return False
    meta = msg.get("_metadata")
    if isinstance(meta, dict) and "tokens" in meta:
        return False
    return isinstance(msg.get("content"), str) and len(msg.get("content", "")) > 10


def parse_report_for_task_ids(report_path: Path) -> list[str]:
    """Parse the markdown report to extract abnormal task IDs."""
    task_ids: list[str] = []
    in_abnormal_section = False

    for line in report_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "## 异常 Task":
            in_abnormal_section = True
            continue
        if in_abnormal_section and stripped.startswith("## "):
            break
        if in_abnormal_section and stripped.startswith("| TASK-"):
            parts = [c.strip() for c in stripped.strip("|").split("|")]
            if parts:
                task_id = parts[0].strip()
                if task_id.startswith("TASK-"):
                    task_ids.append(task_id)

    return task_ids


def find_session_files(source_root: Path, task_ids: list[str]) -> list[Path]:
    """Find all session JSON files for given task IDs."""
    files: list[Path] = []
    for task_id in task_ids:
        task_dir = source_root / task_id / "sessions"
        if not task_dir.is_dir():
            task_dir = source_root / task_id
        if not task_dir.is_dir():
            print(f"  WARNING: task dir not found: {task_dir}", file=sys.stderr)
            continue
        for json_file in sorted(task_dir.glob("*.json")):
            if json_file.is_file():
                files.append(json_file)
    return files


def fix_session(session_path: Path, *, skip_llm: bool = False) -> dict:
    """Add summary anchor to a session file."""
    doc = json.loads(session_path.read_text(encoding="utf-8"))
    messages = doc.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return {"status": "skipped", "reason": "no messages", "path": str(session_path)}

    last = messages[-1]
    if _is_summary_anchor(last):
        return {"status": "skipped", "reason": "already has summary anchor", "path": str(session_path)}

    if skip_llm:
        summary_text = "Session completed."
    else:
        summary_text = generate_summary(messages)

    ts = _last_timestamp(messages)

    anchor_msg = {
        "role": "assistant",
        "agent": "main",
        "content": summary_text,
        "_metadata": {
            "timestamp": ts,
        },
    }

    messages.append(anchor_msg)

    session_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "path": str(session_path),
        "message_count": len(messages),
        "summary_length": len(summary_text),
        "summary_preview": summary_text[:100],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fix abnormal tasks by adding summary anchor messages."
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Path to pipeline_report_summary_finalcheck.md",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Root directory containing TASK-* subdirectories. Defaults to report's parent.",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip LLM call, use placeholder text.",
    )
    args = parser.parse_args()

    report_path: Path = args.report.expanduser().resolve()
    source_root: Path = (
        args.source_root.expanduser().resolve()
        if args.source_root
        else report_path.parent
    )

    if not report_path.is_file():
        print(f"ERROR: report not found: {report_path}", file=sys.stderr)
        return 1
    if not source_root.is_dir():
        print(f"ERROR: source root not found: {source_root}", file=sys.stderr)
        return 1

    task_ids = parse_report_for_task_ids(report_path)
    print(f"Found {len(task_ids)} abnormal task(s): {task_ids}")

    if not task_ids:
        print("No abnormal tasks to fix.")
        return 0

    session_files = find_session_files(source_root, task_ids)
    print(f"Found {len(session_files)} session file(s) to process.\n")

    results = {"ok": 0, "skipped": 0, "failed": 0}

    for session_path in session_files:
        rel = session_path.relative_to(source_root)
        try:
            result = fix_session(session_path, skip_llm=args.skip_llm)
            if result["status"] == "ok":
                print(f"  FIXED: {rel} (summary_len={result['summary_length']})")
                results["ok"] += 1
            else:
                print(f"  SKIP:  {rel} ({result['reason']})")
                results["skipped"] += 1
        except Exception as exc:
            print(f"  FAIL:  {rel} ({exc})")
            results["failed"] += 1

    print(f"\nDone. fixed={results['ok']}, skipped={results['skipped']}, failed={results['failed']}")
    return 1 if results["failed"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())

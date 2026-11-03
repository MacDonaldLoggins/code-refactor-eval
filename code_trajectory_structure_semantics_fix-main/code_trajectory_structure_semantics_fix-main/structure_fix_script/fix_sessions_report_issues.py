#!/usr/bin/env python3
"""Fix reported sensitive keyword and CJK findings inside final sessions JSON.

This script is intentionally narrow: it reads per-TASK report/check_pipeline.json
files and only edits the string fields identified by the reported json_path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHECK_NAMES = {"sensitive_keywords", "no_chinese"}
ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_PROFILE = "LongCat"
LONGCAT_PROFILE = "LongCat"
MINDFLOW_PROFILE = "MindFlow_deepseek"
COMMON_SCRIPT_NAMES = (
    "clean_original_sessions.py",
    "check_chinese.py",
    "validate_package.py",
    "merge_claude_subagents_trajectory.py",
)
COMMON_SCRIPT_PATTERN = re.compile(
    r"(?<![\w.-])(?:" + "|".join(re.escape(name) for name in COMMON_SCRIPT_NAMES) + r")(?![\w.-])"
)
CJK_PATTERN = re.compile(
    "["
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uf900-\ufaff"
    "\U00020000-\U0002a6df"
    "\u3000-\u303f"
    "\uff01-\uff5e"
    "\uffe0-\uffe6"
    "]"
)


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


def load_model_config() -> tuple[str, str, str]:
    load_env_file(ENV_FILE_PATH)
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
FALLBACK_CHAR_REPLACEMENTS = {
    "\u3007": "0",
    "\u3000": " ",
    "\u303f": " ",
}
SENSITIVE_REPLACEMENT = "internal_tool.py"


@dataclass(frozen=True)
class Finding:
    task_id: str
    check_name: str
    file_id: str
    json_path: str
    matched_text: str
    report_item: dict[str, Any]

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.task_id, self.check_name, self.file_id, self.json_path, self.matched_text)


def field(item: dict[str, Any], *names: str) -> str:
    for name in names:
        value = item.get(name)
        if isinstance(value, str):
            return value
    return ""


def collect_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for task_dir in sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("TASK-")):
        report_path = task_dir / "report" / "check_pipeline.json"
        if not report_path.is_file():
            continue
        data = json.loads(report_path.read_text(encoding="utf-8"))
        for result in data.get("results", []):
            if not isinstance(result, dict) or result.get("name") not in CHECK_NAMES:
                continue
            if result.get("status") != "FAIL":
                continue
            check_name = str(result["name"])
            for item in result.get("tasks", []):
                if not isinstance(item, dict):
                    continue
                file_id = field(item, "文件id", "鏂囦欢id", "file_id")
                json_path = field(item, "json_path")
                matched_text = field(item, "matched_text")
                if not file_id or not json_path or not matched_text:
                    continue
                finding = Finding(task_dir.name, check_name, file_id, json_path, matched_text, item)
                if finding.key in seen:
                    continue
                seen.add(finding.key)
                findings.append(finding)
    return findings


def parse_path_part(part: str) -> list[str | int]:
    pieces: list[str | int] = []
    match = re.match(r"^([^\[]+)", part)
    if match:
        pieces.append(match.group(1))
    for index_text in re.findall(r"\[(\d+)\]", part):
        pieces.append(int(index_text))
    return pieces


def parse_json_path(path: str) -> list[str | int]:
    pieces: list[str | int] = []
    for part in path.split("."):
        if not part or part.startswith("<key:"):
            raise ValueError(f"unsupported json_path segment: {part!r}")
        pieces.extend(parse_path_part(part))
    return pieces


def get_parent_and_key(payload: Any, json_path: str) -> tuple[Any, str | int]:
    pieces = parse_json_path(json_path)
    if not pieces:
        raise ValueError("empty json_path")
    current = payload
    for piece in pieces[:-1]:
        current = current[piece]
    return current, pieces[-1]


def normalize_char(char: str) -> str:
    if char in FALLBACK_CHAR_REPLACEMENTS:
        return FALLBACK_CHAR_REPLACEMENTS[char]
    normalized = unicodedata.normalize("NFKC", char)
    if normalized != char and not CJK_PATTERN.search(normalized):
        return normalized
    return ""


def fallback_replacements(chars: set[str]) -> dict[str, str]:
    return {char: normalize_char(char) for char in sorted(chars)}


def request_ai_replacements(chars: set[str], samples: dict[str, list[str]]) -> dict[str, str]:
    import requests

    model, base_url, api_key = load_model_config()
    prompt = {
        "instruction": (
            "Return strict JSON only. For each reported CJK/fullwidth character, choose a safe ASCII "
            "replacement that preserves the surrounding technical transcript as much as possible. "
            "If the character is only decorative or cannot be represented safely, use an empty string."
        ),
        "characters": sorted(chars),
        "samples": samples,
        "schema": {"replacements": {"<character>": "<ASCII replacement>"}},
    }
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": "You produce strict JSON only."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    response = requests.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S)
    data = json.loads(content)
    raw_replacements = data.get("replacements", {})
    replacements: dict[str, str] = {}
    if isinstance(raw_replacements, dict):
        for char in chars:
            value = raw_replacements.get(char, "")
            if isinstance(value, str) and not CJK_PATTERN.search(value):
                replacements[char] = value
    return replacements


def build_ai_samples(root: Path, findings: list[Finding]) -> tuple[set[str], dict[str, list[str]]]:
    chars: set[str] = set()
    samples: dict[str, list[str]] = {}
    for finding in findings:
        if finding.check_name != "no_chinese":
            continue
        chars.update(finding.matched_text)
        sample = field(finding.report_item, "content 原文", "content 鍘熸枃", "content")
        if not sample:
            path = root / finding.task_id / finding.file_id
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                parent, key = get_parent_and_key(payload, finding.json_path)
                value = parent[key]
                if isinstance(value, str):
                    sample = value[:500]
            except Exception:
                sample = ""
        for char in finding.matched_text:
            samples.setdefault(char, [])
            if sample and len(samples[char]) < 3:
                samples[char].append(sample[:500])
    return chars, samples


def apply_replacements(value: str, finding: Finding, char_replacements: dict[str, str]) -> tuple[str, int]:
    if finding.check_name == "sensitive_keywords":
        new_value, count = COMMON_SCRIPT_PATTERN.subn(SENSITIVE_REPLACEMENT, value)
        return new_value, count
    count = 0
    new_value = value
    for char in finding.matched_text:
        replacement = char_replacements.get(char, normalize_char(char))
        occurrences = new_value.count(char)
        if occurrences:
            new_value = new_value.replace(char, replacement)
            count += occurrences
    return new_value, count


def process_file(path: Path, file_findings: list[Finding], char_replacements: dict[str, str], *, apply: bool) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    changed = False
    for finding in file_findings:
        parent, key = get_parent_and_key(payload, finding.json_path)
        value = parent[key]
        if not isinstance(value, str):
            records.append({**finding.__dict__, "status": "skipped_non_string", "path": str(path)})
            continue
        new_value, count = apply_replacements(value, finding, char_replacements)
        status = "changed" if count and new_value != value else "unchanged"
        if status == "changed":
            parent[key] = new_value
            changed = True
        records.append({
            "task_id": finding.task_id,
            "check_name": finding.check_name,
            "file_id": finding.file_id,
            "json_path": finding.json_path,
            "matched_text": finding.matched_text,
            "replacement_count": count,
            "status": status if apply else f"dry_run_{status}",
            "path": str(path),
        })
    if changed and apply:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix reported sessions sensitive/no_chinese issues.")
    parser.add_argument("-r", "--root", type=Path, required=True, help="Root containing TASK-* directories.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    parser.add_argument("--ai", action="store_true", help="Use configured model to choose CJK ASCII replacements.")
    parser.add_argument("--output", type=Path, help="JSON repair report path.")
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: root not found: {root}", file=sys.stderr)
        return 1

    findings = collect_findings(root)
    print(f"found reported findings: {len(findings)}")

    chars, samples = build_ai_samples(root, findings)
    replacements = fallback_replacements(chars)
    ai_error = ""
    if args.ai and chars:
        try:
            started = time.monotonic()
            ai_replacements = request_ai_replacements(chars, samples)
            replacements.update({char: ai_replacements.get(char, replacements.get(char, "")) for char in chars})
            print(f"ai replacements: {replacements} ({time.monotonic() - started:.1f}s)")
        except Exception as exc:  # noqa: BLE001 - fallback keeps repair deterministic.
            ai_error = str(exc)
            print(f"AI replacement request failed, using fallback: {exc}", file=sys.stderr)
    elif chars:
        print(f"fallback replacements: {replacements}")

    by_file: dict[Path, list[Finding]] = {}
    for finding in findings:
        by_file.setdefault(root / finding.task_id / finding.file_id, []).append(finding)

    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path, file_findings in sorted(by_file.items(), key=lambda item: str(item[0])):
        if not path.is_file():
            errors.append({"path": str(path), "error": "file not found"})
            continue
        try:
            file_records = process_file(path, file_findings, replacements, apply=args.apply)
            records.extend(file_records)
            changed = sum(1 for record in file_records if "changed" in record["status"])
            print(f"{'APPLY' if args.apply else 'DRY'} {path}: findings={len(file_findings)} changed={changed}")
        except Exception as exc:  # noqa: BLE001 - keep processing other files.
            errors.append({"path": str(path), "error": str(exc)})
            print(f"ERROR {path}: {exc}", file=sys.stderr)

    report = {
        "apply": args.apply,
        "ai_requested": args.ai,
        "ai_error": ai_error,
        "char_replacements": replacements,
        "summary": {
            "findings": len(findings),
            "files": len(by_file),
            "records": len(records),
            "changed_records": sum(1 for record in records if "changed" in record["status"]),
            "errors": len(errors),
        },
        "records": records,
        "errors": errors,
    }
    output = args.output or (root / "fix_sessions_report_issues_report.json")
    if args.apply:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"report: {output}")
    else:
        print(json.dumps(report["summary"], ensure_ascii=False))

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

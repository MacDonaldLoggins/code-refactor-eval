#!/usr/bin/env python3
"""Claude Code 轨迹中文/CJK 处理脚本。

参数说明：
  -r, --root
      必填。指定 TASK 根目录或单个 TASK 目录。脚本会扫描其中所有 TASK，
      并检查这些 TASK 中 repo、docs、轨迹、metadata、prompt
      等位置的中文/CJK 内容。
  -o, --output
      可选。指定报告输出目录。不传时，每个 TASK 的报告写入该 TASK 的
      report/ 目录；传入且只处理一个 TASK 时，报告写入 OUTPUT/report/；
      传入且处理多个 TASK 时，报告写入 OUTPUT/<TASK>/report/。

使用样例：
      python cjk_session_format.py -r path/to/TASK
      python cjk_session_format.py -r path/to/tasks-root
      python cjk_session_format.py -r path/to/TASK -o path/to/TASK

处理流程：
  1. 读取 TASK/.tmp/validation_report.md 中的中文检查结果；如果没有该报告，
      则扫描 repo、docs、original_sessions、fix_original_sessions、sessions、metadata*.json、prompt*.md 等文本文件。
  2. 对每条中文/CJK 命中读取源文件上下文。
  3. 调用 .env 或环境变量 MODEL_NAME、MODEL_BASE_URL、MODEL_API_KEY 配置的大模型接口，判断应保留、翻译、替换、
     人工复核或从源文件重建。
  4. 对模型明确认为低风险且唯一匹配的替换执行修改；对 vendor、dist、
     minified、report 等高风险位置只生成报告，不自动修改。

报告逻辑：
  - 每个 TASK 的中文/CJK 处理报告写入 TASK/report/cjk_repair_case_report.md。
  - 传入 -o/--output 时，只改变报告输出位置，不改变扫描和自动替换的源 TASK。
  - 不生成 cjk_repair_report.md，也不生成 cjk_manual_review.jsonl。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from json import JSONDecodeError
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
REPO_ENV_FILE = SCRIPT_DIR.parents[1] / ".env"
for import_path in (SCRIPT_DIR, ROOT_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from check_session_format import discover_task_dirs


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
VALIDATION_LINE_RE = re.compile(r"行号:\s*([0-9,，\s]+)\s*\(([^)]+)\)")
DEFAULT_VALIDATION_REPORT = "validation_report.md"
DEFAULT_CJK_CASE_REPORT = "cjk_repair_case_report.md"
DEFAULT_OUTPUT_PREFIX = "cjk"
DEFAULT_MODEL = "deepseek-v4-flash"
MODEL_NAME_ENV = "MODEL_NAME"
MODEL_BASE_URL_ENV = "MODEL_BASE_URL"
MODEL_API_KEY_ENV = "MODEL_API_KEY"
CJK_LLM_BATCH_SIZE = 8
REPORT_DIR_NAME = "report"
KEEP_TERMS = {"微软雅黑": "font name", "宋体": "font name", "黑体": "font name", "楷体": "font name", "仿宋": "font name", "华文细黑": "font name", "华文宋体": "font name"}
THIRD_PARTY_HINTS = ("/vendor/", "/vendors/", "/lib/", "/libs/", "/dist/", "/static/lib/", "/static/vendor/")
SCAN_TOP_LEVEL_DIRS = {"repo", "docs", "original_sessions", "fix_original_sessions", "sessions", "repaired_sessions"}
SCAN_ROOT_FILE_PATTERNS = ("metadata*.json", "prompt*.md")
TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".jsonl",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".xml",
    ".yml",
    ".yaml",
    ".properties",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".sh",
}
APPLY_ACTIONS = {"apply", "translate", "replace", "replace_font_name"}


def _load_dotenv_if_present() -> None:
    if not REPO_ENV_FILE.is_file():
        return
    try:
        lines = REPO_ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {MODEL_NAME_ENV, MODEL_BASE_URL_ENV, MODEL_API_KEY_ENV}:
            continue
        value = value.strip().strip("'\"")
        if key:
            os.environ[key] = value


def _llm_config() -> tuple[str, str, str]:
    _load_dotenv_if_present()
    model = os.getenv(MODEL_NAME_ENV, DEFAULT_MODEL).strip()
    base_url = os.getenv(MODEL_BASE_URL_ENV, "").strip()
    api_key = os.getenv(MODEL_API_KEY_ENV, "").strip()
    missing = [
        name
        for name, value in (
            (MODEL_BASE_URL_ENV, base_url),
            (MODEL_API_KEY_ENV, api_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing LLM config: {', '.join(missing)}")
    return model, base_url, api_key


@dataclass
class CjkHit:
    task: str
    path: str
    line_no: int
    category: str
    reason: str
    fragments: list[str]
    line_preview: str
    source_context: str
    output_path: str


def _has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def _cjk_fragments(text: str) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for match in CJK_RE.finditer(text):
        fragment = match.group(0)
        if fragment not in seen:
            values.append(fragment)
            seen.add(fragment)
    return values


def _preview_cjk(line: str, context: int = 90) -> str:
    line = line.strip()
    matches = list(CJK_RE.finditer(line))
    if not matches:
        return line if len(line) <= context * 2 else line[: context * 2 - 3] + "..."
    spans: list[tuple[int, int]] = []
    for match in matches:
        spans.append((max(0, match.start() - context), min(len(line), match.end() + context)))
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if not merged or start > merged[-1][1] + 20:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    parts: list[str] = []
    previous_end = 0
    for start, end in merged:
        if parts and start > previous_end:
            parts.append("\n...\n")
        elif start > 0 and not parts:
            parts.append("...\n")
        parts.append(line[start:end])
        previous_end = end
    if previous_end < len(line):
        parts.append("\n...")
    return "".join(parts)


def _source_context(path: Path, line_no: int, before: int = 8, after: int = 8) -> str:
    if line_no < 1 or not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return ""
    start = max(1, line_no - before)
    end = min(len(lines), line_no + after)
    return "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1))


def _is_text_file(path: Path) -> bool:
    if path.name.startswith(".") or path.name.startswith("._"):
        return False
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return True


def _scan_candidate_files(task_path: Path) -> list[Path]:
    files: dict[str, Path] = {}
    for top in SCAN_TOP_LEVEL_DIRS:
        base = task_path / top
        if base.is_dir():
            for path in base.rglob("*"):
                if path.is_file() and _is_text_file(path):
                    files[path.relative_to(task_path).as_posix()] = path
    for pattern in SCAN_ROOT_FILE_PATTERNS:
        for path in task_path.glob(pattern):
            if path.is_file() and _is_text_file(path):
                files[path.relative_to(task_path).as_posix()] = path
    return [files[key] for key in sorted(files)]


def _is_third_party_or_minified(relative_path: str) -> bool:
    normalized = "/" + relative_path.replace("\\", "/")
    return normalized.endswith(".min.js") or any(hint in normalized for hint in THIRD_PARTY_HINTS)


def _classify_cjk(relative_path: str, fragments: list[str]) -> tuple[str, str]:
    return "auto_translate_candidate", "mandatory translation of Chinese/CJK content"


def _cjk_output_path(relative_path: str, output_prefix: str) -> str:
    path = Path(relative_path)
    if not path.parts:
        return relative_path
    top = path.parts[0]
    if top in {"repo", "docs"}:
        return Path(f"{output_prefix}_{top}", *path.parts[1:]).as_posix()
    if top == "metadata.json":
        return Path(f"{output_prefix}_metadata", top).as_posix()
    if top == "prompt.md":
        return Path(f"{output_prefix}_prompt", top).as_posix()
    return Path(f"{output_prefix}_{top}", *path.parts[1:]).as_posix()


def _parse_validation_targets(report_path: Path) -> dict[Path, set[int]]:
    targets: dict[Path, set[int]] = {}
    if not report_path.is_file():
        return targets
    in_chinese_section = False
    for raw in report_path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            in_chinese_section = "中文检查" in raw
            continue
        if not in_chinese_section:
            continue
        match = VALIDATION_LINE_RE.search(raw)
        if not match:
            continue
        line_numbers = [int(part) for part in re.split(r"[,，\s]+", match.group(1).strip()) if part]
        targets.setdefault(Path(match.group(2).strip()), set()).update(line_numbers)
    return targets


def scan_cjk_task(task_dir: str | Path, validation_report: str = DEFAULT_VALIDATION_REPORT, output_prefix: str = DEFAULT_OUTPUT_PREFIX, full_scan: bool = True) -> list[CjkHit]:
    task_path = Path(task_dir).expanduser().resolve()
    targets = _parse_validation_targets(task_path / ".tmp" / validation_report)
    if full_scan or not targets:
        for path in _scan_candidate_files(task_path):
            try:
                line_count = len(path.read_text(encoding="utf-8").splitlines())
            except UnicodeDecodeError:
                continue
            rel = path.relative_to(task_path)
            existing = targets.setdefault(rel, set())
            existing.update(range(1, line_count + 1))
    hits: list[CjkHit] = []
    for relative_path, line_numbers in sorted(targets.items(), key=lambda item: item[0].as_posix()):
        path = task_path / relative_path
        if not path.is_file():
            hits.append(CjkHit(task_path.name, relative_path.as_posix(), 0, "manual_review", "reported file is missing", [], "", "", _cjk_output_path(relative_path.as_posix(), output_prefix)))
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            hits.append(CjkHit(task_path.name, relative_path.as_posix(), 0, "manual_review", f"encoding error: {exc}", [], "", "", _cjk_output_path(relative_path.as_posix(), output_prefix)))
            continue
        for line_no in sorted(line_numbers):
            if line_no < 1 or line_no > len(lines):
                hits.append(CjkHit(task_path.name, relative_path.as_posix(), line_no, "manual_review", "reported line is out of range", [], "", "", _cjk_output_path(relative_path.as_posix(), output_prefix)))
                continue
            line = lines[line_no - 1]
            if not _has_cjk(line):
                continue
            fragments = _cjk_fragments(line)
            category, reason = _classify_cjk(relative_path.as_posix(), fragments)
            hits.append(CjkHit(task_path.name, relative_path.as_posix(), line_no, category, reason, fragments, _preview_cjk(line), _source_context(path, line_no), _cjk_output_path(relative_path.as_posix(), output_prefix)))
    return hits


def _call_llm_json(messages: list[dict[str, str]]) -> dict[str, Any]:
    model, base_url, api_key = _llm_config()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw_body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed: HTTP {exc.code}: {body}") from exc
    except JSONDecodeError as exc:
        preview = raw_body[:500] if "raw_body" in locals() else ""
        raise RuntimeError(f"LLM response is not JSON: {exc}; body preview={preview!r}") from exc
    content = payload["choices"][0]["message"].get("content")
    if not content:
        return {"items": []}
    try:
        return json.loads(content)
    except JSONDecodeError as exc:
        decoder = json.JSONDecoder()
        stripped = content.lstrip()
        for anchor in ("{", "["):
            start = stripped.find(anchor)
            if start < 0:
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[start:])
                return parsed
            except JSONDecodeError:
                continue
        raise RuntimeError(f"LLM message content is not JSON: {exc}; content preview={content[:500]!r}") from exc


def _build_cjk_prompt(task_dir: Path, hits: list[CjkHit]) -> list[dict[str, str]]:
    items = [
        {
            "path": hit.path,
            "line_no": hit.line_no,
            "category": hit.category,
            "reason": hit.reason,
            "fragments": hit.fragments,
            "original_trace": hit.line_preview,
            "source_context": hit.source_context,
            "proposed_output_path": hit.output_path,
        }
        for hit in hits
    ]
    payload = {
        "task": task_dir.name,
        "items": items,
        "instructions": [
            "Write the report in English. Absolutely NO Chinese characters allowed in any field of the output JSON.",
            "MANDATORY: You MUST translate all Chinese/CJK content found in 'original_trace' or 'source_context' to natural English.",
            "Each item must output: problem, change_plan, modified_trace, action, replacements.",
            "Even for technical terms, font names (e.g. '微软雅黑' -> 'Microsoft YaHei'), or third-party assets, you MUST translate them.",
            "The 'action' MUST be 'translate'.",
            "The 'replacements' must provide the exact 'from' and 'to' strings to eliminate all Chinese characters.",
            "The 'modified_trace' must be the final English version of the trace line.",
            "Do not provide any reason to keep Chinese; keeping Chinese is strictly forbidden.",
            "Return strict JSON only.",
        ],
        "required_schema": {
            "items": [
                {
                    "path": "string",
                    "line_no": "number",
                    "problem": "string",
                    "change_plan": "string",
                    "modified_trace": "string",
                    "action": "translate",
                    "replacements": [{"from": "string", "to": "string", "reason": "string"}],
                }
            ]
        },
    }
    return [
        {"role": "system", "content": "你是数据清洗报告撰写助手，只返回严格 JSON。"},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _hit_signature(hit: CjkHit) -> tuple[str, tuple[str, ...]]:
    return (hit.line_preview, tuple(hit.fragments))


def _llm_case_items(task_dir: Path, hits: list[CjkHit]) -> dict[tuple[str, int], dict[str, str]]:
    if not hits:
        return {}
    mapped: dict[tuple[str, int], dict[str, str]] = {}
    grouped_hits: list[CjkHit] = []
    grouped_members: dict[tuple[str, tuple[str, ...]], list[CjkHit]] = {}
    for hit in hits:
        signature = _hit_signature(hit)
        members = grouped_members.setdefault(signature, [])
        if not members:
            grouped_hits.append(hit)
        members.append(hit)
    for start in range(0, len(grouped_hits), CJK_LLM_BATCH_SIZE):
        batch = grouped_hits[start : start + CJK_LLM_BATCH_SIZE]
        try:
            result = _call_llm_json(_build_cjk_prompt(task_dir, batch))
        except Exception as exc:
            for hit in batch:
                fallback = {
                    "problem": f"The trace contains Chinese/CJK fragments: {', '.join(hit.fragments)}. They must be translated to English-only text.",
                    "change_plan": "The model did not return valid JSON. Re-run is required to complete forced translation.",
                    "modified_trace": f"LLM call failed; no automatic translation was applied: {exc}",
                    "action": "manual_review",
                    "replacements": [],
                }
                for member in grouped_members[_hit_signature(hit)]:
                    mapped[(member.path, member.line_no)] = fallback
            continue
        for item in result.get("items", []):
            path = item.get("path")
            line_no = item.get("line_no")
            if isinstance(path, str) and isinstance(line_no, int):
                current = next((hit for hit in batch if hit.path == path and hit.line_no == line_no), None)
                if current is None:
                    continue
                resolved = {
                    "problem": str(item.get("problem", "")),
                    "change_plan": str(item.get("change_plan", "")),
                    "modified_trace": str(item.get("modified_trace", "")),
                    "action": str(item.get("action", "manual_review")),
                    "replacements": item.get("replacements", []),
                }
                for member in grouped_members[_hit_signature(current)]:
                    mapped[(member.path, member.line_no)] = resolved
    return mapped


def _apply_llm_replacements(task_dir: Path, hit: CjkHit, llm: dict[str, Any]) -> str:
    action = str(llm.get("action", "manual_review"))
    replacements = llm.get("replacements", [])
    if action not in APPLY_ACTIONS or not isinstance(replacements, list) or not replacements:
        return str(llm.get("modified_trace", ""))
    path = task_dir / hit.path
    if not path.is_file() or hit.path.startswith("report/"):
        return str(llm.get("modified_trace", "")) + "\nSkip: file is missing or is a report."
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return str(llm.get("modified_trace", "")) + "\n未自动修改：文件编码不是 UTF-8。"
    changed = text
    applied: list[str] = []
    for replacement in replacements:
        if not isinstance(replacement, dict):
            continue
        old = str(replacement.get("from", ""))
        new = str(replacement.get("to", ""))
        reason = str(replacement.get("reason", ""))
        if not old or old == new:
            continue
        if changed.count(old) != 1:
            applied.append(f"未替换 `{old}`：匹配次数不是 1，避免误改。")
            continue
        changed = changed.replace(old, new, 1)
        applied.append(f"已替换 `{old}` -> `{new}`。{reason}")
    if changed != text:
        path.write_text(changed, encoding="utf-8")
    prefix = str(llm.get("modified_trace", "")).strip()
    suffix = "\n".join(applied)
    return (prefix + "\n" + suffix).strip() if prefix else suffix


def write_cjk_case_report(task_dir: Path, hits: list[CjkHit], report_name: str = DEFAULT_CJK_CASE_REPORT, output_dir: Path | None = None) -> Path:
    report_base = output_dir if output_dir is not None else task_dir
    report_dir = report_base / REPORT_DIR_NAME
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / report_name
    categories = sorted({hit.category for hit in hits})
    lines = ["# 中文/CJK 修复案例报告", "", f"项目 ID：{task_dir.name}", "模式：`analysis-only`", "", "汇总：", f"- 命中数量：{len(hits)}"]
    for category in categories:
        lines.append(f"- {category}: {sum(1 for hit in hits if hit.category == category)}")
    lines.append("")
    llm_items = _llm_case_items(task_dir, hits) if hits else {}
    if not hits:
        lines.extend(["# 修改1 无需中文/CJK修复", "", f"项目 ID：{task_dir.name}", "问题：未发现 validation_report.md 指出的中文/CJK 内容。", "更改方案：无需修改。", "修复方法：`cjk-no-op`", ""])
    else:
        for index, hit in enumerate(hits, 1):
            llm = llm_items.get((hit.path, hit.line_no), {})
            problem = llm.get("problem") or f"该轨迹中包含中文/CJK片段：{', '.join(hit.fragments)}，需要判断是否应翻译、保留或人工复核。"
            change_plan = llm.get("change_plan") or "当前脚本只生成分析报告，不直接修改业务文件。"
            modified_trace = _apply_llm_replacements(task_dir, hit, llm) or f"category={hit.category}; reason={hit.reason}; proposed_output_path={hit.output_path}"
            lines.extend([
                f"# 修改{index} 中文/CJK 内容复核",
                "",
                f"项目 ID：{task_dir.name}",
                f"问题：{problem}",
                f"更改方案：{change_plan}",
                "修复方法：`cjk-analysis-only`",
                "",
                f"文件和行号：{hit.path}:{hit.line_no}",
                "",
                "原始轨迹：",
                "",
                "```text",
                hit.line_preview,
                "```",
                "",
                "修改轨迹：",
                "",
                "```text",
                modified_trace,
                "```",
                "",
            ])
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report_path


def check_cjk_task(task_dir: str | Path, validation_report: str = DEFAULT_VALIDATION_REPORT, report: str = DEFAULT_CJK_CASE_REPORT, output_prefix: str = DEFAULT_OUTPUT_PREFIX, output_dir: str | Path | None = None, full_scan: bool = True) -> dict[str, Any]:
    task_path = Path(task_dir).expanduser().resolve()
    hits = scan_cjk_task(task_path, validation_report, output_prefix, full_scan=full_scan)
    resolved_output_dir = Path(output_dir).expanduser().resolve() if output_dir is not None else None
    report_path = write_cjk_case_report(task_path, hits, report, resolved_output_dir)
    return {"task": str(task_path), "hits": len(hits), "manual_review": sum(1 for hit in hits if hit.category == "manual_review"), "auto_translate_candidate": sum(1 for hit in hits if hit.category == "auto_translate_candidate"), "keep_candidate": sum(1 for hit in hits if hit.category == "keep_candidate"), "report": str(report_path), "case_report": str(report_path), "output_prefix": output_prefix}


def check_cjk_target(target: str | Path, output: str | Path | None = None, validation_report: str = DEFAULT_VALIDATION_REPORT, full_scan: bool = True) -> dict[str, Any]:
    discovered = discover_task_dirs(target)
    task_dirs = discovered
    output_root = Path(output).expanduser().resolve() if output is not None else None
    if output_root is not None and len(task_dirs) == 1:
        output_dirs = {task_dirs[0]: output_root}
    elif output_root is not None:
        output_dirs = {task_dir: output_root / task_dir.name for task_dir in task_dirs}
    else:
        output_dirs = {task_dir: None for task_dir in task_dirs}
    results = [check_cjk_task(task_dir, validation_report=validation_report, output_dir=output_dirs[task_dir], full_scan=full_scan) for task_dir in task_dirs]
    return {"total": len(results), "hits": sum(item["hits"] for item in results), "manual_review": sum(item["manual_review"] for item in results), "auto_translate_candidate": sum(item["auto_translate_candidate"] for item in results), "keep_candidate": sum(item["keep_candidate"] for item in results), "results": results}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查 Claude Code TASK 的中文/CJK 内容")
    parser.add_argument("-r", "--root", required=True, type=Path, help="TASK 目录或包含多个 TASK-* 的目录")
    parser.add_argument("-o", "--output", type=Path, help="报告输出目录；单 TASK 写入 OUTPUT/report，多 TASK 写入 OUTPUT/<TASK>/report")
    parser.add_argument("--validation-report", default=DEFAULT_VALIDATION_REPORT, help=".tmp 下的 validation report 文件名")
    parser.add_argument("--report-only", action="store_true", help="仅根据 validation report 中列出的路径和行号处理，不做全量扫描")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = check_cjk_target(args.root, args.output, validation_report=args.validation_report, full_scan=not args.report_only)
    print(f"CJK total={result['total']} hits={result['hits']} manual={result['manual_review']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Claude Code 轨迹修复脚本。

参数说明：
  -r, --root
      必填。指定待修复的 TASK 目录或 Claude Code 轨迹目录。
      如果指定 TASK 目录，脚本会自动使用 TASK/original_sessions。
  -o, --output
      可选。指定 TASK 输出目录。脚本读取该目录下的
      report/json_format_report.md，并把修复报告写回 report/。
      不传时，如果 -r 是 TASK 目录，则默认使用该 TASK 目录。

使用样例：
      python repair_session_format.py -r path/to/TASK -o path/to/TASK

修复流程：
  1. 读取 TASK/report/json_format_report.md。
  2. 将 TASK/original_sessions 重命名为 TASK/fix_original_sessions。
  3. 从 fix_original_sessions 修复，输出为新的 original_sessions。
  4. 将修复报告写入 TASK/report/。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "repair_scripts" else SCRIPT_DIR
for import_path in (SCRIPT_DIR, ROOT_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from check_session_format import DEFAULT_REPORT_NAME, REPORT_DIR_NAME, discover_task_dirs


DEFAULT_FORMAT_REPORT = "json_format_report.md"
DEFAULT_REPAIR_REPORT = "json_repair_report.md"
DEFAULT_CASE_REPORT = "json_repair_case_report.md"
DEFAULT_SOURCE = Path("original_sessions")
DEFAULT_OUTPUT = Path("original_sessions")
FIX_SOURCE = Path("fix_original_sessions")
CLI_FORMAT_REPORT = "json_format_report.fix_original_sessions.md"
BACKUP_DIR_NAME = ".backup_before_json_repair"
CASE_DIFF_CONTEXT = 70


REPAIR_CASE_DESCRIPTIONS = {
    "escape-invalid-backslashes": (
        "字符串内部存在非法反斜杠转义",
        "JSON 字符串中出现非法反斜杠转义，导致解析器报 `Invalid \\escape`。",
        "将非法裸反斜杠改为合法 JSON 转义，保留原始文本语义。",
    ),
    "escape-bare-quotes": (
        "字符串内部存在未转义双引号",
        "字符串正文中的英文双引号没有转义，导致 JSON 解析器误判字符串提前结束。",
        "将字符串正文中的裸双引号转义为 `\\\"`。",
    ),
    "root-cause-block-join": (
        "JSONL 记录被真实换行拆成多行",
        "一条 JSONL 记录被真实换行打断，导致字符串、数组或对象结构无法在同一行闭合。",
        "将属于同一条记录的物理行重新合并，并把正文中的真实换行写成 `\\n`。",
    ),
    "extract-json-values-from-nul-or-embedded-text": (
        "NUL 字节、空洞填充或嵌入文本混入 JSONL",
        "JSONL 行中混入 NUL 字节、空洞填充、坏前缀或内部事件文本，导致整行无法解析。",
        "删除无效填充并从有效锚点提取完整 JSON 对象。",
    ),
    "insert-tool-result-content": (
        "tool_result 缺少 content 字段",
        "`tool_result` 后面直接跟随裸文本，缺少合法的 `content` 字段。",
        "把裸文本包装为 `content` 字符串字段。",
    ),
    "close-truncated-tail-json": (
        "文件尾部 JSON 结构截断",
        "文件尾部记录被截断，导致字符串、数组或对象未闭合。",
        "补齐可推断的字符串、数组和对象闭合结构。",
    ),
    "drop-plain-text-jsonl-record": (
        "纯文本错误行混入 JSONL",
        "该行是纯文本错误提示，不是合法 JSONL 记录；JSONL 也不支持注释。",
        "从修复后的 JSONL 中删除该行，并将原始内容写入 `.tmp/quarantined_jsonl_records.jsonl`。",
    ),
    "split-concatenated-json": (
        "多个 JSON 对象粘在同一行",
        "一条物理行中连续写入多个完整 JSON 值，导致解析器报 `Extra data`。",
        "按 JSON 解码边界拆成多条 JSONL 记录。",
    ),
    "remove-bom": (
        "JSONL 行首存在 BOM",
        "JSONL 行首包含 UTF-8 BOM，导致该行不能作为普通 JSON 值解析。",
        "删除行首 BOM 后重新解析。",
    ),
    "remove-trailing-comma": (
        "JSONL 行尾存在多余逗号",
        "JSONL 单行末尾带有数组元素式逗号，导致该行不能单独解析。",
        "删除行尾多余逗号。",
    ),
    "fix-utf16-to-utf8": (
        "文件编码不是 UTF-8",
        "文件使用 UTF-16 或带 BOM 编码，直接按 UTF-8 读取失败。",
        "按可识别编码读取并验证 JSON 合法后，以 UTF-8 写回。",
    ),
}


@dataclass(frozen=True)
class Issue:
    path: Path
    line_no: int
    message: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="基于 report/json_format_report.md 精准修复 Claude Code 轨迹"
    )
    parser.add_argument("-r", "--root", required=True, type=Path, help="要修复的 TASK 目录或 Claude Code 轨迹目录")
    parser.add_argument("-o", "--output", type=Path, help="TASK 输出目录；报告读取和生成都在该目录下的 report 文件夹")
    args = parser.parse_args(argv)
    args.target = args.root
    return args


def _resolve_cli_paths(root: str | Path, output: str | Path | None = None) -> tuple[Path, Path, Path]:
    root_path = Path(root).expanduser().resolve()
    if root_path.is_dir() and (root_path / DEFAULT_SOURCE).is_dir():
        source_path = root_path / DEFAULT_SOURCE
        default_task = root_path
    elif root_path.is_dir() and root_path.name == DEFAULT_SOURCE.name:
        source_path = root_path
        default_task = root_path.parent
    else:
        source_path = root_path
        default_task = root_path.parent if root_path.is_file() else root_path
    task_path = Path(output).expanduser().resolve() if output else default_task
    return root_path, task_path, source_path


def _prepare_cli_source(task_path: Path, source_path: Path) -> Path:
    original = task_path / DEFAULT_SOURCE
    fixed = task_path / FIX_SOURCE
    if fixed.is_dir():
        return FIX_SOURCE
    if source_path.resolve() == original.resolve() and original.is_dir():
        original.rename(fixed)
        return FIX_SOURCE
    if source_path.is_dir() and source_path.name == FIX_SOURCE.name and source_path.parent.resolve() == task_path.resolve():
        return FIX_SOURCE
    raise FileNotFoundError(f"missing source directory: {original}")


def _rewrite_report_issue_path(path_text: str, task_path: Path, source: Path) -> str:
    path = Path(path_text)
    if path.is_absolute():
        try:
            relative = path.relative_to(task_path)
        except ValueError:
            try:
                relative = path.relative_to(task_path / DEFAULT_SOURCE)
                relative = DEFAULT_SOURCE / relative
            except ValueError:
                relative = Path(path.name)
    else:
        relative = path

    if relative.parts and relative.parts[0] in {DEFAULT_SOURCE.name, FIX_SOURCE.name}:
        return Path(source, *relative.parts[1:]).as_posix()
    return Path(source, relative).as_posix()


def _prepare_cli_format_report(task_path: Path, source: Path) -> Path:
    report_path = task_path / REPORT_DIR_NAME / DEFAULT_REPORT_NAME
    if not report_path.is_file():
        raise FileNotFoundError(report_path)

    tmp = task_path / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / CLI_FORMAT_REPORT
    lines: list[str] = []
    for raw in report_path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("- "):
            payload = raw[2:]
            try:
                path_text, line_no, message = payload.rsplit(":", 2)
            except ValueError:
                lines.append(raw)
                continue
            fixed_path = _rewrite_report_issue_path(path_text, task_path, source)
            lines.append(f"- {fixed_path}:{line_no}: {message.strip()}")
        else:
            lines.append(raw)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _move_cli_report(path_text: str, report_dir: Path) -> str:
    path = Path(path_text)
    if not path.is_file():
        return path_text
    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / path.name
    if target.exists():
        target.unlink()
    shutil.move(str(path), str(target))
    return str(target)


def repair_cli_target(root: str | Path, output: str | Path | None = None) -> dict[str, Any]:
    _, task_path, source_path = _resolve_cli_paths(root, output)
    source = _prepare_cli_source(task_path, source_path)
    format_report_path = _prepare_cli_format_report(task_path, source)
    result = repair_task(
        task_path,
        apply=True,
        source=source,
        output=DEFAULT_SOURCE,
        in_place=False,
        format_report=format_report_path.name,
        repair_report=DEFAULT_REPAIR_REPORT,
    )
    report_dir = task_path / REPORT_DIR_NAME
    result["report"] = _move_cli_report(result["report"], report_dir)
    result["case_report"] = _move_cli_report(result["case_report"], report_dir)
    try:
        format_report_path.unlink()
    except OSError:
        pass
    return result


def read_report(task_dir: Path, report_name: str = DEFAULT_FORMAT_REPORT) -> list[Issue]:
    report = task_dir / ".tmp" / report_name
    issues: list[Issue] = []
    if not report.is_file():
        raise FileNotFoundError(report)
    issue_re = re.compile(r"^- (.*):(\d+): (.*)$")
    encoding_re = re.compile(r"^- (.*): encoding error: (.*)$")
    for raw in report.read_text(encoding="utf-8").splitlines():
        match = issue_re.match(raw)
        if match:
            path_text, line_no, message = match.groups()
            issues.append(Issue(task_dir / path_text, int(line_no), message.strip()))
            continue
        match = encoding_re.match(raw)
        if match:
            path_text, message = match.groups()
            issues.append(Issue(task_dir / path_text, 0, f"encoding error: {message.strip()}"))
    return issues


def _single_file_format_report_name(path: Path, report_name: str) -> str:
    if report_name != DEFAULT_FORMAT_REPORT:
        return report_name
    return f"json_format_report.{path.name}.md"


def _single_file_repair_report_name(path: Path, report_name: str) -> str:
    if report_name != DEFAULT_REPAIR_REPORT:
        return report_name
    return f"json_repair_report.{path.name}.md"


def _single_file_output_path(input_file: Path, output: Path) -> Path:
    output = Path(output).expanduser()
    if output.suffix.lower() in {".json", ".jsonl"}:
        return output.resolve()
    if output.is_absolute():
        return output / input_file.name
    return input_file.parent / output / input_file.name


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    pos = index - 1
    while pos >= 0 and text[pos] == "\\":
        backslashes += 1
        pos -= 1
    return backslashes % 2 == 1


def _loads_ok(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def split_concatenated_objects(line: str) -> list[str] | None:
    parts: list[str] = []
    start = 0
    depth = 0
    in_string = False
    for index, char in enumerate(line):
        if char == '"' and not _is_escaped(line, index):
            in_string = not in_string
        if in_string:
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                part = line[start : index + 1].strip()
                if part:
                    parts.append(part)
                start = index + 1
    tail = line[start:].strip()
    if tail or len(parts) <= 1:
        return None
    if all(_loads_ok(part) for part in parts):
        return parts
    return None


def _escape_quote_candidates(line: str, error_pos: int) -> list[str]:
    candidates: list[str] = []
    quote_indexes = [i for i, char in enumerate(line) if char == '"' and not _is_escaped(line, i)]
    near = sorted(quote_indexes, key=lambda i: abs(i - error_pos))[:20]
    for index in near + quote_indexes:
        if index <= 0:
            continue
        candidate = f"{line[:index]}\\{line[index:]}"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def escape_bare_quotes_by_context(line: str) -> str:
    result: list[str] = []
    in_string = False
    index = 0
    while index < len(line):
        char = line[index]
        if char != '"' or _is_escaped(line, index):
            result.append(char)
            index += 1
            continue
        if not in_string:
            in_string = True
            result.append(char)
            index += 1
            continue
        next_index = index + 1
        while next_index < len(line) and line[next_index] in " \t\r\n":
            next_index += 1
        next_char = line[next_index] if next_index < len(line) else ""
        if next_char in {":", ",", "}", "]", ""}:
            in_string = False
            result.append(char)
        else:
            result.append('\\"')
        index += 1
    return "".join(result)


def repair_line(line: str) -> tuple[list[str] | None, str]:
    stripped = line.rstrip("\n\r")
    if not stripped.strip():
        return [stripped], "blank"
    if _loads_ok(stripped):
        return [stripped], "already-valid"
    if stripped.startswith("\ufeff") and _loads_ok(stripped.lstrip("\ufeff")):
        return [stripped.lstrip("\ufeff")], "remove-bom"
    split = split_concatenated_objects(stripped)
    if split:
        return split, "split-concatenated-json"
    if stripped.endswith(",") and _loads_ok(stripped[:-1]):
        return [stripped[:-1]], "remove-trailing-comma"
    try:
        json.loads(stripped)
    except json.JSONDecodeError as exc:
        contextual = escape_bare_quotes_by_context(stripped)
        if contextual != stripped and _loads_ok(contextual):
            return [contextual], "escape-bare-quotes-by-context"
        for candidate in _escape_quote_candidates(stripped, exc.pos):
            if _loads_ok(candidate):
                return [candidate], "escape-unescaped-quote"
    return None, "unresolved"


def repair_line_iteratively(line: str, max_steps: int = 200) -> tuple[str | None, str]:
    current = line.rstrip("\n\r")
    methods: list[str] = []
    for _ in range(max_steps):
        try:
            json.loads(current)
            return current, "+".join(methods) if methods else "already-valid"
        except json.JSONDecodeError as exc:
            fixed, method = repair_line(current)
            if fixed and len(fixed) == 1 and fixed[0] != current:
                current = fixed[0]
                methods.append(method)
                continue
            candidates = _escape_quote_candidates(current, exc.pos)
            applied = False
            for candidate in candidates[:8]:
                if candidate == current:
                    continue
                try:
                    json.loads(candidate)
                    return candidate, "+".join(methods + ["escape-near-error-quote"])
                except json.JSONDecodeError as next_exc:
                    # Accept a local quote escape if the parser moves forward;
                    # this supports iterative repair of multiple bare quotes.
                    if next_exc.pos > exc.pos:
                        current = candidate
                        methods.append("escape-near-error-quote")
                        applied = True
                        break
            if applied:
                continue
            return None, "unresolved"
    return None, "max-steps-exceeded"


def escape_invalid_json_backslashes(text: str) -> str:
    valid = {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}
    result: list[str] = []
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                if char in valid:
                    result.append("\\")
                    result.append(char)
                else:
                    result.append("\\\\")
                    result.append(char)
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
                result.append(char)
            else:
                result.append(char)
        else:
            result.append(char)
            if char == '"':
                in_string = True
    if escape:
        result.append("\\\\")
    return "".join(result)


def _is_session_level_value(value: object) -> bool:
    return isinstance(value, dict) and (
        "parentUuid" in value
        or "sessionId" in value
        or value.get("type") in {"queue-operation", "file-history-snapshot"}
    )


def extract_json_values_from_text(text: str) -> list[str]:
    cleaned = text.replace("\x00", "")
    values: list[str] = []
    decoder = json.JSONDecoder()
    anchors = ['{"parentUuid"', '{"type":"queue-operation"', '{"type":"file-history-snapshot"']
    starts = sorted({pos for anchor in anchors for pos in [cleaned.find(anchor)] if pos != -1})
    # find() only returns the first anchor; collect all occurrences explicitly.
    starts = []
    for anchor in anchors:
        pos = cleaned.find(anchor)
        while pos != -1:
            starts.append(pos)
            pos = cleaned.find(anchor, pos + 1)
    for index in sorted(set(starts)):
        try:
            value, end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if _is_session_level_value(value):
            values.append(cleaned[index : index + end])
    return values


def repair_nul_or_embedded_json_values(text: str) -> tuple[list[str] | None, str]:
    if "\x00" not in text and text.count('{"parentUuid"') <= 1 and text.count('{"type":"queue-operation"') <= 1:
        return None, "not-applicable"
    values = extract_json_values_from_text(text)
    if values and all(_loads_ok(value) for value in values):
        return values, "extract-json-values-from-nul-or-embedded-text"
    return None, "unresolved"


def repair_missing_tool_result_content(text: str) -> tuple[str | None, str]:
    marker = '"type":"tool_result",'
    pos = text.find(marker)
    if pos == -1:
        return None, "not-applicable"
    insert_pos = pos + len(marker)
    suffix = text[insert_pos:]
    # Find the rest of the outer record metadata after the tool_result object.
    tail_marker = '}]} ,'
    # Actual logs do not contain the extra space, but keep the search simple and
    # robust by using the known parent array/object close sequence.
    tail_pos = suffix.find('}]}')
    if tail_pos == -1:
        return None, "unresolved"
    raw_content = suffix[:tail_pos].strip()
    tail = suffix[tail_pos:]
    if raw_content.startswith('"content"'):
        return None, "not-applicable"
    candidate = text[:insert_pos] + '"content":' + json.dumps(raw_content, ensure_ascii=False) + tail
    if _loads_ok(candidate):
        return candidate, "add-missing-tool-result-content"
    return None, "unresolved"


def is_plain_text_jsonl_garbage(text: str) -> bool:
    """Return True for non-empty JSONL garbage that should be dropped.

    JSONL does not support comments. Plain text fragments are quarantined outside
    the repaired session instead of being wrapped as synthetic system events.
    """
    stripped = text.strip()
    if not stripped or "\x00" in text:
        return False
    if stripped[0] in "{[":
        return False
    return True


def close_truncated_tail_json(text: str) -> tuple[str | None, str]:
    if not text.lstrip().startswith('{"parentUuid"'):
        return None, "not-applicable"
    current = text.rstrip("\n\r")
    suffixes = [
        '"}]}',
        '"}]} }',
        '"}]} } }',
        '"}]}},"type":"assistant"}',
    ]
    # Common Claude assistant text shape: message.content[0].text is open.
    for suffix in [
        '"}]} ,"stop_reason":null,"stop_sequence":null,"stop_details":null,"usage":{}} ,"type":"assistant"}',
        '"}]} ,"stop_reason":null,"stop_sequence":null,"usage":{}} ,"type":"assistant"}',
        '"}]} ,"type":"assistant"}',
        '"}]} ,"type":"assistant"}',
    ]:
        candidate = current + suffix.replace(" ", "")
        if _loads_ok(candidate):
            return candidate, "close-truncated-tail-json"

    # General structural closer: close string if needed, then close containers.
    in_string = False
    escape = False
    stack: list[str] = []
    for char in current:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]" and stack and stack[-1] == char:
            stack.pop()
    candidate = current + ('"' if in_string else "") + "".join(reversed(stack))
    if _loads_ok(candidate):
        return candidate, "close-truncated-tail-json"
    return None, "unresolved"


def repair_encoding_file(path: Path) -> tuple[str | None, str]:
    data = path.read_bytes()
    encodings = ["utf-16", "utf-16-le", "utf-16-be", "utf-8-sig"]
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            json.loads(text)
            return text, f"transcode-{encoding}-to-utf-8"
        except json.JSONDecodeError:
            continue
    return None, "encoding-unresolved"


def _verify_lines(lines: list[str]) -> list[str]:
    issues: list[str] = []
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"{line_no}: {exc.msg}")
    return issues


def rebuild_jsonl_records_by_state(lines: list[str]) -> tuple[list[str] | None, list[dict[str, Any]]]:
    text = "\n".join(lines)
    records: list[str] = []
    repairs: list[dict[str, Any]] = []
    buffer: list[str] = []
    depth = 0
    in_string = False
    escape = False
    line_no = 1
    record_start_line = 0
    record_had_newline = False
    record_had_quote_fix = False
    index = 0

    def next_nonspace(pos: int) -> str:
        pos += 1
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        return text[pos] if pos < len(text) else ""

    while index < len(text):
        char = text[index]

        if depth == 0 and not buffer:
            if char in " \t\r\n":
                if char == "\n":
                    line_no += 1
                index += 1
                continue
            if char != "{":
                return None, []
            record_start_line = line_no
            record_had_newline = False
            record_had_quote_fix = False

        if char == "\n":
            if in_string:
                buffer.append("\\n")
                record_had_newline = True
            elif depth > 0:
                record_had_newline = True
            line_no += 1
            index += 1
            continue

        if in_string:
            if escape:
                buffer.append(char)
                escape = False
            elif char == "\\":
                buffer.append(char)
                escape = True
            elif char == '"':
                following = next_nonspace(index)
                if following in {":", ",", "}", "]", ""}:
                    buffer.append(char)
                    in_string = False
                else:
                    buffer.append('\\"')
                    record_had_quote_fix = True
            else:
                buffer.append(char)
            index += 1
            continue

        buffer.append(char)
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                record = "".join(buffer).strip()
                fixed_record = record
                method_parts = []
                if record_had_newline:
                    method_parts.append("state-join-multiline-jsonl")
                if record_had_quote_fix:
                    method_parts.append("escape-bare-quotes-by-state")
                if not _loads_ok(fixed_record):
                    fixed, method = repair_line(fixed_record)
                    if not fixed or len(fixed) != 1 or not _loads_ok(fixed[0]):
                        return None, []
                    fixed_record = fixed[0]
                    method_parts.append(method)
                records.append(fixed_record)
                if method_parts:
                    repairs.append(
                        {
                            "line_no": record_start_line,
                            "end_line_no": line_no,
                            "method": "+".join(method_parts),
                        }
                    )
                buffer = []
        index += 1

    if buffer or depth != 0 or in_string:
        return None, []
    if repairs and not _verify_lines(records):
        return None, []
    if repairs:
        return records, repairs
    return None, []


def repair_reported_multiline_jsonl_records(
    lines: list[str], file_issues: list[Issue]
) -> tuple[list[str] | None, list[dict[str, Any]]]:
    # Keep this fallback deliberately small. For heavily corrupted records,
    # repeated parse/quote-repair attempts over hundreds of physical lines are
    # slower than useful and should become a manual/AI repair item instead.
    max_join_lines = 200
    start_candidates = sorted(
        issue.line_no
        for issue in file_issues
        if "Unterminated string" in issue.message or "Unterminated" in issue.message
    )
    if not start_candidates:
        return None, []

    repaired = list(lines)
    offset = 0
    repairs: list[dict[str, Any]] = []
    for start_line in start_candidates:
        start = start_line - 1 + offset
        if start < 0 or start >= len(repaired) or _loads_ok(repaired[start]):
            continue
        accumulator = repaired[start]
        fixed_record: str | None = None
        end = start
        for cursor in range(start + 1, len(repaired)):
            if cursor - start > max_join_lines:
                break
            accumulator = f"{accumulator}\\n{repaired[cursor]}"
            if _loads_ok(accumulator):
                fixed_record = accumulator
                end = cursor
                break
            fixed, method = repair_line(accumulator)
            if fixed and len(fixed) == 1 and _loads_ok(fixed[0]):
                fixed_record = fixed[0]
                end = cursor
                repairs.append(
                    {
                        "line_no": start_line,
                        "end_line_no": start_line + (cursor - start),
                        "method": f"join-reported-multiline-jsonl+{method}",
                    }
                )
                break
        if fixed_record is None:
            continue
        repaired[start : end + 1] = [fixed_record]
        if not repairs or repairs[-1].get("line_no") != start_line:
            repairs.append(
                {
                    "line_no": start_line,
                    "end_line_no": start_line + (end - start),
                    "method": "join-reported-multiline-jsonl",
                }
            )
        offset -= end - start

    if repairs and not _verify_lines(repaired):
        return repaired, repairs
    if repairs:
        return repaired, repairs
    return None, []


def repair_first_root_issue_block(lines: list[str], file_issues: list[Issue]) -> tuple[list[str] | None, list[dict[str, Any]]]:
    root_issues = sorted(file_issues, key=lambda issue: issue.line_no)
    if not root_issues:
        return None, []
    start_line = root_issues[0].line_no
    if not (
        "Unterminated" in root_issues[0].message
        or "Expecting ',' delimiter" in root_issues[0].message
        or "Expecting ':' delimiter" in root_issues[0].message
        or "Expecting value" in root_issues[0].message
        or "Expecting property name enclosed in double quotes" in root_issues[0].message
    ):
        return None, []
    start = start_line - 1
    if start < 0 or start >= len(lines):
        return None, []

    end = len(lines)
    for cursor in range(start + 1, len(lines)):
        if not lines[cursor].startswith('{"parentUuid"'):
            continue
        try:
            json.loads(lines[cursor])
            end = cursor
            break
        except json.JSONDecodeError:
            continue

    candidate = "\\n".join(lines[start:end])
    extracted, extracted_method = repair_nul_or_embedded_json_values(candidate)
    if extracted:
        repaired = lines[:start] + extracted + lines[end:]
        remaining = _verify_lines(repaired)
        if len(remaining) < len(file_issues):
            return repaired, [
                {
                    "line_no": start_line,
                    "end_line_no": end,
                    "method": extracted_method,
                }
            ]

    tool_fixed, tool_method = repair_missing_tool_result_content(candidate)
    if tool_fixed:
        repaired = lines[:start] + [tool_fixed] + lines[end:]
        remaining = _verify_lines(repaired)
        if len(remaining) < len(file_issues):
            return repaired, [
                {
                    "line_no": start_line,
                    "end_line_no": end,
                    "method": tool_method,
                }
            ]

    if is_plain_text_jsonl_garbage(candidate):
        repaired = lines[:start] + lines[end:]
        remaining = _verify_lines(repaired)
        if len(remaining) < len(file_issues):
            return repaired, [
                {
                    "line_no": start_line,
                    "end_line_no": end,
                    "method": "drop-plain-text-jsonl-record",
                }
            ]

    closed, close_method = close_truncated_tail_json(candidate)
    if closed:
        repaired = lines[:start] + [closed] + lines[end:]
        remaining = _verify_lines(repaired)
        if len(remaining) < len(file_issues):
            return repaired, [
                {
                    "line_no": start_line,
                    "end_line_no": end,
                    "method": close_method,
                }
            ]

    fixed, method = repair_line_iteratively(candidate)
    if fixed is None:
        return None, []
    repaired = lines[:start] + [fixed] + lines[end:]
    if _verify_lines(repaired):
        return repaired, [
            {
                "line_no": start_line,
                "end_line_no": end,
                "method": f"root-cause-block-join+{method}",
            }
        ]
    return repaired, [
        {
            "line_no": start_line,
            "end_line_no": end,
            "method": f"root-cause-block-join+{method}+needs-next-iteration",
        }
    ]


def quarantine_first_root_issue_block(
    task_path: Path,
    path: Path,
    lines: list[str],
    file_issues: list[Issue],
) -> tuple[list[str] | None, list[dict[str, Any]]]:
    root_issues = sorted(file_issues, key=lambda issue: issue.line_no)
    if not root_issues:
        return None, []
    if not ("Unterminated" in root_issues[0].message or "Expecting" in root_issues[0].message):
        return None, []
    start_line = root_issues[0].line_no
    start = start_line - 1
    if start < 0 or start >= len(lines):
        return None, []
    end = len(lines)
    for cursor in range(start + 1, len(lines)):
        if not lines[cursor].startswith('{"parentUuid"'):
            continue
        try:
            json.loads(lines[cursor])
            end = cursor
            break
        except json.JSONDecodeError:
            continue
    repaired = lines[:start] + lines[end:]
    remaining = _verify_lines(repaired)
    if remaining:
        return None, []
    quarantine_path = task_path / ".tmp" / "quarantined_jsonl_records.jsonl"
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    with quarantine_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "task": task_path.name,
                    "file": path.relative_to(task_path).as_posix(),
                    "line_no": start_line,
                    "end_line_no": end,
                    "reason": "unresolved root JSONL block quarantined",
                    "content": "\n".join(lines[start:end]),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return repaired, [
        {
            "line_no": start_line,
            "end_line_no": end,
            "method": "quarantine-unresolved-root-block",
        }
    ]


def _copy_source(task_dir: Path, source: Path, output: Path, apply: bool) -> None:
    src = task_dir / source
    dst = task_dir / output
    if not apply or src.resolve() == dst.resolve():
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _backup_file(task_dir: Path, path: Path) -> Path:
    rel = path.relative_to(task_dir)
    backup = task_dir / BACKUP_DIR_NAME / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def _write_repair_report(task_dir: Path, report_name: str, result: dict[str, Any]) -> Path:
    lines = [
        "# JSON Repair Report",
        "",
        f"TASK: `{Path(result['task']).name}`",
        f"Mode: `{'apply' if result['apply'] else 'dry-run'}`",
        f"Source: `{result['source']}`",
        f"Output: `{result['output']}`",
        f"In place: `{result['in_place']}`",
        "",
        "## Summary",
        "",
        f"- report_issues: {result['report_issues']}",
        f"- repairable_lines: {result['repairable_lines']}",
        f"- repaired_files: {result['repaired_files']}",
        f"- unresolved: {len(result['unresolved'])}",
        "",
        "## Repaired",
        "",
    ]
    if result["repairs"]:
        for repair in result["repairs"]:
            lines.append(f"- {repair['path']}:{repair['line_no']} `{repair['method']}`")
    else:
        lines.append("无")
    lines.extend(["", "## Unresolved", ""])
    if result["unresolved"]:
        lines.extend(f"- {item}" for item in result["unresolved"])
    else:
        lines.append("无")
    lines.extend(["", "## Backups", ""])
    if result["backups"]:
        lines.extend(f"- {item}" for item in result["backups"])
    else:
        lines.append("无")
    lines.extend(["", "## Quarantined", ""])
    if result.get("quarantined"):
        lines.extend(f"- {item}" for item in result["quarantined"])
    else:
        lines.append("无")
    report_path = task_dir / ".tmp" / report_name
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def append_quarantined_records(task_path: Path, file_path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    quarantine_path = task_path / ".tmp" / "quarantined_jsonl_records.jsonl"
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    rel = file_path.relative_to(task_path).as_posix()
    with quarantine_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    {
                        "task": task_path.name,
                        "file": rel,
                        **record,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _method_key(method: str) -> str:
    for key in REPAIR_CASE_DESCRIPTIONS:
        if key in method:
            return key
    if "escape-near-error-quote" in method or "escape-unescaped-quote" in method:
        return "escape-bare-quotes"
    return method


def _read_line(path: Path, line_no: int | str) -> str:
    if isinstance(line_no, int):
        start_line = end_line = line_no
    else:
        parts = str(line_no).split("-", 1)
        if not parts[0].isdigit():
            return ""
        start_line = int(parts[0])
        end_line = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else start_line
    if start_line < 1 or end_line < start_line or not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if start_line > len(lines):
        return ""
    return "\n".join(lines[start_line - 1 : min(end_line, len(lines))])


def _read_quarantine_items(task_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    path = task_dir / ".tmp" / "quarantined_jsonl_records.jsonl"
    items: dict[tuple[str, int], dict[str, Any]] = {}
    if not path.is_file():
        return items
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        file = item.get("file")
        line_no = item.get("line_no")
        if isinstance(file, str) and isinstance(line_no, int):
            items[(file, line_no)] = item
    return items


def _merge_spans(spans: list[tuple[int, int]], text_len: int) -> list[tuple[int, int]]:
    if not spans:
        return []
    normalized = sorted((max(0, start), min(text_len, end)) for start, end in spans)
    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1] + 20:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _diff_snippet(original: str, repaired: str, context: int = CASE_DIFF_CONTEXT) -> tuple[str, str]:
    if not original or not repaired or original == repaired:
        return original[:4000], repaired[:4000]
    matcher = difflib.SequenceMatcher(a=original, b=repaired, autojunk=True)
    original_spans: list[tuple[int, int]] = []
    repaired_spans: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        original_spans.append((i1 - context, i2 + context))
        repaired_spans.append((j1 - context, j2 + context))

    def build(text: str, spans: list[tuple[int, int]]) -> str:
        parts: list[str] = []
        previous_end = 0
        for start, end in _merge_spans(spans, len(text)):
            if parts and start > previous_end:
                parts.append("\n...\n")
            elif start > 0 and not parts:
                parts.append("...\n")
            parts.append(text[start:end])
            previous_end = end
        if previous_end < len(text):
            parts.append("\n...")
        return "".join(parts) if parts else text[:4000]

    return build(original, original_spans), build(repaired, repaired_spans)


def _write_repair_case_report(
    task_dir: Path,
    result: dict[str, Any],
    report_name: str = DEFAULT_CASE_REPORT,
    append: bool = False,
) -> Path:
    repairs = result.get("repairs") or []
    quarantine_items = _read_quarantine_items(task_dir)
    source = Path(str(result.get("source", DEFAULT_SOURCE.as_posix())))
    output = Path(str(result.get("output", DEFAULT_OUTPUT.as_posix())))
    by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for repair in repairs:
        by_method[_method_key(str(repair.get("method", "")))].append(repair)

    report_path = task_dir / ".tmp" / report_name
    report_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# JSON 修复案例报告",
        "",
        f"项目 ID：{task_dir.name}",
        f"源目录：`{source.as_posix()}`",
        f"输出目录：`{output.as_posix()}`",
        f"模式：`{'apply' if result.get('apply') else 'dry-run'}`",
        "",
        "汇总：",
        f"- 报告问题数：{result.get('report_issues', 0)}",
        f"- 修复项数：{result.get('repairable_lines', 0)}",
        f"- 修复文件数：{result.get('repaired_files', 0)}",
        f"- 未解决问题数：{len(result.get('unresolved') or [])}",
        "",
    ]

    if not by_method:
        if append:
            return report_path
        lines.append("未产生 JSON 修复案例。")
    for number, (method_key, method_repairs) in enumerate(sorted(by_method.items()), 1):
        title, problem, solution = REPAIR_CASE_DESCRIPTIONS.get(
            method_key,
            ("其他 JSON 修复", "该问题由 JSON 解析报告定位，属于未单独归类的修复类型。", "按修复脚本中的对应策略处理。"),
        )
        lines.extend(
            [
                f"# 修改{number} {title}",
                "",
                f"项目 ID：{task_dir.name}",
                f"问题：{problem}",
                f"更改方案：{solution}",
                f"修复方法：`{method_key}`",
                "",
            ]
        )
        for repair in method_repairs:
            output_rel = Path(str(repair.get("path", "")))
            line_no = repair.get("line_no")
            source_rel = Path(*((source.parts) + output_rel.parts[len(output.parts) :])) if output_rel.parts[: len(output.parts)] == output.parts else output_rel
            original_line = _read_line(task_dir / source_rel, line_no)
            if not original_line and result.get("in_place"):
                original_line = _read_line(task_dir / BACKUP_DIR_NAME / source_rel, line_no)
            repaired_line = _read_line(task_dir / output_rel, line_no)
            quarantine = quarantine_items.get((output_rel.as_posix(), int(line_no) if isinstance(line_no, int) else -1))
            if quarantine:
                original_line = str(quarantine.get("content", original_line))
                repaired_line = "该行已从修复后的 JSONL 中删除；原始内容已写入 `.tmp/quarantined_jsonl_records.jsonl`。"
                original_snippet, repaired_snippet = original_line, repaired_line
            else:
                original_snippet, repaired_snippet = _diff_snippet(original_line, repaired_line)
            lines.extend(
                [
                    f"文件和行号：{source_rel.as_posix()}:{line_no}",
                    "",
                    "原始轨迹：",
                    "",
                    "```text",
                    original_snippet if original_snippet else "未能读取原始轨迹。",
                    "```",
                    "",
                    "修改轨迹：",
                    "",
                    "```text",
                    repaired_snippet if repaired_snippet else "未能读取修改轨迹，可能是多行合并或删除型修复。",
                    "```",
                    "",
                ]
            )

    content = "\n".join(lines).rstrip() + "\n"
    if append and report_path.is_file():
        previous = report_path.read_text(encoding="utf-8").rstrip()
        content = previous + "\n\n---\n\n" + content
    report_path.write_text(content, encoding="utf-8")
    return report_path


def write_no_repair_case_report(task_dir: str | Path, checked: int, status: str = "passed") -> Path:
    task_path = Path(task_dir).expanduser().resolve()
    report_dir = task_path / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / DEFAULT_CASE_REPORT
    lines = [
        "# JSON 修复案例报告",
        "",
        f"项目 ID：{task_path.name}",
        "源目录：`original_sessions`",
        "输出目录：`original_sessions`",
        "模式：`check-only`",
        "",
        "汇总：",
        "- 报告问题数：0",
        "- 修复项数：0",
        "- 修复文件数：0",
        "- 未解决问题数：0",
        "",
        "# 修改1 无需 JSON 修复",
        "",
        f"项目 ID：{task_path.name}",
        f"问题：original_sessions 中已检查 {checked} 个 JSON/JSONL 文件，检查状态为 `{status}`，未发现 JSON 语法错误。",
        "更改方案：无需修改 session 轨迹文件。",
        "修复方法：`no-repair-needed`",
        "",
        "原始轨迹：",
        "",
        "```text",
        "所有已发现的 .json/.jsonl 文件均可被 JSON parser 正常解析。",
        "```",
        "",
        "修改轨迹：",
        "",
        "```text",
        "未修改 original_sessions。",
        "```",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def repair_task(
    task_dir: str | Path,
    apply: bool = False,
    source: Path = DEFAULT_SOURCE,
    output: Path = DEFAULT_OUTPUT,
    in_place: bool = False,
    format_report: str = DEFAULT_FORMAT_REPORT,
    repair_report: str = DEFAULT_REPAIR_REPORT,
) -> dict[str, Any]:
    task_path = Path(task_dir).expanduser().resolve()
    source_path = task_path / source
    output_relative = source if in_place else output
    output_path = task_path / output_relative
    issues = read_report(task_path, format_report)

    result: dict[str, Any] = {
        "task": str(task_path),
        "apply": apply,
        "source": source.as_posix(),
        "output": output_relative.as_posix(),
        "in_place": in_place,
        "report_issues": len(issues),
        "repairable_lines": 0,
        "repaired_files": 0,
        "repairs": [],
        "unresolved": [],
        "backups": [],
        "quarantined": [],
        "report": "",
        "status": "passed",
    }

    if not source_path.is_dir():
        result["status"] = "skipped"
        result["unresolved"].append(f"missing source directory: {source.as_posix()}")
        result["report"] = str(_write_repair_report(task_path, repair_report, result))
        result["case_report"] = str(_write_repair_case_report(task_path, result, append=in_place))
        return result

    _copy_source(task_path, source, output_relative, apply)
    by_file: dict[Path, list[Issue]] = defaultdict(list)
    for issue in issues:
        try:
            rel = issue.path.relative_to(task_path)
        except ValueError:
            result["unresolved"].append(f"outside task path: {issue.path}")
            continue
        if not rel.parts or rel.parts[0] != source.parts[0]:
            result["unresolved"].append(f"outside source: {rel.as_posix()}")
            continue
        target_rel = Path(*((output_relative.parts) + rel.parts[len(source.parts) :]))
        by_file[task_path / target_rel].append(Issue(task_path / target_rel, issue.line_no, issue.message))

    for path, file_issues in sorted(by_file.items(), key=lambda item: str(item[0])):
        if any(issue.line_no == 0 and issue.message.startswith("encoding error") for issue in file_issues):
            read_path = path if apply or in_place else task_path / source / path.relative_to(task_path / output_relative)
            if not read_path.is_file():
                result["unresolved"].append(f"missing file: {read_path.relative_to(task_path).as_posix()}")
                continue
            fixed_text, method = repair_encoding_file(read_path)
            if fixed_text is None:
                result["unresolved"].append(f"encoding unresolved: {path.relative_to(task_path).as_posix()}")
                continue
            result["repaired_files"] += 1
            result["repairable_lines"] += 1
            result["repairs"].append({"path": path.relative_to(task_path).as_posix(), "line_no": 0, "method": method})
            if apply:
                if in_place:
                    backup = _backup_file(task_path, path)
                    result["backups"].append(backup.relative_to(task_path).as_posix())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(fixed_text, encoding="utf-8")
            continue

        if path.suffix.lower() != ".jsonl":
            for issue in file_issues:
                result["unresolved"].append(f"non-jsonl unsupported: {path.relative_to(task_path).as_posix()}:{issue.line_no}")
            continue
        read_path = path if apply or in_place else task_path / source / path.relative_to(task_path / output_relative)
        if not read_path.is_file():
            result["unresolved"].append(f"missing file: {read_path.relative_to(task_path).as_posix()}")
            continue
        lines = read_path.read_text(encoding="utf-8").splitlines()

        multiline_lines, multiline_repairs = repair_first_root_issue_block(lines, file_issues)
        if multiline_lines is None:
            multiline_lines, multiline_repairs = rebuild_jsonl_records_by_state(lines)
        if multiline_lines is None:
            multiline_lines, multiline_repairs = repair_reported_multiline_jsonl_records(lines, file_issues)
        if multiline_lines is None:
            multiline_lines, multiline_repairs = quarantine_first_root_issue_block(task_path, path, lines, file_issues)
        if multiline_lines is not None:
            remaining = _verify_lines(multiline_lines)
            if not remaining:
                result["repaired_files"] += 1
                result["repairable_lines"] += len(multiline_repairs)
                for repair in multiline_repairs:
                    if repair["line_no"] == repair["end_line_no"]:
                        label = str(repair["line_no"])
                    else:
                        label = f"{repair['line_no']}-{repair['end_line_no']}"
                    result["repairs"].append(
                        {
                            "path": path.relative_to(task_path).as_posix(),
                            "line_no": label,
                            "method": repair["method"],
                        }
                    )
                    if repair["method"].startswith("quarantine-"):
                        result["quarantined"].append(
                            f"{path.relative_to(task_path).as_posix()}: lines {label}"
                        )
                if apply:
                    if in_place:
                        backup = _backup_file(task_path, path)
                        result["backups"].append(backup.relative_to(task_path).as_posix())
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("\n".join(multiline_lines) + "\n", encoding="utf-8")
                continue

        if any("Unterminated" in issue.message for issue in file_issues):
            result["unresolved"].append(
                f"unresolved multiline JSONL record: {path.relative_to(task_path).as_posix()} "
                "(state-machine rebuild failed; skipped slow per-line fallback)"
            )
            continue

        replacements: dict[int, list[str]] = {}
        methods: dict[int, str] = {}
        unresolved_indexes: set[int] = set()
        dropped_records: list[dict[str, Any]] = []
        for issue in file_issues:
            index = issue.line_no - 1
            if index < 0 or index >= len(lines):
                result["unresolved"].append(f"missing line: {path.relative_to(task_path).as_posix()}:{issue.line_no}")
                continue
            if "Invalid \\escape" in issue.message:
                candidate = escape_invalid_json_backslashes(lines[index])
                if _loads_ok(candidate):
                    fixed, method = [candidate], "escape-invalid-backslashes"
                else:
                    iterative, iterative_method = repair_line_iteratively(lines[index])
                    fixed, method = ([iterative], iterative_method) if iterative is not None else repair_line(lines[index])
            elif (
                "Expecting ',' delimiter" in issue.message
                or "Expecting ':' delimiter" in issue.message
                or "Expecting property name enclosed in double quotes" in issue.message
                or "Extra data" in issue.message
            ):
                iterative, iterative_method = repair_line_iteratively(lines[index])
                fixed, method = ([iterative], iterative_method) if iterative is not None else repair_line(lines[index])
            else:
                extracted, extracted_method = repair_nul_or_embedded_json_values(lines[index])
                if extracted:
                    fixed, method = extracted, extracted_method
                else:
                    tool_fixed, tool_method = repair_missing_tool_result_content(lines[index])
                    if tool_fixed:
                        fixed, method = [tool_fixed], tool_method
                    else:
                        if is_plain_text_jsonl_garbage(lines[index]):
                            fixed, method = [], "drop-plain-text-jsonl-record"
                            dropped_records.append(
                                {
                                    "line_no": index + 1,
                                    "reason": "plain text JSONL garbage dropped",
                                    "method": method,
                                    "content": lines[index],
                                }
                            )
                        else:
                            fixed, method = repair_line(lines[index])
            if fixed is None:
                unresolved_indexes.add(index)
                continue
            replacements[index] = fixed
            methods[index] = method

        if unresolved_indexes:
            quarantine_path = task_path / ".tmp" / "quarantined_jsonl_records.jsonl"
            quarantine_path.parent.mkdir(parents=True, exist_ok=True)
            rel = path.relative_to(task_path).as_posix()
            with quarantine_path.open("a", encoding="utf-8") as handle:
                for index in sorted(unresolved_indexes):
                    handle.write(
                        json.dumps(
                            {
                                "task": task_path.name,
                                "file": rel,
                                "line_no": index + 1,
                                "reason": "unresolved single-line JSONL issue quarantined",
                                "content": lines[index] if 0 <= index < len(lines) else "",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            result["quarantined"].append(f"{rel}: lines {', '.join(str(i + 1) for i in sorted(unresolved_indexes))}")

        if not replacements and not unresolved_indexes:
            continue
        new_lines: list[str] = []
        for index, line in enumerate(lines):
            if index in unresolved_indexes:
                continue
            new_lines.extend(replacements.get(index, [line]))
        remaining = _verify_lines(new_lines)
        if remaining:
            for item in remaining[:20]:
                result["unresolved"].append(f"postcheck: {path.relative_to(task_path).as_posix()}:{item}")
            continue

        result["repaired_files"] += 1
        result["repairable_lines"] += len(replacements)
        for index in sorted(replacements):
            result["repairs"].append(
                {"path": path.relative_to(task_path).as_posix(), "line_no": index + 1, "method": methods[index]}
            )
        if apply:
            if in_place:
                backup = _backup_file(task_path, path)
                result["backups"].append(backup.relative_to(task_path).as_posix())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            append_quarantined_records(task_path, path, dropped_records)

    if result["unresolved"]:
        result["status"] = "partial" if result["repairable_lines"] else "failed"
    else:
        result["status"] = "repaired" if result["repairable_lines"] else "passed"
    result["report"] = str(_write_repair_report(task_path, repair_report, result))
    result["case_report"] = str(_write_repair_case_report(task_path, result, append=in_place))
    return result


def repair_file_target(
    target: str | Path,
    apply: bool = False,
    output: Path = DEFAULT_OUTPUT,
    format_report: str = DEFAULT_FORMAT_REPORT,
    repair_report: str = DEFAULT_REPAIR_REPORT,
) -> dict[str, Any]:
    input_file = Path(target).expanduser().resolve()
    output_file = _single_file_output_path(input_file, output)
    report_name = _single_file_format_report_name(input_file, format_report)
    issues = read_report(input_file.parent, report_name)
    # Single-file reports store paths relative to the input file's directory.
    issues = [issue for issue in issues if issue.path.resolve() == input_file]
    result: dict[str, Any] = {
        "task": str(input_file),
        "apply": apply,
        "source": input_file.as_posix(),
        "output": output_file.as_posix(),
        "in_place": False,
        "report_issues": len(issues),
        "repairable_lines": 0,
        "repaired_files": 0,
        "repairs": [],
        "unresolved": [],
        "backups": [],
        "quarantined": [],
        "report": "",
        "status": "passed",
    }

    if not issues:
        result["report"] = str(_write_repair_report(input_file.parent, _single_file_repair_report_name(input_file, repair_report), result))
        result["case_report"] = str(_write_repair_case_report(input_file.parent, result))
        return result

    if any(issue.line_no == 0 and issue.message.startswith("encoding error") for issue in issues):
        fixed_text, method = repair_encoding_file(input_file)
        if fixed_text is None:
            result["unresolved"].append(f"encoding unresolved: {input_file.as_posix()}")
        else:
            result["repairable_lines"] = 1
            result["repaired_files"] = 1
            result["repairs"].append({"path": input_file.name, "line_no": 0, "method": method})
            if apply:
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(fixed_text, encoding="utf-8")
    elif input_file.suffix.lower() != ".jsonl":
        result["unresolved"].append(f"non-jsonl unsupported for structural repair: {input_file.name}")
    else:
        lines = input_file.read_text(encoding="utf-8", errors="replace").splitlines()
        multiline_lines, multiline_repairs = repair_first_root_issue_block(lines, issues)
        if multiline_lines is None:
            multiline_lines, multiline_repairs = rebuild_jsonl_records_by_state(lines)
        if multiline_lines is None:
            multiline_lines, multiline_repairs = repair_reported_multiline_jsonl_records(lines, issues)
        if multiline_lines is not None and not _verify_lines(multiline_lines):
            result["repairable_lines"] = len(multiline_repairs)
            result["repaired_files"] = 1
            for repair in multiline_repairs:
                label = str(repair["line_no"]) if repair["line_no"] == repair["end_line_no"] else f"{repair['line_no']}-{repair['end_line_no']}"
                result["repairs"].append({"path": input_file.name, "line_no": label, "method": repair["method"]})
            if apply:
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text("\n".join(multiline_lines) + "\n", encoding="utf-8")
        else:
            replacements: dict[int, list[str]] = {}
            methods: dict[int, str] = {}
            dropped_records: list[dict[str, Any]] = []
            for issue in issues:
                index = issue.line_no - 1
                if index < 0 or index >= len(lines):
                    result["unresolved"].append(f"missing line: {input_file.name}:{issue.line_no}")
                    continue
                fixed: list[str] | None
                method: str
                if "Invalid \\escape" in issue.message:
                    candidate = escape_invalid_json_backslashes(lines[index])
                    if _loads_ok(candidate):
                        fixed, method = [candidate], "escape-invalid-backslashes"
                    else:
                        iterative, iterative_method = repair_line_iteratively(lines[index])
                        fixed, method = ([iterative], iterative_method) if iterative is not None else repair_line(lines[index])
                elif (
                    "Expecting ',' delimiter" in issue.message
                    or "Expecting ':' delimiter" in issue.message
                    or "Expecting property name enclosed in double quotes" in issue.message
                    or "Extra data" in issue.message
                ):
                    iterative, iterative_method = repair_line_iteratively(lines[index])
                    fixed, method = ([iterative], iterative_method) if iterative is not None else repair_line(lines[index])
                else:
                    extracted, extracted_method = repair_nul_or_embedded_json_values(lines[index])
                    if extracted:
                        fixed, method = extracted, extracted_method
                    else:
                        tool_fixed, tool_method = repair_missing_tool_result_content(lines[index])
                        if tool_fixed:
                            fixed, method = [tool_fixed], tool_method
                        else:
                            if is_plain_text_jsonl_garbage(lines[index]):
                                fixed, method = [], "drop-plain-text-jsonl-record"
                                dropped_records.append(
                                    {
                                        "line_no": index + 1,
                                        "reason": "plain text JSONL garbage dropped",
                                        "method": method,
                                        "content": lines[index],
                                    }
                                )
                            else:
                                fixed, method = repair_line(lines[index])
                if fixed is None:
                    result["unresolved"].append(f"unresolved: {input_file.name}:{issue.line_no}: {issue.message}")
                    continue
                replacements[index] = fixed
                methods[index] = method

            if replacements:
                new_lines: list[str] = []
                for index, line in enumerate(lines):
                    new_lines.extend(replacements.get(index, [line]))
                remaining = _verify_lines(new_lines)
                if remaining:
                    result["unresolved"].extend(f"postcheck: {item}" for item in remaining[:20])
                else:
                    result["repairable_lines"] = len(replacements)
                    result["repaired_files"] = 1
                    for index in sorted(replacements):
                        result["repairs"].append({"path": input_file.name, "line_no": index + 1, "method": methods[index]})
                    if apply:
                        output_file.parent.mkdir(parents=True, exist_ok=True)
                        output_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                        append_quarantined_records(input_file.parent, input_file, dropped_records)

    if result["unresolved"]:
        result["status"] = "partial" if result["repairable_lines"] else "failed"
    else:
        result["status"] = "repaired" if result["repairable_lines"] else "passed"
    result["report"] = str(_write_repair_report(input_file.parent, _single_file_repair_report_name(input_file, repair_report), result))
    result["case_report"] = str(_write_repair_case_report(input_file.parent, result))
    return result


def repair_target(
    target: str | Path,
    apply: bool = False,
    source: Path = DEFAULT_SOURCE,
    output: Path = DEFAULT_OUTPUT,
    in_place: bool = False,
    format_report: str = DEFAULT_FORMAT_REPORT,
    repair_report: str = DEFAULT_REPAIR_REPORT,
) -> dict[str, Any]:
    target_path = Path(target).expanduser().resolve()
    if target_path.is_file():
        result = repair_file_target(
            target_path,
            apply=apply,
            output=output,
            format_report=format_report,
            repair_report=repair_report,
        )
        return {
            "total": 1,
            "repaired": 1 if result["status"] == "repaired" else 0,
            "partial": 1 if result["status"] == "partial" else 0,
            "failed": 1 if result["status"] == "failed" else 0,
            "passed": 1 if result["status"] == "passed" else 0,
            "skipped": 1 if result["status"] == "skipped" else 0,
            "repairable_lines": int(result["repairable_lines"]),
            "results": [result],
        }
    task_dirs = discover_task_dirs(target, sources=[source])
    results = [
        repair_task(
            task_dir,
            apply=apply,
            source=source,
            output=output,
            in_place=in_place,
            format_report=format_report,
            repair_report=repair_report,
        )
        for task_dir in task_dirs
    ]
    return {
        "total": len(results),
        "repaired": sum(1 for item in results if item["status"] == "repaired"),
        "partial": sum(1 for item in results if item["status"] == "partial"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "passed": sum(1 for item in results if item["status"] == "passed"),
        "skipped": sum(1 for item in results if item["status"] == "skipped"),
        "repairable_lines": sum(int(item["repairable_lines"]) for item in results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = repair_cli_target(args.root, args.output)
    except (OSError, ValueError) as exc:
        print(f"处理路径无效: {exc}", file=sys.stderr)
        return 2

    print(
        f"{result['status'].upper()}: {Path(result['task']).name} "
        f"files={result['repaired_files']} lines={result['repairable_lines']} "
        f"report={result['report']} case_report={result['case_report']}"
    )
    return 1 if result["status"] in {"failed", "partial"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

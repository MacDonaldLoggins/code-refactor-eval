"""S27L-style deterministic audit over content actually shown to the agent."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


AUDIT_SCHEMA = "unittest-rl-answer-leakage-audit-v1"
VALIDATION_SCHEMA = "unittest-rl-answer-leakage-validation-v1"
MIN_FRAGMENT_LENGTH = 24
MAX_TEXT_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".rst",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".diff",
    ".patch",
}


class LeakageAuditError(ValueError):
    """The inbound-content audit could not be completed."""


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


_SIMPLE_SOURCE_ESCAPES = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def _unescape_embedded_source(value: str) -> str:
    """Decode simple JS/JSON escapes used by unified agent tool wrappers."""

    return re.sub(
        r"\\([\"'\\/bfnrt])",
        lambda match: _SIMPLE_SOURCE_ESCAPES[match.group(1)],
        value,
    )


def _json_scalars(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _json_scalars(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_scalars(child)


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _fragments(path: Path, text: str) -> list[str]:
    raw_values: Iterable[str]
    if path.suffix.casefold() == ".json":
        try:
            raw_values = _json_scalars(json.loads(text))
        except json.JSONDecodeError:
            raw_values = text.splitlines()
    elif path.suffix.casefold() == ".jsonl":
        values: list[str] = []
        for line in text.splitlines():
            try:
                values.extend(_json_scalars(json.loads(line)))
            except json.JSONDecodeError:
                values.append(line)
        raw_values = values
    else:
        raw_values = text.splitlines()
    result: set[str] = set()
    for raw in raw_values:
        raw_text = str(raw)
        if path.suffix.casefold() in {".diff", ".patch"} and raw_text[:1] in {
            "+",
            "-",
            " ",
        }:
            # Compare the payload of unified-diff lines with public files.
            # Keeping the diff marker here makes a public source line such as
            # ``f\"message\"`` look private merely because the sealed patch
            # spells it as ``-f\"message\"``.
            raw_text = raw_text[1:]
        normalized = _normalize(raw_text)
        if len(normalized) < MIN_FRAGMENT_LENGTH:
            continue
        if len(re.findall(r"[\w\u3400-\u9fff]+", normalized)) < 3:
            continue
        result.add(normalized[:1000])
        if len(result) >= 400:
            break
    whole = _normalize(text)
    if MIN_FRAGMENT_LENGTH <= len(whole) <= 4000:
        result.add(whole)
    return sorted(result, key=lambda item: (-len(item), item))


def _iter_text_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and not path.is_symlink()
            and path.suffix.casefold() in TEXT_SUFFIXES
        ):
            yield path


def _public_module_runtime_fragments(
    *,
    root: Path,
    path: Path,
    text: str,
) -> Iterable[str]:
    """Yield public attributes exposed dynamically by a Python module.

    A module-level ``__getattr__`` may build a fully-qualified public name at
    runtime from a literal mapping key.  The resulting string is observable by
    executing public repository code even when it is not present verbatim in
    any source line.  Treat only identifier keys declared inside that public
    ``__getattr__`` as public; do not infer arbitrary combinations of strings.
    """

    if path.suffix.casefold() != ".py" or not root.is_dir():
        return
    try:
        relative = path.relative_to(root)
        tree = ast.parse(text, filename=relative.as_posix())
    except (OSError, SyntaxError, ValueError):
        return

    parts = list(relative.with_suffix("").parts)
    if parts[-1:] == ["__init__"]:
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return
    module_name = ".".join(parts)

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "__getattr__":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Dict):
                continue
            for key in child.keys:
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.isidentifier()
                ):
                    yield f"{module_name}.{key.value}"


def _inbound_items(trajectory: dict[str, Any]) -> list[dict[str, str]]:
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        raise LeakageAuditError("ATIF trajectory has no steps array")
    result: list[dict[str, str]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise LeakageAuditError(f"ATIF step {index + 1} is not an object")
        source = step.get("source")
        if source in {"system", "user"}:
            message = step.get("message")
            if isinstance(message, str):
                result.append(
                    {
                        "location": f"steps[{index}].message",
                        "channel": str(source),
                        "text": _normalize(message),
                    }
                )
            elif isinstance(message, list):
                result.append(
                    {
                        "location": f"steps[{index}].message",
                        "channel": str(source),
                        "text": _normalize(
                            json.dumps(message, ensure_ascii=False)
                        ),
                    }
                )
        observation = step.get("observation")
        if not isinstance(observation, dict):
            continue
        results = observation.get("results")
        if not isinstance(results, list):
            raise LeakageAuditError(
                f"ATIF step {index + 1} observation has no results array"
            )
        for result_index, item in enumerate(results):
            strings = list(_json_scalars(item))
            if strings:
                result.append(
                    {
                        "location": (
                            f"steps[{index}].observation.results[{result_index}]"
                        ),
                        "channel": "tool_result",
                        "text": _normalize("\n".join(strings)),
                    }
                )
    if not result:
        raise LeakageAuditError("ATIF has no auditable inbound content")
    return result


def _agent_generated_corpus(trajectory: dict[str, Any]) -> str:
    """Return model-authored text that later tool output may legitimately echo."""

    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        return ""
    values: list[str] = []
    for step in steps:
        if not isinstance(step, dict) or step.get("source") != "agent":
            continue
        message = step.get("message")
        if isinstance(message, str):
            values.append(message)
        elif isinstance(message, list):
            values.extend(_json_scalars(message))
        tool_calls = step.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            arguments = call.get("arguments")
            if isinstance(arguments, dict):
                values.extend(_json_scalars(arguments))
    variants = [
        variant
        for value in values
        for variant in (value, _unescape_embedded_source(value))
    ]
    return _normalize("\n".join(variants))


def _public_corpus(public_roots: list[Path]) -> str:
    """Return public file contents plus paths observable through file tools."""

    values: list[str] = []
    for root in public_roots:
        for path in _iter_text_files(root):
            text = _read_text(path)
            if text is not None:
                values.append(text)
                values.extend(
                    _public_module_runtime_fragments(
                        root=root,
                        path=path,
                        text=text,
                    )
                )
            try:
                public_path = path.relative_to(root).as_posix()
            except ValueError:
                public_path = path.name
            if public_path == ".":
                public_path = path.name
            values.append(public_path)
    return " ".join(_normalize(value) for value in values)


def _source_category(path: Path) -> str:
    lowered = path.as_posix().casefold()
    if "reference" in lowered or "answer" in lowered:
        return "reference_answer"
    if "fault" in lowered or "mutation" in lowered:
        return "fault_pool"
    if "rubric" in lowered or "reward" in lowered:
        return "rubric"
    if "obligation" in lowered or "oracle" in lowered:
        return "oracle"
    if "behavior" in lowered:
        return "behavior_partition"
    return "evaluation_asset"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_inbound_content(
    *,
    trajectory_path: Path,
    evaluation_assets: Path,
    public_roots: list[Path],
    candidate_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compare sealed answer fragments with actual ATIF inbound content."""

    try:
        trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LeakageAuditError(f"cannot read ATIF trajectory: {exc}") from exc
    if not isinstance(trajectory, dict):
        raise LeakageAuditError("ATIF trajectory root must be an object")
    inbound = _inbound_items(trajectory)
    generated_corpus = _agent_generated_corpus(trajectory)
    public_corpus = _public_corpus(public_roots)
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in _iter_text_files(evaluation_assets):
        source_text = _read_text(source)
        if source_text is None:
            continue
        source_sha = _sha256_file(source)
        for fragment in _fragments(source, source_text):
            if fragment in public_corpus:
                continue
            for item in inbound:
                if fragment not in item["text"]:
                    continue
                if (
                    item["channel"] == "tool_result"
                    and fragment in generated_corpus
                ):
                    continue
                key = (source_sha, item["location"])
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    {
                        "finding_id": f"leak-{len(findings) + 1:03d}",
                        "status": "confirmed",
                        "leak_type": _source_category(source),
                        "severity": "critical",
                        "attribution": "pipeline",
                        "exposure_channel": item["channel"],
                        "observable_location": item["location"],
                        "source_artifact": (
                            "evaluation_assets/"
                            + source.relative_to(evaluation_assets).as_posix()
                        ),
                        "source_sha256": source_sha,
                        "match_method": "normalized_exact",
                        "publicly_derivable": False,
                        "confidence": 1.0,
                        "exposed_information_summary": fragment[:160],
                    }
                )
                if len(findings) >= 200:
                    break
            if len(findings) >= 200:
                break
        if len(findings) >= 200:
            break
    status = "confirmed_pipeline_exposure" if findings else "clean"
    report = {
        "schema_version": AUDIT_SCHEMA,
        "stage_id": "S27L",
        "status": "invalid" if findings else "pass",
        "candidate_id": candidate_id,
        "answer_leakage_status": status,
        "severity": "critical" if findings else "none",
        "attribution": "pipeline" if findings else None,
        "official_score_eligible": not findings,
        "training_eligible": not findings,
        "finding_count": len(findings),
        "findings": findings,
        "observable_input_count": len(inbound),
        "hidden_reasoning": "not_recorded",
        "policy": "normalized-exact-after-public-subtraction-v1",
    }
    validation = {
        "schema_version": VALIDATION_SCHEMA,
        "status": "pass" if not findings else "fail",
        "candidate_id": candidate_id,
        "answer_leakage_status": status,
        "official_score_eligible": not findings,
        "errors": (
            [] if not findings else ["candidate_input_integrity:confirmed"]
        ),
    }
    return report, validation

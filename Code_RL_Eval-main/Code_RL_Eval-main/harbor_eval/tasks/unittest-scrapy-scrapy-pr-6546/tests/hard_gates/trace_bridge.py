"""Validate Harbor ATIF and derive Unit Test RL generation-audit records."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


AUDIT_SCHEMA = "unittest-rl-harbor-trajectory-audit-v1"
SUPPORTED_ATIF_VERSIONS = frozenset({"ATIF-v1.7"})
ALLOWED_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "bash",
        "edit",
        "exec",
        "exec_command",
        "glob",
        "grep",
        "read",
        "read_file",
        "view_image",
        "write",
        "write_stdin",
    }
)
COMMAND_TOOL_NAMES = frozenset({"bash", "exec", "exec_command"})
CODEX_EXEC_ALLOWED_NESTED_TOOLS = frozenset(
    {"apply_patch", "exec_command", "update_plan"}
)
NETWORK_TOOL_MARKERS = frozenset(
    {
        "browser",
        "http",
        "search_query",
        "web.run",
        "web_search",
        "web_search_call",
    }
)
SEALED_PATH_MARKERS = (
    "evaluation_assets",
    "reference_tests",
    "sealed",
)
SEALED_ABSOLUTE_ROOTS = (
    "/tests",
    "/solution",
    "/logs/verifier",
)
ALLOWED_WRITE_ROOTS = (
    PurePosixPath("/app/candidate_tests"),
    PurePosixPath("/app/.candidate_tmp"),
)
_NETWORK_COMMAND = re.compile(
    r"(^|[;&|]\s*)(curl|wget|ssh|scp|sftp|nc|ncat|telnet)\b"
    r"|https?://",
    re.IGNORECASE,
)
_MUTATING_COMMAND = re.compile(
    r"(^|[;&|]\s*)(sudo\s+)?"
    r"(rm|mv|cp|touch|mkdir|install|chmod|chown|ln)\b"
    r"|\bsed\s+[^\n;]*\s-i(?:\s|$)",
    re.IGNORECASE,
)
_PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (?P<path>.+)$",
    re.MULTILINE,
)
_CODEX_EXEC_TOOL = re.compile(
    r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\("
)


def _as_object(value: Any, location: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{location}: expected object")
        return {}
    return value


def _as_list(value: Any, location: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{location}: expected array")
        return []
    return value


def _nonempty_string(
    value: Any,
    location: str,
    errors: list[str],
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{location}: expected non-empty string")
        return None
    return value


def _normalized_tool_name(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower()


def _iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_string_values(child)


def _normalize_path(raw: str, workdir: str = "/app") -> PurePosixPath:
    cleaned = raw.strip().strip("'\"")
    path = PurePosixPath(cleaned)
    if path.is_absolute():
        return path
    return PurePosixPath(workdir) / path


def _is_allowed_write(path: PurePosixPath) -> bool:
    return any(path == root or root in path.parents for root in ALLOWED_WRITE_ROOTS)


def _iter_redirection_targets(command: str) -> Iterable[str]:
    """Yield active shell output-redirection targets, ignoring quoted ``>``."""

    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char != ">":
            index += 1
            continue

        index += 1
        if index < len(command) and command[index] == ">":
            index += 1
        while index < len(command) and command[index].isspace():
            index += 1
        if index < len(command) and command[index] == "&":
            while index < len(command) and command[index] not in " \t\r\n;|":
                index += 1
            continue
        if index >= len(command):
            continue

        if command[index] in {"'", '"'}:
            target_quote = command[index]
            index += 1
            start = index
            while index < len(command) and command[index] != target_quote:
                index += 1
            yield command[start:index]
            index += 1
            continue

        start = index
        while index < len(command) and command[index] not in " \t\r\n;&|":
            index += 1
        if index > start:
            yield command[start:index]


def _extract_codex_exec_commands(source: str) -> list[str]:
    """Decode the ``cmd`` string passed to each unified ``exec_command`` call."""

    commands: list[str] = []
    marker = re.compile(r"\btools\.exec_command\s*\(\s*\{")
    cmd_key = re.compile(r"(?:[\"']cmd[\"']|\bcmd\b)\s*:\s*")
    decoder = json.JSONDecoder()
    for tool_match in marker.finditer(source):
        key_match = cmd_key.search(source, tool_match.end())
        if key_match is None:
            continue
        start = key_match.end()
        if start >= len(source) or source[start] != '"':
            continue
        try:
            command, _ = decoder.raw_decode(source[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(command, str):
            commands.append(command)
    return commands


def _has_sealed_marker(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in SEALED_PATH_MARKERS):
        return True
    for root in SEALED_ABSOLUTE_ROOTS:
        if (
            lowered == root
            or lowered.startswith(f"{root}/")
            or f" {root}" in lowered
            or f"'{root}" in lowered
            or f'"{root}' in lowered
        ):
            return True
    return False


def _scan_command(
    command: str,
    *,
    workdir: str,
    tool_call_id: str,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    accesses: list[dict[str, Any]] = []
    violations: list[str] = []
    network_attempted = bool(_NETWORK_COMMAND.search(command))

    if _has_sealed_marker(command):
        violations.append(f"{tool_call_id}:sealed_path_referenced")
    if "../" in command or command.strip().startswith(".."):
        violations.append(f"{tool_call_id}:parent_path_traversal")

    mutating = bool(_MUTATING_COMMAND.search(command))
    if mutating and "/app/repository" in command:
        violations.append(f"{tool_call_id}:production_tree_mutation_attempt")

    for raw_path in _iter_redirection_targets(command):
        if raw_path in {"/dev/null", "&1", "&2"}:
            continue
        path = _normalize_path(raw_path, workdir)
        accesses.append(
            {
                "tool_call_id": tool_call_id,
                "operation": "write",
                "path": str(path),
                "source": "shell_redirection",
            }
        )
        if not _is_allowed_write(path):
            violations.append(
                f"{tool_call_id}:write_outside_candidate_roots:{path}"
            )

    return accesses, violations, network_attempted


def _scan_apply_patch(
    arguments: dict[str, Any],
    *,
    tool_call_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    patch = arguments.get("input")
    if not isinstance(patch, str):
        patch = arguments.get("patch")
    if not isinstance(patch, str):
        return [], [f"{tool_call_id}:apply_patch_payload_missing"]

    accesses: list[dict[str, Any]] = []
    violations: list[str] = []
    paths = [match.group("path").strip() for match in _PATCH_PATH.finditer(patch)]
    if not paths:
        violations.append(f"{tool_call_id}:apply_patch_paths_unresolved")
        return accesses, violations

    for raw_path in paths:
        if raw_path == "/dev/null":
            continue
        path = _normalize_path(raw_path)
        accesses.append(
            {
                "tool_call_id": tool_call_id,
                "operation": "write",
                "path": str(path),
                "source": "apply_patch",
            }
        )
        if not _is_allowed_write(path):
            violations.append(
                f"{tool_call_id}:write_outside_candidate_roots:{path}"
            )
    return accesses, violations


def _scan_codex_exec(
    arguments: dict[str, Any],
    *,
    tool_call_id: str,
) -> tuple[list[dict[str, Any]], list[str], bool, int]:
    """Audit the nested tool calls encoded by Codex's unified exec tool."""

    source = arguments.get("input")
    if not isinstance(source, str):
        return [], [f"{tool_call_id}:exec_input_missing"], False, 0

    nested_tools = _CODEX_EXEC_TOOL.findall(source)
    if not nested_tools:
        return [], [f"{tool_call_id}:exec_nested_tools_missing"], False, 0

    violations = [
        f"{tool_call_id}:exec_unsupported_nested_tool:{name}"
        for name in nested_tools
        if name not in CODEX_EXEC_ALLOWED_NESTED_TOOLS
    ]
    accesses: list[dict[str, Any]] = []
    network_attempted = False
    exec_commands = _extract_codex_exec_commands(source)
    if nested_tools.count("exec_command") != len(exec_commands):
        violations.append(f"{tool_call_id}:exec_command_payload_unresolved")
    for command in exec_commands:
        command_accesses, command_violations, command_network = _scan_command(
            command,
            workdir="/app",
            tool_call_id=tool_call_id,
        )
        accesses.extend(command_accesses)
        violations.extend(command_violations)
        network_attempted = network_attempted or command_network

    if "apply_patch" in nested_tools:
        decoded_source = source.replace("\\r\\n", "\n").replace("\\n", "\n")
        patch_accesses, patch_violations = _scan_apply_patch(
            {"input": decoded_source},
            tool_call_id=tool_call_id,
        )
        accesses.extend(patch_accesses)
        violations.extend(patch_violations)

    return (
        accesses,
        violations,
        network_attempted,
        nested_tools.count("exec_command"),
    )


def _scan_structured_read(
    arguments: dict[str, Any],
    *,
    tool_call_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str):
        raw_path = arguments.get("file_path")
    if not isinstance(raw_path, str):
        return [], [f"{tool_call_id}:read_path_missing"]

    path = _normalize_path(raw_path)
    violations: list[str] = []
    if _has_sealed_marker(str(path)):
        violations.append(f"{tool_call_id}:sealed_path_referenced")
    if "../" in raw_path or raw_path.strip().startswith(".."):
        violations.append(f"{tool_call_id}:parent_path_traversal")
    return (
        [
            {
                "tool_call_id": tool_call_id,
                "operation": "read",
                "path": str(path),
                "source": "structured_tool",
            }
        ],
        violations,
    )


def _scan_structured_search(
    arguments: dict[str, Any],
    *,
    tool_call_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Audit Claude Glob/Grep reads, whose path argument is optional."""

    raw_path = arguments.get("path")
    if raw_path is None:
        raw_path = "/app"
    if not isinstance(raw_path, str):
        return [], [f"{tool_call_id}:read_path_invalid"]

    path = _normalize_path(raw_path)
    violations: list[str] = []
    if _has_sealed_marker(str(path)):
        violations.append(f"{tool_call_id}:sealed_path_referenced")
    if "../" in raw_path or raw_path.strip().startswith(".."):
        violations.append(f"{tool_call_id}:parent_path_traversal")
    return (
        [
            {
                "tool_call_id": tool_call_id,
                "operation": "read",
                "path": str(path),
                "source": "structured_search_tool",
            }
        ],
        violations,
    )


def _scan_structured_write(
    arguments: dict[str, Any],
    *,
    tool_call_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Audit Claude Write/Edit calls before accepting their file target."""

    raw_path = arguments.get("file_path")
    if not isinstance(raw_path, str):
        raw_path = arguments.get("path")
    if not isinstance(raw_path, str):
        return [], [f"{tool_call_id}:write_path_missing"]

    path = _normalize_path(raw_path)
    violations: list[str] = []
    if "../" in raw_path or raw_path.strip().startswith(".."):
        violations.append(f"{tool_call_id}:parent_path_traversal")
    if not _is_allowed_write(path):
        violations.append(f"{tool_call_id}:write_outside_candidate_roots:{path}")
    return (
        [
            {
                "tool_call_id": tool_call_id,
                "operation": "write",
                "path": str(path),
                "source": "structured_write_tool",
            }
        ],
        violations,
    )


def _validate_atif(data: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: list[str] = []
    root = _as_object(data, "trajectory", errors)
    schema_version = root.get("schema_version")
    if schema_version not in SUPPORTED_ATIF_VERSIONS:
        errors.append(
            "trajectory.schema_version: expected one of "
            f"{sorted(SUPPORTED_ATIF_VERSIONS)}, got {schema_version!r}"
        )

    session_id = _nonempty_string(
        root.get("session_id"),
        "trajectory.session_id",
        errors,
    )
    agent = _as_object(root.get("agent"), "trajectory.agent", errors)
    agent_name = _nonempty_string(
        agent.get("name"),
        "trajectory.agent.name",
        errors,
    )
    agent_version = _nonempty_string(
        agent.get("version"),
        "trajectory.agent.version",
        errors,
    )
    model_name = agent.get("model_name")
    if model_name is not None and (
        not isinstance(model_name, str) or not model_name.strip()
    ):
        errors.append(
            "trajectory.agent.model_name: expected non-empty string or null"
        )

    steps = _as_list(root.get("steps"), "trajectory.steps", errors)
    if not steps:
        errors.append("trajectory.steps: at least one step is required")

    normalized_steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps):
        location = f"trajectory.steps[{index}]"
        step = _as_object(raw_step, location, errors)
        expected_id = index + 1
        if step.get("step_id") != expected_id:
            errors.append(
                f"{location}.step_id: expected {expected_id}, "
                f"got {step.get('step_id')!r}"
            )
        source = step.get("source")
        if source not in {"system", "user", "agent"}:
            errors.append(
                f"{location}.source: expected system, user, or agent"
            )
        if not isinstance(step.get("message"), (str, list)):
            errors.append(f"{location}.message: expected string or array")

        timestamp = step.get("timestamp")
        if timestamp is not None:
            if not isinstance(timestamp, str):
                errors.append(f"{location}.timestamp: expected string or null")
            else:
                try:
                    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    errors.append(f"{location}.timestamp: invalid ISO-8601")

        llm_call_count = step.get("llm_call_count")
        if llm_call_count is not None and (
            not isinstance(llm_call_count, int) or llm_call_count < 0
        ):
            errors.append(
                f"{location}.llm_call_count: expected non-negative integer"
            )
        if (
            source == "agent"
            and llm_call_count == 0
            and (
                step.get("metrics") is not None
                or step.get("reasoning_content") is not None
            )
        ):
            errors.append(
                f"{location}: deterministic agent step contains LLM-only fields"
            )

        tool_calls = step.get("tool_calls")
        if tool_calls is None:
            tool_calls = []
        tool_calls = _as_list(tool_calls, f"{location}.tool_calls", errors)
        call_ids: set[str] = set()
        normalized_calls: list[dict[str, Any]] = []
        for call_index, raw_call in enumerate(tool_calls):
            call_location = f"{location}.tool_calls[{call_index}]"
            call = _as_object(raw_call, call_location, errors)
            call_id_value = call.get("tool_call_id")
            if not isinstance(call_id_value, str):
                errors.append(
                    f"{call_location}.tool_call_id: expected string"
                )
                call_id = None
            else:
                call_id = call_id_value
            function_name = _nonempty_string(
                call.get("function_name"),
                f"{call_location}.function_name",
                errors,
            )
            arguments = _as_object(
                call.get("arguments"),
                f"{call_location}.arguments",
                errors,
            )
            if call_id:
                if call_id in call_ids:
                    errors.append(
                        f"{call_location}.tool_call_id: duplicate {call_id!r}"
                    )
                call_ids.add(call_id)
            normalized_calls.append(
                {
                    "tool_call_id": call_id or "",
                    "function_name": function_name or "",
                    "arguments": arguments,
                }
            )

        observation = step.get("observation")
        if observation is not None:
            observation = _as_object(
                observation,
                f"{location}.observation",
                errors,
            )
            results = _as_list(
                observation.get("results"),
                f"{location}.observation.results",
                errors,
            )
            for result_index, raw_result in enumerate(results):
                result = _as_object(
                    raw_result,
                    f"{location}.observation.results[{result_index}]",
                    errors,
                )
                source_call_id = result.get("source_call_id")
                if (
                    source_call_id is not None
                    and source_call_id not in call_ids
                ):
                    errors.append(
                        f"{location}.observation.results[{result_index}]"
                        f".source_call_id: unknown {source_call_id!r}"
                    )

        normalized_steps.append(
            {
                "step_id": expected_id,
                "source": source,
                "model_name": step.get("model_name"),
                "llm_call_count": llm_call_count,
                "tool_calls": normalized_calls,
            }
        )

    if root.get("continued_trajectory_ref") is not None:
        errors.append(
            "trajectory.continued_trajectory_ref: continuations are not "
            "allowed for single-session Unit Test RL candidates"
        )
    if root.get("subagent_trajectories"):
        errors.append(
            "trajectory.subagent_trajectories: subagents are not allowed "
            "for single-session Unit Test RL candidates"
        )

    identity = {
        "session_id": session_id,
        "agent_name": agent_name,
        "agent_version": agent_version,
        "model_name": model_name,
        "schema_version": schema_version,
    }
    return {
        "identity": identity,
        "steps": normalized_steps,
        "errors": errors,
    }, normalized_steps


def audit_trajectory_data(
    data: Any,
    *,
    expected_agent_name: str | None = None,
    expected_model_name: str | None = None,
    allowed_identities: list[dict[str, str]] | None = None,
    require_model_call: bool = False,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Validate ATIF and produce Unit Test RL-compatible audit summaries."""

    validation, steps = _validate_atif(data)
    validation_errors = list(validation["errors"])
    identity = validation["identity"]
    identity_errors: list[str] = []
    allowed_pairs: set[tuple[str, str]] | None = None
    if allowed_identities is not None:
        if not isinstance(allowed_identities, list) or not allowed_identities:
            raise ValueError("allowed_identities must be a non-empty array")
        allowed_pairs = set()
        for index, item in enumerate(allowed_identities):
            if not isinstance(item, dict):
                raise ValueError(f"allowed_identities[{index}] must be an object")
            if set(item) != {"agent_name", "model_name"}:
                raise ValueError(
                    f"allowed_identities[{index}] must contain only "
                    "agent_name and model_name"
                )
            agent_name = item.get("agent_name")
            model_name = item.get("model_name")
            if not isinstance(agent_name, str) or not agent_name.strip():
                raise ValueError(
                    f"allowed_identities[{index}].agent_name must be non-empty"
                )
            if not isinstance(model_name, str) or not model_name.strip():
                raise ValueError(
                    f"allowed_identities[{index}].model_name must be non-empty"
                )
            pair = (agent_name.strip(), model_name.strip())
            if pair in allowed_pairs:
                raise ValueError(f"duplicate allowed identity: {pair!r}")
            allowed_pairs.add(pair)
    if (
        expected_agent_name is not None
        and identity["agent_name"] != expected_agent_name
    ):
        identity_errors.append(
            "agent_name_mismatch:"
            f"expected={expected_agent_name!r}:actual={identity['agent_name']!r}"
        )
    if (
        expected_model_name is not None
        and identity["model_name"] != expected_model_name
    ):
        identity_errors.append(
            "model_name_mismatch:"
            f"expected={expected_model_name!r}:actual={identity['model_name']!r}"
        )
    actual_pair = (identity["agent_name"], identity["model_name"])
    if allowed_pairs is not None and actual_pair not in allowed_pairs:
        identity_errors.append(
            "candidate_identity_not_allowed:"
            f"actual={actual_pair!r}:allowed={sorted(allowed_pairs)!r}"
        )

    tool_count = 0
    command_count = 0
    model_call_count = 0
    accesses: list[dict[str, Any]] = []
    violations: list[str] = []
    network_attempted = False

    for step in steps:
        if step["source"] == "agent":
            llm_call_count = step["llm_call_count"]
            if isinstance(llm_call_count, int):
                model_call_count += llm_call_count
            elif step["model_name"] or identity["model_name"]:
                model_call_count += 1

        for call in step["tool_calls"]:
            tool_count += 1
            call_id = call["tool_call_id"] or f"step-{step['step_id']}-tool"
            raw_name = call["function_name"]
            name = _normalized_tool_name(raw_name)
            arguments = call["arguments"]

            lowered_raw_name = raw_name.lower()
            if (
                name == "web_search_call"
                or any(marker in lowered_raw_name for marker in NETWORK_TOOL_MARKERS)
            ):
                network_attempted = True
                violations.append(f"{call_id}:network_tool:{raw_name}")
                continue

            if name not in ALLOWED_TOOL_NAMES:
                violations.append(f"{call_id}:unsupported_tool:{raw_name}")
                continue

            if name == "apply_patch":
                call_accesses, call_violations = _scan_apply_patch(
                    arguments,
                    tool_call_id=call_id,
                )
                accesses.extend(call_accesses)
                violations.extend(call_violations)
                continue

            if name in {"read", "read_file", "view_image"}:
                call_accesses, call_violations = _scan_structured_read(
                    arguments,
                    tool_call_id=call_id,
                )
                accesses.extend(call_accesses)
                violations.extend(call_violations)
                continue

            if name in {"glob", "grep"}:
                call_accesses, call_violations = _scan_structured_search(
                    arguments,
                    tool_call_id=call_id,
                )
                accesses.extend(call_accesses)
                violations.extend(call_violations)
                continue

            if name in {"edit", "write"}:
                call_accesses, call_violations = _scan_structured_write(
                    arguments,
                    tool_call_id=call_id,
                )
                accesses.extend(call_accesses)
                violations.extend(call_violations)
                continue

            if name == "exec" and "input" in arguments:
                (
                    call_accesses,
                    call_violations,
                    call_network,
                    nested_command_count,
                ) = _scan_codex_exec(arguments, tool_call_id=call_id)
                accesses.extend(call_accesses)
                violations.extend(call_violations)
                network_attempted = network_attempted or call_network
                command_count += nested_command_count
                continue

            if name in {"bash", "exec", "exec_command", "write_stdin"}:
                command = arguments.get("cmd")
                if not isinstance(command, str):
                    command = arguments.get("command")
                if not isinstance(command, str) and name == "write_stdin":
                    command = arguments.get("chars")
                if not isinstance(command, str):
                    violations.append(f"{call_id}:{name}_command_missing")
                    continue
                if name in COMMAND_TOOL_NAMES:
                    command_count += 1
                workdir = arguments.get("workdir")
                if not isinstance(workdir, str):
                    workdir = "/app"
                call_accesses, call_violations, call_network = _scan_command(
                    command,
                    workdir=workdir,
                    tool_call_id=call_id,
                )
                accesses.extend(call_accesses)
                violations.extend(call_violations)
                network_attempted = network_attempted or call_network

    if network_attempted:
        violations.append("network_access_attempted")

    execution_errors: list[str] = []
    if (expected_model_name is not None or require_model_call) and model_call_count == 0:
        execution_errors.append("generation_model_call_missing")

    trace_status = "pass" if not validation_errors else "fail"
    boundary_status = "pass" if not violations else "fail"
    session_errors = [*validation_errors, *execution_errors]
    session_status = "pass" if not session_errors else "fail"
    if identity_errors or execution_errors:
        status = "evaluation_invalid"
        classification = (
            "generation_identity_mismatch"
            if identity_errors
            else "generation_execution_incomplete"
        )
    elif validation_errors or violations:
        status = "candidate_fail"
        classification = (
            "invalid_atif" if validation_errors else "candidate_boundary"
        )
    else:
        status = "pass"
        classification = "trajectory_integrity_pass"

    resolved_candidate_id = (
        candidate_id
        or (
            f"harbor:{identity['session_id']}"
            if identity["session_id"]
            else "harbor:unknown"
        )
    )
    return {
        "schema_version": AUDIT_SCHEMA,
        "status": status,
        "classification": classification,
        "candidate_id": resolved_candidate_id,
        "identity": {
            **identity,
            "expected_agent_name": expected_agent_name,
            "expected_model_name": expected_model_name,
            "allowed_identities": (
                [
                    {"agent_name": pair[0], "model_name": pair[1]}
                    for pair in sorted(allowed_pairs)
                ]
                if allowed_pairs is not None
                else None
            ),
            "errors": identity_errors,
        },
        "atif_validation": {
            "status": trace_status,
            "errors": validation_errors,
        },
        "session_validation": {
            "schema_version": "unittest-rl-candidate-session-validation-v1",
            "status": session_status,
            "candidate_id": resolved_candidate_id,
            "session_id": identity["session_id"],
            "event_count": len(steps),
            "model_call_count": model_call_count,
            "model_call_required": require_model_call,
            "tool_call_count": tool_count,
            "command_count": command_count,
            "file_access_count": len(accesses),
            "errors": session_errors,
        },
        "boundary_validation": {
            "schema_version": (
                "unittest-rl-candidate-boundary-validation-v1"
            ),
            "status": boundary_status,
            "mode": "strict",
            "candidate_id": resolved_candidate_id,
            "out_of_scope_read_attempted": any(
                "sealed_path_referenced" in item
                or "parent_path_traversal" in item
                for item in violations
            ),
            "out_of_scope_read_succeeded": False,
            "out_of_scope_write_attempted": any(
                "write_outside_candidate_roots" in item
                or "production_tree_mutation_attempt" in item
                for item in violations
            ),
            "out_of_scope_write_effective": False,
            "network_access_attempted": network_attempted,
            "session_trace_status": trace_status,
            "session_count": 1 if identity["session_id"] else 0,
            "accesses": accesses,
            "violations": sorted(set(violations)),
        },
    }


def audit_trajectory_file(
    path: Path,
    *,
    expected_agent_name: str | None = None,
    expected_model_name: str | None = None,
    allowed_identities: list[dict[str, str]] | None = None,
    require_model_call: bool = False,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Load an ATIF JSON file and return a deterministic audit report."""

    if not path.is_file():
        return {
            "schema_version": AUDIT_SCHEMA,
            "status": "candidate_fail",
            "classification": "trajectory_missing",
            "candidate_id": candidate_id or "harbor:unknown",
            "identity": {
                "session_id": None,
                "agent_name": None,
                "agent_version": None,
                "model_name": None,
                "schema_version": None,
                "expected_agent_name": expected_agent_name,
                "expected_model_name": expected_model_name,
                "allowed_identities": allowed_identities,
                "errors": [],
            },
            "atif_validation": {
                "status": "fail",
                "errors": [f"trajectory_missing:{path}"],
            },
            "session_validation": {
                "schema_version": (
                    "unittest-rl-candidate-session-validation-v1"
                ),
                "status": "fail",
                "candidate_id": candidate_id or "harbor:unknown",
                "session_id": None,
                "event_count": 0,
                "model_call_count": 0,
                "model_call_required": require_model_call,
                "tool_call_count": 0,
                "command_count": 0,
                "file_access_count": 0,
                "errors": [f"trajectory_missing:{path}"],
            },
            "boundary_validation": {
                "schema_version": (
                    "unittest-rl-candidate-boundary-validation-v1"
                ),
                "status": "fail",
                "mode": "strict",
                "candidate_id": candidate_id or "harbor:unknown",
                "out_of_scope_read_attempted": False,
                "out_of_scope_read_succeeded": False,
                "out_of_scope_write_attempted": False,
                "out_of_scope_write_effective": False,
                "network_access_attempted": False,
                "session_trace_status": "fail",
                "session_count": 0,
                "accesses": [],
                "violations": ["trajectory_missing"],
            },
        }

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        report = audit_trajectory_file(
            Path("/nonexistent-unittest-rl-trajectory"),
            expected_agent_name=expected_agent_name,
            expected_model_name=expected_model_name,
            allowed_identities=allowed_identities,
            require_model_call=require_model_call,
            candidate_id=candidate_id,
        )
        report["classification"] = "trajectory_unreadable"
        error = f"trajectory_unreadable:{type(exc).__name__}:{exc}"
        report["atif_validation"]["errors"] = [error]
        report["session_validation"]["errors"] = [error]
        report["boundary_validation"]["violations"] = [
            "trajectory_unreadable"
        ]
        return report

    return audit_trajectory_data(
        data,
        expected_agent_name=expected_agent_name,
        expected_model_name=expected_model_name,
        allowed_identities=allowed_identities,
        require_model_call=require_model_call,
        candidate_id=candidate_id,
    )

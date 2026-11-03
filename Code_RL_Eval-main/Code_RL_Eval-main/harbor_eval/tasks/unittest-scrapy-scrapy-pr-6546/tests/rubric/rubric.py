"""Frozen-input Judge runner for Unit Test RL Harbor evaluations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:  # package import during adapter tests; flat import inside /tests verifier
    from .judge_runtime import JudgeRuntimeError, invoke_judge
except ImportError:  # pragma: no cover - exercised in the exported container
    from judge_runtime import JudgeRuntimeError, invoke_judge


DIMENSIONS = ("SC-J1", "SC-J2", "SC-J3", "SC-J4", "SC-J5")
CONFIG_SCHEMA = "unittest-rl-harbor-judge-config-v1"
REPLAY_SCHEMA = "unittest-rl-harbor-judge-replay-v1"
SNAPSHOT_SCHEMA = "unittest-rl-harbor-judge-input-v3"
MODEL_RESULT_SCHEMA = "unittest-rl-harbor-judge-model-result-v2"
FORMAL_RESULT_SCHEMA = "unittest-rl-formal-judge-score-v3"
PANEL_SCHEMA = "unittest-rl-harbor-judge-panel-v1"
STABILITY_SCHEMA = "unittest-rl-harbor-judge-stability-v1"
TASK_FROZEN_APPLICABILITY = "task_frozen_all_rubric_items"
J5_RUNTIME_APPLICABILITY = "stable_killed_diagnostic_units"
AGGREGATION_POLICY = "per-rubric-majority"
STABILITY_POLICY = "report_only"
J5_REQUIRED_REPLAY_COUNT = 3
J1_EXECUTION_EVIDENCE_SCHEMA = (
    "unittest-rl-harbor-j1-execution-evidence-v1"
)
J1_EXECUTION_EVIDENCE_SCHEMAS = {J1_EXECUTION_EVIDENCE_SCHEMA}


class JudgeError(RuntimeError):
    """An evaluation infrastructure or Judge-contract failure."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidate_tree_sha256(root: Path) -> str:
    """Hash candidate file paths, modes, and bytes in a stable order."""

    if not root.is_dir():
        raise JudgeError(f"candidate root is missing: {root}")
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if not files:
        raise JudgeError("candidate tree is empty")
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(oct(path.stat().st_mode & 0o777).encode("ascii"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_judge_config(
    config: dict[str, Any],
    *,
    task_core_fingerprint: str,
) -> None:
    if config.get("schema_version") != CONFIG_SCHEMA:
        raise JudgeError("judge config schema is invalid")
    if config.get("task_core_fingerprint") != task_core_fingerprint:
        raise JudgeError("judge config task fingerprint mismatch")
    required_strings = (
        "config_id",
        "model_key",
        "provider",
        "model",
        "base_url",
        "credential_env",
        "reasoning_effort",
    )
    for key in required_strings:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise JudgeError(f"judge config field {key!r} is invalid")
    if config["provider"] != "openai":
        raise JudgeError("only the OpenAI Responses provider is supported")
    runner = config.get("runner", "api")
    if runner not in {"api", "codex-cli", "claude-code"}:
        raise JudgeError("judge config runner is unsupported")
    expected_protocol = {
        "api": "responses",
        "codex-cli": "responses",
        "claude-code": "anthropic-messages",
    }[runner]
    if config.get("protocol", expected_protocol) != expected_protocol:
        raise JudgeError(
            f"judge config runner {runner!r} requires {expected_protocol!r}"
        )
    if config.get("auth_mode", "bearer") not in {"api-key", "bearer"}:
        raise JudgeError("judge config auth_mode is unsupported")
    if not config["base_url"].startswith(("https://", "http://")):
        raise JudgeError("judge config base_url must be HTTP(S)")
    for key in ("timeout_seconds", "max_output_tokens", "max_attempts"):
        if not isinstance(config.get(key), int) or config[key] < 1:
            raise JudgeError(f"judge config field {key!r} must be positive")
    fallback_tokens = config.get("fallback_max_output_tokens")
    if fallback_tokens is not None and (
        not isinstance(fallback_tokens, int) or fallback_tokens < 1
    ):
        raise JudgeError(
            "judge config fallback_max_output_tokens must be positive"
        )
    parallelism = config.get("parallelism")
    if not isinstance(parallelism, int) or not 1 <= parallelism <= 5:
        raise JudgeError("judge config parallelism must be between 1 and 5")
    repetitions = config.get("judge_repetitions", 1)
    if (
        not isinstance(repetitions, int)
        or repetitions < 1
        or repetitions > 9
        or repetitions % 2 == 0
    ):
        raise JudgeError(
            "judge config judge_repetitions must be an odd integer from 1 to 9"
        )
    aggregation = config.get("aggregation_policy", AGGREGATION_POLICY)
    if aggregation != AGGREGATION_POLICY:
        raise JudgeError(
            f"judge config aggregation_policy must be {AGGREGATION_POLICY!r}"
        )
    stability = config.get("stability_policy", STABILITY_POLICY)
    if stability != STABILITY_POLICY:
        raise JudgeError(
            f"judge config stability_policy must be {STABILITY_POLICY!r}"
        )


def judge_repetitions(config: dict[str, Any]) -> int:
    """Return the validated number of independent Judge rounds."""

    value = config.get("judge_repetitions", 1)
    if not isinstance(value, int):
        raise JudgeError("judge_repetitions must be an integer")
    return value


def project_j1_execution_evidence(
    behavior_observations: dict[str, Any],
) -> dict[str, Any]:
    """Project the shared positive observation into an embedded J1-only view."""

    if behavior_observations.get("status") != "pass":
        raise JudgeError("J1 source observations did not pass")
    matrix_profiles = behavior_observations.get("profiles")
    if isinstance(matrix_profiles, dict):
        if not matrix_profiles:
            raise JudgeError("J1 source observation profile matrix is empty")
        combined_runs: list[dict[str, Any]] = []
        combined_stable: list[str] = []
        for profile_id, profile_observations in matrix_profiles.items():
            if not isinstance(profile_id, str) or not profile_id:
                raise JudgeError("J1 execution profile identity is invalid")
            if not isinstance(profile_observations, dict):
                raise JudgeError("J1 execution profile observation is invalid")
            projected = project_j1_execution_evidence(profile_observations)
            for nodeid in projected["stable_passed_nodeids"]:
                combined_stable.append(f"{profile_id}:{nodeid}")
            for run in projected["runs"]:
                matrix_run = {**run, "execution_profile_id": profile_id}
                for collection in ("calls", "warnings", "assertions"):
                    matrix_run[collection] = [
                        {
                            **item,
                            "test_execution_id": (
                                f"{profile_id}:{item['test_execution_id']}"
                            ),
                        }
                        for item in run[collection]
                    ]
                combined_runs.append(matrix_run)
        return {
            "schema_version": J1_EXECUTION_EVIDENCE_SCHEMA,
            "status": "pass",
            "source": "projected positive-behavior-observations.json",
            "source_observation_sha256": sha256_json(behavior_observations),
            "execution_profile_ids": sorted(matrix_profiles),
            "stable_passed_nodeids": sorted(combined_stable),
            "runs": combined_runs,
            "excluded": [
                "SC-E1 result",
                "SC-E2-v2 result",
                "E2 obligation classifications",
                "return values, exception details, and warning details",
                "assertion expressions, expected values, and actual values",
                "pytest stdout and stderr",
                "SC-J2 through SC-J5 results",
            ],
        }
    replay_count = behavior_observations.get("replay_count")
    runs = behavior_observations.get("runs")
    if (
        not isinstance(replay_count, int)
        or replay_count < 1
        or not isinstance(runs, list)
        or len(runs) != replay_count
    ):
        raise JudgeError("J1 source observation runs are invalid")
    stable_nodeids = behavior_observations.get("stable_passed_nodeids")
    if not isinstance(stable_nodeids, list) or not all(
        isinstance(item, str) and item for item in stable_nodeids
    ):
        raise JudgeError("J1 stable passed nodeids are invalid")

    projected_runs: list[dict[str, Any]] = []
    for expected_index, run in enumerate(runs, start=1):
        if not isinstance(run, dict) or run.get("status") != "pass":
            raise JudgeError("J1 source observation run did not pass")
        run_index = run.get("run_index")
        if run_index != expected_index:
            raise JudgeError("J1 source observation run index is invalid")
        passed_nodeids = run.get("passed_nodeids")
        invocations = run.get("invocations")
        warnings = run.get("warnings")
        assertions = run.get("assertions")
        run_id = run.get("run_id")
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(passed_nodeids, list)
            or not all(isinstance(item, str) for item in passed_nodeids)
            or not isinstance(invocations, list)
            or not isinstance(warnings, list)
            or not isinstance(assertions, list)
        ):
            raise JudgeError("J1 source observation run payload is invalid")

        projected_calls: list[dict[str, Any]] = []
        known_call_ids: set[str] = set()
        for invocation in invocations:
            if not isinstance(invocation, dict):
                raise JudgeError("J1 invocation is not an object")
            call_id = invocation.get("call_id")
            parent_call_id = invocation.get("parent_call_id")
            nodeid = invocation.get("nodeid")
            phase = invocation.get("phase")
            target = invocation.get("target")
            outcome = invocation.get("outcome")
            arguments = invocation.get("arguments")
            test_execution_id = invocation.get("test_execution_id")
            event_kind = invocation.get("event_kind")
            implementation_origin = invocation.get("implementation_origin")
            implementation_path = invocation.get("implementation_path")
            implementation_sha256 = invocation.get("implementation_sha256")
            production_call_observed = invocation.get(
                "production_call_observed"
            )
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in known_call_ids
                or (
                    parent_call_id is not None
                    and not isinstance(parent_call_id, str)
                )
                or not isinstance(nodeid, str)
                or not nodeid
                or not isinstance(phase, str)
                or not phase
                or not isinstance(target, str)
                or not target
                or outcome not in {"return", "exception"}
                or not isinstance(arguments, dict)
                or test_execution_id != f"{run_id}:{nodeid}"
                or event_kind
                not in {"python_call", "synthetic_production_line_event"}
                or implementation_origin
                not in {"target_repository", "frozen_dependency", "untrusted"}
                or not isinstance(implementation_path, str)
                or not implementation_path
                or (
                    implementation_sha256 is not None
                    and not isinstance(implementation_sha256, str)
                )
                or not isinstance(production_call_observed, bool)
            ):
                raise JudgeError("J1 invocation fields are invalid")
            known_call_ids.add(call_id)
            projected = {
                "test_execution_id": test_execution_id,
                "nodeid": nodeid,
                "phase": phase,
                "call_id": call_id,
                "parent_call_id": parent_call_id,
                "target": target,
                "event_kind": event_kind,
                "production_call_observed": production_call_observed,
                "implementation_origin": implementation_origin,
                "implementation_path": implementation_path,
                "implementation_firstlineno": invocation.get(
                    "implementation_firstlineno"
                ),
                "implementation_sha256": implementation_sha256,
                "arguments": arguments,
                "outcome": outcome,
            }
            projected_calls.append(projected)

        for call in projected_calls:
            parent = call["parent_call_id"]
            if parent is not None and parent not in known_call_ids:
                raise JudgeError("J1 invocation parent is unresolved")

        projected_warnings: list[dict[str, Any]] = []
        for warning in warnings:
            if not isinstance(warning, dict):
                raise JudgeError("J1 warning is not an object")
            warning_id = warning.get("warning_id")
            nodeid = warning.get("nodeid")
            phase = warning.get("phase")
            active_call_ids = warning.get("active_call_ids")
            test_execution_id = warning.get("test_execution_id")
            if (
                not isinstance(warning_id, str)
                or not warning_id
                or not isinstance(nodeid, str)
                or not nodeid
                or not isinstance(phase, str)
                or not isinstance(active_call_ids, list)
                or not all(
                    isinstance(item, str) and item in known_call_ids
                    for item in active_call_ids
                )
                or test_execution_id != f"{run_id}:{nodeid}"
            ):
                raise JudgeError("J1 warning fields are invalid")
            projected_warnings.append(
                {
                    "test_execution_id": test_execution_id,
                    "nodeid": nodeid,
                    "phase": phase,
                    "warning_id": warning_id,
                    "active_call_ids": active_call_ids,
                    "warning_observed": True,
                }
            )

        projected_assertions: list[dict[str, Any]] = []
        for assertion in assertions:
            if not isinstance(assertion, dict):
                raise JudgeError("J1 assertion event is not an object")
            assertion_id = assertion.get("assertion_id")
            nodeid = assertion.get("nodeid")
            test_execution_id = assertion.get("test_execution_id")
            kind = assertion.get("kind")
            source_path = assertion.get("source_path")
            source_line = assertion.get("source_line")
            reached = assertion.get("reached")
            passed = assertion.get("passed")
            if (
                not isinstance(assertion_id, str)
                or not assertion_id
                or not isinstance(nodeid, str)
                or not nodeid
                or test_execution_id != f"{run_id}:{nodeid}"
                or kind not in {
                    "assert",
                    "pytest.raises",
                    "pytest.warns",
                    "pytest.deprecated_call",
                }
                or not isinstance(source_path, str)
                or not source_path
                or not isinstance(source_line, int)
                or source_line < 1
                or reached is not True
                or passed not in {True, None}
            ):
                raise JudgeError("J1 assertion event fields are invalid")
            projected_assertions.append(
                {
                    "assertion_id": assertion_id,
                    "test_execution_id": test_execution_id,
                    "nodeid": nodeid,
                    "kind": kind,
                    "source_path": source_path,
                    "source_line": source_line,
                    "reached": True,
                    "passed": passed,
                }
            )

        projected_runs.append(
            {
                "run_index": run_index,
                "run_id": run_id,
                "status": "pass",
                "passed_nodeids": passed_nodeids,
                "calls": projected_calls,
                "warnings": projected_warnings,
                "assertions": projected_assertions,
            }
        )

    return {
        "schema_version": J1_EXECUTION_EVIDENCE_SCHEMA,
        "status": "pass",
        "source": "projected positive-behavior-observations.json",
        "source_observation_sha256": sha256_json(behavior_observations),
        "replay_count": replay_count,
        "stable_passed_nodeids": stable_nodeids,
        "runs": projected_runs,
        "excluded": [
            "SC-E1 result",
            "SC-E2-v2 result",
            "E2 obligation classifications",
            "return values, exception details, and warning details",
            "assertion expressions, expected values, and actual values",
            "pytest stdout and stderr",
            "SC-J2 through SC-J5 results",
        ],
    }


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JudgeError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise JudgeError(f"expected JSON object: {path}")
    return value


def _load_rubric(path: Path) -> list[dict[str, Any]] | dict[str, Any]:
    """Load either the v4 array or a legacy wrapped Rubric document."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JudgeError(f"cannot read Rubric JSON {path}: {exc}") from exc
    if isinstance(value, list):
        if not value or not all(isinstance(item, dict) for item in value):
            raise JudgeError(f"Rubric array is empty or invalid: {path}")
        return value
    if isinstance(value, dict):
        return value
    raise JudgeError(f"Rubric JSON root is invalid: {path}")


def _load_j5_applicability_map(evaluation_assets: Path) -> dict[str, Any] | None:
    path = evaluation_assets / "j5_applicability_map.json"
    return _load_object(path) if path.is_file() else None


def _candidate_files(root: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise JudgeError(f"candidate file is not UTF-8: {path}") from exc
        result.append(
            {
                "path": f"candidate_tests/{path.relative_to(root).as_posix()}",
                "content": content,
            }
        )
    if not result:
        raise JudgeError("candidate test source is absent")
    return result


def _fault_ids(fault_replay: dict[str, Any]) -> set[str]:
    records = fault_replay.get("faults")
    if not isinstance(records, list):
        raise JudgeError("fault replay has no faults array")
    result: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise JudgeError("fault replay record must be an object")
        fault_id = item.get("fault_id")
        if not isinstance(fault_id, str) or not fault_id:
            raise JudgeError("fault replay record identity is invalid")
        if fault_id in result:
            raise JudgeError(f"duplicate fault replay record: {fault_id}")
        if not isinstance(item.get("classification"), str):
            raise JudgeError(f"fault replay classification is invalid: {fault_id}")
        result.add(fault_id)
    return result


def _stable_killed_fault_ids(fault_replay: dict[str, Any]) -> set[str]:
    known = _fault_ids(fault_replay)
    records = fault_replay["faults"]
    return {
        str(item["fault_id"])
        for item in records
        if item["fault_id"] in known
        and item.get("classification") == "stable_killed"
    }


def _j5_unit_applicability(
    facts: dict[str, Any],
    stable_killed: set[str],
) -> dict[str, bool]:
    units = facts.get("diagnostic_units")
    if not isinstance(units, list):
        raise JudgeError("J5 facts have no diagnostic_units array")
    result: dict[str, bool] = {}
    for unit in units:
        if not isinstance(unit, dict):
            raise JudgeError("J5 diagnostic unit must be an object")
        unit_id, fault_ids = unit.get("diagnostic_unit_id"), unit.get("fault_ids")
        if not isinstance(unit_id, str) or not isinstance(fault_ids, list):
            raise JudgeError("J5 diagnostic unit identity is invalid")
        result[unit_id] = bool(stable_killed.intersection(map(str, fault_ids)))
    return result


def _j5_fault_evidence(
    fault_replay: dict[str, Any],
    fault_log_dir: Path,
) -> list[dict[str, Any]]:
    stable = _stable_killed_fault_ids(fault_replay)
    raw_replay_count = fault_replay.get("replay_count")
    if (
        type(raw_replay_count) is not int
        or raw_replay_count != J5_REQUIRED_REPLAY_COUNT
    ):
        raise JudgeError(
            "fault replay_count must be exactly "
            f"{J5_REQUIRED_REPLAY_COUNT}, got {raw_replay_count!r}"
        )
    replay_count = raw_replay_count
    records = fault_replay.get("faults", [])
    evidence: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or record.get("fault_id") not in stable:
            continue
        fault_id = str(record["fault_id"])
        logs: list[dict[str, Any]] = []
        replay_indexes: list[int] = []
        for path in sorted(fault_log_dir.glob(f"{fault_id}-replay-*.log")):
            match = re.fullmatch(
                rf"{re.escape(fault_id)}-replay-(\d+)\.log",
                path.name,
            )
            if match is None:
                raise JudgeError(
                    f"stable-killed fault {fault_id} has invalid replay log name"
                )
            replay_index = int(match.group(1))
            raw_content = path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            if not raw_content.strip():
                raise JudgeError(
                    f"stable-killed fault {fault_id} replay {replay_index} is empty"
                )
            normalized_content = normalize_fault_log(raw_content)
            replay_indexes.append(replay_index)
            logs.append(
                {
                    "path": f"logs/faults/{path.name}",
                    "replay_index": replay_index,
                    "content": normalized_content,
                    "raw_sha256": hashlib.sha256(
                        raw_content.encode("utf-8")
                    ).hexdigest(),
                    "normalized_sha256": hashlib.sha256(
                        normalized_content.encode("utf-8")
                    ).hexdigest(),
                }
            )
        if not logs:
            raise JudgeError(f"stable-killed fault {fault_id} has no replay logs")
        if sorted(replay_indexes) != list(
            range(1, replay_count + 1)
        ):
            raise JudgeError(
                f"stable-killed fault {fault_id} replay logs are incomplete: "
                f"expected 1..{replay_count}, got {sorted(replay_indexes)}"
            )
        evidence.append(
            {
                "fault_id": fault_id,
                "classification": "stable_killed",
                "logs": logs,
            }
        )
    return evidence


def _j5_rubric_evidence_scope(
    rubric: list[dict[str, Any]] | dict[str, Any],
    applicability_map: dict[str, Any],
    fault_evidence: list[dict[str, Any]],
    known_fault_ids: set[str],
) -> list[dict[str, Any]]:
    """Project stable-killed fault evidence into one explicit scope per Rubric."""

    rubric_items = (
        rubric if isinstance(rubric, list) else rubric.get("rubrics")
    )
    if not isinstance(rubric_items, list) or not rubric_items:
        raise JudgeError("J5 rubric is empty or invalid")
    entries = applicability_map.get("rubrics")
    if not isinstance(entries, list):
        raise JudgeError("J5 applicability map has no rubrics array")

    mapped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise JudgeError("J5 applicability entry must be an object")
        rubric_id, fault_ids = entry.get("rubric_id"), entry.get("fault_ids")
        if (
            not isinstance(rubric_id, str)
            or not isinstance(fault_ids, list)
            or not fault_ids
            or not all(isinstance(item, str) and item for item in fault_ids)
        ):
            raise JudgeError("J5 applicability entry is invalid")
        if rubric_id in mapped:
            raise JudgeError(f"duplicate J5 applicability: {rubric_id}")
        mapped[rubric_id] = entry

    mapped_fault_ids = {
        str(fault_id)
        for entry in mapped.values()
        for fault_id in entry["fault_ids"]
    }
    missing_fault_ids = mapped_fault_ids - known_fault_ids
    if missing_fault_ids:
        raise JudgeError(
            "J5 applicability map references faults absent from fault replay: "
            + ", ".join(sorted(missing_fault_ids))
        )

    evidence_by_fault: dict[str, dict[str, Any]] = {}
    for item in fault_evidence:
        fault_id = item.get("fault_id")
        if not isinstance(fault_id, str) or fault_id in evidence_by_fault:
            raise JudgeError("J5 fault evidence identity is invalid")
        evidence_by_fault[fault_id] = item

    rubric_ids = {
        str(item["rubric_id"])
        for item in rubric_items
        if isinstance(item, dict) and isinstance(item.get("rubric_id"), str)
    }
    if len(rubric_ids) != len(rubric_items) or set(mapped) != rubric_ids:
        raise JudgeError("J5 applicability map does not match frozen rubric IDs")

    result: list[dict[str, Any]] = []
    for rubric_item in rubric_items:
        rubric_id = str(rubric_item["rubric_id"])
        entry = mapped[rubric_id]
        fault_ids = list(entry["fault_ids"])
        scope: dict[str, Any] = {
            "rubric_id": rubric_id,
            "fault_ids": fault_ids,
            "fault_evidence": [
                evidence_by_fault[fault_id]
                for fault_id in fault_ids
                if fault_id in evidence_by_fault
            ],
        }
        unit_id = entry.get("diagnostic_unit_id")
        if isinstance(unit_id, str):
            scope["diagnostic_unit_id"] = unit_id
        result.append(scope)
    return result


def normalize_fault_log(content: str) -> str:
    """Remove volatile run metadata without changing failure semantics."""

    normalized = re.sub(r"0x[0-9a-fA-F]+", "0x<ADDR>", content)
    normalized = re.sub(
        r"(?:/[^\s:'\"]+)*?/scrapy6546-evaluation-faults-[^/\s:'\"]+",
        "/<FAULT_WORKSPACE>",
        normalized,
    )
    normalized = re.sub(
        r"(?:/private)?/tmp/[^\s:'\"]+",
        "/<TMP_PATH>",
        normalized,
    )
    normalized = re.sub(
        r"(?m)(=+\s+.+?\s+in\s+)\d+(?:\.\d+)?s(\s+=+)$",
        r"\1<ELAPSED>\2",
        normalized,
    )
    return normalized


def build_dimension_snapshot(
    *,
    dimension: str,
    task_core_fingerprint: str,
    evaluation_id: str,
    candidate_root: Path,
    evaluation_assets: Path,
    fault_replay: dict[str, Any],
    fault_log_dir: Path,
    sc_e1: dict[str, Any],
    sc_e2: dict[str, Any],
    hard_gates: dict[str, Any] | None = None,
    j1_execution_evidence: dict[str, Any] | None = None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build one self-contained, dimension-isolated Judge snapshot."""

    if dimension not in DIMENSIONS:
        raise JudgeError(f"unknown Judge dimension: {dimension}")
    suffix = dimension[-1].lower()
    rubric = _load_rubric(evaluation_assets / "rubrics" / f"j{suffix}.json")
    facts = _load_object(evaluation_assets / f"j{suffix}_facts.json")
    evidence_policy_path = evaluation_assets / "judge_evidence_policy.json"
    evidence_policy = (
        _load_object(evidence_policy_path)
        if evidence_policy_path.is_file()
        else {
            "schema_version": "unittest-rl-judge-evidence-policy-runtime-v2",
            "status": "runtime_stability_default",
            "conflict_rules": [
                "Use direct evidence only and never infer missing execution.",
                "Every applicable rubric receives score 0 or 1. If direct "
                "evidence is malformed, incomplete, empty, or unresolved, "
                "the affected required condition is unproved and scores 0; "
                "the reason must identify the evidence defect.",
                "SC-E2 obligation evidence does not prove SC-J1 through "
                "SC-J5 or candidate assertion correctness.",
            ],
            "non_overlap_contract": {
                "general": [
                    "The same evidence record or path may support multiple "
                    "rubrics when it contains distinct observable facets; "
                    "do not require separate files, lines, or failure records.",
                    "Treat shared entrypoints, test_execution_id, and "
                    "production provenance as evidence constraints.",
                    "For each rubric identify its behavior-specific terminal, "
                    "exact oracle, structural property, relation edge, or "
                    "diagnostic property; do not repeat another rubric's "
                    "scoring rationale as the sole justification.",
                ],
                "SC-J4": [
                    "Score only independent and distinguishable relation "
                    "endpoints; do not re-score SC-J2 exact oracle values."
                ],
                "SC-J5": {
                    "failure_location": "Use node, file, and assertion location.",
                    "expected_actual_visible": "Use labeled expected and actual.",
                    "input_context": (
                        "Use direct input provenance from the input area, full "
                        "parameter/call arguments, or candidate source and "
                        "attribute-access records bound to the same failing "
                        "node; do not use expected text alone. Profile-level "
                        "preconditions are applicability facts and need not "
                        "be repeated in candidate output."
                    ),
                    "no_cascade_noise": "Use complete failure-set behavior membership.",
                },
            },
            "dimension_contract": {
                "SC-J1": [
                    "Use j1_execution_evidence for production calls and "
                    "test_execution_id; do not infer object identity or "
                    "assertion correctness when those fields are absent."
                ],
                "SC-J4": [
                    "Bind candidate source assertions to successful "
                    "collection and isolated execution; do not substitute "
                    "SC-E2 obligation status."
                ],
                "SC-J5": [
                    "Use only the matching j5_rubric_evidence_scope entry, "
                    "read every replay, and never use another rubric's faults."
                ],
            },
            "reason_contract": [
                "Cite direct snapshot evidence for every necessary condition.",
                "Name this rubric's unique scoring object rather than a shared prefix.",
                "Name the specific missing or contradicted condition for score 0.",
            ],
        }
    )
    input_contract_path = evaluation_assets / "judge_input_contract.json"
    input_contract = (
        _load_object(input_contract_path)
        if input_contract_path.is_file()
        else {
            "schema_version": "unittest-rl-judge-input-contract-v1",
            "status": "runtime_default",
            "candidate_source": "required_for_all_dimensions",
        }
    )
    snapshot: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA,
        "task_core_fingerprint": task_core_fingerprint,
        "evaluation_id": evaluation_id,
        "dimension": dimension,
        "model_key": config["model_key"],
        "judge_model": config["model"],
        "judge_runtime": {
            "runner": config.get("runner", "api"),
            "protocol": config.get("protocol", "responses"),
            "runner_version": config.get("runner_version"),
            "runtime_config_sha256": config.get("runtime_config_sha256"),
        },
        "candidate_tree_sha256": candidate_tree_sha256(candidate_root),
        "candidate_files": _candidate_files(candidate_root),
        "rubric": rubric,
        "facts": facts,
        "evidence_policy": evidence_policy,
        "evidence_policy_sha256": sha256_json(evidence_policy),
        "input_contract": input_contract,
        "input_contract_sha256": sha256_json(input_contract),
        "applicability_policy": (
            J5_RUNTIME_APPLICABILITY
            if dimension == "SC-J5"
            else TASK_FROZEN_APPLICABILITY
        ),
        "input_evidence_manifest": {
            "policy": "dimension_specific_minimum_evidence",
            "included": [
                f"evaluation_assets/rubrics/j{suffix}.json",
                f"evaluation_assets/j{suffix}_facts.json",
                "evaluation_assets/judge_evidence_policy.json",
                "evaluation_assets/judge_input_contract.json",
                "candidate_tests/",
            ],
            "excluded_score_components": ["SC-E1", "SC-E2-v2"],
        },
    }
    if dimension == "SC-J1" and j1_execution_evidence is not None:
        if (
            j1_execution_evidence.get("schema_version")
            not in J1_EXECUTION_EVIDENCE_SCHEMAS
            or j1_execution_evidence.get("status") != "pass"
        ):
            raise JudgeError("J1 execution evidence is invalid")
        snapshot["j1_execution_evidence"] = j1_execution_evidence
        snapshot["input_evidence_manifest"]["included"].append(
            "judge-input/j1_execution_evidence"
        )
    if dimension in {"SC-J2", "SC-J3", "SC-J4"} and hard_gates is not None:
        snapshot["positive_execution_evidence"] = hard_gates
        snapshot["input_evidence_manifest"]["included"].append(
            "scoring/hard-gates.json"
        )
    if dimension == "SC-J5":
        applicability_map = _load_j5_applicability_map(evaluation_assets)
        if applicability_map is not None:
            snapshot["j5_applicability_map"] = applicability_map
            snapshot["input_evidence_manifest"]["included"].append(
                "evaluation_assets/j5_applicability_map.json"
            )
        snapshot["hard_evidence"] = {"SC-E1": sc_e1}
        snapshot["input_evidence_manifest"]["included"].append(
            "scoring/sc-e1.json"
        )
        snapshot["input_evidence_manifest"]["excluded_score_components"] = [
            "SC-E2-v2"
        ]
        fault_evidence = _j5_fault_evidence(
            fault_replay,
            fault_log_dir,
        )
        if applicability_map is not None:
            snapshot["j5_rubric_evidence_scope"] = _j5_rubric_evidence_scope(
                rubric,
                applicability_map,
                fault_evidence,
                _fault_ids(fault_replay),
            )
        else:
            snapshot["stable_killed_fault_evidence"] = fault_evidence
    snapshot["input_snapshot_sha256"] = sha256_json(snapshot)
    return snapshot


def _rubrics(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    payload = snapshot.get("rubric")
    rubrics = payload if isinstance(payload, list) else (
        payload.get("rubrics") if isinstance(payload, dict) else None
    )
    if not isinstance(rubrics, list) or not rubrics:
        raise JudgeError("Judge snapshot has no rubrics")
    if not all(isinstance(item, dict) for item in rubrics):
        raise JudgeError("Judge rubrics must be objects")
    return rubrics


def _expected_applicability(
    snapshot: dict[str, Any],
) -> dict[str, bool]:
    rubrics = _rubrics(snapshot)
    if snapshot["dimension"] != "SC-J5":
        # Presence in a frozen J1-J4 rubric asset means task-applicable.  The
        # candidate cannot shrink the denominator by omitting the behavior;
        # omission is a normal score of zero.  Legacy applies_if prose is not
        # re-evaluated after candidate generation.
        return {str(item["rubric_id"]): True for item in rubrics}
    scoped_evidence = snapshot.get("j5_rubric_evidence_scope")
    if isinstance(scoped_evidence, list):
        stable = {
            str(fault["fault_id"])
            for scope in scoped_evidence
            if isinstance(scope, dict)
            for fault in scope.get("fault_evidence", [])
            if isinstance(fault, dict) and isinstance(fault.get("fault_id"), str)
        }
    else:
        stable = {
            str(item["fault_id"])
            for item in snapshot.get("stable_killed_fault_evidence", [])
            if isinstance(item, dict) and isinstance(item.get("fault_id"), str)
        }
    applicability_map = snapshot.get("j5_applicability_map")
    mapped_faults: dict[str, set[str]] = {}
    if isinstance(applicability_map, dict):
        entries = applicability_map.get("rubrics")
        if not isinstance(entries, list):
            raise JudgeError("J5 applicability map has no rubrics array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise JudgeError("J5 applicability entry must be an object")
            rubric_id = entry.get("rubric_id")
            fault_ids = entry.get("fault_ids")
            if (
                not isinstance(rubric_id, str)
                or not isinstance(fault_ids, list)
                or not fault_ids
                or not all(isinstance(item, str) for item in fault_ids)
            ):
                raise JudgeError("J5 applicability entry is invalid")
            if rubric_id in mapped_faults:
                raise JudgeError(f"duplicate J5 applicability: {rubric_id}")
            mapped_faults[rubric_id] = set(fault_ids)
    else:
        units = _j5_unit_applicability(snapshot["facts"], stable)
    result: dict[str, bool] = {}
    for item in rubrics:
        rubric_id = item.get("rubric_id")
        if not isinstance(rubric_id, str):
            raise JudgeError("J5 rubric identity is invalid")
        if mapped_faults:
            if rubric_id not in mapped_faults:
                raise JudgeError(
                    f"J5 applicability is missing rubric: {rubric_id}"
                )
            result[rubric_id] = bool(mapped_faults[rubric_id] & stable)
        else:
            unit_id = item.get("diagnostic_unit_id")
            if not isinstance(unit_id, str):
                raise JudgeError("legacy J5 rubric unit identity is invalid")
            if unit_id not in units:
                raise JudgeError(f"J5 rubric references unknown unit: {unit_id}")
            result[rubric_id] = units[unit_id]
    return result


def _canonical_evidence_paths(snapshot: dict[str, Any]) -> list[str]:
    """Return the exact, snapshot-backed paths a Judge result may cite."""

    suffix = str(snapshot["dimension"])[-1].lower()
    paths = {
        f"evaluation_assets/rubrics/j{suffix}.json",
        f"evaluation_assets/j{suffix}_facts.json",
        "evaluation_assets/judge_evidence_policy.json",
        "evaluation_assets/judge_input_contract.json",
    }
    if "positive_execution_evidence" in snapshot:
        paths.add("scoring/hard-gates.json")
    if "j1_execution_evidence" in snapshot:
        paths.add("judge-input/j1_execution_evidence")
    if snapshot["dimension"] == "SC-J5":
        paths.add("scoring/sc-e1.json")
        if "j5_applicability_map" in snapshot:
            paths.add("evaluation_assets/j5_applicability_map.json")
    for item in snapshot.get("candidate_files", []):
        if isinstance(item, dict):
            path = item.get("path")
            if isinstance(path, str) and path.startswith("candidate_tests/"):
                paths.add(path)
    for fault in snapshot.get("stable_killed_fault_evidence", []):
        if not isinstance(fault, dict):
            continue
        for log in fault.get("logs", []):
            if not isinstance(log, dict):
                continue
            path = log.get("path")
            if isinstance(path, str) and path.startswith("logs/faults/"):
                paths.add(path)
    for scope in snapshot.get("j5_rubric_evidence_scope", []):
        if not isinstance(scope, dict):
            continue
        for fault in scope.get("fault_evidence", []):
            if not isinstance(fault, dict):
                continue
            for log in fault.get("logs", []):
                if not isinstance(log, dict):
                    continue
                path = log.get("path")
                if isinstance(path, str) and path.startswith("logs/faults/"):
                    paths.add(path)
    return sorted(paths)


def structured_output_schema(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Responses API strict JSON schema for one dimension."""

    rubric_ids = [str(item["rubric_id"]) for item in _rubrics(snapshot)]
    task_frozen = snapshot["dimension"] != "SC-J5"
    evidence_paths = _canonical_evidence_paths(snapshot)
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "const": MODEL_RESULT_SCHEMA},
            "dimension": {"type": "string", "const": snapshot["dimension"]},
            "input_snapshot_sha256": {
                "type": "string",
                "const": snapshot["input_snapshot_sha256"],
            },
            "rubric_results": {
                "type": "array",
                "minItems": len(rubric_ids),
                "maxItems": len(rubric_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "rubric_id": {"type": "string", "enum": rubric_ids},
                        "applicable": (
                            {"type": "boolean", "const": True}
                            if task_frozen
                            else {"type": "boolean"}
                        ),
                        "score": (
                            {"type": "integer", "enum": [0, 1]}
                            if task_frozen
                            else {
                                "type": ["integer", "null"],
                                "enum": [0, 1, None],
                            }
                        ),
                        "reason": {"type": "string", "minLength": 8},
                    },
                    "required": [
                        "rubric_id",
                        "applicable",
                        "score",
                        "reason",
                    ],
                    "additionalProperties": False,
                },
            },
            "evidence_paths": {
                "type": "array",
                "items": {"type": "string", "enum": evidence_paths},
                "minItems": 1,
                "maxItems": len(evidence_paths),
            },
            "summary": {"type": "string", "minLength": 8},
        },
        "required": [
            "schema_version",
            "dimension",
            "input_snapshot_sha256",
            "rubric_results",
            "evidence_paths",
            "summary",
        ],
        "additionalProperties": False,
    }


def _prompt(snapshot: dict[str, Any]) -> tuple[str, str]:
    non_overlap_instruction = (
        "The same evidence record or path may support multiple rubrics when "
        "it contains distinct observable facets; do not require separate "
        "files, lines, or failure records. Treat shared entrypoints, "
        "production provenance, and test_execution_id as evidence "
        "constraints. For every rubric, identify and justify its unique "
        "behavior terminal, exact oracle, structural property, relation edge, "
        "or diagnostic property. Do not repeat another rubric's scoring "
        "rationale as the sole justification. For SC-J4 score only whether "
        "both relation "
        "endpoints are independently instantiated, distinguishable, and "
        "checked; do not re-score SC-J2 exact expected values. For SC-J5, "
        "failure_location uses node/file/assertion location, "
        "expected_actual_visible uses labeled expected/actual, input_context "
        "uses direct input provenance from the failure record or candidate "
        "source bound to the same failing node rather than expected text alone, "
        "and profile-level preconditions are not required to be repeated in "
        "candidate output. "
        "no_cascade_noise uses complete failure-set behavior membership. "
    )
    j5_scope_instruction = (
        "For SC-J5, evaluate each rubric only from the matching rubric_id "
        "entry in j5_rubric_evidence_scope. Read every replay log in that "
        "entry and never use another rubric's fault scope to fill a "
        "condition. Replay completeness is a verifier gate, not a score-0 "
        "condition. For a no_cascade_noise rubric inspect the complete "
        "candidate failure set in every mapped replay; for other J5 "
        "directions inspect only failure records tied to that rubric's "
        "behavior and required observable. "
        if "j5_rubric_evidence_scope" in snapshot
        else "For a legacy SC-J5 snapshot without rubric evidence scopes, "
        "use only stable-killed faults mapped to that rubric's diagnostic "
        "unit. "
    )
    instructions = (
        "You are the formal Unit Test RL binary rubric judge. Evaluate only "
        "the supplied dimension and only the supplied frozen evidence. Return "
        "the strict JSON object required by the response schema. Give each "
        "rubric exactly one result. Compare the evidence independently with "
        "that rubric's score_1_condition and score_0_condition. For SC-J1 "
        "through SC-J4, every "
        "rubric present in the frozen task asset is applicable: always return "
        "applicable=true, and score candidate omission as 0 rather than making "
        "the item non-applicable. Legacy applies_if prose must not shrink this "
        "task-frozen denominator. For SC-J5, preserve the supplied fault "
        "applicability: non-applicable items require applicable=false and "
        "score=null. Apply the supplied evidence_policy exactly. Never infer "
        "missing execution evidence from source, rubric text, facts, "
        "candidate_absent, or stable_killed. Treat every conjunction in "
        "score_1_condition as necessary: explain every required condition and cite the "
        "evidence path or identifier that proves it. When scoring 0, name the "
        "specific missing, contradicted, or unexecuted condition. "
        + non_overlap_instruction
        + "For "
        "SC-J1, j1_execution_evidence is the formal source for candidate "
        "entrypoint, production-call, same-test-execution, warning, and "
        "assertion-reach facts. "
        "The frozen J1 facts define the required path but never prove that the "
        "candidate executed it; candidate source and hard-gate passage also "
        "never prove a missing J1 call. A production_call_observed event proves "
        "that named production function ran in its test_execution_id; compare "
        "call_id and parent_call_id only when the Rubric requires a nested call. "
        "Candidate source proves that an assertion structure exists, while an "
        "assertion event proves that its source location was reached in the "
        "named test_execution_id. Never use either one alone to prove both. "
        + j5_scope_instruction
        + "For "
        "evidence_paths, select only exact path strings exposed by the "
        "response schema enum; never invent JSON field paths or append line "
        "numbers, identifiers, ranges, or explanations. When evidence "
        "conflicts, inspect the complete supplied records and explain which "
        "precedence rule selected the controlling evidence and why the "
        "lower-priority evidence was not used. Every applicable rubric must "
        "still receive score 0 or 1; if a required condition remains unproved "
        "after that inspection, score it 0 and identify the unresolved "
        "evidence in the reason."
    )
    user_input = (
        "Evaluate this immutable Judge snapshot:\n"
        + json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    )
    return instructions, user_input


def _validate_evidence_paths(
    paths: Any,
    *,
    snapshot: dict[str, Any],
) -> list[str]:
    if not isinstance(paths, list) or not paths:
        raise JudgeError("Judge result evidence_paths must be non-empty")
    allowed = set(_canonical_evidence_paths(snapshot))
    result: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise JudgeError("Judge evidence path is invalid")
        if raw.startswith("/") or ".." in Path(raw).parts:
            raise JudgeError(f"Judge evidence path escapes snapshot: {raw}")
        if raw not in allowed:
            raise JudgeError(f"Judge evidence path is outside allowlist: {raw}")
        result.append(raw)
    return result


def formalize_result(
    model_result: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    mode: str,
    replay_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Independently validate and aggregate model-returned rubric decisions."""

    if model_result.get("schema_version") != MODEL_RESULT_SCHEMA:
        raise JudgeError("Judge model result schema is invalid")
    for key in ("dimension", "input_snapshot_sha256"):
        if model_result.get(key) != snapshot.get(key):
            raise JudgeError(f"Judge model result {key} mismatch")
    expected_applicability = _expected_applicability(snapshot)
    rubric_ids = [str(item["rubric_id"]) for item in _rubrics(snapshot)]
    items = model_result.get("rubric_results")
    if not isinstance(items, list):
        raise JudgeError("Judge model result has no rubric_results array")
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(
            item.get("rubric_id"), str
        ):
            raise JudgeError("Judge rubric result identity is invalid")
        rubric_id = item["rubric_id"]
        if rubric_id in by_id:
            raise JudgeError(f"duplicate Judge rubric result: {rubric_id}")
        by_id[rubric_id] = item
    if set(by_id) != set(rubric_ids):
        raise JudgeError("Judge rubric result IDs do not match frozen rubric")

    ordered: list[dict[str, Any]] = []
    earned = 0
    applicable_count = 0
    rubric_by_id = {
        str(item["rubric_id"]): item for item in _rubrics(snapshot)
    }
    for rubric_id in rubric_ids:
        item = by_id[rubric_id]
        applicable = item.get("applicable")
        if type(applicable) is not bool:
            raise JudgeError(f"Judge applicability must be boolean: {rubric_id}")
        expected = expected_applicability[rubric_id]
        if applicable is not expected:
            raise JudgeError(f"Judge applicability mismatch: {rubric_id}")
        score = item.get("score")
        if applicable:
            if type(score) is not int or score not in (0, 1):
                raise JudgeError(f"Judge score must be binary: {rubric_id}")
            earned += score
            applicable_count += 1
        elif score is not None:
            raise JudgeError(
                f"non-applicable Judge score must be null: {rubric_id}"
            )
        reason = item.get("reason")
        if not isinstance(reason, str) or len(reason.strip()) < 8:
            raise JudgeError(f"Judge reason is too short: {rubric_id}")
        formal_item = {
            "rubric_id": rubric_id,
            "applicable": applicable,
            "score": score,
            "reason": reason,
        }
        unit_id = rubric_by_id[rubric_id].get("diagnostic_unit_id")
        if isinstance(unit_id, str):
            formal_item["diagnostic_unit_id"] = unit_id
        ordered.append(formal_item)
    if applicable_count == 0:
        raise JudgeError("Judge dimension has no applicable rubrics")
    score = earned / applicable_count
    return {
        "schema_version": FORMAL_RESULT_SCHEMA,
        "status": "pass",
        "task_core_fingerprint": snapshot["task_core_fingerprint"],
        "evaluation_id": snapshot["evaluation_id"],
        "model_key": snapshot["model_key"],
        "judge_model": snapshot["judge_model"],
        "dimension": snapshot["dimension"],
        "score": score,
        "evidence_paths": _validate_evidence_paths(
            model_result.get("evidence_paths"),
            snapshot=snapshot,
        ),
        "input_snapshot_sha256": snapshot["input_snapshot_sha256"],
        **(
            {"panel_snapshot_sha256": snapshot["panel_snapshot_sha256"]}
            if isinstance(snapshot.get("panel_snapshot_sha256"), str)
            else {}
        ),
        "candidate_tree_sha256": snapshot["candidate_tree_sha256"],
        "applicability_policy": snapshot["applicability_policy"],
        "applicable_rubric_count": applicable_count,
        "earned_rubric_count": earned,
        "rubric_results": ordered,
        "summary": model_result["summary"],
        "execution_mode": mode,
        **(
            {"replay_source_sha256": replay_source_sha256}
            if replay_source_sha256 is not None
            else {}
        ),
    }


def _response_text(response: dict[str, Any]) -> str:
    if response.get("status") != "completed":
        detail = response.get("incomplete_details") or response.get("error")
        raise JudgeError(f"Responses API did not complete: {detail!r}")
    for output in response.get("output", []):
        if not isinstance(output, dict) or output.get("type") != "message":
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal":
                raise JudgeError(f"Judge refused request: {content.get('refusal')}")
            if content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text:
                    return text
    raise JudgeError("Responses API returned no output_text")


def _live_model_result(
    snapshot: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    instructions, user_input = _prompt(snapshot)
    try:
        return invoke_judge(
            config=config,
            instructions=instructions,
            user_input=user_input,
            output_schema=structured_output_schema(snapshot),
        )
    except JudgeRuntimeError as exc:
        raise JudgeError(str(exc)) from exc


def _replay_model_result(
    snapshot: dict[str, Any],
    replay: dict[str, Any],
    *,
    round_index: int = 1,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if replay.get("schema_version") != REPLAY_SCHEMA:
        raise JudgeError("Judge replay schema is invalid")
    if replay.get("task_core_fingerprint") != snapshot["task_core_fingerprint"]:
        raise JudgeError("Judge replay task fingerprint mismatch")
    if replay.get("candidate_tree_sha256") != snapshot["candidate_tree_sha256"]:
        raise JudgeError("Judge replay candidate tree mismatch")
    rounds = replay.get("rounds")
    if isinstance(rounds, list):
        matching = [
            item
            for item in rounds
            if isinstance(item, dict) and item.get("round") == round_index
        ]
        if len(matching) != 1:
            raise JudgeError(f"Judge replay is missing round {round_index}")
        results = matching[0].get("results")
    elif round_index == 1:
        results = replay.get("results")
    else:
        raise JudgeError("multi-round Judge replay has no rounds array")
    if not isinstance(results, dict):
        raise JudgeError("Judge replay has no results object")
    entry = results.get(snapshot["dimension"])
    if not isinstance(entry, dict):
        raise JudgeError(f"Judge replay is missing {snapshot['dimension']}")
    source_sha = entry.get("source_result_sha256")
    items = entry.get("rubric_results")
    if not isinstance(source_sha, str) or not isinstance(items, list):
        raise JudgeError("Judge replay result provenance is invalid")
    model_result = {
        "schema_version": MODEL_RESULT_SCHEMA,
        "dimension": snapshot["dimension"],
        "input_snapshot_sha256": snapshot["input_snapshot_sha256"],
        "rubric_results": [
            {
                "rubric_id": item.get("rubric_id"),
                "applicable": item.get("applicable", True),
                "score": item.get("score"),
                "reason": item.get("reason"),
            }
            for item in items
            if isinstance(item, dict)
        ],
        "evidence_paths": entry.get(
            "evidence_paths",
            ["candidate_tests/replay-bound-result"],
        ),
        "summary": entry.get(
            "summary",
            f"Recorded {snapshot['dimension']} result replay.",
        ),
    }
    request_record = {
        "mode": "replay",
        "round": round_index,
        "source_result_sha256": source_sha,
        "candidate_tree_sha256": replay["candidate_tree_sha256"],
    }
    return model_result, request_record, entry


def _majority_formal_result(
    *,
    snapshot: dict[str, Any],
    round_artifacts: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    """Aggregate an odd number of valid rounds per Rubric, not by mean score."""

    repetitions = len(round_artifacts)
    if repetitions < 1 or repetitions % 2 == 0:
        raise JudgeError("Judge majority requires an odd number of rounds")
    hashes = {
        artifact.get("formal_result", {}).get("input_snapshot_sha256")
        for artifact in round_artifacts
    }
    if hashes != {snapshot["input_snapshot_sha256"]}:
        raise JudgeError("Judge rounds do not share one frozen input snapshot")

    formal_rounds = [artifact["formal_result"] for artifact in round_artifacts]
    round_by_rubric = [
        {item["rubric_id"]: item for item in result["rubric_results"]}
        for result in formal_rounds
    ]
    majority_items: list[dict[str, Any]] = []
    unstable_count = 0
    for rubric in _rubrics(snapshot):
        rubric_id = str(rubric["rubric_id"])
        items = [by_id[rubric_id] for by_id in round_by_rubric]
        applicability = {item["applicable"] for item in items}
        if len(applicability) != 1:
            raise JudgeError(
                f"Judge round applicability changed for {rubric_id}"
            )
        applicable = items[0]["applicable"]
        scores = [item["score"] for item in items]
        if applicable:
            if not all(type(score) is int and score in (0, 1) for score in scores):
                raise JudgeError(f"Judge round score is invalid for {rubric_id}")
            majority_score: int | None = int(sum(scores) > repetitions // 2)
            supporting = [
                index + 1
                for index, score in enumerate(scores)
                if score == majority_score
            ]
            if len(set(scores)) > 1:
                unstable_count += 1
            reason = (
                f"The per-Rubric majority is {majority_score}; the votes across "
                f"{repetitions} rounds are {scores}, with rounds {supporting} "
                "supporting the majority."
            )
        else:
            if any(score is not None for score in scores):
                raise JudgeError(
                    f"non-applicable round score must be null: {rubric_id}"
                )
            majority_score = None
            reason = (
                f"All {repetitions} rounds are non-applicable under the frozen "
                "J5 applicability decision."
            )
        majority_items.append(
            {
                "rubric_id": rubric_id,
                "applicable": applicable,
                "score": majority_score,
                "reason": reason,
            }
        )

    evidence_paths = sorted(
        {
            path
            for result in formal_rounds
            for path in result["evidence_paths"]
        }
    )
    model_result = {
        "schema_version": MODEL_RESULT_SCHEMA,
        "dimension": snapshot["dimension"],
        "input_snapshot_sha256": snapshot["input_snapshot_sha256"],
        "rubric_results": majority_items,
        "evidence_paths": evidence_paths,
        "summary": (
            f"Applied majority voting to {repetitions} independent judgments "
            f"per Rubric; found {unstable_count} reversals."
        ),
    }
    majority = formalize_result(
        model_result,
        snapshot,
        mode=f"{mode}-majority",
    )
    majority.update(
        {
            "judge_repetitions": repetitions,
            "aggregation_policy": AGGREGATION_POLICY,
            "stability_policy": STABILITY_POLICY,
            "round_dimension_scores": [
                result["score"] for result in formal_rounds
            ],
            "unstable_rubric_count": unstable_count,
        }
    )
    return majority


def _instability_cause(
    rubric: dict[str, Any],
    reasons: list[str],
) -> dict[str, str]:
    text = " ".join(
        str(value)
        for value in (
            rubric.get("criterion"),
            rubric.get("direction"),
            rubric.get("score_1_condition", rubric.get("score_1")),
            rubric.get("score_0_condition", rubric.get("score_0")),
            *reasons,
        )
        if value
    )
    if any(
        token in text
        for token in (
            "first responsible",
            "remaining failures",
            "all other failures",
            "masking",
            "cascade",
        )
    ):
        return {
            "cause_code": "FAILURE_SCOPE_INTERPRETATION",
            "cause_analysis": (
                "The rounds interpret the boundary between the first responsible "
                "failure and other permitted failures inconsistently."
            ),
            "improvement": (
                "Verify that the normalized failure record identifies the first "
                "responsible failure and maps every other failure individually to "
                "allowed_behavior_ids; consistently assign 0 when a mapping is "
                "impossible."
            ),
        }
    if any(
        token in text
        for token in (
            "simultaneously",
            "respectively",
            "each Rubric",
            "all necessary",
            "as well as",
        )
    ):
        return {
            "cause_code": "CONJUNCTIVE_BOUNDARY_INTERPRETATION",
            "cause_analysis": (
                "The rounds disagree on whether all necessary conditions for a "
                "score of 1 are satisfied."
            ),
            "improvement": (
                "Create an itemized checklist of necessary conditions; the reason "
                "must provide evidence for every conjunct, and must consistently "
                "assign 0 when any condition is missing, unexecuted, or conflicting."
            ),
        }
    if any(
        token in text
        for token in (
            "source code",
            "candidate_absent",
            "stable_killed",
            "runtime evidence",
            "actual execution",
        )
    ):
        return {
            "cause_code": "EVIDENCE_ROLE_INTERPRETATION",
            "cause_analysis": (
                "The rounds interpret the scope of facts proved by source code, "
                "fault detection, and actual runtime evidence inconsistently."
            ),
            "improvement": (
                "Recheck against the frozen evidence policy: accept only structured "
                "runtime evidence for execution facts; source code and stable_killed "
                "cannot fill an unexecuted condition, and missing runtime proof must "
                "consistently receive 0."
            ),
        }
    return {
        "cause_code": "MODEL_VARIANCE_WITH_FIXED_CONTRACT",
        "cause_analysis": (
            "The model's decision boundary still varies under the same frozen "
            "evidence and judging text."
        ),
        "improvement": (
            "Retain all three reasons for a targeted review, and use the divergent "
            "reasons to make any remaining implicit boundary explicit in the "
            "existing score_0_condition or score_1_condition text."
        ),
    }


def build_stability_report(
    *,
    panel_snapshot_sha256: str,
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Record every per-Rubric reversal without changing reward eligibility."""

    unstable: list[dict[str, Any]] = []
    dimensions: dict[str, Any] = {}
    repetitions = 0
    for dimension in DIMENSIONS:
        artifact = artifacts[dimension]
        rounds = artifact["rounds"]
        repetitions = len(rounds)
        snapshot = artifact["snapshot"]
        rubric_by_id = {
            str(item["rubric_id"]): item for item in _rubrics(snapshot)
        }
        round_maps = [
            {
                item["rubric_id"]: item
                for item in round_artifact["formal_result"]["rubric_results"]
            }
            for round_artifact in rounds
        ]
        dimension_unstable = 0
        applicable = 0
        for rubric_id, rubric in rubric_by_id.items():
            items = [mapping[rubric_id] for mapping in round_maps]
            if items[0]["applicable"]:
                applicable += 1
            scores = [item["score"] for item in items]
            if len(set(scores)) <= 1:
                continue
            dimension_unstable += 1
            reasons = [item["reason"] for item in items]
            majority_score = int(sum(scores) > repetitions // 2)
            unstable.append(
                {
                    "dimension": dimension,
                    "rubric_id": rubric_id,
                    "round_scores": scores,
                    "majority_score": majority_score,
                    "round_reasons": [
                        {"round": index + 1, "reason": reason}
                        for index, reason in enumerate(reasons)
                    ],
                    "round_evidence_paths": [
                        {
                            "round": index + 1,
                            "paths": round_artifact["formal_result"][
                                "evidence_paths"
                            ],
                        }
                        for index, round_artifact in enumerate(rounds)
                    ],
                    **_instability_cause(rubric, reasons),
                }
            )
        dimensions[dimension] = {
            "applicable_rubric_count": applicable,
            "unstable_rubric_count": dimension_unstable,
            "agreement_rate": (
                (applicable - dimension_unstable) / applicable
                if applicable
                else None
            ),
        }
    return {
        "schema_version": STABILITY_SCHEMA,
        "status": "pass",
        "policy": STABILITY_POLICY,
        "aggregation_policy": AGGREGATION_POLICY,
        "panel_snapshot_sha256": panel_snapshot_sha256,
        "judge_repetitions": repetitions,
        "unstable_rubric_count": len(unstable),
        "unstable_rubrics": unstable,
        "dimensions": dimensions,
    }


def validate_dimension_snapshots(
    snapshots: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Fail closed when a dimension receives another dimension's evidence."""

    if set(snapshots) != set(DIMENSIONS):
        raise JudgeError("Judge snapshot dimension set is incomplete")
    candidate_hashes = {
        snapshot.get("candidate_tree_sha256") for snapshot in snapshots.values()
    }
    candidate_sources = {
        sha256_json(snapshot.get("candidate_files"))
        for snapshot in snapshots.values()
    }
    if len(candidate_hashes) != 1 or None in candidate_hashes:
        raise JudgeError("Judge dimensions do not share one candidate hash")
    if len(candidate_sources) != 1:
        raise JudgeError("Judge dimensions do not receive identical full source")

    forbidden: dict[str, set[str]] = {
        "SC-J1": {
            "positive_execution_evidence",
            "hard_evidence",
            "j5_applicability_map",
            "j5_rubric_evidence_scope",
            "stable_killed_fault_evidence",
        },
        "SC-J2": {
            "j1_execution_evidence",
            "hard_evidence",
            "j5_applicability_map",
            "j5_rubric_evidence_scope",
            "stable_killed_fault_evidence",
        },
        "SC-J3": {
            "j1_execution_evidence",
            "hard_evidence",
            "j5_applicability_map",
            "j5_rubric_evidence_scope",
            "stable_killed_fault_evidence",
        },
        "SC-J4": {
            "j1_execution_evidence",
            "hard_evidence",
            "j5_applicability_map",
            "j5_rubric_evidence_scope",
            "stable_killed_fault_evidence",
        },
        "SC-J5": {
            "j1_execution_evidence",
            "positive_execution_evidence",
        },
    }
    required: dict[str, set[str]] = {
        "SC-J1": {"candidate_files", "j1_execution_evidence"},
        "SC-J2": {"candidate_files", "positive_execution_evidence"},
        "SC-J3": {"candidate_files", "positive_execution_evidence"},
        "SC-J4": {"candidate_files", "positive_execution_evidence"},
        "SC-J5": {
            "candidate_files",
            "hard_evidence",
            "j5_rubric_evidence_scope",
        },
    }
    records: dict[str, Any] = {}
    for dimension, snapshot in snapshots.items():
        missing = sorted(required[dimension] - set(snapshot))
        present_forbidden = sorted(forbidden[dimension] & set(snapshot))
        if missing or present_forbidden:
            raise JudgeError(
                f"{dimension} evidence isolation failed: "
                f"missing={missing}, forbidden={present_forbidden}"
            )
        if "sc_e2" in snapshot or "SC-E2-v2" in snapshot:
            raise JudgeError(f"{dimension} received SC-E2 score evidence")
        records[dimension] = {
            "candidate_source_sha256": next(iter(candidate_sources)),
            "included_top_level_keys": sorted(snapshot),
            "forbidden_top_level_keys_present": [],
        }
    return {
        "schema_version": "unittest-rl-judge-input-isolation-validation-v1",
        "status": "pass",
        "candidate_tree_sha256": next(iter(candidate_hashes)),
        "candidate_source_sha256": next(iter(candidate_sources)),
        "dimensions": records,
    }


def run_judge_panel(
    *,
    mode: str,
    task_core_fingerprint: str,
    evaluation_id: str,
    candidate_root: Path,
    evaluation_assets: Path,
    fault_replay: dict[str, Any],
    fault_log_dir: Path,
    sc_e1: dict[str, Any],
    sc_e2: dict[str, Any],
    hard_gates: dict[str, Any] | None = None,
    j1_execution_evidence: dict[str, Any] | None = None,
    config: dict[str, Any],
    replay: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Run repeated five-dimension panels against one frozen snapshot."""

    if mode not in {"live", "replay"}:
        raise JudgeError(f"unsupported Judge mode: {mode}")
    validate_judge_config(
        config,
        task_core_fingerprint=task_core_fingerprint,
    )
    repetitions = judge_repetitions(config)
    snapshots = {
        dimension: build_dimension_snapshot(
            dimension=dimension,
            task_core_fingerprint=task_core_fingerprint,
            evaluation_id=evaluation_id,
            candidate_root=candidate_root,
            evaluation_assets=evaluation_assets,
            fault_replay=fault_replay,
            fault_log_dir=fault_log_dir,
            sc_e1=sc_e1,
            sc_e2=sc_e2,
            hard_gates=hard_gates,
            j1_execution_evidence=j1_execution_evidence,
            config=config,
        )
        for dimension in DIMENSIONS
    }
    input_validation = validate_dimension_snapshots(snapshots)
    panel_snapshot_sha256 = sha256_json(
        {
            "schema_version": PANEL_SCHEMA,
            "task_core_fingerprint": task_core_fingerprint,
            "evaluation_id": evaluation_id,
            "candidate_tree_sha256": next(iter(snapshots.values()))[
                "candidate_tree_sha256"
            ],
            "config_id": config["config_id"],
            "judge_runtime": config.get("runtime_receipt"),
            "dimensions": list(DIMENSIONS),
            "judge_repetitions": repetitions,
            "aggregation_policy": AGGREGATION_POLICY,
            "stability_policy": STABILITY_POLICY,
            "fault_replay_sha256": sha256_json(fault_replay),
            "sc_e1_sha256": sha256_json(sc_e1),
            "hard_gates_sha256": (
                sha256_json(hard_gates) if hard_gates is not None else None
            ),
            "j1_execution_evidence_sha256": (
                sha256_json(j1_execution_evidence)
                if j1_execution_evidence is not None
                else None
            ),
            "input_validation_sha256": sha256_json(input_validation),
        }
    )
    for snapshot in snapshots.values():
        snapshot.pop("input_snapshot_sha256")
        snapshot["panel_snapshot_sha256"] = panel_snapshot_sha256
        snapshot["input_snapshot_sha256"] = sha256_json(snapshot)
    artifacts: dict[str, dict[str, Any]] = {
        dimension: {"snapshot": snapshots[dimension], "rounds": []}
        for dimension in DIMENSIONS
    }
    errors: list[str] = []

    def run_one(
        dimension: str,
        round_index: int,
    ) -> tuple[str, dict[str, Any]]:
        snapshot = snapshots[dimension]
        if mode == "live":
            model_result, request_record, raw_response = _live_model_result(
                snapshot,
                config,
            )
            source_sha = None
        else:
            if replay is None:
                raise JudgeError("Judge replay mode requires a replay bundle")
            model_result, request_record, raw_response = _replay_model_result(
                snapshot,
                replay,
                round_index=round_index,
            )
            source_sha = request_record["source_result_sha256"]
        formal = formalize_result(
            model_result,
            snapshot,
            mode=mode,
            replay_source_sha256=source_sha,
        )
        return dimension, {
            "round": round_index,
            "request": request_record,
            "raw_response": raw_response,
            "model_result": model_result,
            "formal_result": formal,
        }

    for round_index in range(1, repetitions + 1):
        with ThreadPoolExecutor(max_workers=config["parallelism"]) as executor:
            futures = {
                executor.submit(run_one, dimension, round_index): dimension
                for dimension in DIMENSIONS
            }
            for future in as_completed(futures):
                dimension = futures[future]
                try:
                    key, value = future.result()
                    artifacts[key]["rounds"].append(value)
                except Exception as exc:  # collect every branch failure
                    errors.append(
                        f"round-{round_index}:{dimension}:"
                        f"{type(exc).__name__}:{exc}"
                    )

    for artifact in artifacts.values():
        artifact["rounds"].sort(key=lambda item: item["round"])

    incomplete = {
        dimension
        for dimension, artifact in artifacts.items()
        if len(artifact["rounds"]) != repetitions
    }
    if errors or incomplete:
        panel = {
            "schema_version": PANEL_SCHEMA,
            "status": "evaluation_invalid",
            "mode": mode,
            "judge_repetitions": repetitions,
            "errors": sorted(errors),
            "completed_dimensions": sorted(set(DIMENSIONS) - incomplete),
            "missing_dimensions": sorted(incomplete),
        }
        return panel, artifacts

    try:
        for dimension in DIMENSIONS:
            artifacts[dimension]["formal_result"] = _majority_formal_result(
                snapshot=snapshots[dimension],
                round_artifacts=artifacts[dimension]["rounds"],
                mode=mode,
            )
        stability = build_stability_report(
            panel_snapshot_sha256=panel_snapshot_sha256,
            artifacts=artifacts,
        )
    except JudgeError as exc:
        return (
            {
                "schema_version": PANEL_SCHEMA,
                "status": "evaluation_invalid",
                "mode": mode,
                "judge_repetitions": repetitions,
                "errors": [f"majority:{type(exc).__name__}:{exc}"],
                "completed_dimensions": [],
                "missing_dimensions": list(DIMENSIONS),
            },
            artifacts,
        )
    results = {
        dimension: artifacts[dimension]["formal_result"]
        for dimension in DIMENSIONS
    }
    return (
        {
            "schema_version": PANEL_SCHEMA,
            "status": "pass",
            "mode": mode,
            "task_core_fingerprint": task_core_fingerprint,
            "evaluation_id": evaluation_id,
            "candidate_tree_sha256": next(iter(snapshots.values()))[
                "candidate_tree_sha256"
            ],
            "judge_runtime": config.get(
                "runtime_receipt",
                {
                    "runner": config.get("runner", "api"),
                    "protocol": config.get("protocol", "responses"),
                    "model": config.get("model"),
                },
            ),
            "panel_snapshot_sha256": panel_snapshot_sha256,
            "judge_repetitions": repetitions,
            "aggregation_policy": AGGREGATION_POLICY,
            "stability_policy": STABILITY_POLICY,
            "stability_report": stability,
            "input_validation": input_validation,
            "dimensions": {
                dimension: {
                    "score": results[dimension]["score"],
                    "round_scores": results[dimension][
                        "round_dimension_scores"
                    ],
                    "unstable_rubric_count": results[dimension][
                        "unstable_rubric_count"
                    ],
                    "applicability_policy": results[dimension][
                        "applicability_policy"
                    ],
                    "input_snapshot_sha256": results[dimension][
                        "input_snapshot_sha256"
                    ],
                }
                for dimension in DIMENSIONS
            },
        },
        artifacts,
    )

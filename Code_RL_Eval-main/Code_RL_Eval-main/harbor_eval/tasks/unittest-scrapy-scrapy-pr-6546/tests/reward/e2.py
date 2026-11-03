"""Deterministic SC-E2-v2 evidence construction and aggregation."""

from __future__ import annotations

import ast
import itertools
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


LEGACY_CONTRACT_SCHEMA = "unittest-rl-harbor-e2-evidence-contract-v1"
CONTRACT_SCHEMA = "unittest-rl-harbor-e2-positive-execution-contract-v2"
LEGACY_REPORT_SCHEMA = "unittest-rl-sc-e2-v2-v2"
REPORT_SCHEMA = "unittest-rl-sc-e2-v2-v3"
AGGREGATION_POLICY = "hierarchical_behavior_risk_weighted_v2"


class E2EvidenceError(ValueError):
    """Raised when E2 evidence is incomplete or structurally invalid."""


def _positive(value: Any, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise E2EvidenceError(f"{label} must be a finite positive number")
    return float(value)


def _string_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise E2EvidenceError(f"{label} must be an array of non-empty strings")
    return list(value)


def _test_sources(candidate_root: Path) -> list[dict[str, str]]:
    if not candidate_root.is_dir():
        raise E2EvidenceError("candidate test directory is missing")
    records: list[dict[str, str]] = []
    for path in sorted(candidate_root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise E2EvidenceError(
                f"candidate test source cannot be analyzed: {path}: {exc}"
            ) from exc
        lines = source.splitlines(keepends=True)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            start = node.lineno
            if node.decorator_list:
                start = min(item.lineno for item in node.decorator_list)
            end = node.end_lineno or node.lineno
            records.append(
                {
                    "file": str(path.relative_to(candidate_root)),
                    "name": node.name,
                    "source": "".join(lines[start - 1 : end]),
                }
            )
    records.sort(key=lambda item: (item["file"], item["name"]))
    return records


def _fault_map(fault_replay: dict[str, Any]) -> dict[str, str]:
    if fault_replay.get("status") != "pass":
        raise E2EvidenceError("fault replay evidence is not complete")
    faults = fault_replay.get("faults")
    if not isinstance(faults, list):
        raise E2EvidenceError("fault replay records are missing")
    result: dict[str, str] = {}
    for index, item in enumerate(faults):
        if not isinstance(item, dict):
            raise E2EvidenceError(f"faults[{index}] must be an object")
        fault_id = item.get("fault_id")
        classification = item.get("classification")
        if not isinstance(fault_id, str) or not fault_id:
            raise E2EvidenceError(f"faults[{index}].fault_id is invalid")
        if classification not in {"stable_killed", "survived"}:
            raise E2EvidenceError(
                f"{fault_id} has unresolved classification {classification!r}"
            )
        if fault_id in result:
            raise E2EvidenceError(f"duplicate fault replay record: {fault_id}")
        result[fault_id] = str(classification)
    return result


def _match_test_source(
    predicate: dict[str, Any],
    tests: list[dict[str, str]],
    *,
    label: str,
) -> tuple[bool, list[str]]:
    all_tokens = _string_list(
        predicate.get("all_tokens", []),
        label=f"{label}.all_tokens",
    )
    none_tokens = _string_list(
        predicate.get("none_tokens", []),
        label=f"{label}.none_tokens",
    )
    raw_groups = predicate.get("any_token_groups", [])
    if not isinstance(raw_groups, list):
        raise E2EvidenceError(f"{label}.any_token_groups must be an array")
    any_groups = [
        _string_list(group, label=f"{label}.any_token_groups[{index}]")
        for index, group in enumerate(raw_groups)
    ]
    if not all_tokens and not any_groups:
        raise E2EvidenceError(
            f"{label} must define all_tokens or any_token_groups"
        )

    matches: list[str] = []
    for test in tests:
        source = test["source"]
        if not all(token in source for token in all_tokens):
            continue
        if any(not any(token in source for token in group) for group in any_groups):
            continue
        if any(token in source for token in none_tokens):
            continue
        matches.append(f"candidate_tests/{test['file']}::{test['name']}")
    return bool(matches), matches


def _evaluate_predicate(
    predicate: dict[str, Any],
    *,
    tests: list[dict[str, str]],
    faults: dict[str, str],
    label: str,
) -> tuple[bool, list[str]]:
    kind = predicate.get("kind")
    if kind == "fault":
        fault_id = predicate.get("fault_id")
        if not isinstance(fault_id, str) or not fault_id:
            raise E2EvidenceError(f"{label}.fault_id is invalid")
        if fault_id not in faults:
            raise E2EvidenceError(
                f"{label} references missing fault replay {fault_id}"
            )
        expected = predicate.get("classification", "stable_killed")
        if expected not in {"stable_killed", "survived"}:
            raise E2EvidenceError(f"{label}.classification is invalid")
        actual = faults[fault_id]
        return actual == expected, [
            f"fault-replay.json:{fault_id}={actual}"
        ]
    if kind == "test_source":
        return _match_test_source(predicate, tests, label=label)
    raise E2EvidenceError(f"{label}.kind is unsupported: {kind!r}")


def _evaluate_rule(
    rule: dict[str, Any],
    *,
    tests: list[dict[str, str]],
    faults: dict[str, str],
    label: str,
) -> tuple[int, str, list[str]]:
    raw_groups = rule.get("evidence_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise E2EvidenceError(f"{label}.evidence_groups must be non-empty")
    attempted: list[str] = []
    for group_index, group in enumerate(raw_groups):
        if not isinstance(group, list) or not group:
            raise E2EvidenceError(
                f"{label}.evidence_groups[{group_index}] must be non-empty"
            )
        group_passed = True
        group_evidence: list[str] = []
        for predicate_index, predicate in enumerate(group):
            if not isinstance(predicate, dict):
                raise E2EvidenceError(
                    f"{label}.evidence_groups[{group_index}]"
                    f"[{predicate_index}] must be an object"
                )
            passed, evidence = _evaluate_predicate(
                predicate,
                tests=tests,
                faults=faults,
                label=(
                    f"{label}.evidence_groups[{group_index}]"
                    f"[{predicate_index}]"
                ),
            )
            group_passed = group_passed and passed
            group_evidence.extend(evidence)
        attempted.extend(group_evidence)
        if group_passed:
            return 1, "complete", sorted(set(group_evidence))
    return 0, "candidate_absent", sorted(set(attempted))


def aggregate_e2(
    payload: dict[str, Any],
    *,
    report_schema: str = REPORT_SCHEMA,
    semantic_version: str = "positive-execution-obligation-adequacy-v3",
) -> dict[str, Any]:
    """Apply the frozen hierarchical E2 aggregation formula."""
    profiles = payload.get("behavior_profiles")
    obligations = payload.get("obligation_results")
    if not isinstance(profiles, list) or not profiles:
        raise E2EvidenceError("behavior_profiles must be a non-empty array")
    if not isinstance(obligations, list) or not obligations:
        raise E2EvidenceError("obligation_results must be a non-empty array")

    risks: dict[str, float] = {}
    quality_goals: dict[str, Any] = {}
    for index, item in enumerate(profiles):
        if not isinstance(item, dict):
            raise E2EvidenceError(f"behavior_profiles[{index}] is invalid")
        behavior_id = item.get("behavior_id")
        if not isinstance(behavior_id, str) or not behavior_id:
            raise E2EvidenceError(
                f"behavior_profiles[{index}].behavior_id is invalid"
            )
        if behavior_id in risks:
            raise E2EvidenceError(f"duplicate behavior profile: {behavior_id}")
        risks[behavior_id] = _positive(
            item.get("behavior_risk_weight"),
            label=f"behavior_profiles[{index}].behavior_risk_weight",
        )
        quality_goals[behavior_id] = item.get("quality_goal_id")

    grouped: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    seen_ids: set[str] = set()
    satisfied = 0
    for index, item in enumerate(obligations):
        if not isinstance(item, dict):
            raise E2EvidenceError(f"obligation_results[{index}] is invalid")
        obligation_id = item.get("obligation_id")
        behavior_id = item.get("behavior_id")
        criterion = item.get("criterion")
        if not all(
            isinstance(value, str) and value
            for value in (obligation_id, behavior_id, criterion)
        ):
            raise E2EvidenceError(
                f"obligation_results[{index}] identity is invalid"
            )
        if obligation_id in seen_ids:
            raise E2EvidenceError(f"duplicate obligation: {obligation_id}")
        seen_ids.add(obligation_id)
        if behavior_id not in risks:
            raise E2EvidenceError(f"missing behavior profile: {behavior_id}")
        score = item.get("score")
        if isinstance(score, bool) or score not in {0, 1}:
            raise E2EvidenceError(f"{obligation_id}.score must be 0 or 1")
        weight = _positive(
            item.get("obligation_weight"),
            label=f"{obligation_id}.obligation_weight",
        )
        grouped[(behavior_id, criterion)].append((weight, int(score)))
        satisfied += int(score)

    cells: dict[str, dict[str, float]] = defaultdict(dict)
    for (behavior_id, criterion), values in grouped.items():
        denominator = math.fsum(weight for weight, _ in values)
        numerator = math.fsum(weight * score for weight, score in values)
        cells[behavior_id][criterion] = numerator / denominator

    behavior_scores: dict[str, dict[str, Any]] = {}
    numerator = 0.0
    denominator = 0.0
    for behavior_id, criterion_scores in sorted(cells.items()):
        behavior_score = (
            math.fsum(criterion_scores.values()) / len(criterion_scores)
        )
        risk = risks[behavior_id]
        behavior_scores[behavior_id] = {
            "quality_goal_id": quality_goals[behavior_id],
            "behavior_risk_weight": risk,
            "criterion_scores": dict(sorted(criterion_scores.items())),
            "score": behavior_score,
        }
        numerator += risk * behavior_score
        denominator += risk

    return {
        "schema_version": report_schema,
        "status": "pass",
        "evaluation_id": payload.get("evaluation_id"),
        "dimension": "SC-E2-v2",
        "semantic_version": semantic_version,
        "evaluation_mode": "formal",
        "affects_total_reward": True,
        "aggregation_policy": AGGREGATION_POLICY,
        "obligation_count": len(obligations),
        "satisfied_count": satisfied,
        "indeterminate_count": 0,
        "behavior_profiles": profiles,
        "behavior_scores": behavior_scores,
        "obligation_results": obligations,
        "score": numerator / denominator,
        "evidence": payload.get("evidence", []),
    }


def _evaluate_legacy_e2(
    *,
    obligations: dict[str, Any],
    obligations_validation: dict[str, Any],
    contract: dict[str, Any],
    candidate_root: Path,
    fault_replay: dict[str, Any],
    task_core_fingerprint: str,
    evaluation_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Construct auditable evidence and return both input and score reports."""
    try:
        if contract.get("schema_version") != LEGACY_CONTRACT_SCHEMA:
            raise E2EvidenceError("E2 evidence contract schema is invalid")
        if contract.get("task_core_fingerprint") != task_core_fingerprint:
            raise E2EvidenceError("E2 evidence contract task fingerprint mismatch")
        if obligations.get("status") != "frozen":
            raise E2EvidenceError("test obligations are not frozen")
        if obligations_validation.get("status") != "pass":
            raise E2EvidenceError("test obligations validation did not pass")
        if (
            obligations.get("obligation_revision_id")
            != contract.get("obligation_revision_id")
        ):
            raise E2EvidenceError("obligation revision mismatch")
        if obligations.get("aggregation_policy") != AGGREGATION_POLICY:
            raise E2EvidenceError("obligation aggregation policy mismatch")

        raw_obligations = obligations.get("obligations")
        profiles = obligations.get("behavior_profiles")
        rules = contract.get("rules")
        if not isinstance(raw_obligations, list) or not raw_obligations:
            raise E2EvidenceError("frozen obligations are missing")
        if not isinstance(profiles, list) or not profiles:
            raise E2EvidenceError("frozen behavior profiles are missing")
        if not isinstance(rules, list) or not rules:
            raise E2EvidenceError("E2 evidence contract rules are missing")

        rule_map: dict[str, dict[str, Any]] = {}
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise E2EvidenceError(f"rules[{index}] must be an object")
            obligation_id = rule.get("obligation_id")
            if not isinstance(obligation_id, str) or not obligation_id:
                raise E2EvidenceError(f"rules[{index}].obligation_id is invalid")
            if obligation_id in rule_map:
                raise E2EvidenceError(
                    f"duplicate E2 evidence rule: {obligation_id}"
                )
            rule_map[obligation_id] = rule

        obligation_ids = {
            item.get("obligation_id")
            for item in raw_obligations
            if isinstance(item, dict)
        }
        if obligation_ids != set(rule_map):
            raise E2EvidenceError(
                "E2 evidence rules do not exactly cover frozen obligations"
            )

        tests = _test_sources(candidate_root)
        faults = _fault_map(fault_replay)
        results: list[dict[str, Any]] = []
        for index, obligation in enumerate(raw_obligations):
            if not isinstance(obligation, dict):
                raise E2EvidenceError(
                    f"obligations[{index}] must be an object"
                )
            obligation_id = obligation.get("obligation_id")
            behavior_id = obligation.get("behavior_id")
            criterion = obligation.get("criterion")
            if not all(
                isinstance(value, str) and value
                for value in (obligation_id, behavior_id, criterion)
            ):
                raise E2EvidenceError(
                    f"obligations[{index}] identity is invalid"
                )
            weight = _positive(
                obligation.get("obligation_weight"),
                label=f"{obligation_id}.obligation_weight",
            )
            score, evidence_status, evidence = _evaluate_rule(
                rule_map[obligation_id],
                tests=tests,
                faults=faults,
                label=f"rules[{obligation_id}]",
            )
            results.append(
                {
                    "obligation_id": obligation_id,
                    "behavior_id": behavior_id,
                    "criterion": criterion,
                    "obligation_weight": weight,
                    "score": score,
                    "evidence_status": evidence_status,
                    "evidence": evidence,
                }
            )

        input_report = {
            "schema_version": "unittest-rl-harbor-sc-e2-input-v1",
            "status": "complete",
            "evaluation_id": evaluation_id,
            "task_core_fingerprint": task_core_fingerprint,
            "contract_id": contract.get("contract_id"),
            "obligation_revision_id": obligations.get(
                "obligation_revision_id"
            ),
            "behavior_profiles": profiles,
            "obligation_results": results,
            "evidence": [
                "evaluation_assets/obligations/test_obligations.json",
                "evaluation_assets/obligations/obligations_validation.json",
                "e2-evidence-contract.json",
                "candidate_tests/",
                "fault-replay.json",
            ],
        }
        score_report = aggregate_e2(
            input_report,
            report_schema=LEGACY_REPORT_SCHEMA,
            semantic_version="test-obligation-adequacy-v2",
        )
        return input_report, score_report
    except E2EvidenceError as exc:
        invalid = {
            "schema_version": LEGACY_REPORT_SCHEMA,
            "status": "evaluation_invalid",
            "evaluation_id": evaluation_id,
            "dimension": "SC-E2-v2",
            "semantic_version": "test-obligation-adequacy-v2",
            "evaluation_mode": "formal",
            "affects_total_reward": True,
            "aggregation_policy": AGGREGATION_POLICY,
            "score": None,
            "errors": [str(exc)],
        }
        return {
            "schema_version": "unittest-rl-harbor-sc-e2-input-v1",
            "status": "evaluation_invalid",
            "evaluation_id": evaluation_id,
            "task_core_fingerprint": task_core_fingerprint,
            "errors": [str(exc)],
        }, invalid


def _path_value(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _condition_matches(*, present: bool, actual: Any, condition: Any) -> bool:
    if not isinstance(condition, dict):
        return present and actual == condition
    if set(condition) == {"equals"}:
        return present and actual == condition["equals"]
    if set(condition) == {"not_equals"}:
        return present and actual != condition["not_equals"]
    if set(condition) == {"in"}:
        values = condition["in"]
        if not isinstance(values, list):
            raise E2EvidenceError("argument condition 'in' must contain an array")
        return present and actual in values
    if set(condition) == {"not_in"}:
        values = condition["not_in"]
        if not isinstance(values, list):
            raise E2EvidenceError(
                "argument condition 'not_in' must contain an array"
            )
        return present and actual not in values
    if set(condition) == {"present"} and isinstance(condition["present"], bool):
        return present is condition["present"]
    raise E2EvidenceError("argument condition is invalid")


def _argument_set_matches(
    invocation_arguments: dict[str, Any],
    conditions: dict[str, Any],
    *,
    label: str,
) -> bool:
    for path, condition in conditions.items():
        if not isinstance(path, str) or not path:
            raise E2EvidenceError(f"{label} has an invalid path")
        present, actual = _path_value(invocation_arguments, path)
        if not _condition_matches(
            present=present,
            actual=actual,
            condition=condition,
        ):
            return False
    return True


def _invocation_matches(
    invocation: dict[str, Any],
    predicate: dict[str, Any],
    *,
    accepted_phases: set[str],
    label: str,
) -> bool:
    target = predicate.get("target")
    if not isinstance(target, str) or not target:
        raise E2EvidenceError(f"{label}.target is invalid")
    alias = predicate.get("alias")
    if not isinstance(alias, str) or not alias:
        raise E2EvidenceError(f"{label}.alias is invalid")
    outcome = predicate.get("outcome")
    if outcome not in {"return", "exception"}:
        raise E2EvidenceError(f"{label}.outcome is invalid")
    if invocation.get("target") != target:
        return False
    if invocation.get("phase") not in accepted_phases:
        return False
    if invocation.get("outcome") != outcome:
        return False

    arguments = predicate.get("arguments", {})
    if not isinstance(arguments, dict):
        raise E2EvidenceError(f"{label}.arguments must be an object")
    invocation_arguments = invocation.get("arguments")
    if not isinstance(invocation_arguments, dict):
        return False
    if not _argument_set_matches(
        invocation_arguments,
        arguments,
        label=f"{label}.arguments",
    ):
        return False
    raw_any_sets = predicate.get("any_argument_sets", [])
    if not isinstance(raw_any_sets, list):
        raise E2EvidenceError(f"{label}.any_argument_sets must be an array")
    if raw_any_sets:
        if not all(isinstance(item, dict) and item for item in raw_any_sets):
            raise E2EvidenceError(
                f"{label}.any_argument_sets must contain non-empty objects"
            )
        if not any(
            _argument_set_matches(
                invocation_arguments,
                item,
                label=f"{label}.any_argument_sets",
            )
            for item in raw_any_sets
        ):
            return False

    raw_fields = predicate.get("observable_fields", [])
    fields = _string_list(raw_fields, label=f"{label}.observable_fields")
    observable = invocation.get("observable")
    if not isinstance(observable, dict):
        return False
    for path in fields:
        present, _ = _path_value(observable, path)
        if not present:
            return False
    return True


def _node_witness(
    *,
    nodeid: str,
    observation: dict[str, Any],
    rule: dict[str, Any],
    label: str,
) -> tuple[bool, list[str]]:
    raw_phases = rule.get("accepted_phases")
    accepted_phases = set(
        _string_list(raw_phases, label=f"{label}.accepted_phases")
    )
    if not accepted_phases <= {"setup", "call"}:
        raise E2EvidenceError(
            f"{label}.accepted_phases may contain only setup and call"
        )
    raw_predicates = rule.get("required_invocations")
    if not isinstance(raw_predicates, list) or not raw_predicates:
        raise E2EvidenceError(
            f"{label}.required_invocations must be non-empty"
        )
    invocations = observation.get("invocations")
    warnings = observation.get("warnings")
    if not isinstance(invocations, list) or not isinstance(warnings, list):
        raise E2EvidenceError("positive observation events are missing")
    node_invocations = [
        item
        for item in invocations
        if isinstance(item, dict) and item.get("nodeid") == nodeid
    ]

    aliases: set[str] = set()
    candidates: list[list[dict[str, Any]]] = []
    for index, predicate in enumerate(raw_predicates):
        if not isinstance(predicate, dict):
            raise E2EvidenceError(
                f"{label}.required_invocations[{index}] must be an object"
            )
        alias = predicate.get("alias")
        if not isinstance(alias, str) or not alias or alias in aliases:
            raise E2EvidenceError(
                f"{label}.required_invocations[{index}].alias is invalid"
            )
        aliases.add(alias)
        candidates.append(
            [
                item
                for item in node_invocations
                if _invocation_matches(
                    item,
                    predicate,
                    accepted_phases=accepted_phases,
                    label=f"{label}.required_invocations[{index}]",
                )
            ]
        )
    if any(not items for items in candidates):
        return False, []

    raw_warning_predicates = rule.get("required_warnings", [])
    if not isinstance(raw_warning_predicates, list):
        raise E2EvidenceError(f"{label}.required_warnings must be an array")
    node_warnings = [
        item
        for item in warnings
        if isinstance(item, dict)
        and item.get("nodeid") == nodeid
        and item.get("phase") in accepted_phases
    ]

    for selected in itertools.product(*candidates):
        call_ids = [item.get("call_id") for item in selected]
        if not all(isinstance(item, str) and item for item in call_ids):
            raise E2EvidenceError("positive invocation call_id is invalid")
        if len(set(call_ids)) != len(call_ids):
            continue
        by_alias = {
            str(predicate["alias"]): invocation
            for predicate, invocation in zip(raw_predicates, selected)
        }
        warning_ids: list[str] = []
        warnings_satisfied = True
        for index, predicate in enumerate(raw_warning_predicates):
            if not isinstance(predicate, dict):
                raise E2EvidenceError(
                    f"{label}.required_warnings[{index}] must be an object"
                )
            alias = predicate.get("within_invocation")
            if alias not in by_alias:
                raise E2EvidenceError(
                    f"{label}.required_warnings[{index}] references "
                    "an unknown invocation alias"
                )
            expected_call_id = by_alias[str(alias)]["call_id"]
            matching = [
                item
                for item in node_warnings
                if expected_call_id in item.get("active_call_ids", [])
            ]
            if not matching:
                warnings_satisfied = False
                break
            warning_id = matching[0].get("warning_id")
            if isinstance(warning_id, str):
                warning_ids.append(warning_id)
        if warnings_satisfied:
            evidence = [
                f"{nodeid}:invocation:{call_id}" for call_id in call_ids
            ]
            evidence.extend(
                f"{nodeid}:warning:{warning_id}" for warning_id in warning_ids
            )
            return True, evidence
    return False, []


def _evaluate_positive_rule(
    rule: dict[str, Any],
    *,
    observation_runs: list[dict[str, Any]],
    execution_profile_id: str,
    label: str,
) -> tuple[int, str, list[str]]:
    stable_nodeids: set[str] | None = None
    evidence_by_nodeid: dict[str, list[str]] = defaultdict(list)
    target_activated = False
    raw_predicates = rule.get("required_invocations")
    if not isinstance(raw_predicates, list) or not raw_predicates:
        raise E2EvidenceError(f"{label}.required_invocations is invalid")
    targets = {
        item.get("target") for item in raw_predicates if isinstance(item, dict)
    }
    for run_index, run in enumerate(observation_runs, 1):
        if not isinstance(run, dict) or run.get("status") != "pass":
            raise E2EvidenceError(
                f"positive observation run {run_index} is not a clean pass"
            )
        passed_nodeids = run.get("passed_nodeids")
        if not isinstance(passed_nodeids, list) or not all(
            isinstance(item, str) and item for item in passed_nodeids
        ):
            raise E2EvidenceError(
                f"positive observation run {run_index} passed_nodeids is invalid"
            )
        invocations = run.get("invocations")
        if not isinstance(invocations, list):
            raise E2EvidenceError(
                f"positive observation run {run_index} invocations is invalid"
            )
        if any(
            isinstance(item, dict) and item.get("target") in targets
            for item in invocations
        ):
            target_activated = True
        witnessed: set[str] = set()
        for nodeid in passed_nodeids:
            matched, evidence = _node_witness(
                nodeid=nodeid,
                observation=run,
                rule=rule,
                label=label,
            )
            if matched:
                witnessed.add(nodeid)
                evidence_by_nodeid[nodeid].extend(
                    f"{execution_profile_id}:run-{run_index}:{item}"
                    for item in evidence
                )
        stable_nodeids = (
            witnessed
            if stable_nodeids is None
            else stable_nodeids & witnessed
        )
    if stable_nodeids:
        evidence: list[str] = []
        for nodeid in sorted(stable_nodeids):
            evidence.extend(evidence_by_nodeid[nodeid])
        return 1, "satisfied", sorted(set(evidence))
    return (
        0,
        "execution_incomplete" if target_activated else "not_activated",
        [],
    )


def _evaluate_positive_e2(
    *,
    obligations: dict[str, Any],
    obligations_validation: dict[str, Any],
    contract: dict[str, Any],
    behavior_observations: dict[str, Any],
    execution_profile_bindings: dict[str, Any] | None,
    task_core_fingerprint: str,
    evaluation_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        if contract.get("task_core_fingerprint") != task_core_fingerprint:
            raise E2EvidenceError("E2 evidence contract task fingerprint mismatch")
        if obligations.get("status") != "frozen":
            raise E2EvidenceError("test obligations are not frozen")
        if obligations_validation.get("status") != "pass":
            raise E2EvidenceError("test obligations validation did not pass")
        if (
            obligations.get("obligation_revision_id")
            != contract.get("obligation_revision_id")
        ):
            raise E2EvidenceError("obligation revision mismatch")
        if obligations.get("aggregation_policy") != AGGREGATION_POLICY:
            raise E2EvidenceError("obligation aggregation policy mismatch")
        if behavior_observations.get("status") != "pass":
            raise E2EvidenceError("positive behavior observation is not complete")
        matrix_profiles = behavior_observations.get("profiles")
        matrix_mode = isinstance(matrix_profiles, dict)
        if matrix_mode:
            if not matrix_profiles:
                raise E2EvidenceError("positive observation profile matrix is empty")
            if not isinstance(execution_profile_bindings, dict):
                raise E2EvidenceError(
                    "positive observation matrix requires execution profile bindings"
                )
            raw_e2_bindings = execution_profile_bindings.get(
                "e2_obligation_profiles"
            )
            if not isinstance(raw_e2_bindings, dict):
                raise E2EvidenceError("E2 execution profile bindings are missing")
        else:
            raw_e2_bindings = {}
            if behavior_observations.get("replay_count") != 3:
                raise E2EvidenceError(
                    "positive behavior observation must contain 3 runs"
                )
            observation_runs = behavior_observations.get("runs")
            if not isinstance(observation_runs, list) or len(observation_runs) != 3:
                raise E2EvidenceError(
                    "positive behavior observation runs are invalid"
                )

        raw_obligations = obligations.get("obligations")
        profiles = obligations.get("behavior_profiles")
        rules = contract.get("rules")
        if not isinstance(raw_obligations, list) or not raw_obligations:
            raise E2EvidenceError("frozen obligations are missing")
        if not isinstance(profiles, list) or not profiles:
            raise E2EvidenceError("frozen behavior profiles are missing")
        if not isinstance(rules, list) or not rules:
            raise E2EvidenceError("E2 positive execution rules are missing")

        rule_map: dict[str, dict[str, Any]] = {}
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise E2EvidenceError(f"rules[{index}] must be an object")
            obligation_id = rule.get("obligation_id")
            if not isinstance(obligation_id, str) or not obligation_id:
                raise E2EvidenceError(f"rules[{index}].obligation_id is invalid")
            if obligation_id in rule_map:
                raise E2EvidenceError(
                    f"duplicate E2 positive execution rule: {obligation_id}"
                )
            rule_map[obligation_id] = rule
        obligation_ids = {
            item.get("obligation_id")
            for item in raw_obligations
            if isinstance(item, dict)
        }
        if obligation_ids != set(rule_map):
            raise E2EvidenceError(
                "E2 positive execution rules do not exactly cover frozen obligations"
            )

        results: list[dict[str, Any]] = []
        for index, obligation in enumerate(raw_obligations):
            if not isinstance(obligation, dict):
                raise E2EvidenceError(f"obligations[{index}] must be an object")
            obligation_id = obligation.get("obligation_id")
            behavior_id = obligation.get("behavior_id")
            criterion = obligation.get("criterion")
            if not all(
                isinstance(value, str) and value
                for value in (obligation_id, behavior_id, criterion)
            ):
                raise E2EvidenceError(
                    f"obligations[{index}] identity is invalid"
                )
            if matrix_mode:
                execution_profile_id = raw_e2_bindings.get(str(obligation_id))
                if not isinstance(execution_profile_id, str):
                    raise E2EvidenceError(
                        f"{obligation_id} has no execution profile binding"
                    )
                profile_observation = matrix_profiles.get(execution_profile_id)
                if not isinstance(profile_observation, dict):
                    raise E2EvidenceError(
                        f"{obligation_id} references missing observation profile "
                        f"{execution_profile_id}"
                    )
                if profile_observation.get("status") != "pass":
                    raise E2EvidenceError(
                        f"observation profile {execution_profile_id} is not complete"
                    )
                if profile_observation.get("replay_count") != 3:
                    raise E2EvidenceError(
                        f"observation profile {execution_profile_id} must contain 3 runs"
                    )
                observation_runs = profile_observation.get("runs")
                if not isinstance(observation_runs, list) or len(observation_runs) != 3:
                    raise E2EvidenceError(
                        f"observation profile {execution_profile_id} runs are invalid"
                    )
            else:
                execution_profile_id = "current"
            score, evidence_status, evidence = _evaluate_positive_rule(
                rule_map[str(obligation_id)],
                observation_runs=observation_runs,
                execution_profile_id=execution_profile_id,
                label=f"rules[{obligation_id}]",
            )
            results.append(
                {
                    "obligation_id": obligation_id,
                    "behavior_id": behavior_id,
                    "criterion": criterion,
                    "execution_profile_id": execution_profile_id,
                    "obligation_weight": _positive(
                        obligation.get("obligation_weight"),
                        label=f"{obligation_id}.obligation_weight",
                    ),
                    "score": score,
                    "evidence_status": evidence_status,
                    "stable_witnesses": evidence,
                }
            )

        input_report = {
            "schema_version": "unittest-rl-harbor-sc-e2-positive-input-v2",
            "status": "complete",
            "evaluation_id": evaluation_id,
            "task_core_fingerprint": task_core_fingerprint,
            "contract_id": contract.get("contract_id"),
            "obligation_revision_id": obligations.get("obligation_revision_id"),
            "behavior_profiles": profiles,
            "obligation_results": results,
            "evidence": [
                "evaluation_assets/obligations/test_obligations.json",
                "evaluation_assets/obligations/obligations_validation.json",
                "e2-evidence-contract.json",
                "positive-behavior-observations.json",
                *( 
                    ["evaluation_assets/execution_profile_bindings.json"]
                    if matrix_mode
                    else []
                ),
            ],
            "excluded_evidence": [
                "candidate source text",
                "fault-replay.json",
                "SC-E1",
                "SC-J1 through SC-J5",
            ],
        }
        return input_report, aggregate_e2(input_report)
    except E2EvidenceError as exc:
        invalid = {
            "schema_version": REPORT_SCHEMA,
            "status": "evaluation_invalid",
            "evaluation_id": evaluation_id,
            "dimension": "SC-E2-v2",
            "semantic_version": "positive-execution-obligation-adequacy-v3",
            "evaluation_mode": "formal",
            "affects_total_reward": True,
            "aggregation_policy": AGGREGATION_POLICY,
            "score": None,
            "errors": [str(exc)],
        }
        return {
            "schema_version": "unittest-rl-harbor-sc-e2-positive-input-v2",
            "status": "evaluation_invalid",
            "evaluation_id": evaluation_id,
            "task_core_fingerprint": task_core_fingerprint,
            "errors": [str(exc)],
        }, invalid


def evaluate_e2(
    *,
    obligations: dict[str, Any],
    obligations_validation: dict[str, Any],
    contract: dict[str, Any],
    task_core_fingerprint: str,
    evaluation_id: str | None = None,
    candidate_root: Path | None = None,
    fault_replay: dict[str, Any] | None = None,
    behavior_observations: dict[str, Any] | None = None,
    execution_profile_bindings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate either a historical contract or the positive-only contract."""
    schema = contract.get("schema_version")
    if schema == LEGACY_CONTRACT_SCHEMA:
        if candidate_root is None or fault_replay is None:
            raise E2EvidenceError(
                "legacy E2 evaluation requires candidate source and fault replay"
            )
        return _evaluate_legacy_e2(
            obligations=obligations,
            obligations_validation=obligations_validation,
            contract=contract,
            candidate_root=candidate_root,
            fault_replay=fault_replay,
            task_core_fingerprint=task_core_fingerprint,
            evaluation_id=evaluation_id,
        )
    if schema == CONTRACT_SCHEMA:
        if behavior_observations is None:
            raise E2EvidenceError(
                "positive E2 evaluation requires behavior observations"
            )
        return _evaluate_positive_e2(
            obligations=obligations,
            obligations_validation=obligations_validation,
            contract=contract,
            behavior_observations=behavior_observations,
            execution_profile_bindings=execution_profile_bindings,
            task_core_fingerprint=task_core_fingerprint,
            evaluation_id=evaluation_id,
        )
    invalid = {
        "schema_version": REPORT_SCHEMA,
        "status": "evaluation_invalid",
        "evaluation_id": evaluation_id,
        "dimension": "SC-E2-v2",
        "semantic_version": "positive-execution-obligation-adequacy-v3",
        "evaluation_mode": "formal",
        "affects_total_reward": True,
        "aggregation_policy": AGGREGATION_POLICY,
        "score": None,
        "errors": ["E2 evidence contract schema is invalid"],
    }
    return {
        "schema_version": "unittest-rl-harbor-sc-e2-positive-input-v2",
        "status": "evaluation_invalid",
        "evaluation_id": evaluation_id,
        "task_core_fingerprint": task_core_fingerprint,
        "errors": ["E2 evidence contract schema is invalid"],
    }, invalid

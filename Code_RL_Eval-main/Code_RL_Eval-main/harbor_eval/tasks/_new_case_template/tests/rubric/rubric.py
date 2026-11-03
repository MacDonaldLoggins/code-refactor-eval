from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import tomllib
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any


TESTS_ROOT = Path(__file__).resolve().parents[1]
JUDGE_TOML = Path(__file__).resolve().parent / "judge.toml"
EVALUATION_TOML = TESTS_ROOT / "evaluation.toml"


@lru_cache(maxsize=1)
def _allowed_rubric_scores() -> set[float]:
    evaluation = tomllib.loads(EVALUATION_TOML.read_text(encoding="utf-8"))
    configured = evaluation.get("policy", {}).get(
        "allowed_rubric_scores", [0.0, 0.5, 1.0]
    )
    return {float(value) for value in configured}


@lru_cache(maxsize=1)
def atoms() -> list[dict[str, Any]]:
    judge = tomllib.loads(JUDGE_TOML.read_text(encoding="utf-8"))
    evaluation = tomllib.loads(EVALUATION_TOML.read_text(encoding="utf-8"))
    criteria = judge.get("criterion")
    if not isinstance(criteria, list) or not criteria:
        raise RuntimeError("judge.toml must contain criterion entries")

    expected_total = float(
        evaluation.get("policy", {}).get("rubric_weight_total", 100.0)
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0.0
    for entry in criteria:
        if not isinstance(entry, dict):
            raise RuntimeError("judge.toml criterion entry must be an object")
        atom_id = entry.get("id")
        name = entry.get("name")
        description = entry.get("description")
        weight = entry.get("weight")
        evidence_refs = entry.get("evidence_refs")
        if not isinstance(atom_id, str) or not atom_id:
            raise RuntimeError("criterion is missing id")
        if atom_id in seen:
            raise RuntimeError(f"duplicate criterion id: {atom_id}")
        seen.add(atom_id)
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"criterion {atom_id} is missing name")
        if not isinstance(description, str) or not description:
            raise RuntimeError(f"criterion {atom_id} is missing description")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
            raise RuntimeError(f"criterion {atom_id} has invalid weight")
        if not isinstance(evidence_refs, list) or not evidence_refs or not all(
            isinstance(value, str) and value for value in evidence_refs
        ):
            raise RuntimeError(f"criterion {atom_id} requires evidence_refs")
        total += float(weight)
        result.append(
            {
                "id": atom_id,
                "name": name,
                "description": description,
                "points": float(weight),
                "hard": bool(entry.get("hard", False)),
                "evidence_refs": list(evidence_refs),
            }
        )

    if not math.isclose(total, expected_total, rel_tol=0.0, abs_tol=1e-9):
        raise RuntimeError(
            f"rubric weight total is {total}, expected {expected_total}"
        )
    return result


def _write_judge_config(path: Path) -> None:
    base_url = os.environ.get("OPENAI_BASE_URL")
    if not base_url:
        raise RuntimeError("OPENAI_BASE_URL is required for the independent Judge")
    effort = os.environ.get("REWARDKIT_REASONING_EFFORT", "high")
    path.write_text(
        'approval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\n'
        f"model_reasoning_effort = {json.dumps(effort)}\n"
        'model_provider = "judge_gateway"\n\n'
        '[model_providers.judge_gateway]\n'
        'name = "Judge gateway"\n'
        f"base_url = {json.dumps(base_url)}\n"
        'env_key = "OPENAI_API_KEY"\n',
        encoding="utf-8",
    )


def _write_codex_wrapper(path: Path, codex: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "exec" ]; then\n'
        f"  exec {shlex.quote(codex)} \"$@\" --dangerously-bypass-approvals-and-sandbox --ignore-rules --ephemeral\n"
        "fi\n"
        f"exec {shlex.quote(codex)} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _judge_environment(judge_home: Path) -> dict[str, str]:
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI is not installed")

    judge_home.mkdir(parents=True, exist_ok=True)
    judge_bin = judge_home / "bin"
    judge_bin.mkdir(parents=True, exist_ok=True)
    _write_judge_config(judge_home / "config.toml")
    _write_codex_wrapper(judge_bin / "codex", codex)

    env = os.environ.copy()
    env["HOME"] = str(judge_home)
    env["CODEX_HOME"] = str(judge_home)
    env["PATH"] = f"{judge_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run_rewardkit(
    tests_dir: Path,
    repo: Path,
    output: Path,
    logs: Path,
    label: str,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    output.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = logs / f"rewardkit-{label}-stdout.txt"
    stderr_path = logs / f"rewardkit-{label}-stderr.txt"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        subprocess.run(
            [
                "rewardkit",
                str(tests_dir),
                "--workspace",
                str(repo),
                "--output",
                str(output),
                "--max-concurrent-programmatic",
                "1",
                "--max-concurrent-agent",
                "1",
            ],
            env=env,
            stdout=stdout,
            stderr=stderr,
            check=True,
        )
    detail_path = output.with_name("reward-details.json")
    return (
        json.loads(output.read_text(encoding="utf-8")),
        json.loads(detail_path.read_text(encoding="utf-8")),
    )


def _reported_errors(value: Any):
    if isinstance(value, dict):
        if value.get("error"):
            yield value
        for child in value.values():
            yield from _reported_errors(child)
    elif isinstance(value, list):
        for child in value:
            yield from _reported_errors(child)


def _score(result: dict[str, Any], key: str = "reward") -> float:
    value = result.get(key)
    if key == "reward" and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        value = result.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"missing numeric score: {key}")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise RuntimeError(f"invalid score: {key}={value!r}")
    return value


def _detail_section(details: dict[str, Any], name: str) -> dict[str, Any]:
    section = details.get(name)
    if not isinstance(section, dict):
        raise RuntimeError(f"RewardKit details missing section {name!r}")
    return section


def _validate_criteria(
    detail: dict[str, Any],
    expected: list[dict[str, Any]],
    *,
    strict_scores: bool,
) -> tuple[float, list[dict[str, Any]], list[str]]:
    criteria = detail.get("criteria")
    if not isinstance(criteria, list):
        raise RuntimeError("RewardKit detail is missing a criteria list")
    expected_by_id = {item["id"]: item for item in expected}
    seen: set[str] = set()
    hard_failures: list[str] = []
    validated: list[dict[str, Any]] = []
    total_weight = Decimal("0")
    total_earned = Decimal("0")
    for entry in criteria:
        if not isinstance(entry, dict):
            raise RuntimeError("RewardKit criterion must be an object")
        atom_id = entry.get("id")
        if not isinstance(atom_id, str) or atom_id in seen:
            raise RuntimeError(f"invalid or duplicate criterion id: {atom_id!r}")
        seen.add(atom_id)
        item = expected_by_id.get(atom_id)
        if item is None:
            raise RuntimeError(f"unexpected criterion id: {atom_id}")
        if entry.get("name") != item["name"]:
            raise RuntimeError(f"criterion {atom_id} has an unexpected name")
        if entry.get("description") != item["description"]:
            raise RuntimeError(f"criterion {atom_id} has an unexpected description")
        weight = entry.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise RuntimeError(f"criterion {atom_id} has invalid weight")
        if not math.isclose(float(weight), float(item["points"]), abs_tol=1e-9):
            raise RuntimeError(f"criterion {atom_id} has an unexpected weight")
        raw = entry.get("raw")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise RuntimeError(f"criterion {atom_id} has invalid raw score")
        raw = float(raw)
        if strict_scores:
            if raw not in _allowed_rubric_scores():
                raise RuntimeError(f"criterion {atom_id} returned score {raw}, not allowed")
        elif not math.isfinite(raw) or not 0.0 <= raw <= 1.0:
            raise RuntimeError(f"criterion {atom_id} returned invalid objective score")
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"criterion {atom_id} has invalid normalized value")
        if not math.isclose(float(value), round(raw, 4), abs_tol=1e-9):
            raise RuntimeError(f"criterion {atom_id} value does not match raw score")
        reasoning = entry.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise RuntimeError(f"criterion {atom_id} is missing reasoning")
        if entry.get("error") is not None:
            raise RuntimeError(f"criterion {atom_id} reported an error")
        if strict_scores and item["hard"] and raw < 1.0:
            hard_failures.append(atom_id)
        points = Decimal(str(float(item["points"])))
        total_weight += points
        total_earned += points * Decimal(str(raw))
        validated.append(
            {
                "id": atom_id,
                "name": item["name"],
                "description": item["description"],
                "weight": float(weight),
                "raw": raw,
                "value": float(value),
                "hard": bool(item["hard"]),
                "evidence_refs": item["evidence_refs"],
                "reasoning": reasoning,
            }
        )
    missing = [item["id"] for item in expected if item["id"] not in seen]
    if missing:
        raise RuntimeError(f"RewardKit detail is missing criterion ids: {', '.join(missing)}")
    return (
        float(total_earned / total_weight) if total_weight else 0.0,
        validated,
        hard_failures,
    )


def run_rubric(
    repo: Path,
    tests: Path,
    logs: Path,
    judge_workdir: Path,
    judge_home: Path,
) -> dict[str, Any]:
    rubric_result, rubric_details = _run_rewardkit(
        tests / "rubric",
        repo,
        judge_workdir / "rubric" / "reward.json",
        logs,
        "rubric",
        env=_judge_environment(judge_home),
    )
    if list(_reported_errors(rubric_details)):
        raise RuntimeError("RewardKit rubric reported an error")
    rubric_detail = _detail_section(rubric_details, "reward")
    rubric_reported = _score(rubric_result)
    raw_score, criteria, hard_failures = _validate_criteria(
        rubric_detail,
        atoms(),
        strict_scores=True,
    )
    if not math.isclose(rubric_reported, raw_score, abs_tol=1e-4):
        raise RuntimeError("rubric score does not match validated criterion scores")
    reward = 0.0 if hard_failures else raw_score
    status = "no_credit" if hard_failures else "pass"
    return {
        "score": reward,
        "raw_score": raw_score,
        "rewardkit_score": rubric_reported,
        "hard_failures": hard_failures,
        "criteria": criteria,
        "rewardkit": rubric_detail,
        "status": status,
        "trainable": not hard_failures,
        "reason": (
            f"hard atom failed: {', '.join(hard_failures)}"
            if hard_failures
            else "rubric score validated"
        ),
    }

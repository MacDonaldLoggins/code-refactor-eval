from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any
import tomllib

from hard_gates import hard_gates
from objective import objective_metrics
import patch_export
from rubric import rubric as rubric_module


SCHEMA_VERSION = "harbor-refactor-eval/v1"
TESTS_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = TESTS_ROOT / "evaluation.toml"


def _config() -> dict[str, Any]:
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _cleanup(logs: Path) -> None:
    for name in (
        "reward.json",
        "reward.txt",
        "reward-details.json",
        patch_export.PATCH_FILENAME,
        patch_export.PATCH_METADATA_FILENAME,
        patch_export.REPO_METADATA_FILENAME,
    ):
        (logs / name).unlink(missing_ok=True)
    shutil.rmtree(logs / patch_export.REPO_EXPORT_DIRNAME, ignore_errors=True)


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


def _synthesized_rewardkit_details(result: dict[str, Any], label: str) -> dict[str, Any]:
    reward = _score(result)
    return {
        "reward": {
            "reward": reward,
            "score": reward,
            "synthesized": True,
            "source": f"missing reward-details.json for {label}",
        }
    }


def _run_rewardkit(
    tests_dir: Path,
    repo: Path,
    output: Path,
    logs: Path,
    label: str,
    allow_missing_details: bool = False,
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
    result = json.loads(output.read_text(encoding="utf-8"))
    detail_path = output.with_name("reward-details.json")
    if detail_path.is_file():
        details = json.loads(detail_path.read_text(encoding="utf-8"))
    elif allow_missing_details:
        details = _synthesized_rewardkit_details(result, label)
    else:
        raise RuntimeError(f"RewardKit {label} did not write reward-details.json")
    return (result, details)


def _objective_expected(metrics: dict[str, Any]) -> float:
    records = metrics.get("metrics", {})
    if not isinstance(records, dict):
        return 0.0
    definitions = objective_metrics.metric_definitions()
    earned = Decimal("0")
    total = Decimal("0")
    for definition in definitions:
        record = records.get(definition["id"])
        if not isinstance(record, dict) or record.get("status") != "ok":
            continue
        value = record.get("candidate")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        weight = Decimal(str(float(definition["weight"])))
        total += weight
        earned += weight * Decimal(str(max(0.0, min(1.0, float(value)))))
    return float(earned / total) if total else 0.0


def _detail_section(details: dict[str, Any], name: str) -> dict[str, Any]:
    section = details.get(name)
    if not isinstance(section, dict):
        raise RuntimeError(f"RewardKit details missing section {name!r}")
    return section


def _result_base(
    gates: dict[str, Any],
    patch_info: dict[str, Any],
    repo_info: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": str(gates.get("task_id", "unknown")),
        "hard_gates": gates,
        "objective_metrics": None,
        "patch_export": patch_info,
        "repo_export": repo_info,
        "rubric": None,
    }


def verify(repo: Path, tests: Path, logs: Path) -> dict[str, Any]:
    repo = repo.resolve()
    tests = tests.resolve()
    logs = logs.resolve()
    judge_home = logs / "judge-home"
    judge_workdir = logs / "judge-workdir"
    logs.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(judge_home, ignore_errors=True)
    shutil.rmtree(judge_workdir, ignore_errors=True)
    _cleanup(logs)
    for name in ("evidence.json", "objective-evidence.json"):
        (logs / name).unlink(missing_ok=True)

    try:
        patch_info = patch_export.export_patch(repo, logs)
        repo_info = patch_export.export_repo(repo, logs)
        gates = hard_gates.run_all(repo)
        _write_json(logs / "evidence.json", gates)
        if gates.get("invalid"):
            result = _result_base(gates, patch_info, repo_info)
            result.update(
                reward=0.0,
                objective_score=None,
                invalid=True,
                trainable=False,
                status="invalid",
                reason="evaluation-integrity gate failed",
            )
        elif gates.get("failed_gates"):
            result = _result_base(gates, patch_info, repo_info)
            result.update(
                reward=0.0,
                objective_score=None,
                invalid=False,
                trainable=False,
                status="failed_hard_gate",
                reason="one or more applicable hard gates failed",
            )
        elif gates.get("uncertain_gates") and _config().get("policy", {}).get(
            "fail_on_uncertain", True
        ):
            result = _result_base(gates, patch_info, repo_info)
            result.update(
                reward=0.0,
                objective_score=None,
                invalid=False,
                trainable=False,
                status="uncertain",
                reason="hard-gate evidence is uncertain",
            )
        else:
            metrics = objective_metrics.collect_metrics(repo)
            _write_json(logs / "objective-evidence.json", metrics)
            if metrics.get("invalid") or metrics.get("status") != "ok":
                result = _result_base(gates, patch_info, repo_info)
                result.update(
                    reward=0.0,
                    objective_score=None,
                    invalid=bool(metrics.get("invalid")),
                    trainable=False,
                    status="invalid" if metrics.get("invalid") else "uncertain",
                    objective_metrics=metrics,
                    reason="objective evidence is unavailable or uncertain",
                )
            else:
                objective_expected = _objective_expected(metrics)
                objective_result, objective_details = _run_rewardkit(
                    tests / "objective",
                    repo,
                    judge_workdir / "objective" / "reward.json",
                    logs,
                    "objective",
                    allow_missing_details=True,
                )
                if list(_reported_errors(objective_details)):
                    raise RuntimeError("RewardKit objective reported an error")
                objective_detail = _detail_section(objective_details, "reward")
                objective_reported = _score(objective_result)
                if not math.isclose(objective_reported, objective_expected, abs_tol=1e-4):
                    raise RuntimeError("objective score does not match metric evidence")

                judge_home.mkdir(parents=True, exist_ok=True)
                rubric_result = rubric_module.run_rubric(
                    repo,
                    tests,
                    logs,
                    judge_workdir,
                    judge_home,
                )
                reward = rubric_result["score"]
                status = rubric_result["status"]
                result = _result_base(gates, patch_info, repo_info)
                result.update(
                    reward=reward,
                    objective_score=objective_expected,
                    objective_rewardkit_score=objective_reported,
                    objective_metrics=metrics,
                    invalid=False,
                    trainable=rubric_result["trainable"],
                    status=status,
                    rubric={**rubric_result, "objective_rewardkit": objective_detail},
                    reason=rubric_result["reason"],
                )
        objective_score = result.get("objective_score")
        if not isinstance(objective_score, (int, float)) or isinstance(objective_score, bool):
            objective_score = 0.0
        reward = float(result["reward"])
        _write_json(
            logs / "reward.json",
            {
                "reward": reward,
                "objective": float(objective_score),
                "rubric": reward,
            },
        )
        _write_json(logs / "reward-details.json", result)
        (logs / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")
        return result
    except Exception:
        _cleanup(logs)
        raise
    finally:
        shutil.rmtree(judge_home, ignore_errors=True)
        shutil.rmtree(judge_workdir, ignore_errors=True)


def main() -> int:
    parser = __import__("argparse").ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("/workspace/repo"))
    parser.add_argument("--tests", type=Path, default=Path("/tests"))
    parser.add_argument("--logs", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    result = verify(args.repo, args.tests, args.logs)
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

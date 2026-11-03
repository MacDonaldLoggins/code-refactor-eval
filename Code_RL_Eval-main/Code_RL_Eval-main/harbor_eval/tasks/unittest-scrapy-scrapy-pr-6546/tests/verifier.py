#!/usr/bin/env python3
"""Harbor verifier: integrity, HC, E1/E2, J1-J5, and reward."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from e2 import evaluate_e2
from judge import (
    DIMENSIONS,
    project_j1_execution_evidence,
    run_judge_panel,
)
from judge_runtime import resolve_judge_runtime_config
from leakage import LeakageAuditError, audit_inbound_content
from reward import calculate_reward, validate_reward_result
from trace_bridge import audit_trajectory_file


WORKSPACE = Path("/app")
LOG_DIR = Path("/logs/verifier")
RUNTIME = Path("/opt/unittest-rl/runtime")
RUNTIME_PROFILES = Path("/opt/unittest-rl/runtimes")
PREPARED_VERIFIER = Path("/tests/prepared_verifier.py")
EXECUTION_PROFILES = Path("/tests/execution-profiles.json")
EXECUTION_PROFILE_BINDINGS = Path("/tests/execution-profile-bindings.json")
FAULTS_DIR = Path("/tests/evaluation_assets/faults")
OBLIGATIONS_DIR = Path("/tests/evaluation_assets/obligations")
E2_CONTRACT = Path("/tests/e2-evidence-contract.json")
JUDGE_CONFIG = Path("/tests/judge-config.json")
JUDGE_REPLAY = Path("/tests/judge-replay.json")
TRAJECTORY = Path("/logs/agent/trajectory.json")
ALLOWED_CANDIDATE_SUFFIXES = {".md", ".py", ".pyi"}
CANDIDATE_FAIL_REWARD = -1.0


def write_json(name: str, payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_candidate_fail_reward() -> None:
    """Emit the fixed terminal reward for a candidate hard-gate failure.

    ``candidate_fail`` remains a positive sentinel for Harbor's failure-count
    aggregation, while ``reward``/``R_total`` carry the formal training score.
    """
    write_json(
        "reward.json",
        {
            "reward": CANDIDATE_FAIL_REWARD,
            "R_total": CANDIDATE_FAIL_REWARD,
            "candidate_fail": 1.0,
            "valid": 0.0,
            "official_reward": 0.0,
        },
    )


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def run_prepared(mode: str, output: Path, *extra: str) -> int:
    binding = load_json(Path("/tests/task_binding.json"))
    profile_mode = binding.get("execution_profiles", {}).get("mode")
    command = [
        "python",
        str(PREPARED_VERIFIER),
        mode,
        "--workspace",
        str(WORKSPACE),
        "--output",
        str(output),
    ]
    if profile_mode == "matrix":
        command.extend(
            [
                "--runtime-root",
                str(RUNTIME_PROFILES),
                "--execution-profiles",
                str(EXECUTION_PROFILES),
            ]
        )
    else:
        command.extend(["--runtime", str(RUNTIME)])
    command.extend(extra)
    completed = subprocess.run(command, check=False)
    return completed.returncode


def candidate_boundary() -> dict[str, Any]:
    root = WORKSPACE / "candidate_tests"
    if not root.is_dir():
        return {
            "status": "candidate_fail",
            "errors": ["candidate_tests_missing"],
            "files": [],
        }
    files = sorted(path for path in root.rglob("*") if path.is_file())
    symlinks = sorted(path for path in root.rglob("*") if path.is_symlink())
    errors: list[str] = []
    if not files:
        errors.append("candidate_tests_empty")
    if symlinks:
        errors.append("candidate_test_symlink")
    invalid = [
        str(path.relative_to(root))
        for path in files
        if path.suffix not in ALLOWED_CANDIDATE_SUFFIXES
    ]
    if invalid:
        errors.append("candidate_test_non_python_file")
    return {
        "status": "pass" if not errors else "candidate_fail",
        "errors": errors,
        "files": [str(path.relative_to(root)) for path in files],
        "invalid_files": invalid,
    }


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    binding = load_json(Path("/tests/task_binding.json"))
    try:
        allowed_raw = os.environ.get(
            "UNITTEST_RL_ALLOWED_CANDIDATE_IDENTITIES_JSON"
        )
        allowed_identities = json.loads(allowed_raw) if allowed_raw else None
        require_call_raw = os.environ.get(
            "UNITTEST_RL_REQUIRE_GENERATION_MODEL_CALL", "false"
        ).strip().lower()
        if require_call_raw not in {"true", "false"}:
            raise ValueError(
                "UNITTEST_RL_REQUIRE_GENERATION_MODEL_CALL must be true or false"
            )
        trajectory = audit_trajectory_file(
            TRAJECTORY,
            expected_agent_name=os.environ.get(
                "UNITTEST_RL_EXPECTED_AGENT_NAME"
            ),
            expected_model_name=os.environ.get(
                "UNITTEST_RL_EXPECTED_MODEL_NAME"
            ),
            allowed_identities=allowed_identities,
            require_model_call=require_call_raw == "true",
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        write_json(
            "verdict.json",
            {
                "status": "evaluation_invalid",
                "classification": "candidate_identity_policy_invalid",
                "official_reward": False,
                "errors": [f"{type(exc).__name__}:{exc}"],
            },
        )
        return 1
    write_json("trajectory-audit.json", trajectory)
    write_json(
        "candidate-session-validation.json",
        trajectory["session_validation"],
    )
    write_json(
        "candidate-boundary-validation.json",
        trajectory["boundary_validation"],
    )
    if trajectory["status"] == "evaluation_invalid":
        write_json(
            "verdict.json",
            {
                "status": "evaluation_invalid",
                "classification": trajectory["classification"],
                "official_reward": False,
            },
        )
        return 1
    if trajectory["status"] != "pass":
        write_json(
            "verdict.json",
            {
                "status": "candidate_fail",
                "classification": trajectory["classification"],
                "official_reward": False,
            },
        )
        write_candidate_fail_reward()
        return 0

    try:
        leakage, leakage_validation = audit_inbound_content(
            trajectory_path=TRAJECTORY,
            evaluation_assets=Path("/tests/evaluation_assets"),
            public_roots=[
                Path("/tests/public-instruction.md"),
                WORKSPACE / "repository",
            ],
            candidate_id=trajectory["candidate_id"],
        )
    except LeakageAuditError as exc:
        write_json(
            "candidate-leakage-audit.json",
            {
                "schema_version": "unittest-rl-answer-leakage-audit-v1",
                "status": "invalid",
                "answer_leakage_status": "not_auditable",
                "official_score_eligible": False,
                "errors": [str(exc)],
            },
        )
        write_json(
            "verdict.json",
            {
                "status": "evaluation_invalid",
                "classification": "candidate_leakage_not_auditable",
                "official_reward": False,
            },
        )
        return 1
    write_json("candidate-leakage-audit.json", leakage)
    write_json("candidate-leakage-validation.json", leakage_validation)
    if leakage.get("answer_leakage_status") != "clean":
        write_json(
            "verdict.json",
            {
                "status": "evaluation_invalid",
                "classification": "candidate_answer_leakage",
                "official_reward": False,
            },
        )
        return 1

    boundary = candidate_boundary()
    write_json("candidate-artifact-boundary.json", boundary)
    if boundary["status"] != "pass":
        write_json(
            "verdict.json",
            {
                "status": "candidate_fail",
                "classification": "candidate_boundary",
                "official_reward": False,
            },
        )
        write_candidate_fail_reward()
        return 0

    hard_gates_path = LOG_DIR / "hard-gates.json"
    hard_gate_exit = run_prepared("hard-gates", hard_gates_path)
    hard_gates = load_json(hard_gates_path) if hard_gates_path.is_file() else {}
    if hard_gates.get("status") == "candidate_fail":
        write_json(
            "verdict.json",
            {
                "status": "candidate_fail",
                "classification": "hard_gates",
                "official_reward": False,
            },
        )
        write_candidate_fail_reward()
        return 0
    if hard_gate_exit != 0 or hard_gates.get("status") != "eligible":
        write_json(
            "verdict.json",
            {
                "status": "evaluation_invalid",
                "classification": "hard_gate_infrastructure",
                "official_reward": False,
            },
        )
        return 1

    e2_binding = binding.get("e2_v2", {})
    judge_binding = binding.get("judge", {})
    positive_observations = None
    j1_execution_evidence = None
    needs_positive_observations = (
        e2_binding.get("enabled") is True
        and e2_binding.get("evidence_mode") == "positive_target_execution"
    ) or (
        judge_binding.get("enabled") is True
        and "SC-J1" in judge_binding.get("dimensions", [])
    )
    if needs_positive_observations:
        observation_path = LOG_DIR / "positive-behavior-observations.json"
        observation_exit = run_prepared("positive-observe", observation_path)
        positive_observations = (
            load_json(observation_path) if observation_path.is_file() else {}
        )
        if (
            observation_exit != 0
            or positive_observations.get("status") != "pass"
        ):
            write_json(
                "verdict.json",
                {
                    "status": "evaluation_invalid",
                    "classification": "positive_behavior_observation_infrastructure",
                    "official_reward": False,
                },
            )
            return 1
        try:
            j1_execution_evidence = project_j1_execution_evidence(
                positive_observations
            )
        except Exception as exc:
            write_json(
                "verdict.json",
                {
                    "status": "evaluation_invalid",
                    "classification": "j1_execution_evidence_infrastructure",
                    "official_reward": False,
                    "errors": [f"{type(exc).__name__}:{exc}"],
                },
            )
            return 1

    replay_path = LOG_DIR / "fault-replay.json"
    replay_exit = run_prepared(
        "fault-replay",
        replay_path,
        "--faults-dir",
        str(FAULTS_DIR),
        "--logs-dir",
        str(LOG_DIR / "faults"),
    )
    replay = load_json(replay_path) if replay_path.is_file() else {}
    records = replay.get("faults")
    if (
        replay_exit != 0
        or replay.get("status") != "pass"
        or not isinstance(records, list)
        or not records
    ):
        write_json(
            "verdict.json",
            {
                "status": "evaluation_invalid",
                "classification": "fault_replay_infrastructure",
                "official_reward": False,
            },
        )
        return 1

    killed = sum(
        1
        for record in records
        if isinstance(record, dict)
        and record.get("classification") == "stable_killed"
    )
    sc_e1 = killed / len(records)
    sc_e1_report = {
        "schema_version": "unittest-rl-harbor-sc-e1-v1",
        "status": "pass",
        "dimension": "SC-E1",
        "fault_count": len(records),
        "killed_count": killed,
        "survived_count": len(records) - killed,
        "score": sc_e1,
        "faults": [
            {
                "fault_id": record.get("fault_id"),
                "classification": record.get("classification"),
            }
            for record in records
            if isinstance(record, dict)
        ],
    }
    write_json("sc-e1.json", sc_e1_report)
    sc_e2_v2 = None
    e2_report = {}
    if e2_binding.get("enabled") is True:
        try:
            e2_input, e2_report = evaluate_e2(
                obligations=load_json(
                    OBLIGATIONS_DIR / "test_obligations.json"
                ),
                obligations_validation=load_json(
                    OBLIGATIONS_DIR / "obligations_validation.json"
                ),
                contract=load_json(E2_CONTRACT),
                candidate_root=WORKSPACE / "candidate_tests",
                fault_replay=replay,
                behavior_observations=positive_observations,
                execution_profile_bindings=load_json(
                    EXECUTION_PROFILE_BINDINGS
                ),
                task_core_fingerprint=binding["task_core_fingerprint"],
                evaluation_id=os.environ.get("HARBOR_TRIAL_ID"),
            )
        except (KeyError, OSError, ValueError) as exc:
            e2_input = {
                "schema_version": "unittest-rl-harbor-sc-e2-input-v1",
                "status": "evaluation_invalid",
                "errors": [str(exc)],
            }
            e2_report = {
                "schema_version": "unittest-rl-sc-e2-v2-v2",
                "status": "evaluation_invalid",
                "dimension": "SC-E2-v2",
                "score": None,
                "errors": [str(exc)],
            }
        write_json("sc-e2-v2-input.json", e2_input)
        write_json("sc-e2-v2.json", e2_report)
        if e2_report.get("status") != "pass":
            write_json(
                "verdict.json",
                {
                    "status": "evaluation_invalid",
                    "classification": "sc_e2_v2_evidence",
                    "official_reward": False,
                },
            )
            return 1
        sc_e2_v2 = e2_report["score"]

    component_scores = {
        "SC-E1": sc_e1,
    }
    if sc_e2_v2 is not None:
        component_scores["SC-E2-v2"] = sc_e2_v2
    judge_mode = os.environ.get("UNITTEST_RL_JUDGE_MODE", "disabled")
    if judge_mode not in {"disabled", "replay", "live"}:
        write_json(
            "verdict.json",
            {
                "status": "evaluation_invalid",
                "classification": "judge_mode_invalid",
                "official_reward": False,
            },
        )
        return 1

    if judge_mode != "disabled":
        if judge_binding.get("enabled") is not True or sc_e2_v2 is None:
            write_json(
                "verdict.json",
                {
                    "status": "evaluation_invalid",
                    "classification": "judge_not_bound",
                    "official_reward": False,
                },
            )
            return 1
        judge_config = load_json(JUDGE_CONFIG)
        judge_replay = (
            load_json(JUDGE_REPLAY) if judge_mode == "replay" else None
        )
        try:
            if judge_mode == "live":
                judge_config = resolve_judge_runtime_config(judge_config)
            panel, judge_artifacts = run_judge_panel(
                mode=judge_mode,
                task_core_fingerprint=binding["task_core_fingerprint"],
                evaluation_id=(
                    os.environ.get("HARBOR_TRIAL_ID") or "harbor-trial"
                ),
                candidate_root=WORKSPACE / "candidate_tests",
                evaluation_assets=Path("/tests/evaluation_assets"),
                fault_replay=replay,
                fault_log_dir=LOG_DIR / "faults",
                sc_e1=sc_e1_report,
                sc_e2=e2_report,
                hard_gates=hard_gates,
                j1_execution_evidence=j1_execution_evidence,
                config=judge_config,
                replay=judge_replay,
            )
        except Exception as exc:
            panel = {
                "schema_version": "unittest-rl-harbor-judge-panel-v1",
                "status": "evaluation_invalid",
                "mode": judge_mode,
                "errors": [f"{type(exc).__name__}:{exc}"],
            }
            judge_artifacts = {}
        for dimension, artifact in judge_artifacts.items():
            slug = dimension.lower()
            write_json(f"judge-runs/{slug}/input.json", artifact["snapshot"])
            for round_artifact in artifact.get("rounds", []):
                round_slug = f"round-{round_artifact['round']:03d}"
                prefix = f"judge-runs/{round_slug}/{slug}"
                write_json(f"{prefix}/input.json", artifact["snapshot"])
                write_json(f"{prefix}/request.json", round_artifact["request"])
                write_json(
                    f"{prefix}/response.json",
                    round_artifact["raw_response"],
                )
                write_json(
                    f"{prefix}/model-result.json",
                    round_artifact["model_result"],
                )
                write_json(
                    f"{prefix}/result.json",
                    round_artifact["formal_result"],
                )
            if "formal_result" in artifact:
                write_json(
                    f"judge-runs/{slug}/majority-result.json",
                    artifact["formal_result"],
                )
                write_json(f"{slug}.json", artifact["formal_result"])
        write_json("judge-panel.json", panel)
        input_validation = panel.get("input_validation")
        if isinstance(input_validation, dict):
            write_json("judge-input-validation.json", input_validation)
        stability_report = panel.get("stability_report")
        if isinstance(stability_report, dict):
            write_json("judge-stability-report.json", stability_report)
        if panel.get("status") != "pass":
            write_json(
                "verdict.json",
                {
                    "status": "evaluation_invalid",
                    "classification": "judge_barrier",
                    "official_reward": False,
                    "errors": panel.get("errors", []),
                },
            )
            return 1
        component_scores.update(
            {
                dimension: panel["dimensions"][dimension]["score"]
                for dimension in DIMENSIONS
            }
        )

        integrity = {
            "answer_leakage_status": leakage["answer_leakage_status"],
            "official_score_eligible": leakage["official_score_eligible"],
            "evidence": "candidate-leakage-audit.json",
        }
        official_reward = (
            judge_mode == "live"
            and panel.get("judge_runtime", {}).get("runner", "api") == "api"
            and integrity["answer_leakage_status"] == "clean"
            and integrity["official_score_eligible"] is True
        )
        try:
            reward_config = load_json(
                Path("/tests/evaluation_assets/reward_config.json")
            )
            reward_details = calculate_reward(
                components=component_scores,
                config=reward_config,
                task_core_fingerprint=binding["task_core_fingerprint"],
                evaluation_id=(
                    os.environ.get("HARBOR_TRIAL_ID") or "harbor-trial"
                ),
                integrity=integrity,
                official_reward=official_reward,
            )
            reward_validation = validate_reward_result(
                reward_details,
                config=reward_config,
            )
        except Exception as exc:
            write_json(
                "verdict.json",
                {
                    "status": "evaluation_invalid",
                    "classification": "reward_calculation",
                    "official_reward": False,
                    "errors": [f"{type(exc).__name__}:{exc}"],
                },
            )
            return 1
        write_json("reward-details.json", reward_details)
        write_json("reward-validation.json", reward_validation)
        if reward_validation.get("status") != "pass":
            write_json(
                "verdict.json",
                {
                    "status": "evaluation_invalid",
                    "classification": "reward_validation",
                    "official_reward": False,
                },
            )
            return 1
        write_json(
            "component-scores.json",
            {
                "schema_version": "unittest-rl-harbor-component-scores-v1",
                "status": "pass",
                "official_reward": official_reward,
                "judge_runtime": panel.get("judge_runtime"),
                "components": component_scores,
            },
        )
        write_json(
            "verdict.json",
            {
                "status": "valid",
                "classification": f"harbor_m4_{judge_mode}",
                "scoring_mode": binding["scoring_mode"],
                "official_reward": official_reward,
                "candidate_identity": trajectory["identity"],
                "judge_runtime": panel.get("judge_runtime"),
                "components": component_scores,
                "R_B": reward_details["R_B"],
                "R_total": reward_details["R_total"],
            },
        )
        write_json(
            "reward.json",
            {
                "reward": reward_details["R_total"],
                "valid": 1.0,
                "official_reward": 1.0 if official_reward else 0.0,
                "R_B": reward_details["R_B"],
            },
        )
        return 0

    write_json(
        "component-scores.json",
        {
            "schema_version": "unittest-rl-harbor-component-scores-v1",
            "status": "pass",
            "official_reward": False,
            "components": component_scores,
        },
    )
    write_json(
        "verdict.json",
        {
            "status": "valid",
            "classification": "harbor_m2_deterministic_only",
            "scoring_mode": binding["scoring_mode"],
            "official_reward": False,
            "sc_e1": sc_e1,
            "sc_e2_v2": sc_e2_v2,
            "killed_faults": killed,
            "total_faults": len(records),
        },
    )
    write_json(
        "reward.json",
        {
            "reward": sc_e1,
            "sc_e1": sc_e1,
            **(
                {"sc_e2_v2": sc_e2_v2}
                if sc_e2_v2 is not None
                else {}
            ),
            "valid": 1.0,
            "official_reward": 0.0,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

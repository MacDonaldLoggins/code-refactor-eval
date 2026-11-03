"""Published Unit Test RL fault-joint reward calculation."""

from __future__ import annotations

from typing import Any


CONFIG_SCHEMA = "unittest-rl-reward-config-v5"
RESULT_SCHEMA = "unittest-rl-reward-result-v6"
VALIDATION_SCHEMA = "unittest-rl-harbor-reward-validation-v2"
REWARD_VERSION = "reward-v5-fault-joint-e2v2-lambda025-001"
FIXED_LAMBDA = 0.25
COMPONENTS = (
    "SC-E1",
    "SC-E2-v2",
    "SC-J1",
    "SC-J2",
    "SC-J3",
    "SC-J4",
    "SC-J5",
)
REWARD_WEIGHTS = {
    "R_fault_B": 0.40,
    "SC-E2-v2": 0.10,
    "SC-J1": 0.10,
    "SC-J2": 0.25,
    "SC-J3": 0.05,
    "SC-J4": 0.10,
}


class RewardError(ValueError):
    """Reward input or independent-validation failure."""


def _validate_config(config: dict[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "reward_version",
        "formal_e2_component",
        "lambda",
        "formula",
    }
    if set(config) != expected_keys:
        raise RewardError("reward config contains retired or unsupported fields")
    if config.get("schema_version") != CONFIG_SCHEMA:
        raise RewardError("reward config schema is invalid")
    if config.get("formal_e2_component") != "SC-E2-v2":
        raise RewardError("reward config formal E2 component mismatch")
    if config.get("reward_version") != REWARD_VERSION:
        raise RewardError("reward config version is unsupported")
    if config.get("formula") != "fault-joint-lambda025-v2":
        raise RewardError("reward config formula is unsupported")
    value = config.get("lambda")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RewardError("reward lambda must be numeric")
    if float(value) != FIXED_LAMBDA:
        raise RewardError(
            f"reward lambda is frozen at {FIXED_LAMBDA}; "
            "create a new reward version to change it"
        )


def _validate_components(components: dict[str, Any]) -> dict[str, float]:
    if set(components) != set(COMPONENTS):
        raise RewardError("reward component set is incomplete or contains extras")
    normalized: dict[str, float] = {}
    for key in COMPONENTS:
        value = components[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise RewardError(f"reward component {key} must be numeric")
        value = float(value)
        if not 0.0 <= value <= 1.0:
            raise RewardError(f"reward component {key} must be in [0, 1]")
        normalized[key] = value
    return normalized


def calculate_reward(
    *,
    components: dict[str, Any],
    config: dict[str, Any],
    task_core_fingerprint: str,
    evaluation_id: str,
    integrity: dict[str, Any],
    official_reward: bool,
) -> dict[str, Any]:
    _validate_config(config)
    values = _validate_components(components)
    if integrity.get("answer_leakage_status") != "clean":
        raise RewardError("candidate answer-leakage status is not clean")
    if integrity.get("official_score_eligible") is not True:
        raise RewardError("candidate is not eligible for an official score")
    lambda_value = float(config["lambda"])
    r_fault_b = values["SC-E1"] * (
        (1.0 - lambda_value) + lambda_value * values["SC-J5"]
    )
    b_values = {"R_fault_B": r_fault_b, **values}
    b_contributions = {
        key: REWARD_WEIGHTS[key] * b_values[key] for key in REWARD_WEIGHTS
    }
    r_b = sum(b_contributions.values())
    return {
        "schema_version": RESULT_SCHEMA,
        "status": "scored",
        "evaluation_status": "valid",
        "hard_gate_status": "eligible",
        "reward_version": config["reward_version"],
        "lambda": lambda_value,
        "formal_e2_component": "SC-E2-v2",
        "candidate_input_integrity": integrity,
        "components": values,
        "R_fault_B": r_fault_b,
        "R_B": r_b,
        "R_total": r_b,
        "formula": {
            "id": "fault-joint-lambda025-v2",
            "fault_formula": "SC-E1 * ((1 - lambda) + lambda * SC-J5)",
            "weights": REWARD_WEIGHTS,
            "weighted_contributions": b_contributions,
        },
        "task_core_fingerprint": task_core_fingerprint,
        "evaluation_id": evaluation_id,
        "official_reward": official_reward,
    }


def validate_reward_result(
    result: dict[str, Any],
    *,
    config: dict[str, Any],
    tolerance: float = 1e-12,
) -> dict[str, Any]:
    """Independently recompute every scalar without calling the calculator."""

    errors: list[str] = []
    try:
        _validate_config(config)
        values = _validate_components(result.get("components", {}))
    except RewardError as exc:
        return {
            "schema_version": VALIDATION_SCHEMA,
            "status": "evaluation_invalid",
            "errors": [str(exc)],
        }
    if result.get("schema_version") != RESULT_SCHEMA:
        errors.append("result_schema_mismatch")
    if result.get("reward_version") != config.get("reward_version"):
        errors.append("reward_version_mismatch")
    lambda_value = float(config["lambda"])
    expected_fault = values["SC-E1"] * (
        (1.0 - lambda_value) + lambda_value * values["SC-J5"]
    )
    b_values = {"R_fault_B": expected_fault, **values}
    expected_b = sum(
        REWARD_WEIGHTS[key] * b_values[key] for key in REWARD_WEIGHTS
    )
    checks = {
        "R_fault_B": expected_fault,
        "R_B": expected_b,
        "R_total": expected_b,
    }
    for key, expected in checks.items():
        actual = result.get(key)
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            errors.append(f"{key}_not_numeric")
        elif abs(float(actual) - expected) > tolerance:
            errors.append(f"{key}_mismatch")
    return {
        "schema_version": VALIDATION_SCHEMA,
        "status": "pass" if not errors else "evaluation_invalid",
        "tolerance": tolerance,
        "recomputed": checks,
        "errors": errors,
    }

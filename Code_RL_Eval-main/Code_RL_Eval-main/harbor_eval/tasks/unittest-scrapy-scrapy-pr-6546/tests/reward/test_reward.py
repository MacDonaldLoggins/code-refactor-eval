from __future__ import annotations

import json
from pathlib import Path

import pytest

from .reward import RewardError, calculate_reward, validate_reward_result


COMPONENTS = {
    "SC-E1": 0.8571428571428571,
    "SC-E2-v2": 0.6156462585034014,
    "SC-J1": 0.25,
    "SC-J2": 0.3,
    "SC-J3": 0.8,
    "SC-J4": 0.5714285714285714,
    "SC-J5": 0.8333333333333334,
}


def _load_config() -> dict[str, object]:
    path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "evaluation_assets"
        / "reward_config.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _integrity() -> dict[str, object]:
    return {
        "answer_leakage_status": "clean",
        "official_score_eligible": True,
    }


def test_fault_joint_reward_is_the_only_total() -> None:
    result = calculate_reward(
        components=COMPONENTS,
        config=_load_config(),
        task_core_fingerprint="task-fingerprint",
        evaluation_id="test-evaluation",
        integrity=_integrity(),
        official_reward=True,
    )

    assert result["R_total"] == pytest.approx(result["R_B"])
    assert result["R_B"] == pytest.approx(0.5872789115646259)
    assert "R_A" not in result
    assert "selected_variant" not in result
    assert validate_reward_result(result, config=_load_config())["status"] == "pass"


def test_retired_dual_variant_config_is_rejected() -> None:
    legacy_config = {
        "schema_version": "unittest-rl-reward-config-v4",
        "reward_version": "reward-v4-dual-ab-e2v2-lambda025-001",
        "formal_e2_component": "SC-E2-v2",
        "compute_all_variants": True,
        "selected_variant": "A",
        "lambda": 0.25,
        "variants": {
            "A": "direct-weighted-v1",
            "B": "fault-joint-lambda025-v1",
        },
    }

    with pytest.raises(RewardError):
        calculate_reward(
            components=COMPONENTS,
            config=legacy_config,
            task_core_fingerprint="task-fingerprint",
            evaluation_id="test-evaluation",
            integrity=_integrity(),
            official_reward=True,
        )

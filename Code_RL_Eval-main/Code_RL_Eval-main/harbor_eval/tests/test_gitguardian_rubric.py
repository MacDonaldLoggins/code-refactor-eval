from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GITGUARDIAN_RUBRIC = (
    ROOT
    / "tasks"
    / "GitGuardian__ggshield__41a2258__947"
    / "tests"
    / "rubric"
    / "rubric.py"
)


def _load_gitguardian_rubric():
    spec = importlib.util.spec_from_file_location(
        "gitguardian_rubric_under_test",
        GITGUARDIAN_RUBRIC,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load GitGuardian rubric helper")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_rewardkit_artifact_allows_nonfatal_warnings():
    rubric = _load_gitguardian_rubric()

    rubric._validate_rewardkit_artifact(
        {
            "reward": {
                "warnings": ["agent retried before returning a valid score"],
                "criteria": [
                    {
                        "id": "C1",
                        "warnings": ["criterion-level diagnostic"],
                    }
                ],
            }
        },
        "rubric",
    )


def test_rewardkit_artifact_rejects_errors():
    rubric = _load_gitguardian_rubric()

    with pytest.raises(RuntimeError, match="RewardKit rubric judge reported an error: 2"):
        rubric._validate_rewardkit_artifact(
            {
                "reward": {
                    "error": "top-level judge failure",
                    "criteria": [
                        {
                            "id": "C1",
                            "error": "criterion failed",
                        }
                    ],
                }
            },
            "rubric",
        )


def test_run_rubric_keeps_valid_score_when_rewardkit_reports_warnings(
    tmp_path,
    monkeypatch,
):
    rubric = _load_gitguardian_rubric()
    repo = tmp_path / "repo"
    tests = tmp_path / "tests"
    logs = tmp_path / "logs"
    judge_workdir = tmp_path / "judge-workdir"
    judge_home = tmp_path / "judge-home"
    repo.mkdir()
    tests.mkdir()

    atom = {
        "id": "C1",
        "name": "behavior",
        "description": "Behavior is preserved",
        "points": 2.0,
        "hard": False,
    }
    rewardkit_detail = {
        "warnings": ["non-fatal RewardKit diagnostic"],
        "criteria": [
            {
                "id": "C1",
                "name": "behavior",
                "description": "Behavior is preserved",
                "weight": 2.0,
                "raw": 1.0,
                "value": 1.0,
                "reasoning": "direct evidence supports full credit",
            }
        ],
    }

    monkeypatch.setattr(rubric, "atoms", lambda: [atom])
    monkeypatch.setattr(rubric, "_judge_environment", lambda home: {"ENV": "ok"})
    monkeypatch.setattr(
        rubric,
        "_run_rewardkit",
        lambda *args, **kwargs: ({"reward": 1.0}, {"reward": rewardkit_detail}),
    )

    result = rubric.run_rubric(
        repo,
        tests,
        logs,
        judge_workdir,
        judge_home,
        {"score": 0.75},
    )

    assert result["score"] == 1.0
    assert result["rewardkit_score"] == 1.0
    assert result["status"] == "pass"
    assert result["rewardkit"]["warnings"] == ["non-fatal RewardKit diagnostic"]

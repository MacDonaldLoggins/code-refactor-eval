from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GITGUARDIAN_TESTS = (
    ROOT
    / "tasks"
    / "GitGuardian__ggshield__41a2258__947"
    / "tests"
)


def _load_gitguardian_verifier():
    sys.path.insert(0, str(GITGUARDIAN_TESTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "gitguardian_verifier_under_test",
            GITGUARDIAN_TESTS / "verifier.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load GitGuardian verifier")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(GITGUARDIAN_TESTS))


def test_run_objective_returns_standard_result_and_writes_evidence(tmp_path, monkeypatch):
    verifier = _load_gitguardian_verifier()
    repo = tmp_path / "repo"
    tests = tmp_path / "tests"
    logs = tmp_path / "logs"
    judge_workdir = tmp_path / "judge-workdir"
    repo.mkdir()

    metrics = {"status": "ok", "metrics": {"metric_a": {"candidate": 1.0}}}
    monkeypatch.setattr(
        verifier.objective_metrics,
        "collect_metrics",
        lambda workspace: metrics,
    )

    def fail_if_rubric_atoms_run():
        raise AssertionError("objective metrics should not depend on rubric atoms")

    monkeypatch.setattr(verifier, "_rubric_atoms", fail_if_rubric_atoms_run)

    result = verifier._run_objective(repo, logs)

    assert result == {
        "passed": True,
        "status": "ok",
        "score": 1.0,
        "metrics": {
            "status": "ok",
            "metrics": {"metric_a": {"candidate": 1.0}},
        },
        "reason": "objective evidence available",
    }
    assert json.loads((logs / "objective-evidence.json").read_text()) == result["metrics"]


def test_run_objective_fails_closed_when_metric_score_is_zero(tmp_path, monkeypatch):
    verifier = _load_gitguardian_verifier()
    repo = tmp_path / "repo"
    tests = tmp_path / "tests"
    logs = tmp_path / "logs"
    judge_workdir = tmp_path / "judge-workdir"
    repo.mkdir()

    metrics = {"status": "ok", "metrics": {"metric_a": {"candidate": 0.0}}}
    monkeypatch.setattr(
        verifier.objective_metrics,
        "collect_metrics",
        lambda workspace: metrics,
    )

    def fail_if_rubric_atoms_run():
        raise AssertionError("objective metrics should not depend on rubric atoms")

    monkeypatch.setattr(verifier, "_rubric_atoms", fail_if_rubric_atoms_run)

    def fail_if_rewardkit_runs(*args, **kwargs):
        raise AssertionError("objective RewardKit should not run after objective fuse trips")

    monkeypatch.setattr(verifier, "_run_rewardkit", fail_if_rewardkit_runs)

    result = verifier._run_objective(repo, logs)

    assert result == {
        "passed": False,
        "status": "failed_objective",
        "score": 0.0,
        "metrics": {
            "status": "ok",
            "metrics": {"metric_a": {"candidate": 0.0}},
        },
        "reason": "objective score failed the circuit breaker",
    }
    assert json.loads((logs / "objective-evidence.json").read_text()) == result["metrics"]


def test_run_rubric_returns_standard_result(tmp_path, monkeypatch):
    verifier = _load_gitguardian_verifier()
    repo = tmp_path / "repo"
    tests = tmp_path / "tests"
    logs = tmp_path / "logs"
    judge_workdir = tmp_path / "judge-workdir"
    judge_home = tmp_path / "judge-home"
    repo.mkdir()

    atom = {
        "id": "C1",
        "name": "behavior",
        "description": "Behavior is preserved",
        "points": 2.0,
        "hard": False,
    }
    rubric_detail = {
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
        ]
    }
    objective = {
        "score": 0.75,
    }
    monkeypatch.setattr(verifier, "_rubric_atoms", lambda: [atom])
    monkeypatch.setattr(verifier, "_judge_environment", lambda home: {"ENV": "ok"})
    monkeypatch.setattr(
        verifier,
        "_run_rewardkit",
        lambda *args, **kwargs: ({"reward": 1.0}, {"reward": rubric_detail}),
    )
    monkeypatch.setattr(verifier, "_validate_rewardkit_artifact", lambda *args: None)

    result = verifier._run_rubric(repo, tests, logs, judge_workdir, judge_home, objective)

    assert result["score"] == 1.0
    assert result["raw_score"] == 1.0
    assert result["rewardkit_score"] == 1.0
    assert result["status"] == "pass"
    assert result["trainable"] is True
    assert result["critical_failures"] == []
    assert result["objective_score"] == 0.75
    assert result["rewardkit"] == rubric_detail


def test_load_verifier_config_reads_run_plan_verifier_section(tmp_path, monkeypatch):
    verifier = _load_gitguardian_verifier()
    monkeypatch.delenv("HARBOR_RUN_PLAN_TOML", raising=False)
    config_path = tmp_path / "run-plan.toml"
    config_path.write_text(
        """
schema_version = "harbor-run-plan/v1"

[verifier]
skip_hard_gates = true
objective_weight = 0.3
rubric_weight = 0.7

[[runs]]
agent = "codex"
models = ["gpt-5.5"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verifier, "RUN_PLAN_CONFIG_PATH", config_path)

    assert verifier._load_verifier_config() == {
        "skip_hard_gates": True,
        "objective_weight": 0.3,
        "rubric_weight": 0.7,
    }


def test_load_verifier_config_defaults_when_run_plan_has_no_verifier_section(tmp_path, monkeypatch):
    verifier = _load_gitguardian_verifier()
    monkeypatch.delenv("HARBOR_RUN_PLAN_TOML", raising=False)
    config_path = tmp_path / "run-plan.toml"
    config_path.write_text(
        """
schema_version = "harbor-run-plan/v1"

[[runs]]
agent = "codex"
models = ["gpt-5.5"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verifier, "RUN_PLAN_CONFIG_PATH", config_path)

    assert verifier._load_verifier_config() == {
        "skip_hard_gates": False,
        "objective_weight": 0.3,
        "rubric_weight": 0.7,
    }


def test_run_hard_gates_passes_patch_path_to_hard_gates(monkeypatch, tmp_path):
    verifier = _load_gitguardian_verifier()
    repo = tmp_path / "repo"
    patch = tmp_path / "refactor.patch"
    repo.mkdir()
    patch.write_text("", encoding="utf-8")
    calls = []

    def fake_run_all(received_repo, patch_path=None):
        calls.append((received_repo, patch_path))
        return {"passed": True, "invalid": False, "gates": {}}

    monkeypatch.setattr(verifier.hard_gates, "run_all", fake_run_all)

    assert verifier._run_hard_gates(repo, patch_path=patch) == {
        "passed": True,
        "invalid": False,
        "gates": {},
    }
    assert calls == [(repo, patch)]


def test_verify_passes_exported_patch_path_to_hard_gates(tmp_path, monkeypatch):
    verifier = _load_gitguardian_verifier()
    repo = tmp_path / "repo"
    tests = tmp_path / "tests"
    logs = tmp_path / "logs"
    patch = logs / "refactor.patch"
    repo.mkdir()
    tests.mkdir()
    calls = []

    monkeypatch.setattr(
        verifier,
        "_load_verifier_config",
        lambda: {
            "skip_hard_gates": False,
            "objective_weight": 0.3,
            "rubric_weight": 0.7,
        },
    )
    monkeypatch.setattr(
        verifier,
        "export_patch",
        lambda received_repo, received_logs: {"patch_path": str(patch)},
    )
    monkeypatch.setattr(verifier, "export_repo", lambda received_repo, received_logs: {})

    def fake_run_hard_gates(received_repo, patch_path=None):
        calls.append((received_repo, patch_path))
        return {
            "passed": False,
            "invalid": False,
            "failed_gates": ["patch_applicable"],
            "uncertain_gates": [],
            "gates": {},
        }

    monkeypatch.setattr(verifier, "_run_hard_gates", fake_run_hard_gates)

    verifier.verify(repo, tests, logs)

    assert calls == [(repo.resolve(), patch)]
    details = json.loads((logs / "reward-details.json").read_text(encoding="utf-8"))
    assert details["status"] == "failed_hard_gate"


def test_weighted_reward_combines_objective_and_rubric_scores():
    verifier = _load_gitguardian_verifier()

    assert verifier._weighted_reward(
        objective_score=0.4,
        rubric_score=0.8,
        config={
            "skip_hard_gates": False,
            "objective_weight": 0.25,
            "rubric_weight": 0.75,
        },
    ) == 0.7000000000000001


def test_run_rubric_keeps_score_when_hard_atom_fails(tmp_path, monkeypatch):
    verifier = _load_gitguardian_verifier()
    repo = tmp_path / "repo"
    tests = tmp_path / "tests"
    logs = tmp_path / "logs"
    judge_workdir = tmp_path / "judge-workdir"
    judge_home = tmp_path / "judge-home"
    repo.mkdir()

    atoms = [
        {
            "id": "C1",
            "name": "behavior",
            "description": "Behavior is preserved",
            "points": 1.0,
            "hard": True,
        },
        {
            "id": "C2",
            "name": "structure",
            "description": "Structure stays tidy",
            "points": 1.0,
            "hard": False,
        },
    ]
    rubric_detail = {
        "criteria": [
            {
                "id": "C1",
                "name": "behavior",
                "description": "Behavior is preserved",
                "weight": 1.0,
                "raw": 0.0,
                "value": 0.0,
                "reasoning": "hard atom is not satisfied",
            },
            {
                "id": "C2",
                "name": "structure",
                "description": "Structure stays tidy",
                "weight": 1.0,
                "raw": 1.0,
                "value": 1.0,
                "reasoning": "soft atom is satisfied",
            },
        ]
    }
    objective = {"score": 0.5}
    monkeypatch.setattr(verifier, "_rubric_atoms", lambda: atoms)
    monkeypatch.setattr(verifier, "_judge_environment", lambda home: {"ENV": "ok"})
    monkeypatch.setattr(
        verifier,
        "_run_rewardkit",
        lambda *args, **kwargs: ({"reward": 0.5}, {"reward": rubric_detail}),
    )
    monkeypatch.setattr(verifier, "_validate_rewardkit_artifact", lambda *args: None)

    result = verifier._run_rubric(repo, tests, logs, judge_workdir, judge_home, objective)

    assert result["score"] == 0.5
    assert result["raw_score"] == 0.5
    assert result["status"] == "pass"
    assert result["trainable"] is True
    assert result["critical_failures"] == ["C1"]

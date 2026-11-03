from __future__ import annotations

import importlib.util
import json
import sys
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATHS = [
    ("template", ROOT / "templates" / "refactor_task_v1" / "tests" / "verifier.py"),
    (
        "twenty",
        ROOT / "tasks" / "twentyhq__twenty__d20e537__23168" / "tests" / "verifier.py",
    ),
]

RUBRIC_PATHS = [
    ("template", ROOT / "templates" / "refactor_task_v1" / "tests" / "rubric" / "rubric.py"),
    (
        "twenty",
        ROOT / "tasks" / "twentyhq__twenty__d20e537__23168" / "tests" / "rubric" / "rubric.py",
    ),
]


def _load_verifier(verifier_path: Path):
    module_names = (
        "hard_gates",
        "hard_gates.hard_gates",
        "objective",
        "objective.objective_metrics",
        "patch_export",
        "rubric",
        "rubric.rubric",
    )
    saved_modules = {
        name: sys.modules.pop(name)
        for name in module_names
        if name in sys.modules
    }
    tests_dir = verifier_path.parent
    sys.path.insert(0, str(tests_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            f"{verifier_path.stem}_under_test",
            verifier_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"unable to load verifier: {verifier_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(tests_dir))
        for name in module_names:
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


@pytest.mark.parametrize("case_name,verifier_path", VERIFIER_PATHS)
def test_reward_json_contains_only_numeric_harbor_rewards(
    case_name,
    verifier_path,
    tmp_path,
    monkeypatch,
):
    verifier = _load_verifier(verifier_path)
    repo = tmp_path / "repo"
    tests = tmp_path / "tests"
    logs = tmp_path / "logs"
    repo.mkdir()
    tests.mkdir()
    logs.mkdir()

    monkeypatch.setattr(
        verifier.patch_export,
        "export_patch",
        lambda received_repo, received_logs: {"patch_path": str(received_logs / "refactor.patch")},
    )
    monkeypatch.setattr(
        verifier.patch_export,
        "export_repo",
        lambda received_repo, received_logs: {"repo_export_path": str(received_logs / "repo")},
    )
    monkeypatch.setattr(
        verifier.hard_gates,
        "run_all",
        lambda received_repo: {
            "task_id": "template-case",
            "invalid": False,
            "failed_gates": ["scope_pass"],
            "uncertain_gates": [],
            "gates": {
                "scope_pass": {
                    "status": "fail",
                    "reason": "outside declared task scope",
                }
            },
        },
    )

    result = verifier.verify(repo, tests, logs)

    reward = json.loads((logs / "reward.json").read_text(encoding="utf-8"))
    assert reward == {
        "reward": 0.0,
        "objective": 0.0,
        "rubric": 0.0,
    }
    assert all(isinstance(value, float) for value in reward.values())
    assert "schema_version" not in reward
    assert "status" not in reward

    details = json.loads((logs / "reward-details.json").read_text(encoding="utf-8"))
    assert details["status"] == "failed_hard_gate"
    assert details["hard_gates"]["failed_gates"] == ["scope_pass"]
    assert result["status"] == "failed_hard_gate"


@pytest.mark.parametrize("payload_key", ["reward", "score"])
@pytest.mark.parametrize("case_name,verifier_path", VERIFIER_PATHS)
def test_run_rewardkit_synthesizes_missing_details(
    case_name,
    verifier_path,
    payload_key,
    tmp_path,
    monkeypatch,
):
    verifier = _load_verifier(verifier_path)
    repo = tmp_path / "repo"
    tests = tmp_path / "tests"
    logs = tmp_path / "logs"
    repo.mkdir()
    tests.mkdir()
    logs.mkdir()

    def fake_run(command, env=None, stdout=None, stderr=None, check=None):
        output_path = Path(command[command.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps({payload_key: 0.625}) + "\n",
            encoding="utf-8",
        )
        stdout.write("rewardkit ran\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)

    result, details = verifier._run_rewardkit(
        tests / "objective",
        repo,
        tmp_path / "judge-workdir" / "objective" / "reward.json",
        logs,
        "objective",
        allow_missing_details=True,
    )

    assert result == {payload_key: 0.625}
    assert details == {
        "reward": {
            "reward": 0.625,
            "score": 0.625,
            "synthesized": True,
            "source": "missing reward-details.json for objective",
        }
    }


@pytest.mark.parametrize("case_name,verifier_path", VERIFIER_PATHS)
def test_verify_uses_deterministic_objective_metrics(
    case_name,
    verifier_path,
    tmp_path,
    monkeypatch,
):
    verifier = _load_verifier(verifier_path)
    repo = tmp_path / "repo"
    tests = tmp_path / "tests"
    logs = tmp_path / "logs"
    repo.mkdir()
    tests.mkdir()
    logs.mkdir()

    definitions = verifier.objective_metrics.metric_definitions()
    metrics = {
        "schema_version": verifier.SCHEMA_VERSION,
        "status": "ok",
        "invalid": False,
        "metrics": {
            definition["id"]: {
                "baseline": 0.0,
                "candidate": 0.5,
                "delta": 0.5,
                "applicable": True,
                "status": "ok",
                "evidence": {},
            }
            for definition in definitions
        },
    }

    monkeypatch.setattr(
        verifier.patch_export,
        "export_patch",
        lambda received_repo, received_logs: {"patch_path": str(received_logs / "refactor.patch")},
    )
    monkeypatch.setattr(
        verifier.patch_export,
        "export_repo",
        lambda received_repo, received_logs: {"repo_export_path": str(received_logs / "repo")},
    )
    monkeypatch.setattr(
        verifier.hard_gates,
        "run_all",
        lambda received_repo: {
            "task_id": f"{case_name}-case",
            "invalid": False,
            "failed_gates": [],
            "uncertain_gates": [],
            "gates": {},
        },
    )
    monkeypatch.setattr(
        verifier.objective_metrics,
        "collect_metrics",
        lambda received_repo: metrics,
    )
    monkeypatch.setattr(
        verifier.rubric_module,
        "run_rubric",
        lambda received_repo, received_tests, received_logs, received_workdir, received_home: {
            "score": 0.75,
            "raw_score": 0.75,
            "rewardkit_score": 0.75,
            "hard_failures": [],
            "criteria": [],
            "status": "pass",
            "trainable": True,
            "reason": "rubric score validated",
        },
    )
    monkeypatch.setattr(
        verifier,
        "_run_rewardkit",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("objective verifier should not call RewardKit")
        ),
    )

    result = verifier.verify(repo, tests, logs)

    assert json.loads((logs / "reward.json").read_text(encoding="utf-8")) == {
        "reward": 0.75,
        "objective": 0.5,
        "rubric": 0.75,
    }
    details = json.loads((logs / "reward-details.json").read_text(encoding="utf-8"))
    assert result["objective_rewardkit_score"] == pytest.approx(0.5)
    assert details["objective_rewardkit"]["synthesized"] is True
    assert len(details["objective_rewardkit"]["criteria"]) == len(definitions)
    assert not (logs / "rewardkit-objective-stdout.txt").exists()


@pytest.mark.parametrize("case_name,rubric_path", RUBRIC_PATHS)
def test_rewardkit_score_accepts_reward_or_score_field(
    case_name,
    rubric_path,
):
    rubric = _load_verifier(rubric_path)

    assert rubric._score({"reward": 0.625}) == pytest.approx(0.625)
    assert rubric._score({"score": 0.625}) == pytest.approx(0.625)

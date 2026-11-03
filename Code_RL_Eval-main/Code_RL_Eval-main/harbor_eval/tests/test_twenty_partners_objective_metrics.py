from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TASK_TESTS = ROOT / "tasks" / "twentyhq__twenty__d20e537__23168" / "tests"


def _load_objective_metrics():
    module_names = (
        "hard_gates",
        "hard_gates.hard_gates",
    )
    saved_modules = {
        name: sys.modules.pop(name)
        for name in module_names
        if name in sys.modules
    }
    sys.path.insert(0, str(TASK_TESTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "twenty_partners_objective_metrics_under_test",
            TASK_TESTS / "objective" / "objective_metrics.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load objective_metrics")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(TASK_TESTS))
        for name in module_names:
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


def test_change_scope_focus_counts_strict_protected_paths_as_outside_scope(monkeypatch, tmp_path):
    objective_metrics = _load_objective_metrics()
    repo = tmp_path / "repo"
    baseline = tmp_path / "baseline"
    repo.mkdir()
    baseline.mkdir()
    objective_metrics.BASELINE_REPO = baseline

    changed = [
        "packages/twenty-apps/internal/twenty-partners/src/roles/partner.role.ts",
        "packages/twenty-apps/internal/twenty-partners/src/skills/custom.skill.ts",
        "packages/twenty-apps/internal/twenty-partners/src/modules/partner/self-service/logic-functions/save-my-partner-profile.logic-function.ts",
    ]
    monkeypatch.setattr(objective_metrics, "_changed_files", lambda received_repo: changed)
    monkeypatch.setattr(
        objective_metrics,
        "_behavior_score",
        lambda baseline_repo, candidate_repo: (
            1.0,
            {"baseline": {}, "candidate": {}},
        ),
    )
    monkeypatch.setattr(
        objective_metrics,
        "_module_layout_score",
        lambda received_repo: (
            1.0,
            {"existing_modules": ["application", "opportunity", "partner", "shared"]},
        ),
    )
    monkeypatch.setattr(
        objective_metrics,
        "_logic_entrypoint_score",
        lambda received_repo: (
            1.0,
            {"logic_function_count": 1, "thin_adapter_count": 1},
        ),
    )
    monkeypatch.setattr(
        objective_metrics,
        "_graphql_locality_score",
        lambda received_repo: (
            1.0,
            {"raw_graphql_operation_file_count": 0},
        ),
    )
    monkeypatch.setattr(
        objective_metrics,
        "_import_only_change_pass",
        lambda received_repo, path: (
            path.endswith("roles/partner.role.ts"),
            {"path": path, "offending_line_count": 1, "offending_lines": ["const x = 1;"]},
        ),
    )

    metrics = objective_metrics.collect_metrics(repo)

    assert metrics["status"] == "ok"
    scope = metrics["metrics"]["change_scope_focus"]
    assert scope["candidate"] == pytest.approx(2.0 / 3.0)
    assert scope["evidence"]["strict_protected_violations"] == [
        "packages/twenty-apps/internal/twenty-partners/src/skills/custom.skill.ts"
    ]

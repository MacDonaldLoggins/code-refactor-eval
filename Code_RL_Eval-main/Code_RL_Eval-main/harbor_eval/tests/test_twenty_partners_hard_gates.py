from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_TESTS = ROOT / "tasks" / "twentyhq__twenty__d20e537__23168" / "tests"


def _load_hard_gates():
    sys.path.insert(0, str(TASK_TESTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "twenty_partners_hard_gates_under_test",
            TASK_TESTS / "hard_gates" / "hard_gates.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load hard_gates")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(TASK_TESTS))


def test_protected_paths_pass_allows_import_only_root_entrypoints(monkeypatch):
    hard_gates = _load_hard_gates()
    repo = Path("/tmp/twenty-partners")
    changed = {
        "packages/twenty-apps/internal/twenty-partners/src/roles/partner.role.ts",
        "packages/twenty-apps/internal/twenty-partners/src/scripts/seed.ts",
    }

    monkeypatch.setattr(hard_gates, "_changed_paths", lambda received_repo: (changed, "ok"))
    monkeypatch.setattr(
        hard_gates,
        "_import_only_change_pass",
        lambda received_repo, path: (True, {"path": path, "offending_lines": []}),
    )

    result = hard_gates.protected_paths_pass(repo)

    assert result["status"] == "pass"
    assert result["passed"] is True


def test_protected_paths_pass_rejects_non_import_only_root_entrypoint_changes(monkeypatch):
    hard_gates = _load_hard_gates()
    repo = Path("/tmp/twenty-partners")
    changed = {
        "packages/twenty-apps/internal/twenty-partners/src/roles/partner.role.ts",
    }

    monkeypatch.setattr(hard_gates, "_changed_paths", lambda received_repo: (changed, "ok"))
    monkeypatch.setattr(
        hard_gates,
        "_import_only_change_pass",
        lambda received_repo, path: (False, {"path": path, "offending_lines": ["const x = 1;"]}),
    )

    result = hard_gates.protected_paths_pass(repo)

    assert result["status"] == "fail"
    assert result["passed"] is False
    assert "import-only protected path violation" in result["reason"]


def test_protected_paths_pass_rejects_strict_protected_paths(monkeypatch):
    hard_gates = _load_hard_gates()
    repo = Path("/tmp/twenty-partners")
    changed = {
        "packages/twenty-apps/internal/twenty-partners/src/skills/custom.skill.ts",
    }

    monkeypatch.setattr(hard_gates, "_changed_paths", lambda received_repo: (changed, "ok"))
    monkeypatch.setattr(
        hard_gates,
        "_import_only_change_pass",
        lambda received_repo, path: (True, {"path": path, "offending_lines": []}),
    )

    result = hard_gates.protected_paths_pass(repo)

    assert result["status"] == "fail"
    assert result["passed"] is False
    assert "strict protected path modification" in result["reason"]


def test_unit_test_gate_accepts_project_or_legacy_config_entrypoint():
    hard_gates = _load_hard_gates()
    command = next(
        entry["command"]
        for entry in hard_gates.GATE_POLICY["commands"]
        if entry["name"] == "partners_unit_tests"
    )

    assert "vitest.unit.config.ts" in command
    assert "yarn vitest run --project unit" in command


def test_scope_allows_package_local_guidance_docs():
    hard_gates = _load_hard_gates()
    allowed = hard_gates.GATE_POLICY["allowed_paths"]

    assert "packages/twenty-apps/internal/twenty-partners/AGENTS.md" in allowed
    assert "packages/twenty-apps/internal/twenty-partners/CLAUDE.md" in allowed

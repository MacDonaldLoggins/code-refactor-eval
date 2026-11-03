from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GITGUARDIAN_TESTS = (
    ROOT
    / "tasks"
    / "GitGuardian__ggshield__41a2258__947"
    / "tests"
)


def _load_gitguardian_hard_gates():
    sys.path.insert(0, str(GITGUARDIAN_TESTS / "hard_gates"))
    try:
        spec = importlib.util.spec_from_file_location(
            "gitguardian_hard_gates_under_test",
            GITGUARDIAN_TESTS / "hard_gates" / "hard_gates.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load GitGuardian hard gates")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(GITGUARDIAN_TESTS / "hard_gates"))


def test_collect_added_lines_prefers_exported_patch_over_diff(tmp_path, monkeypatch):
    hard_gates = _load_gitguardian_hard_gates()
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = tmp_path / "refactor.patch"
    patch.write_text(
        """
diff --git a/tests/unit/test_sample.py b/tests/unit/test_sample.py
index 1111111..2222222 100644
--- a/tests/unit/test_sample.py
+++ b/tests/unit/test_sample.py
@@ -1,2 +1,3 @@
 import pytest
+pytest.skip(\"do not run\")
 unchanged
""".lstrip(),
        encoding="utf-8",
    )

    def fail_if_diff_runs(*args, **kwargs):
        raise AssertionError("patch input should avoid fallback diff")

    monkeypatch.setattr(hard_gates, "_run_subprocess", fail_if_diff_runs)
    monkeypatch.setattr(hard_gates, "_resolve_baseline_repo", lambda repo: None)

    result = hard_gates.no_new_skip_or_xfail(repo, patch_path=patch)

    assert result["status"] == "fail"
    assert result["reason"] == "1 file(s) with newly added skip/xfail markers"
    assert result["details"]["input_scope"]["files_with_additions"] == [
        "tests/unit/test_sample.py"
    ]
    assert result["details"]["evidence"]["violations"] == {
        "tests/unit/test_sample.py": ["pytest.skip"]
    }


def test_cheating_diff_based_uses_exported_patch_when_directory_diff_fails(
    tmp_path,
    monkeypatch,
):
    hard_gates = _load_gitguardian_hard_gates()
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = tmp_path / "refactor.patch"
    patch.write_text(
        """
diff --git a/ggshield/core/scan/file.py b/ggshield/core/scan/file.py
index 1111111..2222222 100644
--- a/ggshield/core/scan/file.py
+++ b/ggshield/core/scan/file.py
@@ -1,2 +1,3 @@
 def collect():
+    return [path for path in paths]
     return []
""".lstrip(),
        encoding="utf-8",
    )

    def fail_if_diff_runs(*args, **kwargs):
        raise AssertionError("patch input should avoid fallback diff")

    monkeypatch.setattr(hard_gates, "_run_subprocess", fail_if_diff_runs)
    monkeypatch.setattr(hard_gates, "_resolve_baseline_repo", lambda repo: None)

    result = hard_gates.cheating_diff_based_pass(repo, patch_path=patch)

    assert result["status"] == "pass"
    assert result["reason"] == "no violations in diff"
    assert result["details"]["input_scope"]["files_with_additions"] == [
        "ggshield/core/scan/file.py"
    ]


def test_collect_added_lines_falls_back_when_patch_is_missing(tmp_path, monkeypatch):
    hard_gates = _load_gitguardian_hard_gates()
    repo = tmp_path / "repo"
    baseline = tmp_path / "baseline"
    repo.mkdir()
    baseline.mkdir()

    monkeypatch.setattr(hard_gates, "_resolve_baseline_repo", lambda repo: baseline)
    monkeypatch.setattr(hard_gates, "_git_available", lambda repo: False)

    def fake_diff(cmd, timeout=300, cwd=None, check=False):
        assert cmd == ["diff", "-ruN", str(baseline), str(repo)]
        return subprocess.CompletedProcess(
            cmd,
            1,
            "--- baseline/a.py\n+++ repo/a.py\n@@ -1 +1,2 @@\n+added\n",
            "",
        )

    monkeypatch.setattr(hard_gates, "_run_subprocess", fake_diff)

    added, reason = hard_gates._collect_added_lines(
        repo,
        patch_path=tmp_path / "missing.patch",
    )

    assert reason == "ok"
    assert added == {"repo/a.py": ["added"]}


def test_run_all_passes_patch_path_to_patch_aware_gates(tmp_path, monkeypatch):
    hard_gates = _load_gitguardian_hard_gates()
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = tmp_path / "refactor.patch"
    patch.write_text("", encoding="utf-8")

    def gate(name, status="pass"):
        return lambda repo: {
            "name": name,
            "applicable": True,
            "status": status,
            "passed": status == "pass",
            "reason": name,
            "details": {},
        }

    for name in (
        "patch_applicable",
        "output_parseable",
        "build_pass",
        "type_check_pass",
        "regression_pass",
        "differential_equivalence_pass",
        "api_baseline_diff_pass",
        "structure_ast_pass",
        "behavior_oracle_pass",
        "critical_structure_pass",
        "tests_not_tampered",
        "anti_cheating_pass",
    ):
        monkeypatch.setattr(hard_gates, name, gate(name))

    seen_patch_paths = []

    def no_new(repo, patch_path=None):
        seen_patch_paths.append(patch_path)
        return gate("no_new_skip_or_xfail")(repo)

    def cheating(repo, patch_path=None):
        seen_patch_paths.append(patch_path)
        return gate("cheating_diff_based_pass")(repo)

    monkeypatch.setattr(hard_gates, "no_new_skip_or_xfail", no_new)
    monkeypatch.setattr(hard_gates, "cheating_diff_based_pass", cheating)

    result = hard_gates.run_all(repo, patch_path=patch)

    assert result["passed"] is True
    assert seen_patch_paths == [patch, patch]


def test_type_check_pass_fails_nonzero_command(tmp_path, monkeypatch):
    hard_gates = _load_gitguardian_hard_gates()

    monkeypatch.setitem(hard_gates.HARD_GATES_CONFIG, "type_check_commands", ["pyright"])
    monkeypatch.setitem(hard_gates.HARD_GATES_CONFIG, "retry_failed_commands", False)
    monkeypatch.setattr(
        hard_gates,
        "_run_subprocess",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            "type error",
            "",
        ),
    )

    result = hard_gates.type_check_pass(tmp_path)

    assert result["status"] == "fail"
    assert result["reason"] == "1 type-check command(s) failed"
    assert result["details"]["evidence"]["failures"][0]["stdout"] == "type error"


def test_api_baseline_diff_fails_strict_signature_change(tmp_path, monkeypatch):
    hard_gates = _load_gitguardian_hard_gates()
    baseline = tmp_path / "baseline"
    repo = tmp_path / "repo"
    baseline.mkdir()
    repo.mkdir()
    (baseline / "api.py").write_text("def collect(path):\n    return path\n")
    (repo / "api.py").write_text("def collect(path, mode=None):\n    return path\n")

    monkeypatch.setitem(hard_gates.HARD_GATES_CONFIG, "editable_paths", ["api.py"])
    monkeypatch.setitem(
        hard_gates.HARD_GATES_CONFIG,
        "api_baseline_check",
        {
            "allow_new_public_api": True,
            "enabled": True,
            "strict_signature": True,
        },
    )
    monkeypatch.setitem(
        hard_gates.HARD_GATES_CONFIG,
        "differential_baseline_repo",
        str(baseline),
    )

    result = hard_gates.api_baseline_diff_pass(repo)

    assert result["status"] == "fail"
    assert result["reason"] == "changed 1 public API signature(s)"
    assert result["details"]["evidence"]["changed_api"][0]["key"] == "api.py::collect"

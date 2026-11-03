from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import Any

from hard_gates import hard_gates


SCHEMA_VERSION = "harbor-refactor-eval/v1"
BASELINE_REPO = Path("/workspace/baseline_repo")

METRIC_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "behavior_evidence_coverage",
        "name": "behavior_evidence_coverage",
        "description": "Executable evidence covers the task's declared behavior contract",
        "weight": 1.0,
    },
    {
        "id": "change_scope_focus",
        "name": "change_scope_focus",
        "description": "Candidate changes remain inside the scope declared by hard gates",
        "weight": 1.0,
    },
]


def metric_definitions() -> list[dict[str, Any]]:
    return [dict(item) for item in METRIC_DEFINITIONS]


def _run(repo: Path, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _base_commit(repo: Path) -> str | None:
    marker = repo / ".base_commit"
    if not marker.is_file():
        return None
    value = marker.read_text(encoding="utf-8", errors="replace").strip()
    return value or None


def _changed_files(repo: Path) -> list[str] | None:
    base = _base_commit(repo)
    if not base:
        return None
    diff = _run(repo, ["git", "diff", "--name-only", base, "--"])
    untracked = _run(repo, ["git", "ls-files", "--others", "--exclude-standard", "--"])
    if diff.returncode != 0 or untracked.returncode != 0:
        return None
    changed = {line.strip() for line in diff.stdout.splitlines() if line.strip()}
    changed.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())
    changed.discard(".base_commit")
    return sorted(changed)


def _allowed_paths() -> list[str]:
    policy = getattr(hard_gates, "GATE_POLICY", {})
    if not isinstance(policy, dict):
        return []
    values = policy.get("allowed_paths", [])
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def _metric(
    baseline: float,
    candidate: float,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    value = max(0.0, min(1.0, float(candidate)))
    return {
        "baseline": float(baseline),
        "candidate": value,
        "delta": value - float(baseline),
        "applicable": True,
        "status": "ok",
        "evidence": evidence,
    }


def _uncertain(reason: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "baseline": None,
        "candidate": None,
        "delta": None,
        "applicable": True,
        "status": "uncertain",
        "reason": reason,
        "evidence": evidence,
    }


def collect_metrics(repo_path: str | Path) -> dict[str, Any]:
    repo = Path(repo_path).resolve()
    baseline = BASELINE_REPO.resolve()
    changed = _changed_files(repo)
    if not repo.is_dir() or not baseline.is_dir() or changed is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "uncertain",
            "invalid": False,
            "metrics": {},
            "evidence": {
                "repo": str(repo),
                "baseline_repo": str(baseline),
                "changed_files": changed,
            },
        }

    allowed = _allowed_paths()
    inside = [
        path
        for path in changed
        if any(fnmatch.fnmatch(path, pattern) for pattern in allowed)
    ]

    # CHANGE_ME: Replace this uncertain record with direct executable evidence
    # derived from behavior_contract.md, such as focused tests, differential
    # oracles, schema comparisons, CLI snapshots, or runtime probes.
    behavior = _uncertain(
        "behavior_evidence_coverage has not been implemented for this case",
        {"required_action": "implement task-specific executable behavior evidence"},
    )

    if allowed:
        scope = _metric(
            1.0,
            len(inside) / max(1, len(changed)),
            {
                "changed_files": changed,
                "inside_scope": inside,
                "allowed_paths": allowed,
                "sample_boundary": "candidate diff only",
            },
        )
    else:
        scope = _uncertain(
            "change_scope_focus cannot run because hard_gates.GATE_POLICY.allowed_paths is empty",
            {"changed_files": changed},
        )

    metrics = {
        "behavior_evidence_coverage": behavior,
        "change_scope_focus": scope,
    }
    uncertain = [
        name for name, record in metrics.items() if record.get("status") == "uncertain"
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "uncertain" if uncertain else "ok",
        "invalid": False,
        "metrics": metrics,
        "uncertain_metrics": uncertain,
        "evidence": {
            "repo": str(repo),
            "baseline_repo": str(baseline),
            "changed_files": changed,
        },
    }

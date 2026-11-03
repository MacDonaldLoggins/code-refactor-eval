from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from hard_gates import hard_gates


SCHEMA_VERSION = "harbor-refactor-eval/v1"
BASELINE_REPO = Path("/workspace/baseline_repo")
PARTNERS_ROOT = Path("packages/twenty-apps/internal/twenty-partners")
SRC_ROOT = PARTNERS_ROOT / "src"
MODULES_ROOT = SRC_ROOT / "modules"
EXPECTED_DOMAIN_MODULES = {"application", "opportunity", "partner", "shared"}
FLAT_PRIMITIVE_DIRS = {
    "fields",
    "front-components",
    "logic-functions",
    "navigation-menu-items",
    "objects",
    "page-layouts",
    "view-fields",
    "views",
}
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

METRIC_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "behavior_evidence_coverage",
        "name": "behavior_evidence_coverage",
        "description": "Static behavior-contract proxies preserve UUIDs and SDK logic-function contracts",
        "weight": 2.0,
    },
    {
        "id": "module_layout_completion",
        "name": "module_layout_completion",
        "description": "The partners app moved from flat primitive directories into expected domain modules",
        "weight": 2.0,
    },
    {
        "id": "logic_entrypoint_thinness",
        "name": "logic_entrypoint_thinness",
        "description": "SDK logic-function entrypoints are thin adapters instead of business-logic owners",
        "weight": 1.0,
    },
    {
        "id": "graphql_locality",
        "name": "graphql_locality",
        "description": "Raw GraphQL client operations are localized in graphql/ modules",
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


def _policy_paths(key: str) -> list[str]:
    policy = getattr(hard_gates, "GATE_POLICY", {})
    if not isinstance(policy, dict):
        return []
    values = policy.get(key, [])
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def _is_import_only_delta_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("import "):
        return True
    if stripped.startswith("from "):
        return True
    if stripped.startswith("}") and " from " in stripped:
        return True
    return False


def _import_only_change_pass(repo: Path, path: str) -> tuple[bool, dict[str, Any]]:
    base = _base_commit(repo)
    if not base:
        return False, {"path": path, "reason": "base commit marker is missing"}
    process = _run(repo, ["git", "diff", "--unified=0", base, "--", path])
    if process.returncode != 0:
        return False, {"path": path, "reason": "git diff could not be evaluated"}

    offending_lines: list[str] = []
    for line in process.stdout.splitlines():
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "@@")):
            continue
        if not line or line[0] not in "+-":
            continue
        content = line[1:]
        if _is_import_only_delta_line(content):
            continue
        offending_lines.append(content[:160])
    return (
        not offending_lines,
        {
            "path": path,
            "offending_line_count": len(offending_lines),
            "offending_lines": offending_lines[:40],
        },
    )


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


def _is_test_path(path: Path) -> bool:
    name = path.name
    return (
        "__tests__" in path.parts
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.tsx")
        or name.endswith(".integration-test.ts")
        or name.endswith(".integration-test.tsx")
    )


def _source_files(repo: Path, *, production_only: bool = True) -> list[Path]:
    root = repo / SRC_ROOT
    if not root.is_dir():
        return []
    result: list[Path] = []
    for suffix in ("*.ts", "*.tsx"):
        for path in root.rglob(suffix):
            if path.is_file() and (not production_only or not _is_test_path(path)):
                result.append(path)
    return sorted(result)


def _uuid_counts(repo: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in _source_files(repo, production_only=True):
        text = path.read_text(encoding="utf-8", errors="replace")
        counts.update(match.group(0).lower() for match in UUID_RE.finditer(text))
    return counts


def _literal_after_key(text: str, key: str) -> str | None:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*(['\"])(.*?)\1", text, re.DOTALL)
    return match.group(2) if match else None


def _boolean_after_key(text: str, key: str) -> str | None:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*(true|false)", text)
    return match.group(1) if match else None


def _number_after_key(text: str, key: str) -> str | None:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*([0-9]+)", text)
    return match.group(1) if match else None


def _logic_function_contracts(repo: Path) -> dict[str, dict[str, str | None]]:
    contracts: dict[str, dict[str, str | None]] = {}
    for path in _source_files(repo, production_only=True):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "defineLogicFunction" not in text:
            continue
        block = text[text.rfind("defineLogicFunction") :]
        name = (
            _literal_after_key(block, "name")
            or path.name.removesuffix(".logic-function.ts").removesuffix(".ts")
        )
        contracts[name] = {
            "route_path": _literal_after_key(block, "path"),
            "http_method": _literal_after_key(block, "httpMethod"),
            "is_auth_required": _boolean_after_key(block, "isAuthRequired"),
            "timeout_seconds": _number_after_key(block, "timeoutSeconds"),
        }
    return contracts


def _behavior_score(baseline: Path, candidate: Path) -> tuple[float, dict[str, Any]]:
    expected_uuids = _uuid_counts(baseline)
    actual_uuids = _uuid_counts(candidate)
    expected_contracts = _logic_function_contracts(baseline)
    actual_contracts = _logic_function_contracts(candidate)
    uuid_same = expected_uuids == actual_uuids
    logic_same = expected_contracts == actual_contracts
    score = (int(uuid_same) + int(logic_same)) / 2
    return score, {
        "uuid_same": uuid_same,
        "logic_function_contracts_same": logic_same,
        "baseline_uuid_count": sum(expected_uuids.values()),
        "candidate_uuid_count": sum(actual_uuids.values()),
        "baseline_logic_functions": sorted(expected_contracts),
        "candidate_logic_functions": sorted(actual_contracts),
        "sample_boundary": "production source UUID strings and defineLogicFunction public metadata only",
    }


def _module_layout_score(repo: Path) -> tuple[float, dict[str, Any]]:
    src = repo / SRC_ROOT
    modules = repo / MODULES_ROOT
    existing_modules = {path.name for path in modules.iterdir() if path.is_dir()} if modules.is_dir() else set()
    flat_dirs = sorted(
        name
        for name in FLAT_PRIMITIVE_DIRS
        if (src / name).is_dir()
        and any(path.is_file() for pattern in ("*.ts", "*.tsx") for path in (src / name).rglob(pattern))
    )
    module_score = len(EXPECTED_DOMAIN_MODULES & existing_modules) / len(EXPECTED_DOMAIN_MODULES)
    flat_score = 1.0 if not flat_dirs else 0.0
    score = (module_score + flat_score) / 2
    return score, {
        "existing_modules": sorted(existing_modules),
        "expected_modules": sorted(EXPECTED_DOMAIN_MODULES),
        "remaining_flat_primitive_dirs": flat_dirs,
        "sample_boundary": "folder shape under twenty-partners/src",
    }


def _nonblank_line_count(path: Path) -> int:
    return sum(
        1
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.strip().startswith("//")
    )


def _logic_entrypoint_score(repo: Path) -> tuple[float, dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in _source_files(repo, production_only=True):
        if not path.name.endswith(".logic-function.ts"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = _nonblank_line_count(path)
        has_raw_io = any(token in text for token in ("client.query(", "client.mutate(", "fetch("))
        good = lines <= 90 and not has_raw_io
        entries.append(
            {
                "file": path.relative_to(repo).as_posix(),
                "nonblank_lines": lines,
                "has_raw_client_or_fetch_call": has_raw_io,
                "thin_adapter": good,
            }
        )
    if not entries:
        return 0.0, {"logic_functions": [], "reason": "no logic-function entrypoints found"}
    score = sum(1 for entry in entries if entry["thin_adapter"]) / len(entries)
    return score, {
        "logic_function_count": len(entries),
        "thin_adapter_count": sum(1 for entry in entries if entry["thin_adapter"]),
        "sample": entries[:80],
        "sample_boundary": "production *.logic-function.ts files; <=90 nonblank lines and no raw client/fetch call",
    }


def _graphql_locality_score(repo: Path) -> tuple[float, dict[str, Any]]:
    offenders: list[str] = []
    total = 0
    for path in _source_files(repo, production_only=True):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(token in text for token in ("client.query(", "client.mutate(", "client.delete(")):
            continue
        total += 1
        relative = path.relative_to(repo).as_posix()
        if "/graphql/" not in relative:
            offenders.append(relative)
    score = 1.0 if total == 0 else (total - len(offenders)) / total
    return score, {
        "raw_graphql_operation_file_count": total,
        "outside_graphql_directory": offenders[:80],
        "outside_graphql_directory_count": len(offenders),
        "sample_boundary": "files containing client.query/client.mutate/client.delete in production source",
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

    behavior_baseline, behavior_before_evidence = _behavior_score(baseline, baseline)
    behavior_candidate, behavior_after_evidence = _behavior_score(baseline, repo)
    layout_baseline, layout_before_evidence = _module_layout_score(baseline)
    layout_candidate, layout_after_evidence = _module_layout_score(repo)
    thin_baseline, thin_before_evidence = _logic_entrypoint_score(baseline)
    thin_candidate, thin_after_evidence = _logic_entrypoint_score(repo)
    graphql_baseline, graphql_before_evidence = _graphql_locality_score(baseline)
    graphql_candidate, graphql_after_evidence = _graphql_locality_score(repo)

    allowed = _allowed_paths()
    protected = _policy_paths("protected_paths")
    import_only = _policy_paths("import_only_paths")
    inside = [
        path
        for path in changed
        if any(fnmatch.fnmatch(path, pattern) for pattern in allowed)
        and not any(fnmatch.fnmatch(path, pattern) for pattern in protected)
    ]
    import_only_violations: list[dict[str, Any]] = []
    for path in changed:
        if not any(fnmatch.fnmatch(path, pattern) for pattern in import_only):
            continue
        passed, evidence = _import_only_change_pass(repo, path)
        if passed:
            inside.append(path)
        else:
            import_only_violations.append(evidence)
    inside = sorted(set(inside))
    strict_protected = sorted(
        path
        for path in changed
        if any(fnmatch.fnmatch(path, pattern) for pattern in protected)
    )
    outside_allowed = sorted(
        path
        for path in changed
        if not any(fnmatch.fnmatch(path, pattern) for pattern in allowed)
    )
    if allowed:
        scope = _metric(
            1.0,
            len(inside) / max(1, len(changed)),
            {
                "changed_files": changed,
                "inside_scope": inside,
                "outside_allowed_scope": outside_allowed,
                "strict_protected_violations": strict_protected,
                "import_only_violations": import_only_violations[:20],
                "allowed_paths": allowed,
                "protected_paths": protected,
                "import_only_paths": import_only,
                "sample_boundary": "candidate diff only",
            },
        )
    else:
        scope = _uncertain(
            "change_scope_focus cannot run because hard_gates.GATE_POLICY.allowed_paths is empty",
            {"changed_files": changed},
        )

    metrics = {
        "behavior_evidence_coverage": _metric(
            behavior_baseline,
            behavior_candidate,
            {
                "baseline": behavior_before_evidence,
                "candidate": behavior_after_evidence,
            },
        ),
        "module_layout_completion": _metric(
            layout_baseline,
            layout_candidate,
            {
                "baseline": layout_before_evidence,
                "candidate": layout_after_evidence,
            },
        ),
        "logic_entrypoint_thinness": _metric(
            thin_baseline,
            thin_candidate,
            {
                "baseline": thin_before_evidence,
                "candidate": thin_after_evidence,
            },
        ),
        "graphql_locality": _metric(
            graphql_baseline,
            graphql_candidate,
            {
                "baseline": graphql_before_evidence,
                "candidate": graphql_after_evidence,
            },
        ),
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

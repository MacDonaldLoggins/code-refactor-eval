from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import Any, Callable
import tomllib


SCHEMA_VERSION = "harbor-refactor-eval/v1"
TESTS_ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = TESTS_ROOT.parent
TASK_TOML_PATH = TASK_ROOT / "task.toml"

# Edit this policy per case. The verifier stays fixed.
GATE_POLICY: dict[str, Any] = {
    "allowed_paths": [],
    "protected_paths": [],
    "anti_cheating_patterns": [],
    "commands": [],
}


def _policy() -> dict[str, Any]:
    return GATE_POLICY


def _task_id() -> str:
    try:
        data = tomllib.loads(TASK_TOML_PATH.read_text(encoding="utf-8"))
    except Exception:
        return TASK_ROOT.name
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        task_id = metadata.get("task_id")
        if isinstance(task_id, str) and task_id.strip():
            return task_id.strip()
    return TASK_ROOT.name


def _result(
    name: str,
    applicable: bool,
    status: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"pass", "fail", "na", "uncertain"}:
        raise ValueError(f"invalid gate status: {status}")
    passed = True if status == "pass" else False if status == "fail" else None
    return {
        "name": name,
        "applicable": applicable,
        "status": status,
        "passed": passed,
        "reason": reason,
        "details": details or {},
    }


def _run(repo: Path, command: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _base_commit(repo: Path) -> str | None:
    path = repo / ".base_commit"
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8", errors="replace").strip()
    return value or None


def _changed_paths(repo: Path) -> tuple[set[str] | None, str]:
    base = _base_commit(repo)
    if not base:
        return None, "base commit marker is missing"

    valid = _run(repo, ["git", "cat-file", "-e", f"{base}^{{commit}}"], timeout=30)
    if valid.returncode != 0:
        return None, "base commit is not available in candidate git history"

    diff = _run(repo, ["git", "diff", "--name-only", base, "--"], timeout=60)
    untracked = _run(
        repo,
        ["git", "ls-files", "--others", "--exclude-standard", "--"],
        timeout=60,
    )
    if diff.returncode != 0 or untracked.returncode != 0:
        return None, "git diff/status could not be evaluated"

    paths = {line.strip() for line in diff.stdout.splitlines() if line.strip()}
    paths.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())
    paths.discard(".base_commit")
    return paths, "ok"


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def patch_applicable(repo: Path) -> dict[str, Any]:
    changed, reason = _changed_paths(repo)
    if changed is None:
        return _result(
            "patch_applicable",
            True,
            "uncertain",
            reason,
            {"evidence": {"candidate_repo": str(repo)}},
        )
    if not changed:
        return _result(
            "patch_applicable",
            True,
            "fail",
            "no candidate changes detected",
            {"evidence": {"changed_files": []}},
        )
    return _result(
        "patch_applicable",
        True,
        "pass",
        f"detected {len(changed)} changed file(s)",
        {"evidence": {"changed_files": sorted(changed)}},
    )


def scope_pass(repo: Path) -> dict[str, Any]:
    policy = _policy()
    allowed = [str(value) for value in policy.get("allowed_paths", [])]
    changed, reason = _changed_paths(repo)
    if changed is None:
        return _result("scope_pass", True, "uncertain", reason)
    if not allowed:
        return _result("scope_pass", True, "uncertain", "allowed_paths is empty")

    outside = sorted(path for path in changed if not _matches(path, allowed))
    details = {
        "input_scope": {"allowed_paths": allowed},
        "evidence": {"changed_files": sorted(changed), "outside_scope": outside},
    }
    if outside:
        return _result(
            "scope_pass",
            True,
            "fail",
            f"{len(outside)} changed file(s) are outside the allowed scope",
            details,
        )
    return _result("scope_pass", True, "pass", "all changes are inside scope", details)


def configured_command_gates(repo: Path) -> list[dict[str, Any]]:
    commands = _policy().get("commands", [])
    if not isinstance(commands, list):
        raise RuntimeError("GATE_POLICY.commands must be an array")

    results: list[dict[str, Any]] = []
    for entry in commands:
        if not isinstance(entry, dict):
            raise RuntimeError("hard gate command entry must be an object")
        name = str(entry.get("name", "")).strip()
        command = str(entry.get("command", "")).strip()
        timeout = int(entry.get("timeout_sec", 300))
        if not name or not command:
            raise RuntimeError("hard gate command requires name and command")

        try:
            process = _run(repo, ["bash", "-lc", command], timeout=timeout)
        except subprocess.TimeoutExpired:
            results.append(
                _result(
                    name,
                    True,
                    "uncertain",
                    f"command timed out after {timeout}s",
                    {"input_scope": {"command": command, "timeout_sec": timeout}},
                )
            )
            continue

        details = {
            "input_scope": {"command": command, "timeout_sec": timeout},
            "evidence": {
                "exit_code": process.returncode,
                "stdout": process.stdout[-4000:],
                "stderr": process.stderr[-4000:],
            },
        }
        missing_tool = process.returncode == 127 or any(
            marker in process.stderr
            for marker in ("command not found", "No module named", "ModuleNotFoundError")
        )
        if missing_tool:
            results.append(_result(name, True, "uncertain", "required tool is unavailable", details))
        elif process.returncode != 0:
            results.append(_result(name, True, "fail", "configured command failed", details))
        else:
            results.append(_result(name, True, "pass", "configured command passed", details))
    return results


def anti_cheating_pass(repo: Path) -> dict[str, Any]:
    policy = _policy()
    protected = [str(value) for value in policy.get("protected_paths", [])]
    patterns = [str(value).lower() for value in policy.get("anti_cheating_patterns", [])]
    changed, reason = _changed_paths(repo)
    if changed is None:
        return _result("anti_cheating_pass", True, "uncertain", reason)

    violations: list[dict[str, Any]] = []
    for path in sorted(changed):
        if _matches(path, protected):
            violations.append({"type": "protected_path_modified", "file": path})

    if patterns:
        added, diff_reason = _added_lines(repo)
        if added is None:
            return _result("anti_cheating_pass", True, "uncertain", diff_reason)
        for path, lines in added.items():
            for line in lines:
                lowered = line.lower()
                for pattern in patterns:
                    if pattern and pattern in lowered:
                        violations.append(
                            {
                                "type": "anti_cheating_pattern",
                                "file": path,
                                "pattern": pattern,
                                "line": line[:160],
                            }
                        )

    details = {
        "input_scope": {
            "protected_paths": protected,
            "anti_cheating_patterns": patterns,
        },
        "evidence": {"violations": violations[:100], "violation_count": len(violations)},
    }
    if violations:
        return _result(
            "anti_cheating_pass",
            True,
            "fail",
            f"detected {len(violations)} evaluation-integrity violation(s)",
            details,
        )
    return _result(
        "anti_cheating_pass",
        True,
        "pass",
        "no evaluation-integrity violations detected",
        details,
    )


def _added_lines(repo: Path) -> tuple[dict[str, list[str]] | None, str]:
    base = _base_commit(repo)
    if not base:
        return None, "base commit marker is missing"
    process = _run(repo, ["git", "diff", "--unified=0", base, "--"], timeout=120)
    if process.returncode != 0:
        return None, "git diff could not be evaluated"

    mapping: dict[str, list[str]] = {}
    current_file: str | None = None
    for line in process.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and current_file:
            mapping.setdefault(current_file, []).append(line[1:])
    return mapping, "ok"


def custom_gates() -> list[Callable[[Path], dict[str, Any]]]:
    """Return task-specific gates such as behavior oracles or API compatibility checks."""
    return []


def run_all(repo_path: str | Path) -> dict[str, Any]:
    repo = Path(repo_path).resolve()
    functions: list[Callable[[Path], dict[str, Any]]] = [patch_applicable, scope_pass]
    results = [function(repo) for function in functions]
    results.extend(configured_command_gates(repo))
    results.extend(function(repo) for function in custom_gates())
    results.append(anti_cheating_pass(repo))

    gates: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    uncertain: list[str] = []
    invalid = False
    for result in results:
        name = result["name"]
        if name in gates:
            raise RuntimeError(f"duplicate hard gate name: {name}")
        gates[name] = result
        if result["status"] == "uncertain":
            uncertain.append(name)
        elif result["applicable"] and result["status"] == "fail":
            if name == "anti_cheating_pass":
                invalid = True
            else:
                failed.append(name)

    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": _task_id(),
        "invalid": invalid,
        "failed_gates": failed,
        "uncertain_gates": uncertain,
        "passed": not invalid and not failed and not uncertain,
        "gates": gates,
    }

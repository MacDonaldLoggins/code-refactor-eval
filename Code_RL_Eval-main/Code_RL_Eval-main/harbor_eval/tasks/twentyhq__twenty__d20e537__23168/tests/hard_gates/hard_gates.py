from __future__ import annotations

import fnmatch
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable
import tomllib


SCHEMA_VERSION = "harbor-refactor-eval/v1"
TESTS_ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = TESTS_ROOT.parent
TASK_TOML_PATH = TASK_ROOT / "task.toml"

# Edit this policy per case. The verifier stays fixed.
GATE_POLICY: dict[str, Any] = {
    "allowed_paths": [
        "packages/twenty-apps/internal/twenty-partners/src/**",
        "packages/twenty-apps/internal/twenty-partners/package.json",
        "packages/twenty-apps/internal/twenty-partners/vitest.config.ts",
        "packages/twenty-apps/internal/twenty-partners/vitest.unit.config.ts",
        "packages/twenty-apps/internal/twenty-partners/tsconfig.json",
        "packages/twenty-apps/internal/twenty-partners/tsconfig.spec.json",
        "packages/twenty-apps/internal/twenty-partners/AGENTS.md",
        "packages/twenty-apps/internal/twenty-partners/CLAUDE.md",
    ],
    "protected_paths": [
        ".github/**",
        "packages/twenty-apps/internal/twenty-partners/yarn.lock",
        "packages/twenty-apps/internal/twenty-partners/.yarnrc.yml",
        "packages/twenty-apps/internal/twenty-partners/.oxlintrc.json",
        "packages/twenty-apps/internal/twenty-partners/src/constants/universal-identifiers.ts",
        "packages/twenty-apps/internal/twenty-partners/src/skills/**",
        "packages/twenty-apps/internal/twenty-partners/src/workflows/**",
    ],
    "import_only_paths": [
        "packages/twenty-apps/internal/twenty-partners/src/roles/**",
        "packages/twenty-apps/internal/twenty-partners/src/scripts/**",
    ],
    "anti_cheating_patterns": [
        "describe.skip(",
        "it.skip(",
        "test.skip(",
        "describe.only(",
        "it.only(",
        "test.only(",
        "/logs/verifier",
        "baseline_repo",
        "rewardkit",
    ],
    "commands": [
        {
            "name": "partners_dependency_install",
            "command": "cd packages/twenty-apps/internal/twenty-partners && yarn install --immutable",
            "timeout_sec": 900,
        },
        {
            "name": "partners_lint",
            "command": "cd packages/twenty-apps/internal/twenty-partners && yarn oxlint -c .oxlintrc.json .",
            "timeout_sec": 300,
        },
        {
            "name": "partners_unit_tests",
            "command": "cd packages/twenty-apps/internal/twenty-partners && if [ -f vitest.unit.config.ts ]; then yarn vitest run --config vitest.unit.config.ts; else yarn vitest run --project unit; fi",
            "timeout_sec": 600,
        },
    ],
}

PARTNERS_ROOT = Path("packages/twenty-apps/internal/twenty-partners")
SRC_ROOT = PARTNERS_ROOT / "src"
MODULES_ROOT = SRC_ROOT / "modules"
BASELINE_REPO = Path("/workspace/baseline_repo")

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
TSC_DIAGNOSTIC_RE = re.compile(
    r"^(?P<path>.+?\.tsx?)\((?P<line>\d+),(?P<column>\d+)\): error "
    r"(?P<code>TS\d+): (?P<message>.*)$"
)
TYPECHECK_CONFIG_TEMPLATE = """{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "noEmit": true,
    "composite": false,
    "declaration": false,
    "declarationMap": false
  },
  "include": ["src/**/*.ts", "src/**/*.tsx"],
  "exclude": [
    "node_modules",
    "dist",
    "src/scripts/**",
    "src/__tests__/**",
    "src/**/*.test.ts",
    "src/**/*.spec.ts",
    "src/**/*.integration-test.ts",
    "src/**/*.setup.ts"
  ]
}
"""


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
        return False, {"reason": "base commit marker is missing"}
    process = _run(repo, ["git", "diff", "--unified=0", base, "--", path], timeout=120)
    if process.returncode != 0:
        return False, {"reason": "git diff could not be evaluated"}

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
            "offending_lines": offending_lines[:40],
            "offending_line_count": len(offending_lines),
        },
    )


def _baseline() -> Path | None:
    return BASELINE_REPO if BASELINE_REPO.is_dir() else None


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


def _relative_to_repo(repo: Path, path: Path) -> str:
    return path.relative_to(repo).as_posix()


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


def _logic_function_contracts(repo: Path) -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    for path in _source_files(repo, production_only=True):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "defineLogicFunction" not in text:
            continue
        block = text[text.rfind("defineLogicFunction") :]
        name = _literal_after_key(block, "name")
        if not name:
            name = path.name.removesuffix(".logic-function.ts").removesuffix(".ts")
        contracts[name] = {
            "source_file": _relative_to_repo(repo, path),
            "route_path": _literal_after_key(block, "path"),
            "http_method": _literal_after_key(block, "httpMethod"),
            "is_auth_required": _boolean_after_key(block, "isAuthRequired"),
            "timeout_seconds": _number_after_key(block, "timeoutSeconds"),
        }
    return contracts


def partners_module_layout_pass(repo: Path) -> dict[str, Any]:
    src = repo / SRC_ROOT
    modules = repo / MODULES_ROOT
    if not src.is_dir():
        return _result(
            "partners_module_layout_pass",
            True,
            "fail",
            "twenty-partners src directory is missing",
            {"input_scope": {"src": SRC_ROOT.as_posix()}},
        )

    existing_modules = {path.name for path in modules.iterdir() if path.is_dir()} if modules.is_dir() else set()
    missing_modules = sorted(EXPECTED_DOMAIN_MODULES - existing_modules)
    flat_dirs = sorted(
        name
        for name in FLAT_PRIMITIVE_DIRS
        if (src / name).is_dir()
        and any(path.is_file() for pattern in ("*.ts", "*.tsx") for path in (src / name).rglob(pattern))
    )
    root_uuid_file = src / "constants" / "universal-identifiers.ts"
    details = {
        "input_scope": {
            "expected_domain_modules": sorted(EXPECTED_DOMAIN_MODULES),
            "flat_primitive_dirs": sorted(FLAT_PRIMITIVE_DIRS),
            "root_universal_identifiers": _relative_to_repo(repo, root_uuid_file),
        },
        "evidence": {
            "existing_modules": sorted(existing_modules),
            "missing_modules": missing_modules,
            "remaining_flat_primitive_dirs": flat_dirs,
            "root_universal_identifiers_exists": root_uuid_file.is_file(),
        },
    }
    failures = []
    if missing_modules:
        failures.append(f"missing module(s): {', '.join(missing_modules)}")
    if flat_dirs:
        failures.append(f"remaining flat primitive dir(s): {', '.join(flat_dirs)}")
    if not root_uuid_file.is_file():
        failures.append("root universal-identifiers.ts was moved or deleted")
    if failures:
        return _result(
            "partners_module_layout_pass",
            True,
            "fail",
            "; ".join(failures),
            details,
        )
    return _result(
        "partners_module_layout_pass",
        True,
        "pass",
        "twenty-partners uses the expected vertical module layout",
        details,
    )


def universal_identifier_preservation_pass(repo: Path) -> dict[str, Any]:
    baseline = _baseline()
    if baseline is None:
        return _result(
            "universal_identifier_preservation_pass",
            True,
            "uncertain",
            "baseline filesystem snapshot is unavailable",
        )
    expected = _uuid_counts(baseline)
    actual = _uuid_counts(repo)
    missing = sorted((expected - actual).elements())
    extra = sorted((actual - expected).elements())
    details = {
        "input_scope": {
            "source_root": SRC_ROOT.as_posix(),
            "sample_boundary": "production .ts/.tsx files under twenty-partners/src; tests excluded",
        },
        "evidence": {
            "baseline_uuid_count": sum(expected.values()),
            "candidate_uuid_count": sum(actual.values()),
            "missing_uuid_sample": missing[:40],
            "extra_uuid_sample": extra[:40],
            "missing_uuid_count": len(missing),
            "extra_uuid_count": len(extra),
        },
    }
    if missing or extra:
        return _result(
            "universal_identifier_preservation_pass",
            True,
            "fail",
            "production UUID string multiset changed",
            details,
        )
    return _result(
        "universal_identifier_preservation_pass",
        True,
        "pass",
        "production UUID string multiset is unchanged",
        details,
    )


def logic_function_contract_pass(repo: Path) -> dict[str, Any]:
    baseline = _baseline()
    if baseline is None:
        return _result(
            "logic_function_contract_pass",
            True,
            "uncertain",
            "baseline filesystem snapshot is unavailable",
        )
    expected = _logic_function_contracts(baseline)
    actual = _logic_function_contracts(repo)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed: dict[str, dict[str, Any]] = {}
    for name in sorted(set(expected) & set(actual)):
        before = {
            key: value
            for key, value in expected[name].items()
            if key != "source_file"
        }
        after = {
            key: value
            for key, value in actual[name].items()
            if key != "source_file"
        }
        if before != after:
            changed[name] = {"baseline": before, "candidate": after}
    details = {
        "input_scope": {
            "source_root": SRC_ROOT.as_posix(),
            "compared_fields": [
                "name",
                "route_path",
                "http_method",
                "is_auth_required",
                "timeout_seconds",
            ],
        },
        "evidence": {
            "baseline_contracts": expected,
            "candidate_contracts": actual,
            "missing_logic_functions": missing,
            "extra_logic_functions": extra,
            "changed_contracts": changed,
        },
    }
    if missing or extra or changed:
        return _result(
            "logic_function_contract_pass",
            True,
            "fail",
            "SDK logic-function public contract changed",
            details,
        )
    return _result(
        "logic_function_contract_pass",
        True,
        "pass",
        "SDK logic-function public contracts are unchanged",
        details,
    )


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
    patterns = [str(value).lower() for value in policy.get("anti_cheating_patterns", [])]
    changed, reason = _changed_paths(repo)
    if changed is None:
        return _result("anti_cheating_pass", True, "uncertain", reason)

    violations: list[dict[str, Any]] = []
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


def protected_paths_pass(repo: Path) -> dict[str, Any]:
    policy = _policy()
    protected = [str(value) for value in policy.get("protected_paths", [])]
    import_only = [str(value) for value in policy.get("import_only_paths", [])]
    changed, reason = _changed_paths(repo)
    if changed is None:
        return _result("protected_paths_pass", True, "uncertain", reason)

    strict_violations = sorted(
        path for path in changed if _matches(path, protected)
    )
    import_only_violations: list[str] = []
    import_only_evidence: list[dict[str, Any]] = []
    for path in sorted(path for path in changed if _matches(path, import_only)):
        passed, evidence = _import_only_change_pass(repo, path)
        if not passed:
            import_only_violations.append(path)
            import_only_evidence.append(evidence)
    details = {
        "input_scope": {
            "protected_paths": protected,
            "import_only_paths": import_only,
        },
        "evidence": {
            "strict_violations": strict_violations[:100],
            "strict_violation_count": len(strict_violations),
            "import_only_violations": import_only_violations[:100],
            "import_only_violation_count": len(import_only_violations),
            "import_only_evidence": import_only_evidence[:20],
        },
    }
    if strict_violations or import_only_violations:
        return _result(
            "protected_paths_pass",
            True,
            "fail",
            (
                f"detected {len(strict_violations)} strict protected path "
                f"modification(s) and {len(import_only_violations)} import-only "
                "protected path violation(s)"
            ),
            details,
        )
    return _result(
        "protected_paths_pass",
        True,
        "pass",
        "no protected paths were modified outside import-only allowances",
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


def _parse_tsc_diagnostics(output: str) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    for line in output.splitlines():
        match = TSC_DIAGNOSTIC_RE.match(line.strip())
        if match:
            diagnostics.append(match.groupdict())
    return diagnostics


def partners_typecheck_pass(repo: Path) -> dict[str, Any]:
    baseline = _baseline()
    temp_config = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", prefix=".harbor-typecheck-", dir=repo / PARTNERS_ROOT, delete=False
    )
    try:
        config_path = Path(temp_config.name)
        temp_config.write(TYPECHECK_CONFIG_TEMPLATE)
        temp_config.flush()
        temp_config.close()
        command = (
            f"cd {PARTNERS_ROOT.as_posix()} && "
            f"yarn tsc --pretty false -p {config_path.name}"
        )
        process = _run(repo, ["bash", "-lc", command], timeout=600)
    finally:
        try:
            temp_config.close()
        except Exception:
            pass
        Path(temp_config.name).unlink(missing_ok=True)

    details = {
        "input_scope": {
            "candidate_command": command,
            "comparison": "candidate diagnostics are compared against the baseline diagnostics; only new diagnostics fail",
            "timeout_sec": 600,
        },
        "evidence": {
            "exit_code": process.returncode,
            "stdout": process.stdout[-4000:],
            "stderr": process.stderr[-4000:],
        },
    }
    candidate_diagnostics = _parse_tsc_diagnostics(process.stdout + "\n" + process.stderr)
    details["evidence"]["candidate_diagnostic_count"] = len(candidate_diagnostics)

    missing_tool = process.returncode == 127 or any(
        marker in process.stderr
        for marker in ("command not found", "No module named", "ModuleNotFoundError")
    )
    if missing_tool:
        return _result(
            "partners_typecheck",
            True,
            "uncertain",
            "required tool is unavailable",
            details,
        )

    if process.returncode == 0:
        details["evidence"].update(
            {
                "baseline_diagnostic_count": 0,
                "new_diagnostic_count": 0,
                "new_diagnostics": [],
            }
        )
        return _result(
            "partners_typecheck",
            True,
            "pass",
            "typecheck passed cleanly",
            details,
        )

    if baseline is None:
        return _result(
            "partners_typecheck",
            True,
            "uncertain",
            "baseline filesystem snapshot is unavailable",
            details,
        )

    baseline_pkg = baseline / PARTNERS_ROOT
    baseline_temp_config = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix=".harbor-typecheck-",
        dir=baseline_pkg,
        delete=False,
    )
    try:
        baseline_config_path = Path(baseline_temp_config.name)
        baseline_temp_config.write(TYPECHECK_CONFIG_TEMPLATE)
        baseline_temp_config.flush()
        baseline_temp_config.close()
        baseline_install = _run(
            baseline,
            [
                "bash",
                "-lc",
                "cd packages/twenty-apps/internal/twenty-partners && yarn install --immutable",
            ],
            timeout=900,
        )
        baseline_details = {
            "install_exit_code": baseline_install.returncode,
            "install_stdout": baseline_install.stdout[-4000:],
            "install_stderr": baseline_install.stderr[-4000:],
        }
        if baseline_install.returncode != 0:
            details["evidence"]["baseline_install"] = baseline_details
            return _result(
                "partners_typecheck",
                True,
                "uncertain",
                "baseline dependency install failed",
                details,
            )

        baseline_process = _run(
            baseline,
            [
                "bash",
                "-lc",
                f"cd {PARTNERS_ROOT.as_posix()} && yarn tsc --pretty false -p {baseline_config_path.name}",
            ],
            timeout=600,
        )
    finally:
        try:
            baseline_temp_config.close()
        except Exception:
            pass
        Path(baseline_temp_config.name).unlink(missing_ok=True)

    baseline_diagnostics = _parse_tsc_diagnostics(
        baseline_process.stdout + "\n" + baseline_process.stderr
    )
    details["evidence"].update(
        {
            "baseline_exit_code": baseline_process.returncode,
            "baseline_diagnostic_count": len(baseline_diagnostics),
        }
    )

    def fingerprint(diagnostic: dict[str, str], *, repo_root: Path) -> tuple[str, str]:
        message = diagnostic["message"]
        for candidate in {repo_root.as_posix(), repo_root.resolve().as_posix()}:
            message = message.replace(candidate, "<repo>")
        return diagnostic["code"], message

    candidate_counts = Counter(
        fingerprint(diagnostic, repo_root=repo) for diagnostic in candidate_diagnostics
    )
    baseline_counts = Counter(
        fingerprint(diagnostic, repo_root=baseline) for diagnostic in baseline_diagnostics
    )
    new_counts = candidate_counts - baseline_counts
    new_diagnostics: list[dict[str, str]] = []
    for diagnostic in candidate_diagnostics:
        fp = fingerprint(diagnostic, repo_root=repo)
        if new_counts.get(fp, 0) > 0:
            new_diagnostics.append(diagnostic)
            new_counts[fp] -= 1
    details["evidence"].update(
        {
            "new_diagnostic_count": len(new_diagnostics),
            "new_diagnostics": new_diagnostics[:20],
        }
    )
    if new_diagnostics:
        return _result(
            "partners_typecheck",
            True,
            "fail",
            "typecheck introduced diagnostics beyond the baseline snapshot",
            details,
        )
    return _result(
        "partners_typecheck",
        True,
        "pass",
        "typecheck has no diagnostics beyond the baseline snapshot",
        details,
    )


def custom_gates() -> list[Callable[[Path], dict[str, Any]]]:
    """Return task-specific gates such as behavior oracles or API compatibility checks."""
    return [
        partners_typecheck_pass,
        partners_module_layout_pass,
        universal_identifier_preservation_pass,
        logic_function_contract_pass,
        protected_paths_pass,
    ]


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

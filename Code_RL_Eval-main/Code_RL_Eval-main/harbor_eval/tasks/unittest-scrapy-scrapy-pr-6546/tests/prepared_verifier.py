from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any


PROFILE_SCHEMA = "unittest-rl-execution-profiles-v1"
REPORT_REPETITIONS = 3
ALLOWED_CANDIDATE_SUFFIXES = {".md", ".py", ".pyi"}


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int = 180,
) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    return {
        "command": command,
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
    }


def write_report(path: Path | None, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(rendered, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def validate_fault_catalog(
    faults_dir: Path,
    fault_profiles: dict[str, Any],
    profile_ids: set[str],
) -> dict[str, Any]:
    """Fail before replay when a fault number points at stale semantics.

    Fault IDs remain the task-local display identity, but this check verifies
    every current reference against the patch and semantic class.  A global
    text replacement can therefore not silently bind a J5 rubric to another
    mutation or leave a retired fault reference in the current-only task.
    """

    assets_dir = faults_dir.parent
    raw_payload = read_object(faults_dir / "raw_faults.json")
    valid_payload = read_object(faults_dir / "valid_faults.json")
    raw_faults = raw_payload.get("faults")
    valid_faults = valid_payload.get("valid_faults")
    if not isinstance(raw_faults, list) or not isinstance(valid_faults, list):
        raise ValueError("fault catalog arrays are missing")

    raw_by_id = {item.get("fault_id"): item for item in raw_faults if isinstance(item, dict)}
    valid_by_id = {item.get("fault_id"): item for item in valid_faults if isinstance(item, dict)}
    if len(raw_by_id) != len(raw_faults) or len(valid_by_id) != len(valid_faults):
        raise ValueError("fault catalog contains duplicate or malformed fault IDs")
    expected_ids = [f"F{index:03d}" for index in range(1, len(valid_faults) + 1)]
    actual_ids = [item.get("fault_id") for item in valid_faults]
    if actual_ids != expected_ids:
        raise ValueError(
            "fault IDs must be contiguous in canonical order: "
            f"expected {expected_ids}, got {actual_ids}"
        )
    if set(raw_by_id) != set(valid_by_id):
        raise ValueError("raw and valid fault catalogs have different fault IDs")

    for fault_id, valid in valid_by_id.items():
        raw = raw_by_id[fault_id]
        for field in ("patch", "semantic_class", "behavior_ids"):
            if raw.get(field) != valid.get(field):
                raise ValueError(
                    f"fault catalog semantic mismatch for {fault_id}: {field}"
                )
        patch = valid.get("patch")
        patch_name_matches = (
            isinstance(patch, str)
            and Path(patch).name.startswith(f"{fault_id}-")
        )
        if not patch_name_matches:
            raise ValueError(
                f"fault {fault_id} patch filename does not carry the same ID: {patch!r}"
            )

    validation_path = faults_dir / "fault_validation.json"
    if validation_path.is_file():
        validation = read_object(validation_path)
        if set(validation.get("valid_fault_ids", [])) != set(valid_by_id):
            raise ValueError("fault_validation.json does not match valid fault IDs")

    if set(fault_profiles) != set(valid_by_id):
        raise ValueError("execution profile bindings do not cover exactly valid faults")
    unknown_profiles = set(fault_profiles.values()) - profile_ids
    if unknown_profiles:
        raise ValueError(
            "fault execution profile bindings reference unknown profiles: "
            + ", ".join(sorted(map(str, unknown_profiles)))
        )

    facts_path = assets_dir / "j5_facts.json"
    if facts_path.is_file():
        facts = read_object(facts_path)
        for unit in facts.get("diagnostic_units", []):
            if not isinstance(unit, dict):
                raise ValueError("J5 diagnostic unit is malformed")
            unknown = set(unit.get("fault_ids", [])) - set(valid_by_id)
            if unknown:
                raise ValueError(
                    "J5 facts reference unknown faults: "
                    + ", ".join(sorted(map(str, unknown)))
                )

    applicability_path = assets_dir / "j5_applicability_map.json"
    rubric_path = assets_dir / "rubrics" / "j5.json"
    if applicability_path.is_file() and rubric_path.is_file():
        applicability = read_object(applicability_path).get("rubrics")
        rubric_payload = json.loads(rubric_path.read_text(encoding="utf-8"))
        if not isinstance(applicability, list) or not isinstance(rubric_payload, list):
            raise ValueError("J5 rubric/applicability arrays are malformed")
        mapped = {item.get("rubric_id"): item for item in applicability if isinstance(item, dict)}
        frozen = {item.get("rubric_id"): item for item in rubric_payload if isinstance(item, dict)}
        if set(mapped) != set(frozen):
            raise ValueError("J5 applicability and rubric IDs do not match")
        for rubric_id, entry in mapped.items():
            fault_ids = entry.get("fault_ids", [])
            if not fault_ids or set(fault_ids) - set(valid_by_id):
                raise ValueError(f"J5 applicability has unknown fault for {rubric_id}")
            references = set(
                re.findall(
                    r"\bF\d{3}\b",
                    " ".join(
                        str(frozen[rubric_id].get(field, ""))
                        for field in ("score_0_condition", "score_1_condition")
                    ),
                )
            )
            if references and references != set(fault_ids):
                raise ValueError(
                    f"J5 rubric text mapping mismatch for {rubric_id}: "
                    f"mapped={fault_ids}, text={sorted(references)}"
                )

    return {
        "fault_ids": expected_ids,
        "scope_revision_id": raw_payload.get("scope_revision_id"),
    }


def load_profiles(
    profile_path: Path,
    *,
    runtime_root: Path | None,
) -> list[dict[str, Any]]:
    payload = read_object(profile_path)
    if payload.get("schema_version") != PROFILE_SCHEMA:
        raise ValueError("execution profile schema is invalid")
    profiles = payload.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("execution profiles are missing")
    prepared_root = profile_path.resolve().parents[1]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in profiles:
        if not isinstance(raw, dict):
            raise ValueError("execution profile is not an object")
        profile_id = raw.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id or profile_id in seen:
            raise ValueError("execution profile identity is invalid")
        seen.add(profile_id)
        selection = raw.get("test_selection")
        if not isinstance(selection, dict):
            raise ValueError(f"{profile_id} test selection is invalid")
        paths = selection.get("paths")
        ignores = selection.get("ignore_paths", [])
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(item, str) and item for item in paths)
            or not isinstance(ignores, list)
            or not all(isinstance(item, str) and item for item in ignores)
            or selection.get("empty_policy") not in {"candidate_fail", "allow_empty"}
        ):
            raise ValueError(f"{profile_id} test selection is invalid")
        runtime = (
            runtime_root / profile_id
            if runtime_root is not None
            else prepared_root / str(raw["runtime_relative_path"])
        )
        python_candidates = (
            runtime / "bin" / "python",
            runtime / "python3.13",
            runtime / "python",
        )
        python = next((item for item in python_candidates if item.is_file()), None)
        if python is None:
            raise FileNotFoundError(f"{profile_id} runtime Python is missing")
        result.append({**raw, "runtime": runtime, "python": python})
    return result


def legacy_profile(runtime: Path) -> list[dict[str, Any]]:
    python = runtime / "bin" / "python"
    if not python.is_file():
        python = runtime / "python3.13"
    if not python.is_file():
        python = runtime / "python"
    return [
        {
            "profile_id": "current",
            "runtime": runtime,
            "python": python,
            "test_selection": {
                "paths": ["candidate_tests"],
                "ignore_paths": [],
                "empty_policy": "candidate_fail",
            },
            "required_for_hard_gate": True,
        }
    ]


def runtime_environment(
    workspace: Path,
    profile: dict[str, Any],
) -> dict[str, str]:
    runtime = Path(profile["runtime"])
    python_path = [str(Path(__file__).resolve().parent), str(workspace / "repository")]
    site_packages = runtime / "site-packages"
    if site_packages.is_dir():
        python_path.append(str(site_packages))
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": os.pathsep.join(python_path),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }


def selection_args(profile: dict[str, Any]) -> list[str]:
    selection = profile["test_selection"]
    args = list(selection["paths"])
    for ignored in selection["ignore_paths"]:
        args.extend(["--ignore", ignored])
    return args


def selected_files(workspace: Path, profile: dict[str, Any]) -> list[Path]:
    selection = profile["test_selection"]
    ignores = [(workspace / item).resolve() for item in selection["ignore_paths"]]
    result: list[Path] = []
    for raw in selection["paths"]:
        root = workspace / raw
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = list(root.rglob("*"))
        else:
            continue
        for path in candidates:
            resolved = path.resolve()
            if path.is_file() and not any(
                ignored == resolved or ignored in resolved.parents
                for ignored in ignores
            ):
                result.append(path)
    return sorted(set(result))


def load_execution_report(path: Path) -> dict[str, Any]:
    try:
        report = read_object(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "invalid", "errors": ["execution_report_missing"]}
    return report


def pytest_execution(
    workspace: Path,
    profile: dict[str, Any],
    *,
    temporary: Path,
    label: str,
    extra_environment: dict[str, str] | None = None,
    plugin: str = "execution_reporter",
) -> dict[str, Any]:
    report_path = temporary / f"{label}.json"
    environment = runtime_environment(workspace, profile)
    environment["UNITTEST_RL_EXECUTION_REPORT"] = str(report_path)
    if extra_environment:
        environment.update(extra_environment)
    execution = run(
        [
            str(profile["python"]),
            "-m",
            "pytest",
            "-q",
            "-p",
            plugin,
            *selection_args(profile),
        ],
        cwd=workspace,
        environment=environment,
    )
    report = load_execution_report(report_path)
    return {"execution": execution, "report": report}


def profile_hard_gate(
    workspace: Path,
    profile: dict[str, Any],
    *,
    temporary: Path,
) -> dict[str, Any]:
    profile_id = profile["profile_id"]
    files = selected_files(workspace, profile)
    empty = not files
    if empty and profile["test_selection"]["empty_policy"] == "allow_empty":
        return {
            "profile_id": profile_id,
            "status": "pass",
            "selection_status": "empty",
            "test_files": [],
            "collection": None,
            "runs": [],
            "stable_passed_nodeids": [],
        }
    compile_errors: list[str] = []
    invalid_files: list[str] = []
    symlinks: list[str] = []
    for path in files:
        relative = str(path.relative_to(workspace))
        if path.is_symlink():
            symlinks.append(relative)
        if path.suffix not in ALLOWED_CANDIDATE_SUFFIXES:
            invalid_files.append(relative)
        elif path.suffix == ".py":
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except (SyntaxError, UnicodeError) as exc:
                compile_errors.append(f"{relative}: {exc}")
    environment = runtime_environment(workspace, profile)
    collection = run(
        [
            str(profile["python"]),
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            *selection_args(profile),
        ],
        cwd=workspace,
        environment=environment,
    )
    runs = [
        pytest_execution(
            workspace,
            profile,
            temporary=temporary,
            label=f"{profile_id}-baseline-{index}",
        )
        for index in range(1, REPORT_REPETITIONS + 1)
    ]
    reports_complete = all(
        item["report"].get("status") == "complete"
        and not item["execution"].get("timed_out")
        for item in runs
    )
    candidate_passed = (
        bool(files)
        and not symlinks
        and not invalid_files
        and not compile_errors
        and collection["exit_code"] == 0
        and reports_complete
        and all(item["execution"]["exit_code"] == 0 for item in runs)
    )
    infrastructure_invalid = (
        collection.get("timed_out")
        or any(item["execution"].get("timed_out") for item in runs)
        or any(
            item["report"].get("collection_errors")
            or item["report"].get("internal_errors")
            for item in runs
        )
    )
    stable_passed = (
        sorted(
            set.intersection(
                *(
                    set(item["report"].get("passed_nodeids", []))
                    for item in runs
                )
            )
        )
        if runs and reports_complete
        else []
    )
    return {
        "profile_id": profile_id,
        "status": (
            "evaluation_invalid"
            if infrastructure_invalid
            else "pass"
            if candidate_passed
            else "candidate_fail"
        ),
        "selection_status": "present" if files else "empty",
        "test_files": [str(path.relative_to(workspace)) for path in files],
        "symlinks": symlinks,
        "invalid_files": invalid_files,
        "compile_errors": compile_errors,
        "collection": collection,
        "runs": runs,
        "stable_passed_nodeids": stable_passed,
    }


def hard_gates(
    workspace: Path,
    profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="evaluation-run-") as temp:
        temporary = Path(temp)
        records = [
            profile_hard_gate(workspace, profile, temporary=temporary)
            for profile in profiles
        ]
    statuses = {item["status"] for item in records}
    status = (
        "evaluation_invalid"
        if "evaluation_invalid" in statuses
        else "candidate_fail"
        if "candidate_fail" in statuses
        else "eligible"
    )
    return {
        "schema_version": "scrapy-6546-hard-gates-v2",
        "status": status,
        "profile_count": len(records),
        "profiles": {item["profile_id"]: item for item in records},
        "network": "disabled",
        "policy": {
            "selected_test_failure": "candidate_fail",
            "empty_allow_empty_profile": "pass",
            "runtime_collection_internal_or_timeout": "evaluation_invalid",
        },
    }


def positive_profile_observations(
    workspace: Path,
    profile: dict[str, Any],
    *,
    temporary: Path,
) -> dict[str, Any]:
    profile_id = profile["profile_id"]
    if not selected_files(workspace, profile):
        empty_runs = [
            {
                "run_index": index,
                "run_id": f"positive-run-{index:03d}",
                "status": "pass",
                "passed_nodeids": [],
                "invocations": [],
                "warnings": [],
                "assertions": [],
                "collection_errors": [],
                "internal_errors": [],
            }
            for index in range(1, REPORT_REPETITIONS + 1)
        ]
        return {
            "profile_id": profile_id,
            "status": "pass",
            "selection_status": "empty",
            "replay_count": REPORT_REPETITIONS,
            "stable_passed_nodeids": [],
            "runs": empty_runs,
        }
    runs: list[dict[str, Any]] = []
    for index in range(1, REPORT_REPETITIONS + 1):
        observation_path = temporary / f"{profile_id}-observe-{index}.json"
        environment = runtime_environment(workspace, profile)
        environment["UNITTEST_RL_OBSERVATION_OUTPUT"] = str(observation_path)
        environment["UNITTEST_RL_RUN_ID"] = f"positive-run-{index:03d}"
        execution = run(
            [
                str(profile["python"]),
                "-m",
                "pytest",
                "-q",
                "-o",
                "enable_assertion_pass_hook=true",
                "-p",
                "e2_observer",
                *selection_args(profile),
            ],
            cwd=workspace,
            environment=environment,
        )
        try:
            observation = read_object(observation_path)
        except (OSError, ValueError, json.JSONDecodeError):
            observation = {"status": "invalid"}
        observation["run_index"] = index
        observation["execution"] = execution
        if execution["exit_code"] != 0 or execution["timed_out"]:
            observation["status"] = "invalid"
        runs.append(observation)
    complete = all(item.get("status") == "pass" for item in runs)
    stable_passed = (
        sorted(
            set.intersection(
                *(set(item.get("passed_nodeids", [])) for item in runs)
            )
        )
        if complete
        else []
    )
    return {
        "profile_id": profile_id,
        "status": "pass" if complete else "invalid",
        "selection_status": "present",
        "replay_count": REPORT_REPETITIONS,
        "stable_passed_nodeids": stable_passed,
        "runs": runs,
    }


def positive_behavior_observations(
    workspace: Path,
    profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="evaluation-observe-") as temp:
        temporary = Path(temp)
        records = {
            profile["profile_id"]: positive_profile_observations(
                workspace,
                profile,
                temporary=temporary,
            )
            for profile in profiles
        }
    evidence_policy = {
        "included": [
            "positive target-version executions",
            "full parameterized pytest nodeids",
            "production invocation arguments and outcomes",
            "candidate Oracle reach events without expected or actual values",
        ],
        "excluded": [
            "candidate source matching",
            "fault replay classifications",
            "all other score results",
        ],
    }
    if len(records) == 1:
        record = next(iter(records.values()))
        return {
            "schema_version": "unittest-rl-positive-behavior-observations-v2",
            **record,
            "evidence_policy": evidence_policy,
        }
    return {
        "schema_version": "unittest-rl-positive-behavior-observations-v2",
        "status": (
            "pass"
            if all(item["status"] == "pass" for item in records.values())
            else "invalid"
        ),
        "profiles": records,
        "evidence_policy": evidence_policy,
    }


def make_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR
        if path.is_dir():
            mode |= stat.S_IXUSR
        path.chmod(mode)


def failed_by_phase(report: dict[str, Any], phases: set[str]) -> set[str]:
    return {
        str(item["nodeid"])
        for item in report.get("failed_nodeids", [])
        if isinstance(item, dict)
        and isinstance(item.get("nodeid"), str)
        and item.get("phase") in phases
    }


def fault_replay(
    workspace: Path,
    profiles: list[dict[str, Any]],
    faults_dir: Path,
    logs_dir: Path,
) -> dict[str, Any]:
    fault_manifest = read_object(faults_dir / "valid_faults.json")
    bindings = read_object(
        faults_dir.parent / "execution_profile_bindings.json"
    )
    fault_profiles = bindings.get("fault_profiles")
    if not isinstance(fault_profiles, dict):
        raise ValueError("fault execution profile bindings are missing")
    by_profile = {profile["profile_id"]: profile for profile in profiles}
    catalog_identity = validate_fault_catalog(
        faults_dir,
        fault_profiles,
        set(by_profile),
    )
    records: list[dict[str, Any]] = []
    logs_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="evaluation-faults-") as temp:
        temporary_root = Path(temp)
        baseline_cache: dict[str, dict[str, Any]] = {}
        for profile_id, profile in by_profile.items():
            baseline_cache[profile_id] = profile_hard_gate(
                workspace,
                profile,
                temporary=temporary_root,
            )
        for fault in fault_manifest["valid_faults"]:
            fault_id = fault["fault_id"]
            profile_id = (
                profiles[0]["profile_id"]
                if len(profiles) == 1
                else fault_profiles.get(fault_id)
            )
            profile = by_profile.get(profile_id)
            if profile is None:
                records.append(
                    {
                        "fault_id": fault_id,
                        "classification": "invalid",
                        "reason": "profile_binding_missing",
                    }
                )
                continue
            baseline = baseline_cache[profile_id]
            stable_baseline = set(baseline.get("stable_passed_nodeids", []))
            if baseline.get("selection_status") == "empty":
                records.append(
                    {
                        "fault_id": fault_id,
                        "execution_profile_id": profile_id,
                        "classification": "no_coverage",
                        "stable_baseline_nodeids": [],
                        "runs": [],
                    }
                )
                continue
            if baseline.get("status") != "pass":
                records.append(
                    {
                        "fault_id": fault_id,
                        "execution_profile_id": profile_id,
                        "classification": "invalid",
                        "reason": "baseline_not_stably_passed",
                    }
                )
                continue
            work = Path(tempfile.mkdtemp(prefix="run-", dir=temporary_root))
            shutil.copytree(workspace / "repository", work / "repository")
            shutil.copytree(workspace / "candidate_tests", work / "candidate_tests")
            make_writable(work)
            patch_path = faults_dir / fault["patch"]
            safe_environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            check = run(
                ["git", "-C", str(work / "repository"), "apply", "--check", str(patch_path)],
                cwd=work,
                environment=safe_environment,
            )
            if check["exit_code"] != 0:
                records.append(
                    {
                        "fault_id": fault_id,
                        "execution_profile_id": profile_id,
                        "classification": "invalid",
                        "reason": "patch_check_failed",
                        "patch_check": check,
                    }
                )
                continue
            applied = run(
                ["git", "-C", str(work / "repository"), "apply", str(patch_path)],
                cwd=work,
                environment=safe_environment,
            )
            executions = [
                pytest_execution(
                    work,
                    profile,
                    temporary=temporary_root,
                    label=f"fault-run-{index}",
                )
                for index in range(1, REPORT_REPETITIONS + 1)
            ]
            for index, item in enumerate(executions, 1):
                execution = item["execution"]
                (logs_dir / f"{fault_id}-replay-{index}.log").write_text(
                    execution["stdout"] + execution["stderr"],
                    encoding="utf-8",
                )
            phases = set(fault.get("accepted_failure_phases", ["call"]))
            failed_sets = [
                failed_by_phase(item["report"], phases)
                for item in executions
            ]
            clean_reports = all(
                item["report"].get("status") == "complete"
                and not item["execution"].get("timed_out")
                for item in executions
            )
            exits = [item["execution"]["exit_code"] for item in executions]
            same_failed = bool(failed_sets) and all(
                item == failed_sets[0] for item in failed_sets
            )
            stable_failed = failed_sets[0] if same_failed else set()
            if (
                clean_reports
                and all(code == 1 for code in exits)
                and stable_failed
                and stable_failed <= stable_baseline
            ):
                classification = "stable_killed"
            elif clean_reports and all(code == 0 for code in exits):
                classification = "survived"
            else:
                classification = "invalid"
            records.append(
                {
                    "fault_id": fault_id,
                    "patch": fault["patch"],
                    "semantic_class": fault["semantic_class"],
                    "behavior_ids": fault["behavior_ids"],
                    "execution_profile_id": profile_id,
                    "classification": classification,
                    "accepted_failure_phases": sorted(phases),
                    "stable_baseline_nodeids": sorted(stable_baseline),
                    "stable_failed_nodeids": sorted(stable_failed),
                    "patch_check": check,
                    "patch_apply": applied,
                    "runs": executions,
                }
            )
    return {
        "schema_version": "scrapy-6546-fault-replay-v2",
        "status": (
            "pass"
            if all(item["classification"] != "invalid" for item in records)
            else "invalid"
        ),
        "replay_count": REPORT_REPETITIONS,
        "fault_catalog": catalog_identity,
        "faults": records,
    }


def coverage_report(
    workspace: Path,
    profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for profile in profiles:
        if not selected_files(workspace, profile):
            records[profile["profile_id"]] = {
                "status": "pass",
                "selection_status": "empty",
                "coverage": {},
            }
            continue
        temporary = workspace / ".candidate_tmp" / profile["profile_id"]
        temporary.mkdir(parents=True, exist_ok=True)
        json_path = temporary / "coverage.json"
        environment = runtime_environment(workspace, profile)
        environment["COVERAGE_FILE"] = str(temporary / ".coverage")
        execution = run(
            [
                str(profile["python"]),
                "-m",
                "coverage",
                "run",
                "--branch",
                "--source=repository/scrapy",
                "-m",
                "pytest",
                "-q",
                *selection_args(profile),
            ],
            cwd=workspace,
            environment=environment,
        )
        conversion = run(
            [
                str(profile["python"]),
                "-m",
                "coverage",
                "json",
                "-o",
                str(json_path),
            ],
            cwd=workspace,
            environment=environment,
        )
        records[profile["profile_id"]] = {
            "status": (
                "pass"
                if execution["exit_code"] == 0 and conversion["exit_code"] == 0
                else "invalid"
            ),
            "selection_status": "present",
            "execution": execution,
            "conversion": conversion,
            "coverage": read_object(json_path) if json_path.is_file() else {},
        }
    return {
        "schema_version": "scrapy-6546-coverage-v2",
        "status": (
            "pass"
            if all(item["status"] == "pass" for item in records.values())
            else "invalid"
        ),
        "profiles": records,
    }


def analyze(workspace: Path) -> dict[str, Any]:
    root = workspace / "candidate_tests"
    files = sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []
    functions: list[dict[str, Any]] = []
    for path in files:
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                functions.append(
                    {
                        "file": str(path.relative_to(workspace)),
                        "name": node.name,
                        "assert_count": sum(
                            isinstance(child, ast.Assert) for child in ast.walk(node)
                        ),
                        "line": node.lineno,
                    }
                )
    return {
        "schema_version": "scrapy-6546-candidate-analysis-v2",
        "status": "pass",
        "test_files": [str(path.relative_to(workspace)) for path in files],
        "test_functions": functions,
        "test_count": len(functions),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument(
        "mode",
        choices=("hard-gates", "coverage", "positive-observe", "fault-replay", "analyze"),
    )
    root.add_argument("--workspace", type=Path, required=True)
    root.add_argument("--runtime", type=Path)
    root.add_argument("--runtime-root", type=Path)
    root.add_argument("--execution-profiles", type=Path)
    root.add_argument("--faults-dir", type=Path)
    root.add_argument("--logs-dir", type=Path)
    root.add_argument("--output", type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    workspace = args.workspace.resolve()
    if args.execution_profiles is not None:
        profiles = load_profiles(
            args.execution_profiles.resolve(),
            runtime_root=(
                args.runtime_root.resolve() if args.runtime_root else None
            ),
        )
    elif args.runtime is not None:
        profiles = legacy_profile(args.runtime.resolve())
    elif args.mode != "analyze":
        raise SystemExit("runtime configuration is required")
    else:
        profiles = []
    if args.mode == "hard-gates":
        report = hard_gates(workspace, profiles)
    elif args.mode == "coverage":
        report = coverage_report(workspace, profiles)
    elif args.mode == "positive-observe":
        report = positive_behavior_observations(workspace, profiles)
    elif args.mode == "fault-replay":
        if args.faults_dir is None or args.logs_dir is None:
            raise SystemExit("--faults-dir and --logs-dir are required")
        report = fault_replay(
            workspace,
            profiles,
            args.faults_dir.resolve(),
            args.logs_dir.resolve(),
        )
    else:
        report = analyze(workspace)
    write_report(args.output, report)
    return 0 if report["status"] in {"pass", "eligible"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

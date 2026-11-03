from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


TASK = Path(__file__).parents[1] / "tasks" / (
    "scrapy-scrapy-pr-6546-core-bd634698-v0103-prepared-v004-"
    "rubrics-r004-judge3"
)
TESTS = TASK / "tests"


def _judge_module():
    sys.path.insert(0, str(TESTS))
    spec = importlib.util.spec_from_file_location(
        "scrapy_v0103_judge", TESTS / "judge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _judge_runtime_module():
    spec = importlib.util.spec_from_file_location(
        "scrapy_v0103_judge_runtime", TESTS / "judge_runtime.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trace_bridge_module():
    spec = importlib.util.spec_from_file_location(
        "scrapy_v0103_trace_bridge", TESTS / "trace_bridge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _observations() -> dict:
    nodeid = "candidate_tests/test_tls.py::test_tls_range"
    execution_id = f"positive-run-001:{nodeid}"
    return {
        "status": "pass",
        "replay_count": 1,
        "stable_passed_nodeids": [nodeid],
        "runs": [
            {
                "run_index": 1,
                "run_id": "positive-run-001",
                "status": "pass",
                "passed_nodeids": [nodeid],
                "invocations": [
                    {
                        "call_id": "call-000001",
                        "parent_call_id": None,
                        "test_execution_id": execution_id,
                        "nodeid": nodeid,
                        "phase": "call",
                        "target": "scrapy.utils.ssl._make_ssl_context",
                        "arguments": {"min_version": "TLSv1.2"},
                        "outcome": "return",
                        "observable": {"maximum_version": "TLSv1.3"},
                        "event_kind": "python_call",
                        "implementation_origin": "target_repository",
                        "implementation_path": "/app/repository/scrapy/utils/ssl.py",
                        "implementation_firstlineno": 100,
                        "implementation_sha256": "a" * 64,
                        "production_call_observed": True,
                    }
                ],
                "warnings": [
                    {
                        "warning_id": "warning-000001",
                        "test_execution_id": execution_id,
                        "nodeid": nodeid,
                        "phase": "call",
                        "category": "ScrapyDeprecationWarning",
                        "message": "sensitive detail",
                        "active_call_ids": ["call-000001"],
                    }
                ],
                "assertions": [
                    {
                        "assertion_id": "assertion-000001",
                        "test_execution_id": execution_id,
                        "nodeid": nodeid,
                        "kind": "assert",
                        "source_path": "/app/candidate_tests/test_tls.py",
                        "source_line": 12,
                        "reached": True,
                        "passed": True,
                    }
                ],
                "stdout": "not for J1",
                "stderr": "not for J1",
            }
        ],
    }


def test_j1_projection_is_embedded_minimum_runtime_evidence() -> None:
    judge = _judge_module()
    projected = judge.project_j1_execution_evidence(_observations())

    assert projected["status"] == "pass"
    assert projected["source"] == "projected positive-behavior-observations.json"
    run = projected["runs"][0]
    assert run["calls"][0]["production_call_observed"] is True
    assert run["assertions"][0]["reached"] is True
    assert run["warnings"][0]["warning_observed"] is True
    assert "observable" not in run["calls"][0]
    assert "category" not in run["warnings"][0]
    assert "message" not in run["warnings"][0]
    assert "stdout" not in run and "stderr" not in run


def test_j1_projection_rejects_mismatched_test_execution_id() -> None:
    judge = _judge_module()
    observations = _observations()
    observations["runs"][0]["invocations"][0]["test_execution_id"] = "wrong"

    with pytest.raises(judge.JudgeError, match="invocation fields"):
        judge.project_j1_execution_evidence(observations)


def test_dimension_input_isolation_keeps_full_candidate_source() -> None:
    judge = _judge_module()
    candidate_files = [
        {"path": "candidate_tests/test_tls.py", "content": "def test_tls():\n    assert True\n"}
    ]
    common = {
        "candidate_tree_sha256": "b" * 64,
        "candidate_files": candidate_files,
    }
    snapshots = {
        "SC-J1": {**common, "j1_execution_evidence": {}},
        "SC-J2": {**common, "positive_execution_evidence": {}},
        "SC-J3": {**common, "positive_execution_evidence": {}},
        "SC-J4": {**common, "positive_execution_evidence": {}},
        "SC-J5": {
            **common,
            "hard_evidence": {},
            "j5_rubric_evidence_scope": [],
        },
    }

    result = judge.validate_dimension_snapshots(snapshots)

    assert result["status"] == "pass"
    assert all(
        snapshot["candidate_files"] == candidate_files
        for snapshot in snapshots.values()
    )


def test_dimension_input_isolation_rejects_cross_dimension_evidence() -> None:
    judge = _judge_module()
    candidate_files = [{"path": "candidate_tests/test_tls.py", "content": "x = 1\n"}]
    common = {"candidate_tree_sha256": "c" * 64, "candidate_files": candidate_files}
    snapshots = {
        "SC-J1": {
            **common,
            "j1_execution_evidence": {},
            "hard_evidence": {},
        },
        "SC-J2": {**common, "positive_execution_evidence": {}},
        "SC-J3": {**common, "positive_execution_evidence": {}},
        "SC-J4": {**common, "positive_execution_evidence": {}},
        "SC-J5": {
            **common,
            "hard_evidence": {},
            "j5_rubric_evidence_scope": [],
        },
    }

    with pytest.raises(judge.JudgeError, match="evidence isolation failed"):
        judge.validate_dimension_snapshots(snapshots)


def test_runtime_config_preserves_frozen_credential_environment() -> None:
    runtime = _judge_runtime_module()
    frozen = json.loads((TESTS / "judge-config.json").read_text(encoding="utf-8"))

    resolved = runtime.resolve_judge_runtime_config(frozen, environ={})

    assert resolved["credential_env"] == "UNITTEST_RL_MINDFLOW_API_KEY"
    assert (
        resolved["runtime_receipt"]["credential_env"]
        == "UNITTEST_RL_MINDFLOW_API_KEY"
    )


def test_trace_bridge_ignores_quoted_comparisons_and_dev_null() -> None:
    trace = _trace_bridge_module()

    assert list(trace._iter_redirection_targets("awk 'length($0) > 88'")) == []
    accesses, violations, network = trace._scan_command(
        "rg TLS 2>/dev/null",
        workdir="/app",
        tool_call_id="call-1",
    )
    assert accesses == []
    assert violations == []
    assert network is False


def test_trace_bridge_keeps_real_out_of_scope_redirection_blocked() -> None:
    trace = _trace_bridge_module()

    _, violations, _ = trace._scan_command(
        "echo candidate >/app/88",
        workdir="/app",
        tool_call_id="call-2",
    )
    assert violations == ["call-2:write_outside_candidate_roots:/app/88"]


def test_observer_records_assert_and_context_oracle_reach(tmp_path: Path) -> None:
    candidate_root = tmp_path / "candidate_tests"
    candidate_root.mkdir()
    (candidate_root / "test_oracles.py").write_text(
        "import pytest\n\n"
        "def test_oracles():\n"
        "    assert 2 + 2 == 4\n"
        "    with pytest.raises(ValueError):\n"
        "        raise ValueError('expected')\n",
        encoding="utf-8",
    )
    output = tmp_path / "observations.json"
    environment = {
        **os.environ,
        "PYTHONPATH": str(TESTS),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "UNITTEST_RL_OBSERVATION_OUTPUT": str(output),
        "UNITTEST_RL_RUN_ID": "positive-run-001",
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "enable_assertion_pass_hook=true",
            "-p",
            "e2_observer",
            "candidate_tests",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    kinds = {item["kind"] for item in payload["assertions"]}
    assert kinds == {"assert", "pytest.raises"}
    assert all(item["reached"] is True for item in payload["assertions"])
    assert next(
        item for item in payload["assertions"] if item["kind"] == "assert"
    )["passed"] is True

"""Pytest plugin that emits full-nodeid baseline and mutant outcomes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


_reports: dict[str, dict[str, str]] = {}
_collection_errors: list[str] = []
_internal_errors: list[str] = []
_collected_nodeids: list[str] = []


def pytest_collection_finish(session: pytest.Session) -> None:
    _collected_nodeids.extend(item.nodeid for item in session.items)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _reports.setdefault(report.nodeid, {})[report.when] = report.outcome


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _collection_errors.append(str(report.longrepr))


def pytest_internalerror(excrepr: Any, excinfo: Any) -> None:
    _internal_errors.append(str(excrepr))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    output_value = os.environ.get("UNITTEST_RL_EXECUTION_REPORT")
    if not output_value:
        return
    failed: list[dict[str, str]] = []
    passed: list[str] = []
    skipped: list[dict[str, str]] = []
    for nodeid, phases in sorted(_reports.items()):
        failed_phases = [
            phase for phase, outcome in phases.items() if outcome == "failed"
        ]
        skipped_phases = [
            phase for phase, outcome in phases.items() if outcome == "skipped"
        ]
        if failed_phases:
            failed.extend(
                {"nodeid": nodeid, "phase": phase}
                for phase in sorted(failed_phases)
            )
        elif phases.get("call") == "passed":
            passed.append(nodeid)
        elif skipped_phases:
            skipped.extend(
                {"nodeid": nodeid, "phase": phase}
                for phase in sorted(skipped_phases)
            )
    complete = not _collection_errors and not _internal_errors
    payload = {
        "schema_version": "unittest-rl-pytest-execution-report-v1",
        "status": "complete" if complete else "invalid",
        "exit_status": int(exitstatus),
        "collected_nodeids": sorted(set(_collected_nodeids)),
        "passed_nodeids": passed,
        "failed_nodeids": failed,
        "skipped_nodeids": skipped,
        "collection_errors": _collection_errors,
        "internal_errors": _internal_errors,
    }
    output = Path(output_value)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

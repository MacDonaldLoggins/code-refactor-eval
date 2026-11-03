"""Pytest plugin that records positive production-behavior execution events."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest


TARGETS = {
    "scrapy.utils.ssl._make_ssl_context",
    "scrapy.utils.ssl._get_tls_version_limit",
    "scrapy.utils.ssl._get_tls_version_limits",
    "scrapy.utils.ssl._get_cert_options_version_kwargs",
    "scrapy.core.downloader.contextfactory._load_context_factory_from_settings",
    "scrapy.core.downloader.contextfactory.from_crawler",
    "scrapy.core.downloader.tls.__getattr__",
    "constantly._constants.iterconstants",
}
SETTING_NAMES = (
    "DOWNLOAD_TLS_MIN_VERSION",
    "DOWNLOAD_TLS_MAX_VERSION",
    "DOWNLOADER_CLIENT_TLS_METHOD",
    "DOWNLOADER_CLIENTCONTEXTFACTORY",
)

_current_nodeid: str | None = None
_current_phase = "protocol"
_call_sequence = 0
_warning_sequence = 0
_assertion_sequence = 0
_frame_calls: dict[int, str] = {}
_mapping_observed_frames: set[int] = set()
_active_calls: list[str] = []
_invocations: dict[str, dict[str, Any]] = {}
_warnings: list[dict[str, Any]] = []
_assertions: list[dict[str, Any]] = []
_oracle_locations: dict[tuple[str, int], str] = {}
_reached_oracles: set[tuple[str, str, int]] = set()
_file_hashes: dict[str, str | None] = {}
_outcomes: dict[str, dict[str, str]] = {}
_collection_errors: list[str] = []
_internal_errors: list[str] = []
_original_showwarning = warnings.showwarning


def _test_execution_id(nodeid: str) -> str:
    run_id = os.environ.get("UNITTEST_RL_RUN_ID", "positive-run")
    return f"{run_id}:{nodeid}"


def _resolved_filename(frame: Any) -> str:
    try:
        return str(Path(frame.f_code.co_filename).resolve())
    except OSError:
        return str(frame.f_code.co_filename)


def _file_sha256(filename: str) -> str | None:
    if filename in _file_hashes:
        return _file_hashes[filename]
    try:
        digest = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    except OSError:
        digest = None
    _file_hashes[filename] = digest
    return digest


def _implementation(frame: Any, *, synthetic: bool = False) -> dict[str, Any]:
    filename = _resolved_filename(frame)
    normalized = filename.replace("\\", "/")
    if "/repository/" in normalized:
        origin = "target_repository"
    elif "/site-packages/" in normalized or "/dist-packages/" in normalized:
        origin = "frozen_dependency"
    else:
        origin = "untrusted"
    return {
        "event_kind": (
            "synthetic_production_line_event" if synthetic else "python_call"
        ),
        "implementation_path": filename,
        "implementation_firstlineno": int(frame.f_code.co_firstlineno),
        "implementation_sha256": _file_sha256(filename),
        "implementation_origin": origin,
        "production_call_observed": not synthetic
        and origin in {"target_repository", "frozen_dependency"},
    }


def _oracle_kind(node: ast.AST) -> str | None:
    if isinstance(node, ast.Assert):
        return "assert"
    if not isinstance(node, (ast.With, ast.AsyncWith)):
        return None
    for item in node.items:
        expression = item.context_expr
        if not isinstance(expression, ast.Call):
            continue
        function = expression.func
        name = (
            function.attr
            if isinstance(function, ast.Attribute)
            else function.id if isinstance(function, ast.Name) else None
        )
        if name in {"raises", "warns", "deprecated_call"}:
            return f"pytest.{name}"
    return None


def _load_oracle_locations() -> None:
    root = Path.cwd() / "candidate_tests"
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        resolved = str(path.resolve())
        for node in ast.walk(tree):
            kind = _oracle_kind(node)
            lineno = getattr(node, "lineno", None)
            if kind is not None and isinstance(lineno, int):
                _oracle_locations[(resolved, lineno)] = kind


def _record_oracle_reached(frame: Any) -> None:
    global _assertion_sequence
    if _current_nodeid is None:
        return
    filename = _resolved_filename(frame)
    lineno = int(frame.f_lineno)
    kind = _oracle_locations.get((filename, lineno))
    if kind is None:
        return
    key = (_current_nodeid, filename, lineno)
    if key in _reached_oracles:
        return
    _reached_oracles.add(key)
    _assertion_sequence += 1
    _assertions.append(
        {
            "assertion_id": f"assertion-{_assertion_sequence:06d}",
            "test_execution_id": _test_execution_id(_current_nodeid),
            "nodeid": _current_nodeid,
            "phase": _current_phase,
            "kind": kind,
            "source_path": filename,
            "source_line": lineno,
            "reached": True,
            "passed": None,
        }
    )


def _qualified_type(value: Any) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return {"type": _qualified_type(value), "name": name}
    if isinstance(value, dict):
        return {
            str(key): _value(item)
            for key, item in value.items()
            if isinstance(key, (str, int, float, bool))
        }
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return {"type": _qualified_type(value)}


def _settings(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in SETTING_NAMES:
        try:
            result[name] = _value(value[name])
        except Exception:
            result[name] = None
    return result


def _arguments(frame: Any) -> dict[str, Any]:
    local = frame.f_locals
    result: dict[str, Any] = {}
    if "settings" in local:
        result["settings"] = _settings(local["settings"])
    if "crawler" in local:
        result["settings"] = _settings(local["crawler"].settings)
    for name in (
        "setting_name",
        "min_version",
        "max_version",
        "name",
        "tls_min_version",
        "tls_max_version",
    ):
        if name in local:
            result[name] = _value(local[name])
    if "cls" in local:
        result["class"] = _qualified_type(local["cls"])
    result["global_state"] = {
        "TWISTED_TLS_LIMITS_OFFBY1": _value(
            frame.f_globals.get("TWISTED_TLS_LIMITS_OFFBY1")
        )
    }
    return result


def _observable(target: str, value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"value": _value(value)}
    if target == "scrapy.utils.ssl._make_ssl_context" and value is not None:
        for name in ("protocol", "minimum_version", "maximum_version"):
            try:
                result[name] = _value(getattr(value, name))
            except Exception:
                pass
    elif target.endswith("._load_context_factory_from_settings") and value is not None:
        for name in ("_ssl_method", "tls_min_version", "tls_max_version"):
            if hasattr(value, name):
                result[name] = _value(getattr(value, name))
    elif target == "scrapy.utils.ssl._get_cert_options_version_kwargs":
        if isinstance(value, dict):
            result["mapping"] = _value(value)
    return result


def _trace(frame: Any, event: str, arg: Any) -> Any:
    global _call_sequence
    target = f"{frame.f_globals.get('__name__', '')}.{frame.f_code.co_name}"
    frame_id = id(frame)
    if event == "line" and _current_nodeid is not None:
        _record_oracle_reached(frame)
    if (
        event == "line"
        and target
        == "scrapy.core.downloader.contextfactory._load_context_factory_from_settings"
        and _current_nodeid is not None
        and frame_id not in _mapping_observed_frames
        and "tls_method_setting" in frame.f_locals
        and "tls_method" in frame.f_locals
    ):
        _call_sequence += 1
        synthetic_call_id = f"call-{_call_sequence:06d}"
        _mapping_observed_frames.add(frame_id)
        _invocations[synthetic_call_id] = {
            "call_id": synthetic_call_id,
            "parent_call_id": _frame_calls.get(frame_id),
            "test_execution_id": _test_execution_id(_current_nodeid),
            "nodeid": _current_nodeid,
            "phase": _current_phase,
            "target": "scrapy.core.downloader.tls._openssl_methods.__getitem__",
            "arguments": {
                "tls_method_setting": _value(
                    frame.f_locals["tls_method_setting"]
                )
            },
            "outcome": "return",
            "observable": {
                "mapped_method": _value(frame.f_locals["tls_method"])
            },
            **_implementation(frame, synthetic=True),
        }
    if event == "call" and target in TARGETS and _current_nodeid is not None:
        _call_sequence += 1
        call_id = f"call-{_call_sequence:06d}"
        parent_call_id = _active_calls[-1] if _active_calls else None
        _frame_calls[frame_id] = call_id
        _active_calls.append(call_id)
        _invocations[call_id] = {
            "call_id": call_id,
            "parent_call_id": parent_call_id,
            "test_execution_id": _test_execution_id(_current_nodeid),
            "nodeid": _current_nodeid,
            "phase": _current_phase,
            "target": target,
            "arguments": _arguments(frame),
            "outcome": None,
            "observable": {},
            **_implementation(frame),
        }
        return _trace
    call_id = _frame_calls.get(frame_id)
    if call_id is None:
        return _trace
    if event == "exception":
        exception_type, exception, _ = arg
        _invocations[call_id]["outcome"] = "exception"
        _invocations[call_id]["observable"] = {
            "exception": {
                "type": f"{exception_type.__module__}.{exception_type.__qualname__}",
                "message": str(exception),
            }
        }
    elif event == "return":
        invocation = _invocations[call_id]
        if invocation["outcome"] != "exception":
            invocation["outcome"] = "return"
            invocation["observable"] = _observable(invocation["target"], arg)
        _frame_calls.pop(frame_id, None)
        _mapping_observed_frames.discard(frame_id)
        if call_id in _active_calls:
            _active_calls.remove(call_id)
    return _trace


def _showwarning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: Any = None,
    line: str | None = None,
) -> None:
    global _warning_sequence
    if _current_nodeid is not None:
        _warning_sequence += 1
        _warnings.append(
            {
                "warning_id": f"warning-{_warning_sequence:06d}",
                "test_execution_id": _test_execution_id(_current_nodeid),
                "nodeid": _current_nodeid,
                "phase": _current_phase,
                "category": f"{category.__module__}.{category.__qualname__}",
                "message": str(message),
                "active_call_ids": list(_active_calls),
            }
        )
    _original_showwarning(message, category, filename, lineno, file, line)


def _profile(frame: Any, event: str, arg: Any) -> Any:
    global _warning_sequence
    if (
        event == "c_call"
        and _current_nodeid is not None
        and getattr(arg, "__name__", None) == "warn"
        and getattr(arg, "__module__", None) == "_warnings"
    ):
        _warning_sequence += 1
        _warnings.append(
            {
                "warning_id": f"warning-{_warning_sequence:06d}",
                "test_execution_id": _test_execution_id(_current_nodeid),
                "nodeid": _current_nodeid,
                "phase": _current_phase,
                "category": None,
                "message": None,
                "active_call_ids": list(_active_calls),
                "observation": "warnings.warn invocation",
            }
        )
    return _profile


def pytest_sessionstart(session: pytest.Session) -> None:
    _load_oracle_locations()
    warnings.showwarning = _showwarning
    sys.settrace(_trace)
    sys.setprofile(_profile)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    global _current_nodeid, _current_phase
    _current_nodeid = item.nodeid
    _current_phase = "protocol"
    yield
    _current_phase = "protocol"
    _current_nodeid = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    global _current_phase
    _current_phase = "setup"
    yield
    _current_phase = "protocol"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    global _current_phase
    _current_phase = "call"
    yield
    _current_phase = "protocol"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    global _current_phase
    _current_phase = "teardown"
    yield
    _current_phase = "protocol"


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" or report.failed:
        _outcomes.setdefault(report.nodeid, {})[report.when] = report.outcome


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _collection_errors.append(str(report.longrepr))


def pytest_internalerror(excrepr: Any, excinfo: Any) -> None:
    _internal_errors.append(str(excrepr))


def pytest_assertion_pass(
    item: pytest.Item,
    lineno: int,
    orig: str,
    expl: str,
) -> None:
    """Mark a reached Python assert as passed without retaining its values."""

    for assertion in reversed(_assertions):
        if (
            assertion["nodeid"] == item.nodeid
            and assertion["kind"] == "assert"
            and assertion["source_line"] == lineno
        ):
            assertion["passed"] = True
            return


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    sys.settrace(None)
    sys.setprofile(None)
    warnings.showwarning = _original_showwarning
    path_value = os.environ.get("UNITTEST_RL_OBSERVATION_OUTPUT")
    if not path_value:
        return
    passed_nodeids = sorted(
        nodeid
        for nodeid, phases in _outcomes.items()
        if phases.get("call") == "passed"
        and all(outcome != "failed" for outcome in phases.values())
    )
    payload = {
        "schema_version": "unittest-rl-positive-behavior-observation-run-v2",
        "run_id": os.environ.get("UNITTEST_RL_RUN_ID", "positive-run"),
        "status": (
            "pass"
            if exitstatus == 0 and not _collection_errors and not _internal_errors
            else "invalid"
        ),
        "exit_status": int(exitstatus),
        "passed_nodeids": passed_nodeids,
        "invocations": sorted(
            (
                item
                for item in _invocations.values()
                if item.get("outcome") in {"return", "exception"}
            ),
            key=lambda item: item["call_id"],
        ),
        "warnings": list(_warnings),
        "assertions": list(_assertions),
        "collection_errors": list(_collection_errors),
        "internal_errors": list(_internal_errors),
    }
    output = Path(path_value)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

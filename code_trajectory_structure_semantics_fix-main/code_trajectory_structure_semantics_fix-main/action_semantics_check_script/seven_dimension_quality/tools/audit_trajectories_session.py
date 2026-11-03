#!/usr/bin/env python3
"""轨迹问题审计——单文件，仅使用标准库。

备份轨迹评分 jsonl 中引用的 TASK 目录，并生成问题报告，
供后续fix_issues步骤使用。

运行命令：

    python3 audit_trajectories.py                       # 使用全部默认配置
    python3 audit_trajectories.py --jsonl /path/x.jsonl --store-sessions-dir /path/data
    python3 audit_trajectories.py --mode clone --limit 5

处理流程（按顺序）：拆分 jsonl -> 构建 problem<->task 映射 -> 将每个
task 目录复制到目标目录 -> 写入每个 task 的 fix_issues/task_records.json ->
写入一个汇总的 summary_report.json + 一个 Markdown 格式的 error_report.md。

================================ 参数 =================================
所有路径均可自定义；每个参数都有合理的默认值。

  --jsonl PATH        评分用的 .jsonl 文件，或包含多个 *.jsonl 的目录用于合并。
                      默认值：./batch_store_sessions/client_json
  --store-sessions-dir DIR      存放 TASK 源目录的根目录（batch_store_sessions/<batch>/<task>/）。
                      默认值：./data
  --target-dir DIR    备份文件和每个 task 记录的输出目录。
                      默认值：./target-issues-tasks
  --summary-report P  单个汇总 JSON 报告的路径。
                      默认值：<项目根目录>/report/audit_report/summary_report.json
  --error-report P    Markdown 错误计数表的路径。
                      默认值：<项目根目录>/report/audit_report/error_report.md
  --error-report-json P  错误计数报告的机器可读 JSON（供脚本读取）路径。
                      默认值：<项目根目录>/report/audit_report/error_report.json
  --db P              审核数据的 SQLite 数据库路径（逐错误明细 + 维度/任务汇总，
                      可用 SQL 自由排序）。
                      默认值：<项目根目录>/report/audit_report/audit.db
  --mode MODE         备份方式：copy | clone | hardlink | symlink | manifest。
                      默认值：copy
  --limit N           只处理前 N 个不同的 task（用于测试）。
                      默认值：无（处理所有 task）
  --top N             汇总报告中 “top” 列表的长度。默认值：50
  --no-copy           跳过备份阶段（仅执行分析并生成报告）。
  --overwrite         即使目标目录已存在，也重新复制 task。
  --no-per-task       不写入每个 task 的 fix_issues/task_records.json。
  --no-db             不写入 SQLite 数据库。
  --quiet             禁用进度日志输出。
  --version           打印版本信息并退出。

================================ 默认值 / 行为 ======================
* --jsonl 接受一个文件或一个目录；如果是目录，则合并其中所有 *.jsonl 文件
  （按名称排序），并按 task_id 去重/分组。

* 写入轨迹流程：拆分 jsonl -> 构建 problem<->task 映射 -> 复制 task 目录 ->
  写入每个 task 的 task_records.json -> 写入 summary_report.json、
  error_report.md 与 error_report.json。其中 error_report.md / .json 互为孪生
  （同一排序与轨迹错误分布），.json 供下游脚本直接读取。

* 备份会去重（每个 task 只复制一次），且支持断点续跑：如果目标目录已存在，
  则跳过，除非指定 --overwrite。mode=copy 表示完整复制；clone 表示 APFS COW；
  manifest 仅记录清单（不复制任何内容）。

* 如果某个 task 在 --store-sessions-dir 下没有对应的源目录，仍会被分析并写入报告
  （标记为 missing）；其 task_records.json 会写入
  <target>/_fix_issues/<task_id>/fix_issues/，且 session_path 为 null。

* task_records.json：仅为每个 FAILED 错误写入一行；session_path 从目标目录开始
  （<target-name>/<task_id>/sessions/...），并通过 session UUID 解析得到。

* 输出：<summary-report> 和 <error-report> 默认写入自动检测到的项目根目录下的
  report/audit_report/ 目录（项目根目录为最近的包含 .git/pyproject/… 标记的祖先目录；
  如果脚本位于 script/bin/… 中，则使用脚本的父目录作为兜底），另外还会写入
  <target>/<task_id>/fix_issues/task_records.json。

"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, TextIO

try:
    import sqlite3
except ImportError:  # pragma: no cover - depends on local Python build
    sqlite3 = None

__version__ = "2.1.0"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Markers that identify a project root when walking up from the script.
_ROOT_MARKERS = (
    ".git",
    ".hg",
    ".svn",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "go.mod",
    "Cargo.toml",
    "package.json",
    "requirements.txt",
    ".project-root",
)
# Conventional subdirs a script may live in; their parent is the project root.
_SCRIPT_SUBDIRS = {"script", "scripts", "bin", "tools", "src"}


def find_project_root(start: str = SCRIPT_DIR) -> str:
    """Auto-detect the project root by walking up from ``start``.

    Returns the nearest ancestor containing a known project marker (``.git``,
    ``pyproject.toml`` …). If none is found but the script sits in a conventional
    subdir (``script``/``bin`` …), returns that subdir's parent. Otherwise falls
    back to ``start`` itself.
    """
    d = start
    while True:
        if any(os.path.exists(os.path.join(d, m)) for m in _ROOT_MARKERS):
            return d
        parent = os.path.dirname(d)
        if parent == d:  # reached filesystem root
            break
        d = parent
    if os.path.basename(start).lower() in _SCRIPT_SUBDIRS:
        return os.path.dirname(start)
    return start


PROJECT_ROOT = find_project_root()

# 汇总类报告（summary_report / error_report）的默认输出目录：<项目根目录>/report/audit_report
AUDIT_REPORT_DIR = os.path.join(PROJECT_ROOT, "report", "audit_report")

# --------------------------------------------------------------------------- #
# Problem model
# --------------------------------------------------------------------------- #

# The seven scored grade dimensions. Anything else in ``grade`` is scalar metadata.
GRADE_DIMENSIONS = (
    "meaningful_trajectory",
    "task_completion",
    "behavioral_efficiency",
    "tool_content_quality",
    "hacky_behavior_free",
    "hallucination_free",
    "logical_and_coherent_actions",
)

NO_VIOLATION_PLACEHOLDER = ":no_violation_listed"

# Sessions live under <task_dir>/sessions/. The session_id's middle token
# (develop/bugfix/test) is unreliable, but the UUID is unique within a task, so
# we join on it. Subagent ids look like "<base>/subagents/agent-XXXX".
SESSIONS_SUBDIR = "sessions"
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def index_sessions(task_dir: str | None) -> dict[str, str]:
    """Map session id -> on-disk session stem (folder/file name without ``.json``).

    Supports both the legacy ``<task>/sessions/<stem>.json`` layout and the flat
    ``<task>/<task>.json`` layout.
    """
    idx: dict[str, str] = {}
    if not task_dir:
        return idx
    task_name = os.path.basename(task_dir)
    flat = os.path.join(task_dir, task_name + ".json")
    if os.path.isfile(flat):
        idx[task_name] = task_name
        return idx
    sdir = os.path.join(task_dir, SESSIONS_SUBDIR)
    if not os.path.isdir(sdir):
        return idx
    for name in os.listdir(sdir):
        stem = name[:-5] if name.endswith(".json") else name
        m = _UUID_RE.search(stem)
        if m:
            idx.setdefault(m.group(), stem)
    return idx


def resolve_session_path(
    task_dir: str | None, session_id: str, session_index: dict[str, str]
) -> str | None:
    """Resolve a session_id to a path relative to the task directory.

    Returns e.g. ``sessions/1-develop-<uuid>.json`` or, for a subagent,
    ``sessions/<base>/subagents/agent-XXXX.json``.  For a flat workspace returns
    ``<task>.json``. ``None`` if the task has no source dir or the session can't
    be located on disk.
    """
    if not task_dir:
        return None
    task_name = os.path.basename(task_dir)
    flat = os.path.join(task_dir, task_name + ".json")
    if os.path.isfile(flat):
        return task_name + ".json"
    base, _, suffix = session_id.partition("/")
    m = _UUID_RE.search(base)
    if not m:
        return None
    stem = session_index.get(m.group())
    if stem is None:
        return None
    rel_base = os.path.join(SESSIONS_SUBDIR, stem)
    if suffix:
        candidates = [os.path.join(rel_base, suffix) + ".json", os.path.join(rel_base, suffix)]
    else:
        candidates = [rel_base + ".json", rel_base]
    for cand in candidates:
        if os.path.exists(os.path.join(task_dir, cand)):
            return cand
    return None


_TRAJ_NUM_RE = re.compile(r"^(\d+)")


def trajectory_filename(session_id: str) -> str:
    """Filename of the trajectory file a session belongs to.

    A trajectory file is named after its top-level session id, e.g.
    ``1-develop-<uuid>.json``. Subagent sessions
    (``<base>/subagents/agent-XXXX``) belong to — and roll up to — their parent
    trajectory file ``<base>.json``. Ids without a numbered parent (e.g.
    ``session`` or ``subagent/agent-XXXX``) use their own stem.
    """
    if not session_id:
        return "?"
    base = session_id.split("/subagents/", 1)[0]
    return base + ".json"


def _trajectory_sort_key(item: tuple[str, int]) -> tuple[int, int, int, str]:
    """Sort key for ``(trajectory_file, error_count)``: count desc, then number asc."""
    name, cnt = item
    m = _TRAJ_NUM_RE.match(name)
    # numbered trajectories first (ascending by number), then lexical fallback.
    return (-cnt, 0, int(m.group(1)), name) if m else (-cnt, 1, 0, name)


def violation_type(dimension: str, violation: str) -> str:
    """Namespace a violation under its dimension for a stable, unique key."""
    return f"{dimension}/{violation}"


def dimension_of(vtype: str) -> str:
    """Inverse of :func:`violation_type` — recover the dimension name."""
    return vtype.split("/", 1)[0]


@dataclass
class ErrorRow:
    """One failed error within a session: its type, the grader's reason, and turns.

    ``problem_turns`` is the offending dimension's turn ranges (e.g.
    ``["4-31", "37-95"]``); ``[]`` when the dimension lists none.
    """

    error_type: str  # namespaced, e.g. "behavioral_efficiency/repeated_file_views"
    reason: str
    problem_turns: list[str] = field(default_factory=list)


@dataclass
class SessionProblems:
    """Problems found in a single record (one session of one task)."""

    session_id: str
    overall_pass: bool | None
    task_purpose: str | None
    difficulty_level: str | None
    failed_dimensions: list[str] = field(default_factory=list)
    errors: list[ErrorRow] = field(default_factory=list)

    @property
    def violation_types(self) -> list[str]:
        return [e.error_type for e in self.errors]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "overall_pass": self.overall_pass,
            "task_purpose": self.task_purpose,
            "difficulty_level": self.difficulty_level,
            "failed_dimensions": self.failed_dimensions,
            "violations": self.violation_types,
        }


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_str_list(value: Any) -> list[str]:
    """Coerce a value into a list of strings (tolerant of odd/missing shapes)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [v if isinstance(v, str) else str(v) for v in value]
    return [value if isinstance(value, str) else str(value)]


# Different graders emit the scored object under either ``grade`` (singular) or
# ``grades`` (plural). Read whichever one is present.
GRADE_KEYS = ("grade", "grades")


def grade_object(rec: dict[str, Any]) -> Any:
    """Return a record's grade object, tolerating ``grade`` or ``grades``.

    Prefers whichever key holds a dict; if neither does, returns the first
    present key's raw value (so odd/missing shapes fall through to the tolerant
    handling in :func:`extract_session_problems`).
    """
    for key in GRADE_KEYS:
        val = rec.get(key)
        if isinstance(val, dict):
            return val
    for key in GRADE_KEYS:
        if key in rec:
            return rec.get(key)
    return None


def extract_session_problems(session_id: str, grade: Any) -> SessionProblems:
    """Extract problems from one record's ``grade`` object.

    A problem exists for every dimension with ``pass == false``; each string in
    its ``violations`` becomes an :class:`ErrorRow` carrying the dimension's
    ``reason``. A failed dimension with no violations yields a placeholder row.
    Tolerant of missing/odd shapes.
    """
    sp = SessionProblems(session_id, None, None, None)
    if not isinstance(grade, dict):
        return sp

    sp.overall_pass = _as_bool(grade.get("overall_pass"))
    tp = grade.get("task_purpose")
    sp.task_purpose = tp if isinstance(tp, str) else None
    dl = grade.get("difficulty_level")
    sp.difficulty_level = dl if isinstance(dl, str) else None

    for dim in GRADE_DIMENSIONS:
        node = grade.get(dim)
        if not isinstance(node, dict):
            continue
        if node.get("pass") is not False:  # only an explicit false is a failure
            continue
        sp.failed_dimensions.append(dim)
        reason = node.get("reason")
        reason = reason if isinstance(reason, str) else ""
        # Turn ranges live per-dimension (e.g. ["4-31", "37-95"]); may be [] or None.
        turns = _as_str_list(node.get("problem_turns"))

        violations = node.get("violations")
        if not isinstance(violations, list) or len(violations) == 0:
            sp.errors.append(
                ErrorRow(violation_type(dim, NO_VIOLATION_PLACEHOLDER), reason, turns)
            )
            continue
        for v in violations:
            label = v if isinstance(v, str) else str(v)
            sp.errors.append(ErrorRow(violation_type(dim, label), reason, turns))

    return sp


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


class LoadResult:
    def __init__(self) -> None:
        self.grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        self.total_records = 0
        self.skipped_lines = 0
        self.sources: list[str] = []

    @property
    def task_count(self) -> int:
        return len(self.grouped)


def resolve_jsonl_inputs(path: str) -> list[str]:
    """Resolve ``path`` to a list of jsonl files (a file, or every *.jsonl in a dir)."""
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        files = sorted(
            os.path.join(path, n)
            for n in os.listdir(path)
            if n.endswith(".jsonl") and os.path.isfile(os.path.join(path, n))
        )
        if not files:
            raise FileNotFoundError(f"no .jsonl files in directory: {path}")
        return files
    raise FileNotFoundError(f"jsonl path not found: {path}")


def _consume(fh: TextIO, result: LoadResult, limit: int | None) -> None:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            result.skipped_lines += 1
            continue
        if not isinstance(rec, dict):
            result.skipped_lines += 1
            continue
        task_id = rec.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            result.skipped_lines += 1
            continue
        if task_id not in result.grouped:
            if limit is not None and result.task_count >= limit:
                continue
            result.grouped[task_id] = []
        result.grouped[task_id].append(rec)
        result.total_records += 1


def load_jsonl(path: str, limit: int | None = None) -> LoadResult:
    """Parse a jsonl file or a directory of jsonl files, grouping by ``task_id``."""
    result = LoadResult()
    for file_path in resolve_jsonl_inputs(path):
        result.sources.append(file_path)
        with open(file_path, "r", encoding="utf-8") as fh:
            _consume(fh, result, limit)
    return result


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


@dataclass
class TaskProblem:
    type: str
    dimension: str
    occurrences: int
    sessions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "dimension": self.dimension,
            "occurrences": self.occurrences,
            "sessions": self.sessions,
        }


@dataclass
class TaskAudit:
    task_id: str
    has_source_dir: bool
    record_count: int
    session_ids: list[str]
    failed_dimensions: list[str]
    problems: list[TaskProblem]
    sessions: list[SessionProblems]
    task_purposes: list[str]
    difficulty_levels: list[str]
    overall_pass_counts: dict[str, int]

    @property
    def problem_count(self) -> int:
        return len(self.problems)

    @property
    def total_occurrences(self) -> int:
        return sum(p.occurrences for p in self.problems)

    def trajectory_error_counts(self) -> list[tuple[str, int]]:
        """Error count per trajectory file, sorted by count desc (then number asc).

        Sessions are grouped by their trajectory filename (see
        :func:`trajectory_filename`); the count is the number of failed-error
        rows in that trajectory. Trajectories with no errors are omitted.
        """
        counts: Counter[str] = Counter()
        for sp in self.sessions:
            counts[trajectory_filename(sp.session_id)] += len(sp.errors)
        return sorted(
            ((name, cnt) for name, cnt in counts.items() if cnt > 0),
            key=_trajectory_sort_key,
        )

    def error_rows(
        self, session_path_fn: "Callable[[str], str | None] | None" = None
    ) -> list[dict[str, Any]]:
        """Flat per-session failed-error rows: session_id, session_path, error_type, reason, problem_turns.

        The task_id is NOT repeated per row — it lives once at the top of the
        ``task_records.json`` wrapper. ``session_path_fn`` maps a session_id to a
        path relative to the task dir (``None`` if unresolved); when omitted the
        ``session_path`` field is ``None``. ``problem_turns`` carries the offending
        dimension's turn ranges (e.g. ``["4-31", "37-95"]``). Successful
        sessions/dimensions contribute nothing.
        """
        rows: list[dict[str, Any]] = []
        for sp in self.sessions:
            spath = session_path_fn(sp.session_id) if session_path_fn else None
            for err in sp.errors:
                rows.append(
                    {
                        "session_id": sp.session_id,
                        "session_path": spath,
                        "error_type": err.error_type,
                        "reason": err.reason,
                        "problem_turns": err.problem_turns,
                    }
                )
        return rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "has_source_dir": self.has_source_dir,
            "problem_count": self.problem_count,
            "total_occurrences": self.total_occurrences,
            "record_count": self.record_count,
            "session_count": len(self.session_ids),
            "failed_dimensions": self.failed_dimensions,
            "task_purposes": self.task_purposes,
            "difficulty_levels": self.difficulty_levels,
            "overall_pass_counts": self.overall_pass_counts,
            "problems": [p.to_dict() for p in self.problems],
            "sessions": [s.to_dict() for s in self.sessions],
        }


def audit_task(task_id: str, records: list[dict[str, Any]], has_source_dir: bool) -> TaskAudit:
    sessions: list[SessionProblems] = []
    occ: Counter[str] = Counter()
    vtype_sessions: defaultdict[str, list[str]] = defaultdict(list)
    failed_dims: dict[str, None] = {}
    purposes: dict[str, None] = {}
    difficulties: dict[str, None] = {}
    pass_counts = {"true": 0, "false": 0, "unknown": 0}

    for rec in records:
        sid = rec.get("session_id")
        sid = sid if isinstance(sid, str) else ""
        sp = extract_session_problems(sid, grade_object(rec))
        sessions.append(sp)

        if sp.overall_pass is True:
            pass_counts["true"] += 1
        elif sp.overall_pass is False:
            pass_counts["false"] += 1
        else:
            pass_counts["unknown"] += 1

        if sp.task_purpose:
            purposes.setdefault(sp.task_purpose, None)
        if sp.difficulty_level:
            difficulties.setdefault(sp.difficulty_level, None)
        for d in sp.failed_dimensions:
            failed_dims.setdefault(d, None)
        for vt in sp.violation_types:
            occ[vt] += 1
            if sp.session_id not in vtype_sessions[vt]:
                vtype_sessions[vt].append(sp.session_id)

    problems = [
        TaskProblem(vt, dimension_of(vt), count, vtype_sessions[vt])
        for vt, count in occ.items()
    ]
    problems.sort(key=lambda p: (-p.occurrences, p.type))

    return TaskAudit(
        task_id=task_id,
        has_source_dir=has_source_dir,
        record_count=len(records),
        session_ids=[s.session_id for s in sessions],
        failed_dimensions=list(failed_dims),
        problems=problems,
        sessions=sessions,
        task_purposes=list(purposes),
        difficulty_levels=list(difficulties),
        overall_pass_counts=pass_counts,
    )


def audit_all(
    grouped: dict[str, list[dict[str, Any]]],
    has_dir: dict[str, bool] | None = None,
) -> list[TaskAudit]:
    """Audit every task; sorted by problem_count desc, then occurrences, then id."""
    has_dir = has_dir or {}
    audits = [audit_task(tid, recs, has_dir.get(tid, False)) for tid, recs in grouped.items()]
    audits.sort(key=lambda a: (-a.problem_count, -a.total_occurrences, a.task_id))
    return audits


def build_problem_to_tasks(audits: Iterable[TaskAudit]) -> dict[str, Any]:
    tasks_by_type: defaultdict[str, list[str]] = defaultdict(list)
    occ_by_type: Counter[str] = Counter()
    for a in audits:
        for p in a.problems:
            tasks_by_type[p.type].append(a.task_id)
            occ_by_type[p.type] += p.occurrences
    items = sorted(
        tasks_by_type.items(),
        key=lambda kv: (-len(kv[1]), -occ_by_type[kv[0]], kv[0]),
    )
    return {
        vt: {
            "dimension": dimension_of(vt),
            "task_count": len(task_ids),
            "total_occurrences": occ_by_type[vt],
            "task_ids": sorted(task_ids),
        }
        for vt, task_ids in items
    }


def build_dimension_to_tasks(audits: Iterable[TaskAudit]) -> dict[str, Any]:
    tasks_by_dim: defaultdict[str, list[str]] = defaultdict(list)
    for a in audits:
        for d in a.failed_dimensions:
            tasks_by_dim[d].append(a.task_id)
    items = sorted(tasks_by_dim.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return {d: {"task_count": len(tids), "task_ids": sorted(tids)} for d, tids in items}


def _counter_to_sorted(counter: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def build_summary(
    audits: list[TaskAudit],
    top: int = 50,
    *,
    skipped_lines: int = 0,
    backup_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    p2t = build_problem_to_tasks(audits)
    d2t = build_dimension_to_tasks(audits)

    tasks_with_problems = [a for a in audits if a.problem_count > 0]
    total_records = sum(a.record_count for a in audits)
    total_sessions = sum(len(a.session_ids) for a in audits)

    purpose_counter: Counter[str] = Counter()
    difficulty_counter: Counter[str] = Counter()
    pass_true = pass_false = pass_unknown = 0
    for a in audits:
        for pur in a.task_purposes:
            purpose_counter[pur] += 1
        for dif in a.difficulty_levels:
            difficulty_counter[dif] += 1
        pass_true += a.overall_pass_counts["true"]
        pass_false += a.overall_pass_counts["false"]
        pass_unknown += a.overall_pass_counts["unknown"]

    most_common_violations = [
        {
            "type": vt,
            "dimension": info["dimension"],
            "task_count": info["task_count"],
            "total_occurrences": info["total_occurrences"],
        }
        for vt, info in list(p2t.items())[:top]
    ]
    most_common_dimensions = [
        {"dimension": d, "task_count": info["task_count"]}
        for d, info in list(d2t.items())[:top]
    ]
    tasks_most_problems = [
        {
            "task_id": a.task_id,
            "problem_count": a.problem_count,
            "total_occurrences": a.total_occurrences,
            "failed_dimensions": a.failed_dimensions,
            "has_source_dir": a.has_source_dir,
        }
        for a in tasks_with_problems[:top]
    ]

    summary: dict[str, Any] = {
        "totals": {
            "tasks_total": len(audits),
            "tasks_with_problems": len(tasks_with_problems),
            "tasks_clean": len(audits) - len(tasks_with_problems),
            "records_total": total_records,
            "sessions_total": total_sessions,
            "skipped_lines": skipped_lines,
            "distinct_problem_types": len(p2t),
            "failed_dimension_types": len(d2t),
        },
        "overall_pass": {"pass": pass_true, "fail": pass_false, "unknown": pass_unknown},
        "most_common_violation_types": most_common_violations,
        "most_common_failed_dimensions": most_common_dimensions,
        "tasks_with_most_problems": tasks_most_problems,
        "distribution_by_task_purpose": _counter_to_sorted(purpose_counter),
        "distribution_by_difficulty_level": _counter_to_sorted(difficulty_counter),
    }
    if backup_summary is not None:
        summary["backup"] = backup_summary
    return summary


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

VALID_MODES = ("copy", "clone", "hardlink", "symlink", "manifest")
IGNORE = shutil.ignore_patterns(".DS_Store")


@dataclass
class DataIndex:
    by_name: dict[str, str]
    duplicates: dict[str, list[str]] = field(default_factory=dict)


def index_store_sessions_dir(store_sessions_dir: str) -> DataIndex:
    """Index every TASK dir under ``store_sessions_dir`` by basename (depth 1 and 2)."""
    by_name: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}

    def consider(path: str) -> None:
        name = os.path.basename(path)
        if name in by_name:
            duplicates.setdefault(name, [by_name[name]]).append(path)
        else:
            by_name[name] = path

    if not os.path.isdir(store_sessions_dir):
        return DataIndex(by_name={}, duplicates={})

    for entry in sorted(os.scandir(store_sessions_dir), key=lambda e: e.name):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        consider(entry.path)
        for child in sorted(os.scandir(entry.path), key=lambda e: e.name):
            if child.is_dir() and not child.name.startswith("."):
                consider(child.path)
    return DataIndex(by_name=by_name, duplicates=duplicates)


@dataclass
class BackupOutcome:
    task_id: str
    status: str  # copied | skipped_existing | missing | error | manifest_only
    source: str | None
    dest: str | None
    owns_dest: bool = False
    message: str = ""


def _copy_tree(src: str, dst: str, copy_function: Callable | None = None) -> None:
    if copy_function is None:
        shutil.copytree(src, dst, ignore=IGNORE)
    else:
        shutil.copytree(src, dst, copy_function=copy_function, ignore=IGNORE)


def _clone_tree(src: str, dst: str) -> None:
    subprocess.run(["cp", "-c", "-R", src, dst], check=True, capture_output=True, text=True)


def backup_task(
    task_id: str,
    source: str | None,
    target_dir: str,
    mode: str,
    *,
    overwrite: bool = False,
) -> BackupOutcome:
    if source is None:
        return BackupOutcome(task_id, "missing", None, None, owns_dest=False)

    dest = os.path.join(target_dir, task_id)

    if mode == "manifest":
        return BackupOutcome(task_id, "manifest_only", source, None, owns_dest=False)

    if mode == "symlink":
        try:
            if os.path.islink(dest) or os.path.exists(dest):
                if not overwrite:
                    return BackupOutcome(task_id, "skipped_existing", source, dest, False)
                if os.path.islink(dest) or os.path.isfile(dest):
                    os.unlink(dest)
                else:
                    shutil.rmtree(dest)
            os.makedirs(target_dir, exist_ok=True)
            os.symlink(os.path.abspath(source), dest)
            return BackupOutcome(task_id, "copied", source, dest, owns_dest=False)
        except OSError as exc:
            return BackupOutcome(task_id, "error", source, dest, False, str(exc))

    if os.path.exists(dest) and not overwrite:
        return BackupOutcome(task_id, "skipped_existing", source, dest, owns_dest=True)
    try:
        if os.path.exists(dest) and overwrite:
            shutil.rmtree(dest)
        os.makedirs(target_dir, exist_ok=True)
        if mode == "copy":
            _copy_tree(source, dest)
        elif mode == "hardlink":
            _copy_tree(source, dest, copy_function=os.link)
        elif mode == "clone":
            _clone_tree(source, dest)
        else:  # pragma: no cover
            raise ValueError(f"unknown mode {mode!r}")
        return BackupOutcome(task_id, "copied", source, dest, owns_dest=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        msg = getattr(exc, "stderr", None) or str(exc)
        return BackupOutcome(task_id, "error", source, dest, True, str(msg).strip())


def summarize_outcomes(
    outcomes: list[BackupOutcome], mode: str, index: DataIndex
) -> dict[str, Any]:
    buckets: dict[str, list[str]] = {
        "copied": [],
        "skipped_existing": [],
        "missing": [],
        "manifest_only": [],
        "error": [],
    }
    errors: list[dict[str, str]] = []
    for o in outcomes:
        buckets.setdefault(o.status, []).append(o.task_id)
        if o.status == "error":
            errors.append({"task_id": o.task_id, "message": o.message})
    return {
        "mode": mode,
        "counts": {k: len(v) for k, v in buckets.items()},
        "copied": sorted(buckets["copied"]),
        "skipped_existing": sorted(buckets["skipped_existing"]),
        "missing": sorted(buckets["missing"]),
        "manifest_only": sorted(buckets["manifest_only"]),
        "errors": errors,
        "duplicate_source_names": index.duplicates,
    }


# --------------------------------------------------------------------------- #
# Output writers
# --------------------------------------------------------------------------- #

FIX_ISSUES_DIRNAME = "fix_issues"
DETACHED_DIRNAME = "_fix_issues"


def _add_timestamp_to_path(path: str, timestamp: str) -> str:
    """在文件名中添加时间戳，例如 file.json -> file_20260625_143022.json"""
    base, ext = os.path.splitext(path)
    return f"{base}_{timestamp}{ext}"


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_full_report(
    audits: list[TaskAudit],
    *,
    top: int,
    skipped_lines: int,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the single consolidated report.

    Contains everything that used to be split across ``_reports/`` and the root
    ``task_problems.json``: the analytical summary, the backup manifest, the
    problem→task and dimension→task mappings, and full per-task detail.
    """
    report = build_summary(audits, top=top, skipped_lines=skipped_lines)
    report["generated_by"] = f"audit_trajectories {__version__}"
    report["backup_manifest"] = manifest
    report["problem_to_tasks"] = build_problem_to_tasks(audits)
    report["dimension_to_tasks"] = build_dimension_to_tasks(audits)
    report["tasks"] = [a.to_dict() for a in audits]
    return report


def write_summary_report(path: str, report: dict[str, Any], timestamp: str | None = None) -> str:
    """Write the single consolidated summary_report.json."""
    if timestamp:
        path = _add_timestamp_to_path(path, timestamp)
    _write_json(path, report)
    return path


def build_error_markdown(audits: list[TaskAudit]) -> str:
    """A markdown report: every task and its error count, sorted desc.

    'Error count' is the total number of failed-error rows (== task_records
    ``error_count`` == ``total_occurrences``). Ties break by distinct problem
    types, then task_id. The last column breaks that total down per trajectory
    (``轨迹文件名:错误数``), itself sorted by error count desc.
    """
    ranked = sorted(
        audits, key=lambda a: (-a.total_occurrences, -a.problem_count, a.task_id)
    )
    total_errors = sum(a.total_occurrences for a in audits)
    with_problems = sum(1 for a in audits if a.total_occurrences > 0)

    lines = [
        "# Task 错误数量报告",
        "",
        f"- 生成自 `audit_trajectories {__version__}`",
        f"- 任务总数: **{len(audits)}**(有错误的: **{with_problems}**)",
        f"- 错误总数: **{total_errors}**",
        "",
        "| 排名 | Task ID | 错误数 | 问题类型数 | 有源目录 | 轨迹错误分布 (轨迹文件名:错误数) |",
        "| ---: | --- | ---: | ---: | :---: | --- |",
    ]
    for i, a in enumerate(ranked, 1):
        traj = ", ".join(f"{name}:{cnt}" for name, cnt in a.trajectory_error_counts())
        lines.append(
            f"| {i} | `{a.task_id}` | {a.total_occurrences} | "
            f"{a.problem_count} | {'✅' if a.has_source_dir else '—'} | {traj or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_error_json(audits: list[TaskAudit]) -> dict[str, Any]:
    """Machine-readable twin of :func:`build_error_markdown`.

    Same ranking and per-trajectory breakdown as the markdown table, shaped for
    downstream scripts: a ``totals`` block plus a ``tasks`` list where each task
    carries its ``trajectory_error_distribution`` (``trajectory_file`` ->
    ``error_count``), sorted by error count desc.
    """
    ranked = sorted(
        audits, key=lambda a: (-a.total_occurrences, -a.problem_count, a.task_id)
    )
    total_errors = sum(a.total_occurrences for a in audits)
    with_problems = sum(1 for a in audits if a.total_occurrences > 0)
    return {
        "generated_by": f"audit_trajectories {__version__}",
        "totals": {
            "tasks_total": len(audits),
            "tasks_with_problems": with_problems,
            "errors_total": total_errors,
        },
        "tasks": [
            {
                "rank": i,
                "task_id": a.task_id,
                "error_count": a.total_occurrences,
                "problem_type_count": a.problem_count,
                "has_source_dir": a.has_source_dir,
                "trajectory_error_distribution": [
                    {"trajectory_file": name, "error_count": cnt}
                    for name, cnt in a.trajectory_error_counts()
                ],
            }
            for i, a in enumerate(ranked, 1)
        ],
    }


def write_error_markdown(path: str, audits: list[TaskAudit], timestamp: str | None = None) -> str:
    if timestamp:
        path = _add_timestamp_to_path(path, timestamp)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(build_error_markdown(audits))
    os.replace(tmp, path)
    return path


def write_error_json(path: str, audits: list[TaskAudit], timestamp: str | None = None) -> str:
    """Write the machine-readable error report (twin of error_report.md)."""
    if timestamp:
        path = _add_timestamp_to_path(path, timestamp)
    _write_json(path, build_error_json(audits))
    return path


_DB_SCHEMA = """
CREATE TABLE run_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE tasks (
    task_id                TEXT PRIMARY KEY,
    error_count            INTEGER NOT NULL,
    problem_type_count     INTEGER NOT NULL,
    failed_dimension_count INTEGER NOT NULL,
    record_count           INTEGER NOT NULL,
    session_count          INTEGER NOT NULL,
    has_source_dir         INTEGER NOT NULL
);
CREATE TABLE sessions (
    session_id       TEXT,
    task_id          TEXT,
    overall_pass     INTEGER,
    task_purpose     TEXT,
    difficulty_level TEXT,
    error_count      INTEGER NOT NULL
);
CREATE TABLE errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT,
    session_id    TEXT,
    dimension     TEXT,
    violation     TEXT,
    error_type    TEXT,
    reason        TEXT,
    problem_turns TEXT
);
CREATE TABLE dimensions (
    dimension        TEXT PRIMARY KEY,
    task_count       INTEGER NOT NULL,
    session_count    INTEGER NOT NULL,
    occurrence_count INTEGER NOT NULL
);
CREATE TABLE violation_types (
    error_type       TEXT PRIMARY KEY,
    dimension        TEXT,
    task_count       INTEGER NOT NULL,
    occurrence_count INTEGER NOT NULL
);
CREATE INDEX idx_errors_dimension  ON errors(dimension);
CREATE INDEX idx_errors_task_id    ON errors(task_id);
CREATE INDEX idx_errors_session_id ON errors(session_id);
CREATE INDEX idx_sessions_task_id  ON sessions(task_id);
"""


def _violation_of(error_type: str) -> str:
    """Recover the violation label from a namespaced ``dimension/violation``."""
    return error_type.split("/", 1)[1] if "/" in error_type else error_type


def build_db_rows(audits: list[TaskAudit]) -> dict[str, list[tuple]]:
    """Flatten audits into row tuples for each SQLite table.

    ``errors`` is the source of truth (one row per :class:`ErrorRow` = one
    occurrence); ``dimensions`` and ``violation_types`` are rollups computed
    here so the numbers match the JSON reports (see
    :func:`build_dimension_to_tasks` / :func:`build_problem_to_tasks`).
    """
    task_rows: list[tuple] = []
    session_rows: list[tuple] = []
    error_rows: list[tuple] = []

    # dimension rollups
    dim_tasks: defaultdict[str, set] = defaultdict(set)
    dim_sessions: defaultdict[str, set] = defaultdict(set)
    dim_occ: Counter[str] = Counter()
    # violation_type rollups
    vt_tasks: defaultdict[str, set] = defaultdict(set)
    vt_occ: Counter[str] = Counter()
    vt_dim: dict[str, str] = {}

    for a in audits:
        task_rows.append(
            (
                a.task_id,
                a.total_occurrences,
                a.problem_count,
                len(a.failed_dimensions),
                a.record_count,
                len(a.session_ids),
                1 if a.has_source_dir else 0,
            )
        )
        for sp in a.sessions:
            op = None if sp.overall_pass is None else (1 if sp.overall_pass else 0)
            session_rows.append(
                (
                    sp.session_id,
                    a.task_id,
                    op,
                    sp.task_purpose,
                    sp.difficulty_level,
                    len(sp.errors),
                )
            )
            for err in sp.errors:
                dim = dimension_of(err.error_type)
                viol = _violation_of(err.error_type)
                error_rows.append(
                    (
                        a.task_id,
                        sp.session_id,
                        dim,
                        viol,
                        err.error_type,
                        err.reason,
                        ";".join(err.problem_turns),
                    )
                )
                dim_tasks[dim].add(a.task_id)
                dim_sessions[dim].add(sp.session_id)
                dim_occ[dim] += 1
                vt_tasks[err.error_type].add(a.task_id)
                vt_occ[err.error_type] += 1
                vt_dim[err.error_type] = dim

    dimension_rows = [
        (dim, len(dim_tasks[dim]), len(dim_sessions[dim]), dim_occ[dim])
        for dim in sorted(dim_occ, key=lambda d: (-dim_occ[d], d))
    ]
    violation_type_rows = [
        (vt, vt_dim[vt], len(vt_tasks[vt]), vt_occ[vt])
        for vt in sorted(vt_occ, key=lambda v: (-vt_occ[v], v))
    ]
    return {
        "tasks": task_rows,
        "sessions": session_rows,
        "errors": error_rows,
        "dimensions": dimension_rows,
        "violation_types": violation_type_rows,
    }


def write_sqlite_db(path: str, audits: list[TaskAudit], timestamp: str | None = None) -> str:
    """Write a SQLite database of the audit so it can be re-sorted via SQL.

    Materializes per-error detail (``errors``) plus task / session / dimension /
    violation_type rollups. A fresh file each run (timestamped); existing tables
    are dropped for idempotency.
    """
    if sqlite3 is None:
        raise RuntimeError("sqlite3 is unavailable in this Python environment; rerun with --no-db")
    if timestamp:
        path = _add_timestamp_to_path(path, timestamp)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    rows = build_db_rows(audits)
    tasks_with_problems = sum(1 for a in audits if a.problem_count > 0)
    errors_total = sum(a.total_occurrences for a in audits)
    meta_rows = [
        ("generated_by", f"audit_trajectories {__version__}"),
        ("generated_at", timestamp or ""),
        ("tasks_total", str(len(audits))),
        ("tasks_with_problems", str(tasks_with_problems)),
        ("errors_total", str(errors_total)),
        ("distinct_problem_types", str(len(rows["violation_types"]))),
        ("failed_dimension_types", str(len(rows["dimensions"]))),
    ]

    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        for table in ("violation_types", "dimensions", "errors", "sessions", "tasks", "run_meta"):
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.executescript(_DB_SCHEMA)
        cur.executemany("INSERT INTO run_meta(key, value) VALUES (?, ?)", meta_rows)
        cur.executemany(
            "INSERT INTO tasks(task_id, error_count, problem_type_count, "
            "failed_dimension_count, record_count, session_count, has_source_dir) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows["tasks"],
        )
        cur.executemany(
            "INSERT INTO sessions(session_id, task_id, overall_pass, task_purpose, "
            "difficulty_level, error_count) VALUES (?, ?, ?, ?, ?, ?)",
            rows["sessions"],
        )
        cur.executemany(
            "INSERT INTO errors(task_id, session_id, dimension, violation, "
            "error_type, reason, problem_turns) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows["errors"],
        )
        cur.executemany(
            "INSERT INTO dimensions(dimension, task_count, session_count, "
            "occurrence_count) VALUES (?, ?, ?, ?)",
            rows["dimensions"],
        )
        cur.executemany(
            "INSERT INTO violation_types(error_type, dimension, task_count, "
            "occurrence_count) VALUES (?, ?, ?, ?)",
            rows["violation_types"],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def per_task_fix_issues_dir(
    target_dir: str, task_id: str, dest: str | None, owns_dest: bool
) -> str:
    """Where a task's fix_issues lives: inside an owned copy, else a detached dir."""
    if dest and owns_dest and os.path.isdir(dest):
        return os.path.join(dest, FIX_ISSUES_DIRNAME)
    return os.path.join(target_dir, DETACHED_DIRNAME, task_id, FIX_ISSUES_DIRNAME)


def write_task_records(
    fix_dir: str,
    audit: TaskAudit,
    task_dir: str | None = None,
    session_prefix: str | None = None,
    timestamp: str | None = None,
    records_name: str | None = None,
) -> str:
    """Write this task's failed-error records as JSON.

    Shape: ``{task_id, error_count, records: [{session_id, session_path,
    error_type, reason, problem_turns}]}``. ``session_path`` starts from the
    target dir, i.e. ``<target-name>/<task_id>/sessions/...`` (``session_prefix``
    = ``<target-name>/<task_id>``); ``null`` if unresolved. ``problem_turns`` is
    the offending dimension's turn ranges (e.g. ``["4-31", "37-95"]``, ``[]`` if
    absent). Passing sessions/dimensions are omitted, so a clean task yields an
    empty list.
    """
    os.makedirs(fix_dir, exist_ok=True)
    if records_name:
        filename = records_name
    elif timestamp:
        filename = f"task_records_{timestamp}.json"
    else:
        filename = "task_records.json"
    path = os.path.join(fix_dir, filename)
    session_index = index_sessions(task_dir)

    def make_path(sid: str) -> str | None:
        rel = resolve_session_path(task_dir, sid, session_index)
        if rel is None:
            return None
        return os.path.join(session_prefix, rel) if session_prefix else rel

    rows = audit.error_rows(make_path)
    _write_json(
        path,
        {"task_id": audit.task_id, "error_count": len(rows), "records": rows},
    )
    return path


# --------------------------------------------------------------------------- #
# CLI / orchestration
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="audit_trajectories.py",
        description="Back up TASK dirs and audit trajectory-grading issues from a jsonl.",
    )
    p.add_argument(
        "--jsonl",
        default="./batch_store_sessions/client_json",
        help="Grading .jsonl file, or a directory of .jsonl files to merge "
        "(default ./batch_store_sessions/client_json).",
    )
    p.add_argument("--store-sessions-dir", default="./batch_store_sessions", help="Dir holding TASK source dirs (default ./batch_store_sessions).")
    p.add_argument("--target-dir", default="./target-issues-tasks", help="Output dir for backups + reports (default ./target-issues-tasks).")
    p.add_argument(
        "--summary-report",
        default=os.path.join(AUDIT_REPORT_DIR, "summary_report.json"),
        help="Path for the single consolidated summary_report.json "
        "(default: <项目根目录>/report/audit_report). Contains summary + mappings + manifest + per-task detail.",
    )
    p.add_argument(
        "--error-report",
        default=os.path.join(AUDIT_REPORT_DIR, "error_report.md"),
        help="Path for the markdown table of each task's error count, sorted desc "
        "(default: <项目根目录>/report/audit_report/error_report.md).",
    )
    p.add_argument(
        "--error-report-json",
        default=os.path.join(AUDIT_REPORT_DIR, "error_report.json"),
        help="Path for the machine-readable JSON twin of the error report "
        "(default: <项目根目录>/report/audit_report/error_report.json).",
    )
    p.add_argument(
        "--db",
        default=os.path.join(AUDIT_REPORT_DIR, "audit.db"),
        help="Path for the SQLite database of the audit (per-error detail + "
        "dimension/task rollups, re-sortable via SQL). "
        "(default: <项目根目录>/report/audit_report/audit.db).",
    )
    p.add_argument("--mode", choices=VALID_MODES, default="copy", help="Backup method (default copy).")
    p.add_argument("--limit", type=int, default=None, help="Only process the first N distinct tasks (testing).")
    p.add_argument("--top", type=int, default=50, help="List length for 'top' report sections (default 50).")
    p.add_argument("--no-copy", action="store_true", help="Skip the backup stage (analysis + reports only).")
    p.add_argument("--overwrite", action="store_true", help="Re-copy tasks even if the dest already exists.")
    p.add_argument("--no-per-task", action="store_true", help="Skip writing per-task fix_issues/task_records.json.")
    p.add_argument("--records-name", default="task_records.json",
                   help="File name for per-task records (default: task_records.json).")
    p.add_argument("--no-db", action="store_true", help="Skip writing the SQLite database.")
    p.add_argument("--quiet", action="store_true", help="Suppress progress logging.")
    p.add_argument("--version", action="version", version=f"audit_trajectories {__version__}")
    return p


def _log(quiet: bool, msg: str) -> None:
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        jsonl_files = resolve_jsonl_inputs(args.jsonl)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    do_copy = not args.no_copy
    started = time.time()
    quiet = args.quiet

    # 生成时间戳，格式：YYYYMMDD_HHMMSS
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. Split
    if len(jsonl_files) == 1:
        _log(quiet, f"[1/5] loading {jsonl_files[0]} ...")
    else:
        _log(quiet, f"[1/5] loading {len(jsonl_files)} jsonl files from {args.jsonl} ...")
    loaded = load_jsonl(args.jsonl, limit=args.limit)
    _log(quiet, f"      {loaded.task_count} tasks, {loaded.total_records} records, {loaded.skipped_lines} skipped lines")

    # Index source dirs
    _log(quiet, f"[2/5] indexing store-sessions dir {args.store_sessions_dir} ...")
    index = index_store_sessions_dir(args.store_sessions_dir)
    has_dir = {tid: tid in index.by_name for tid in loaded.grouped}
    present = sum(1 for v in has_dir.values() if v)
    _log(quiet, f"      {len(index.by_name)} indexed dirs; {present}/{loaded.task_count} tasks have a source dir")

    # 2. Map + analyse
    _log(quiet, "[3/5] analysing problems ...")
    audits = audit_all(loaded.grouped, has_dir)

    os.makedirs(args.target_dir, exist_ok=True)
    # session_path is written starting from the target dir, e.g.
    # "target-issues-tasks/<task_id>/sessions/...".
    target_name = os.path.basename(os.path.normpath(args.target_dir))

    # 3. Copy (+ 4. per-task task_records.json)
    if do_copy:
        _log(quiet, f"[4/5] backing up (mode={args.mode}) -> {args.target_dir} ...")
    else:
        _log(quiet, "[4/5] backup skipped (--no-copy); writing per-task records only ...")

    outcomes: list[BackupOutcome] = []
    total = len(audits)
    for i, a in enumerate(audits, 1):
        source = index.by_name.get(a.task_id)
        if do_copy:
            outcome = backup_task(a.task_id, source, args.target_dir, args.mode, overwrite=args.overwrite)
        else:
            outcome = BackupOutcome(
                a.task_id, "manifest_only" if source else "missing", source, None, owns_dest=False
            )
        outcomes.append(outcome)

        if not args.no_per_task:
            fix_dir = per_task_fix_issues_dir(args.target_dir, a.task_id, outcome.dest, outcome.owns_dest)
            # resolve session paths from the source dir (exists regardless of mode);
            # write them starting from the target dir: <target-name>/<task_id>/sessions/...
            write_task_records(
                fix_dir, a, task_dir=source,
                session_prefix=os.path.join(target_name, a.task_id),
                records_name=args.records_name,
            )

        if not quiet and (i % 100 == 0 or i == total):
            _log(quiet, f"      {i}/{total} tasks processed")

    # 5. Reports — one consolidated file.
    _log(quiet, "[5/5] writing report ...")
    manifest = summarize_outcomes(outcomes, args.mode if do_copy else "no-copy", index)
    manifest["source_tasks_indexed"] = len(index.by_name)
    report = build_full_report(
        audits, top=args.top, skipped_lines=loaded.skipped_lines, manifest=manifest
    )
    report_path = write_summary_report(args.summary_report, report, timestamp=timestamp)
    error_md_path = write_error_markdown(args.error_report, audits, timestamp=timestamp)
    error_json_path = write_error_json(args.error_report_json, audits, timestamp=timestamp)
    db_path = None if args.no_db else write_sqlite_db(args.db, audits, timestamp=timestamp)

    elapsed = time.time() - started
    _log(quiet, f"done in {elapsed:.1f}s")
    print(
        "\n".join(
            [
                f"tasks: {loaded.task_count} | with problems: {report['totals']['tasks_with_problems']}"
                f" | distinct problem types: {report['totals']['distinct_problem_types']}",
                f"backup[{manifest['mode']}]: copied={manifest['counts'].get('copied', 0)}"
                f" skipped={manifest['counts'].get('skipped_existing', 0)}"
                f" missing={manifest['counts'].get('missing', 0)}"
                f" errors={manifest['counts'].get('error', 0)}",
                f"report (json, consolidated): {report_path}",
                f"report (markdown, error counts): {error_md_path}",
                f"report (json, error counts): {error_json_path}",
                f"database (sqlite): {db_path or '(skipped)'}",
                f"per-task records: {os.path.join(args.target_dir, '<task_id>', 'fix_issues', args.records_name)}",
            ]
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

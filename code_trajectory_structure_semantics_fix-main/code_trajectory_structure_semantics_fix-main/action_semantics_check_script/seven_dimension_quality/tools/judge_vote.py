#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
判分多票表决（方案5）：把同一批轨迹判 N 次，按「每个 (session, dimension)」多数表决，
压低判分（gpt-5.5）的随机抖动。

为什么需要
==========
判分是 LLM-as-judge，纯随机（见 docs/trajectory_repair_convergence_analysis.md 根因 B）。
同一份内容这轮 pass、下轮 fail（subagent r1/r2 通过疑似判分噪声）。单票判分让「是否通过」
不可复现，best-of-N 收敛全靠运气。多票多数表决把单次抖动摊平：
  · 维度 pass/fail 取多数票（平票时**从严判 fail**，宁可多修不可漏修）；
  · 选用「与多数票一致」的那一条记录的 violations/reason 作为代表，保留可读理由。

设计
====
输入：N 份 grades jsonl（同一输入、独立判分得到），路径列表。
输出：合并后的一份 grades（list[dict]），每条 = 一个 session，grades 内每维度按票合并。
仅纯函数，不写盘（由调用方决定落地），便于单测与被 run_pipeline 复用。

口径细节
========
· 以 session_id 对齐各票的记录；某票缺某 session 视为该票对该 session「弃权」。
· 维度的 pass 值只在 True/False 间表决；pass 为 None（unparseable）视为弃权，不计票。
· 平票（含全弃权后无有效票）→ 该维度判 fail（pass=False），并标注 reason 说明为表决产生。
· 非 pass/fail 的元信息（_events/_det 等）取「第一份出现该 session 的记录」为基底。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def _load(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _dimension_nodes(grades: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in grades.items()
            if isinstance(v, dict) and "pass" in v}


def majority_vote_dimension(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """对同一维度的多条判分记录做多数表决，返回合并后的维度节点。

    nodes：来自各票、同一 (session,dimension) 的节点列表（已过滤掉缺失）。
    规则：统计 pass True/False（None 弃权）；多数为 True→pass=True，否则 False（含平票/无有效票）。
    代表性 violations/reason 取「投了胜出票」的第一条；无有效票则给出表决说明。
    """
    votes = Counter()
    for n in nodes:
        p = n.get("pass")
        if p is True:
            votes["pass"] += 1
        elif p is False:
            votes["fail"] += 1
        # None / 其它 → 弃权
    n_pass, n_fail = votes["pass"], votes["fail"]
    if n_pass > n_fail:
        winner = True
    else:
        winner = False  # 平票或多数 fail → 从严 fail
    # 选代表记录：优先与 winner 一致者
    rep = next((n for n in nodes if n.get("pass") is winner), None)
    if rep is None:
        rep = nodes[0] if nodes else {}
    merged = dict(rep)
    merged["pass"] = winner
    merged["_vote"] = {"pass": n_pass, "fail": n_fail,
                       "abstain": len(nodes) - n_pass - n_fail, "n": len(nodes)}
    if not nodes:
        merged["pass"] = False
        merged["reason"] = "no valid votes; defaulted to fail"
        merged.setdefault("violations", [])
    return merged


def merge_grade_files(paths: list[Path]) -> list[dict[str, Any]]:
    """多份 grades jsonl → 多数表决合并后的 list[dict]（每条一个 session）。

    无输入或全空 → []。单份输入 → 原样（每维度也会带上 1 票的 _vote 元信息）。
    """
    ballots = [_load(p) for p in paths]
    ballots = [b for b in ballots if b]
    if not ballots:
        return []

    # 收集所有 session_id（保序：以首份出现顺序为准，再补后续份新出现的）
    order: list[str] = []
    seen: set[str] = set()
    by_session: dict[str, list[dict[str, Any]]] = {}
    for ballot in ballots:
        for rec in ballot:
            sid = rec.get("session_id")
            if sid is None:
                continue
            if sid not in seen:
                seen.add(sid)
                order.append(sid)
            by_session.setdefault(sid, []).append(rec)

    merged_records: list[dict[str, Any]] = []
    for sid in order:
        recs = by_session[sid]
        base = dict(recs[0])  # 元信息基底
        # 汇总所有维度键
        dim_keys: list[str] = []
        dseen: set[str] = set()
        for r in recs:
            for k in _dimension_nodes(r.get("grades") or {}):
                if k not in dseen:
                    dseen.add(k)
                    dim_keys.append(k)
        merged_grades: dict[str, Any] = {}
        # 保留非维度的 grades 字段（少见）来自基底
        for k, v in (base.get("grades") or {}).items():
            if not (isinstance(v, dict) and "pass" in v):
                merged_grades[k] = v
        for dim in dim_keys:
            nodes = [
                (r.get("grades") or {}).get(dim)
                for r in recs
                if isinstance((r.get("grades") or {}).get(dim), dict)
                and "pass" in (r.get("grades") or {}).get(dim)
            ]
            merged_grades[dim] = majority_vote_dimension([n for n in nodes if n])
        base["grades"] = merged_grades
        base["_votes_merged"] = len(recs)
        merged_records.append(base)
    return merged_records


def write_merged(records: list[dict[str, Any]], out: Path) -> None:
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + ("\n" if records else ""),
        encoding="utf-8",
    )


def failing_dims(records: list[dict[str, Any]]) -> int:
    return sum(
        1
        for r in records
        for v in (r.get("grades") or {}).values()
        if isinstance(v, dict) and v.get("pass") is False
    )

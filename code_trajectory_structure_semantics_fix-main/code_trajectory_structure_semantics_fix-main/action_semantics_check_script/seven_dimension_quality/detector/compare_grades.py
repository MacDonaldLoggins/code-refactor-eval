#!/usr/bin/env python3
"""
compare_grades.py — compare a judge run (grades_pro*.jsonl) against ground-truth grades, with
robust multi-level subagent matching, per-dimension accuracy, and a full three-way diff.

WHY THIS EXISTS
  The judge output keys each row by a SHORT id (e.g. "agent-a1b2c3" or "5-develop-<uuid>"), while the
  ground-truth file keys rows by the FULL session path (e.g. "5-develop-<uuid>/subagents/agent-a1b2c3").
  A subagent's bare name can also collide across different parents. This script reconstructs each
  judge id's full session path from the trajectory directory tree and matches on that, so a
  subagent is matched to the GT row under its OWN parent — never a same-named one elsewhere.

INPUTS
  --pred     judge output (grades_pro.jsonl).            row schema: {"id", "grades": {dim: {...}}}
  --gt       ground-truth (failed_trajectories_grading.jsonl).
             row schema: {"session_id"|"id", "grade"|"grades": {dim: {...}, ...}}
  --sessions (optional) directory of raw trajectory files; used to resolve bare subagent ids to
             their full "<parent>/subagents/<agent>" path. Without it, falls back to tail-matching.
  --baseline (optional) a SECOND judge output (e.g. the previous version) to show per-case v_old→v_new.

OUTPUTS
  - stdout summary: matched count, per-dimension accuracy (and Δ vs baseline if given).
  - --out-csv (optional): one row per (session, dimension) with pred/gt/baseline pass + agreement +
    the pred reason, so you can eyeball every disagreement.

USAGE
  python compare_grades.py --pred grades_pro.jsonl --gt failed_trajectories_grading.jsonl \
         --sessions /path/to/sessions --baseline old_grades_pro.jsonl --out-csv diff.csv
"""
import argparse, csv, json, os, sys
from collections import defaultdict

DIMS = ["meaningful_trajectory", "task_completion", "behavioral_efficiency", "tool_content_quality",
        "hacky_behavior_free", "hallucination_free", "logical_and_coherent_actions"]


# --------------------------------------------------------------------------- io
def load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def pred_id(row):
    return row.get("id") or row.get("session_id") or row.get("task_id")


def pred_grades(row):
    """Judge rows use 'grades'; GT rows use 'grade' (singular). Accept either."""
    return row.get("grades") or row.get("grade") or {}


def gt_key(row):
    return row.get("session_id") or row.get("id") or row.get("task_id")


# --------------------------------------------------------------------------- matching
def build_pathmap(sessions_dir):
    """basename(without .json) -> list of full relative paths (without .json), from the trajectory tree."""
    pathmap = defaultdict(list)
    if not sessions_dir:
        return pathmap
    for root, _dirs, files in os.walk(sessions_dir):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            full = os.path.relpath(os.path.join(root, fn), sessions_dir)[:-5]  # strip .json
            base = os.path.basename(full)
            pathmap[base].append(full)
    return pathmap


def index_gt(gt_rows):
    by_sid = {}
    by_tail = defaultdict(list)
    by_pair = {}
    for g in gt_rows:
        s = gt_key(g)
        if s is None:
            continue
        by_sid[s] = g
        t = g.get("task_id")
        if t is not None:
            by_pair[(t, s)] = g
        if "/" in s:
            by_tail[s.split("/")[-1]].append(g)
    return by_sid, by_tail, by_pair


def resolve_gt(pid, ptask, by_sid, by_tail, by_pair, pathmap):
    """Return (gt_row, full_path_used, how) or (None, None, reason)."""
    # 0) exact (task_id, session_id) pair — preferred, avoids cross-task session_id collisions
    if ptask is not None and (ptask, pid) in by_pair:
        return by_pair[(ptask, pid)], pid, "pair"
    # 1) direct id == session_id
    if pid in by_sid:
        return by_sid[pid], pid, "direct"
    # 2) reconstruct full path(s) from the trajectory tree, then match that exact path in GT
    for full in pathmap.get(pid, []):
        for cand in (full,
                     full.replace("/subagents/", "/subagent/"),
                     "/".join([full.split("/")[0], full.split("/")[-1]]) if "/" in full else full):
            if cand in by_sid:
                return by_sid[cand], cand, "path"
    # 3) last resort: unique tail match (warn if ambiguous)
    if pid in by_tail and len(by_tail[pid]) == 1:
        g = by_tail[pid][0]
        return g, gt_key(g), "tail"
    if pid in by_tail and len(by_tail[pid]) > 1:
        return None, None, f"ambiguous_tail({len(by_tail[pid])})"
    return None, None, "unmatched"


# --------------------------------------------------------------------------- compare
def passof(grades, dim):
    d = grades.get(dim)
    if not isinstance(d, dict):
        return None
    return d.get("pass")


def reasonof(grades, dim):
    d = grades.get(dim)
    return (d.get("reason") if isinstance(d, dict) else "") or ""


def violof(grades, dim):
    d = grades.get(dim)
    v = d.get("violations") if isinstance(d, dict) else None
    return ",".join(v) if v else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pred", required=True, help="judge output jsonl to evaluate")
    ap.add_argument("--gt", required=True, help="ground-truth jsonl")
    ap.add_argument("--sessions", default=None, help="dir of raw trajectory files (for path-based matching)")
    ap.add_argument("--baseline", default=None, help="optional previous judge output for v_old→v_new deltas")
    ap.add_argument("--dimension", choices=DIMS, help="compare only one dimension")
    ap.add_argument("--out-csv", default="results/compare_grades.csv", help="per-(session,dim) diff csv")
    ap.add_argument("--show-disagreements", action="store_true", help="print every disagreeing row to stdout")
    a = ap.parse_args()
    dims = [a.dimension] if a.dimension else DIMS

    pred = load_jsonl(a.pred)
    gt = load_jsonl(a.gt)
    base_rows = load_jsonl(a.baseline) if a.baseline else []
    base_by_id = {pred_id(r): r for r in base_rows}

    pathmap = build_pathmap(a.sessions)
    by_sid, by_tail, by_pair = index_gt(gt)

    matched, unmatched = [], []
    for p in pred:
        pid = pred_id(p)
        g, full, how = resolve_gt(pid, p.get("task_id"), by_sid, by_tail, by_pair, pathmap)
        if g is None:
            unmatched.append((pid, how))
        else:
            matched.append((pid, full, how, p, g))

    print(f"pred rows: {len(pred)}   gt rows: {len(gt)}")
    print(f"matched: {len(matched)}   unmatched: {len(unmatched)}")
    how_counts = defaultdict(int)
    for _, _, how, _, _ in matched:
        how_counts[how] += 1
    print(f"  match method: {dict(how_counts)}")
    if unmatched:
        print(f"  unmatched ids ({len(unmatched)}): " +
              ", ".join(f"{i}[{w}]" for i, w in unmatched[:12]) + (" …" if len(unmatched) > 12 else ""))

    # per-dimension accuracy (+ baseline delta)
    print("\n=== per-dimension accuracy vs GT ===")
    def acc_for(rows, getter):
        per = {}
        A = T = 0
        for d in dims:
            a_ = t_ = 0
            for item in rows:
                pp, gg = getter(item, d)
                if pp is None or gg is None:
                    continue
                t_ += 1
                a_ += (pp == gg)
            per[d] = (a_, t_)
            A += a_
            T += t_
        return per, A, T

    def pred_gt(item, d):
        _, _, _, p, g = item
        return passof(pred_grades(p), d), passof(pred_grades(g), d)
    per, A, T = acc_for(matched, pred_gt)

    base_per = None
    if base_rows:
        base_matched = []
        for pid, full, how, p, g in matched:
            b = base_by_id.get(pid)
            if b is not None:
                base_matched.append((pid, full, how, b, g))
        def base_gt(item, d):
            _, _, _, b, g = item
            return passof(pred_grades(b), d), passof(pred_grades(g), d)
        base_per, bA, bT = acc_for(base_matched, base_gt)

    hdr = f"{'dimension':32} {'acc':>12}"
    if base_per:
        hdr += f" {'baseline':>12} {'Δ':>7}"
    print(hdr)
    for d in dims:
        a_, t_ = per[d]
        s = f"{d:32} {a_:>3}/{t_:<3}={ (100*a_/t_ if t_ else 0):5.0f}%"
        if base_per:
            ba, bt = base_per[d]
            base_pct = 100 * ba / bt if bt else 0
            cur_pct = 100 * a_ / t_ if t_ else 0
            s += f" {ba:>3}/{bt:<3}={base_pct:4.0f}% {cur_pct-base_pct:+5.0f}pp"
        print(s)
    overall = 100 * A / T if T else 0
    line = f"{'OVERALL (pooled)':32} {A:>3}/{T:<3}={overall:5.1f}%"
    if base_per:
        bA = sum(base_per[d][0] for d in dims)
        bT = sum(base_per[d][1] for d in dims)
        bp = 100 * bA / bT if bT else 0
        line += f" {bA:>3}/{bT:<3}={bp:4.1f}% {overall-bp:+5.1f}pp"
    print(line)

    # session-level exact match
    exact = 0
    for pid, full, how, p, g in matched:
        pg, gg = pred_grades(p), pred_grades(g)
        if all(passof(pg, d) is not None and passof(gg, d) is not None and passof(pg, d) == passof(gg, d)
               for d in dims):
            exact += 1
    exact_label = "session exact-match (selected)" if a.dimension else "session exact-match (all 7 dims)"
    print(f"{exact_label:32} {exact}/{len(matched)} = "
          f"{(100*exact/len(matched) if matched else 0):.0f}%")

    # disagreement listing
    if a.show_disagreements:
        print("\n=== disagreements (pred != GT) ===")
        for pid, full, how, p, g in matched:
            pg, gg = pred_grades(p), pred_grades(g)
            for d in dims:
                pp, gv = passof(pg, d), passof(gg, d)
                if pp is None or gv is None or pp == gv:
                    continue
                bp = ""
                if base_rows and base_by_id.get(pid):
                    bv = passof(pred_grades(base_by_id[pid]), d)
                    bp = f" v_old={bv}"
                print(f"  {d:30} {pid[:34]:34} pred={pp} gt={gv}{bp}  viol={violof(pg, d)}")

    # csv
    if a.out_csv:
        with open(a.out_csv, "w", newline="") as f:
            w = csv.writer(f)
            cols = ["session_id", "full_path", "match_method", "dimension",
                    "pred_pass", "gt_pass", "agree"]
            if base_rows:
                cols.insert(6, "baseline_pass")
            cols += ["pred_violations", "pred_reason", "gt_reason"]
            w.writerow(cols)
            for pid, full, how, p, g in matched:
                pg, gg = pred_grades(p), pred_grades(g)
                bgrades = pred_grades(base_by_id[pid]) if (base_rows and base_by_id.get(pid)) else None
                for d in dims:
                    pp, gv = passof(pg, d), passof(gg, d)
                    row = [pid, full, how, d, pp, gv,
                           ("" if pp is None or gv is None else ("yes" if pp == gv else "NO"))]
                    if base_rows:
                        row.insert(6, passof(bgrades, d) if bgrades else "")
                    row += [violof(pg, d), reasonof(pg, d)[:500], reasonof(gg, d)[:500]]
                    w.writerow(row)
        print(f"\nwrote per-(session,dim) diff -> {a.out_csv}")


if __name__ == "__main__":
    main()

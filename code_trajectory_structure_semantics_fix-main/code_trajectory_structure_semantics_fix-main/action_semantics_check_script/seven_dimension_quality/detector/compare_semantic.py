#!/usr/bin/env python3
"""
compare_semantic.py — compare a judge run against ground-truth grades at the SESSION level:
one row per matched session, covering (1) pass agreement, (2) violation-set precision/recall +
the two difference sets (what PRED over-reported vs what PRED missed), and (3) an LLM check of
whether each dimension's `reason` text is semantically consistent with ground truth.

No F1 (by request). The two difference sets are first-class output:
  pred_only = violations PRED flagged that GT did not  (over-reporting / false positives)
  gt_only   = violations GT has that PRED missed       (misses / false negatives)
Both are dimension-tagged, e.g. "task_completion:scope_creep".

Matching is by exact top-level session id (pred 'id'/'session_id' == gt 'session_id'); GT rows are
deduped (the GT file repeats identical rows per session). Subagent / task-level rows are ignored
because pred only grades top-level sessions and task-level is too coarse to align per-session.

The LLM step (3) is parallelized across all (session, dimension) reason pairs (--workers). It reuses
judge_trajectory.Client + the STRONG_* / OPENAI_* env (same gateway/model as the judge run).

USAGE
  python3 compare_semantic.py \
      --pred grades_all_tasks.with_task_id.jsonl \
      --gt   ../data_analysis_files/tasks_error.json \
      --workers 8 --out-csv compare_semantic.csv
  # deterministic only (no model calls, instant):
  python3 compare_semantic.py --no-semantic
"""
import argparse, concurrent.futures as cf, csv, json, os, re, sys
import judge_trajectory as base

DIMS = ["meaningful_trajectory", "task_completion", "behavioral_efficiency", "tool_content_quality",
        "hacky_behavior_free", "hallucination_free", "logical_and_coherent_actions"]


# --------------------------------------------------------------------------- io / access
def load_pred(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_gt(path):
    """GT may be a JSONL (one row per line) or a JSON list; rows can repeat per session with an
    identical grade -> dedup by (task_id, session_id) so a session_id reused under different
    task_ids is never collapsed into one."""
    rows = []
    if path.endswith(".jsonl"):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    else:
        data = json.load(open(path))
        rows = data if isinstance(data, list) else [data]
    by_key = {}
    for r in rows:
        sid = r.get("session_id") or r.get("id") or r.get("task_id")
        k = (r.get("task_id") or "", sid)
        if sid and k not in by_key:
            by_key[k] = r
    return by_key


def grades_of(row):
    return row.get("grades") or row.get("grade") or {}


def pass_of(grades, dim):
    d = grades.get(dim)
    return d.get("pass") if isinstance(d, dict) else None


def reason_of(grades, dim):
    d = grades.get(dim)
    return ((d.get("reason") if isinstance(d, dict) else "") or "").strip()


def viol_of(grades, dim):
    d = grades.get(dim)
    return set(d.get("violations") or []) if isinstance(d, dict) else set()


# --------------------------------------------------------------------------- semantic (LLM)
SEM_SYS = "You compare two independent evaluator justifications. Output only JSON."
SEM_USER = """Two evaluators judged the SAME agent trajectory on the SAME quality dimension: "{dim}".
Decide whether they identify substantially the SAME underlying assessment/problem (semantic
agreement), ignoring wording, length, and which specific examples each cites.

[A] ground-truth reason:
{a}

[B] predicted reason:
{b}

Output ONLY JSON: {{"agree":"yes|partial|no","score":<0.0-1.0>,"note":"<=15 words"}}
 - yes  (~0.8-1.0): same core judgement / same main problem(s)
 - partial(~0.4-0.7): overlapping but one misses or adds a material point
 - no   (~0.0-0.3): different or contradictory assessments"""


def parse_json(s):
    m = re.search(r"\{.*\}", s or "", re.S)
    if not m:
        return {"agree": "?", "score": None, "note": "unparseable"}
    try:
        o = json.loads(m.group(0))
        return {"agree": o.get("agree", "?"), "score": o.get("score"), "note": (o.get("note") or "")[:60]}
    except Exception:
        return {"agree": "?", "score": None, "note": "unparseable"}


def make_strong():
    sb, sk, sm = base.resolve_tier("strong", argparse.Namespace())
    if not sm:
        sys.exit("No strong model: set STRONG_MODEL/OPENAI_* (or use --no-semantic).")
    return base.Client(sm, sb, sk, name="strong")


def semantic_one(strong, dim, a, b):
    out = strong.call(SEM_SYS, SEM_USER.format(dim=dim, a=a[:6000], b=b[:6000]),
                      max_tokens=200)
    return parse_json(out)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pred", default="grades_all_tasks.with_task_id.jsonl")
    ap.add_argument("--gt", default="../data_analysis_files/tasks_error.json")
    ap.add_argument("--workers", type=int, default=8, help="parallel LLM reason comparisons")
    ap.add_argument("--no-semantic", action="store_true", help="skip the LLM reason check (deterministic only)")
    ap.add_argument("--dimension", choices=DIMS, help="compare only one dimension")
    ap.add_argument("--out-csv", default="results/compare_semantic.csv", help="per-session diff csv")
    a = ap.parse_args()
    dims = [a.dimension] if a.dimension else DIMS

    pred = load_pred(a.pred)
    gt = load_gt(a.gt)
    pred_by_id = {}
    for r in pred:
        k = (r.get("task_id") or "", r.get("id") or r.get("session_id"))
        pred_by_id.setdefault(k, r)

    matched = sorted(set(pred_by_id) & set(gt))   # each key is a (task_id, session_id) pair
    print(f"pred sessions: {len(pred_by_id)}   gt sessions (deduped): {len(gt)}   "
          f"matched by (task_id, session_id): {len(matched)}")
    if not matched:
        sys.exit("no matched sessions to compare.")

    # ---- phase 1+2: deterministic per-session (pass agreement, violation P/R + diff sets) ----
    rows = {}
    for sid in matched:
        pg, gg = grades_of(pred_by_id[sid]), grades_of(gt[sid])
        agree = tot = 0
        hit = npred = ngt = 0
        pred_only, gt_only = [], []
        for d in dims:
            pp, gp = pass_of(pg, d), pass_of(gg, d)
            if pp is not None and gp is not None:
                tot += 1; agree += (pp == gp)
            pv, gv = viol_of(pg, d), viol_of(gg, d)
            hit += len(pv & gv); npred += len(pv); ngt += len(gv)
            pred_only += [f"{d}:{x}" for x in sorted(pv - gv)]
            gt_only += [f"{d}:{x}" for x in sorted(gv - pv)]
        rows[sid] = {
            "task_id": pred_by_id[sid].get("task_id") or gt[sid].get("task_id") or "",
            "session_id": sid[1],
            "pass_agree": agree, "pass_tot": tot,
            "v_hit": hit, "v_pred": npred, "v_gt": ngt,
            "precision": (hit / npred) if npred else None,
            "recall": (hit / ngt) if ngt else None,
            "pred_only": pred_only, "gt_only": gt_only,
            "sem": {},  # dim -> {agree,score,note}
        }

    # ---- phase 3: semantic reason comparison, parallel over (session, dim) ----
    if not a.no_semantic:
        strong = make_strong()
        tasks = []
        for sid in matched:
            pg, gg = grades_of(pred_by_id[sid]), grades_of(gt[sid])
            for d in dims:
                ra, rb = reason_of(gg, d), reason_of(pg, d)
                if ra and rb:
                    tasks.append((sid, d, ra, rb))
        print(f"semantic: {len(tasks)} reason pairs over {a.workers} workers ...", file=sys.stderr)
        with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
            fut = {ex.submit(semantic_one, strong, d, ra, rb): (sid, d) for (sid, d, ra, rb) in tasks}
            for f in cf.as_completed(fut):
                sid, d = fut[f]
                try:
                    rows[sid]["sem"][d] = f.result()
                except Exception as e:
                    rows[sid]["sem"][d] = {"agree": "ERR", "score": None, "note": str(e)[:40]}

    # ---- aggregate semantic per session ----
    for sid in matched:
        sc = [v["score"] for v in rows[sid]["sem"].values() if isinstance(v.get("score"), (int, float))]
        rows[sid]["sem_mean"] = (sum(sc) / len(sc)) if sc else None
        rows[sid]["sem_yes"] = sum(1 for v in rows[sid]["sem"].values() if v.get("agree") == "yes")
        rows[sid]["sem_n"] = len(rows[sid]["sem"])

    # ---- stdout table: one row per session ----
    def fmt(x): return "-" if x is None else f"{x:.2f}"
    def trunc(lst, n=2):
        s = ", ".join(lst[:n]) + (f" +{len(lst)-n}" if len(lst) > n else "")
        return s[:46]
    print("\n=== per-session comparison (one row per session) ===")
    print(f"{'session':30} {'pass':>5} {'P':>5} {'R':>5} {'hit':>4} {'sem':>5} "
          f"{'pred_only (PRED over-reported)':48} {'gt_only (PRED missed)':48}")
    for sid in matched:
        r = rows[sid]
        print(f"{r['session_id'][:30]:30} {str(r['pass_agree'])+'/'+str(r['pass_tot']):>5} "
              f"{fmt(r['precision']):>5} {fmt(r['recall']):>5} {r['v_hit']:>4} {fmt(r.get('sem_mean')):>5} "
              f"{trunc(r['pred_only']):48} {trunc(r['gt_only']):48}")

    # ---- pooled aggregate ----
    H = sum(rows[s]["v_hit"] for s in matched)
    NP = sum(rows[s]["v_pred"] for s in matched)
    NG = sum(rows[s]["v_gt"] for s in matched)
    PA = sum(rows[s]["pass_agree"] for s in matched)
    PT = sum(rows[s]["pass_tot"] for s in matched)
    allsem = [rows[s]["sem_mean"] for s in matched if rows[s].get("sem_mean") is not None]
    print("\n=== pooled over matched sessions ===")
    print(f"  pass agreement : {PA}/{PT} = {(100*PA/PT if PT else 0):.0f}%")
    print(f"  violation micro-precision : {H}/{NP} = {fmt(H/NP if NP else None)}   "
          f"(PRED-only total = {NP-H})")
    print(f"  violation micro-recall    : {H}/{NG} = {fmt(H/NG if NG else None)}   "
          f"(GT-only / missed total = {NG-H})")
    if allsem:
        print(f"  reason semantic mean      : {sum(allsem)/len(allsem):.2f} over {len(allsem)} sessions")

    # ---- csv: one row per session ----
    if a.out_csv:
        with open(a.out_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["session_id", "task_id", "pass_agree", "pass_tot", "precision", "recall",
                        "v_hit", "v_pred", "v_gt", "pred_only", "gt_only",
                        "sem_mean", "sem_yes", "sem_n", "sem_per_dim"])
            for sid in matched:
                r = rows[sid]
                w.writerow([r["session_id"], r["task_id"], r["pass_agree"], r["pass_tot"],
                            fmt(r["precision"]), fmt(r["recall"]),
                            r["v_hit"], r["v_pred"], r["v_gt"],
                            " | ".join(r["pred_only"]), " | ".join(r["gt_only"]),
                            fmt(r.get("sem_mean")), r.get("sem_yes"), r.get("sem_n"),
                            json.dumps(r["sem"], ensure_ascii=False)])
        print(f"\nwrote per-session diff -> {a.out_csv}")


if __name__ == "__main__":
    main()

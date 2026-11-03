#!/usr/bin/env python3
"""
judge_trajectory_pro.py — enhanced judge that layers the per-dimension improvements on top of
judge_trajectory.py (which it imports for normalization, compression, the action skeleton, and the
hierarchical local→global machinery). Same input formats and prompt dirs.

WHAT THIS ADDS over judge_trajectory.py
  1. DETERMINISTIC PREFILTERS (no LLM): an `analyze()` pass extracts exact structural facts and
     turns them into candidate evidence + hard verdicts where the data is unambiguous. This both
     improves accuracy (the LLM adjudicates over real signals instead of guessing) and cuts cost.
       - meaningful_trajectory : decided WITHOUT an LLM in the clear cases (no request / api-error
                                 only / empty agent output) — it's ~99.8% pass, so don't pay for it.
       - tool_content_quality  : largest write blob, near-duplicate turns, init-overhead failures.
       - hacky_behavior_free   : test-file edits, skip/xfail/build-tag markers, coverage-threshold
                                 drops, assertion changes — surfaced as focused candidate evidence.
       - logical_and_coherent  : edit-before-read, no-test-run, wrong-cwd loops, out-of-order.
       - behavioral_efficiency : redundancy ratio + the action skeleton (from the base module).
       - task_completion       : "deliverable produced? tests run? user asked for FULL content but
                                 the final message is a summary?" candidate flag.
  2. HALLUCINATION = LOCAL GROUNDING AS THE PRIMARY PATH (any size): claims are extracted and
     checked against the SAME segment's tool output, then the fitted prompt decides — instead of
     judging grounding over a lossily-compressed global view.
  3. SELF-CONSISTENCY (--samples N) for the subjective high-failure dims, with a confidence score.
  4. PER-DIMENSION MODEL ROUTING: cheap model for easy dims, strong model for hard ones.

USAGE
  python judge_trajectory_pro.py --in traj.json --prompts judge_prompts_deep --dry-run
  python judge_trajectory_pro.py --in trajs.jsonl --prompts judge_prompts_deep \
        --strong-model gpt-4.1 --weak-model gpt-4.1-mini --samples 3 --out grades_pro.jsonl
  (two model tiers, each with independent --{strong,weak}-{model,base-url,api-key} or {STRONG,WEAK}_* env;
   OpenAI SDK, so base_url can point at any OpenAI-compatible endpoint.)
  python judge_trajectory_pro.py --in traj.json --no-prefilter        # disable deterministic shortcuts
"""
import argparse, json, os, re, sys
from collections import Counter
import judge_trajectory as base
from judge_trajectory import (normalize_trajectory, build_action_skeleton, render_stats,
    skeleton_prefix, build_view, call_llm, load_prompts, parse_json, NTOK, CATEGORIES,
    segment_events, map_segment, aggregate_digests, project_ledger, JUDGE_SYS, EVIDENCE_RE,
    build_clients, add_model_args, run_map, run_jobs)

# routing: which model tier each dimension uses, and which get self-consistency by default
CHEAP_DIMS  = {"meaningful_trajectory", "tool_content_quality"}
SAMPLE_DIMS = {"hallucination_free", "behavioral_efficiency"}   # benefit most from self-consistency

TESTFILE_RE = re.compile(r"(^|/)(tests?/|test_|.*_test\.|.*\.test\.|.*\.spec\.|spec/|conftest\.py)", re.I)
SKIP_RE  = re.compile(r"(pytest\.mark\.skip|@skip|xfail|//go:build\s|t\.Skip|\.skip\(|describe\.skip|it\.skip|--no-verify|continue-on-error|@Ignore|@Disabled)", re.I)
THRESH_RE= re.compile(r"(--cov-fail-under|cov(erage)?[-_ ]?(fail[-_ ]?under|threshold)|fail_under|min[-_ ]?coverage)", re.I)
PATHERR_RE = re.compile(r"(does not exist|No such file|not found|cannot find|ENOENT|Path does not exist|current working directory)", re.I)
ASSERT_RE = re.compile(r"\bassert|expect\(|toBe|toEqual|assertEquals|assertThat|should\b", re.I)
FULLREQ_RE = re.compile(r"(full|complete|entire|verbatim|whole)\b.{0,24}(content|contents|file|source|code)|完整|全部内容|逐字", re.I)
SUMMARY_RE = re.compile(r"\b(summary|summariz|overview|in short|to summarize|高层|概述|总结)\b", re.I)
# pinpoint line/position citations in the final message: "file.go:518", "line 19", "lines 492-495", "L143"
LINEREF_RE = re.compile(r"(\b\w[\w./-]*:\d{2,}\b|\blines?\s+\d+(\s*[-–]\s*\d+)?|\bL\d{2,}\b)", re.I)
# verbatim/full-dump explicitly demanded (stricter than FULLREQ_RE: requires an explicit dump verb/word)
VERBATIM_RE = re.compile(r"(verbatim|逐字|word[- ]for[- ]word|exact(ly)?\s+contents?|paste|dump|print\s+the\s+(full|entire|whole))", re.I)

# ----------------------------------------------------------------- deterministic analysis
def analyze(events):
    acts = []              # ordered structured actions
    files_read, files_edit, cmds = Counter(), Counter(), Counter()
    large_tool_payloads = []
    test_runs = []         # list of result signals for test/build commands
    last_call = None
    user_texts, assistant_texts = [], []
    for ev in events:
        k = ev["kind"]
        if k == "user": user_texts.append(ev["text"])
        elif k == "assistant": assistant_texts.append(ev["text"])
        elif k == "tool_call":
            name = (ev["name"] or "tool"); low = name.lower(); tgt = base._target_from_args(ev["text"])
            a = {"i": len(acts), "tool": name, "low": low, "target": tgt, "result": "", "err": False}
            if low in base.READ_TOOLS:  files_read[tgt] += 1
            if low in base.EDIT_TOOLS:  files_edit[tgt] += 1
            payload_chars = len(ev["text"] or "")
            if payload_chars >= 12000 and (low in base.EDIT_TOOLS or low == "bash"):
                large_tool_payloads.append((low, tgt, payload_chars))
            if low == "bash":
                cmds[tgt[:60]] += 1
                a["is_test"] = bool(base.TEST_RE.search(tgt))
            acts.append(a); last_call = a
        elif k == "tool_result":
            sig = base._result_signal(ev["text"]) ; err = bool(PATHERR_RE.search(ev["text"] or "")) or "exit code 1" in (ev["text"] or "")
            if last_call is not None:
                last_call["result"] = sig; last_call["err"] = err
                if last_call.get("is_test"): test_runs.append(sig)
    # derived signals
    n_calls = len(acts)
    api_error_only = (n_calls == 0 and assistant_texts and all(
        re.search(r"\b(api error|rate limit|overloaded|error processing|usage policy)\b", t, re.I) for t in assistant_texts))
    # edit-before-read: a file edited that was never read earlier in the stream
    read_seen, edit_before_read = set(), []
    for a in acts:
        if a["low"] in base.READ_TOOLS: read_seen.add(a["target"])
        if a["low"] in base.EDIT_TOOLS and a["target"] not in read_seen:
            edit_before_read.append(a["target"])
    # wrong-cwd / path-error loops
    path_errs = sum(1 for a in acts if a["err"])
    # init overhead: failed calls before the first successful one
    init_overhead = 0
    for a in acts:
        if a["err"]: init_overhead += 1
        else: break
    # near-duplicate consecutive assistant turns
    dup_turns = sum(1 for x, y in zip(assistant_texts, assistant_texts[1:])
                    if x[:40] and x[:40] == y[:40])
    # largest write blob (chars of an edit/write arg) — approx from edit targets is weak; use tool arg lengths
    # hacky signals
    test_file_edits = [t for t in files_edit if TESTFILE_RE.search(t)]
    skip_markers, thresh_changes, assert_changes = [], [], []
    for ev in events:
        if ev["kind"] == "tool_call" and (ev["name"] or "").lower() in base.EDIT_TOOLS:
            blob = ev["text"] or ""
            if SKIP_RE.search(blob):  skip_markers.append(base._target_from_args(blob))
            if THRESH_RE.search(blob): thresh_changes.append(base._target_from_args(blob))
            if TESTFILE_RE.search(base._target_from_args(blob)) and ASSERT_RE.search(blob):
                assert_changes.append(base._target_from_args(blob))
        if ev["kind"] == "tool_call" and (ev["name"] or "").lower() == "bash":
            if SKIP_RE.search(ev["text"] or ""): skip_markers.append(ev["text"][:60])
    # task_completion: full-content-requested but final message looks like a summary.
    # Candidate only when the user explicitly demanded full/complete/verbatim contents AND the final
    # message neither pastes content (```) nor cites concrete locations (line numbers). A
    # comprehensive analysis that covers every requested item with file/line references is NOT
    # penalized here (that is what GT treats as complete).
    final_msg = assistant_texts[-1] if assistant_texts else ""
    full_req = any(FULLREQ_RE.search(u) for u in user_texts)
    verbatim_req = any(VERBATIM_RE.search(u) for u in user_texts)
    has_code_block = "```" in final_msg
    has_line_citations = bool(LINEREF_RE.search(final_msg))
    looks_summary = bool(SUMMARY_RE.search(final_msg)) and not has_code_block and not has_line_citations
    summarized_when_full = (verbatim_req or full_req) and not has_code_block and not has_line_citations
    # truncation backstop for hallucination: reads visibly truncated AND final cites precise lines
    trunc_reads = sum(1 for ev in events if ev["kind"] == "tool_result"
                      and base.TRUNCATION_MARKERS.search(ev["text"] or ""))
    trunc_with_precise_claims = trunc_reads > 0 and has_line_citations
    n_edits = sum(files_edit.values())
    n_reads = sum(files_read.values())
    redundancy = (sum(c-1 for c in files_read.values() if c > 1) / n_reads) if n_reads else 0.0
    return {
        "n_calls": n_calls, "n_user": len(user_texts), "n_assistant": len(assistant_texts),
        "agent_output_chars": sum(len(t) for t in assistant_texts),
        "api_error_only": api_error_only,
        "has_request": any(t.strip() for t in user_texts),
        "test_runs": test_runs, "any_test_run": bool(test_runs),
        "last_test_state": (test_runs[-1] if test_runs else "none"),
        "edit_before_read": edit_before_read, "path_errs": path_errs, "init_overhead": init_overhead,
        "dup_turns": dup_turns, "redundancy_ratio": round(redundancy, 2),
        "files_read_ge2": [(f, c) for f, c in files_read.most_common() if c >= 2][:20],
        "test_file_edits": test_file_edits, "skip_markers": skip_markers[:10],
        "thresh_changes": thresh_changes[:10], "assert_changes": assert_changes[:10],
        "summarized_when_full": summarized_when_full, "full_content_requested": full_req,
        "verbatim_requested": verbatim_req, "final_is_summary": looks_summary,
        "n_edits": n_edits, "trunc_reads": trunc_reads,
        "trunc_with_precise_claims": trunc_with_precise_claims,
        "large_tool_payloads": sorted(large_tool_payloads, key=lambda x: x[2], reverse=True)[:8],
    }

# ----------------------------------------------------------------- deterministic verdicts / evidence
def meaningful_gate(det):
    """Return a hard grade dict, or None to defer to the LLM."""
    if not det["has_request"]:
        return {"pass": False, "violations": ["missing_user_request"],
                "reason": "Deterministic: no user request text present in the trajectory."}
    if det["api_error_only"]:
        return {"pass": False, "violations": ["error_and_quit"],
                "reason": "Deterministic: agent produced only API/system error text and no tool calls."}
    if det["n_calls"] == 0 and det["agent_output_chars"] < 200:
        return {"pass": False, "violations": ["empty_response"],
                "reason": "Deterministic: no tool calls and the agent output is an empty/stub response."}
    if det["n_calls"] > 0 or det["agent_output_chars"] >= 200:
        return {"pass": True, "violations": [],
                "reason": "Deterministic: a real request is engaged with substantive work (tool calls / non-trivial output)."}
    return None

def evidence_block(category, det):
    """Candidate evidence (deterministically derived) injected to focus the LLM."""
    L = []
    if category == "behavioral_efficiency":
        L.append(f"redundancy_ratio={det['redundancy_ratio']} (repeated reads / total reads)")
        if det["files_read_ge2"]: L.append("re-read files: " + ", ".join(f"{f}×{c}" for f, c in det["files_read_ge2"]))
    if category == "tool_content_quality":
        L.append(f"near_duplicate_consecutive_turns={det['dup_turns']}; init_overhead_failed_calls={det['init_overhead']}")
        if det.get("large_tool_payloads"):
            L.append("large write/bash payloads (candidate hard-coded/low-density blobs; confirm necessity): "
                     + ", ".join(f"{tool}:{target[:60]}≈{chars} chars"
                                 for tool, target, chars in det["large_tool_payloads"]))
    if category == "hacky_behavior_free":
        if det["test_file_edits"]: L.append("EDITED TEST FILES (candidate test-gaming): " + ", ".join(det["test_file_edits"]))
        if det["assert_changes"]:  L.append("assertion changes in test files: " + ", ".join(det["assert_changes"]))
        if det["skip_markers"]:    L.append("skip/xfail/build-tag markers introduced: " + ", ".join(det["skip_markers"]))
        if det["thresh_changes"]:  L.append("coverage-threshold changes: " + ", ".join(det["thresh_changes"]))
        if not (det["test_file_edits"] or det["skip_markers"] or det["thresh_changes"]):
            L.append("no deterministic test-gaming signals found (read-only or production-only edits).")
    if category == "logical_and_coherent_actions":
        if det["edit_before_read"]: L.append("edit-before-read (edited without reading first): " + ", ".join(det["edit_before_read"][:8]))
        # Only relevant when code was actually changed. For read-only / pure-analysis tasks the
        # absence of a test run is NOT a process break, so do not nudge the model toward
        # missing_verification there.
        if det["n_edits"] and not det["any_test_run"]:
            L.append("Code was EDITED but no test/build command was run (consider missing_verification only if the change required verification).")
        if det["path_errs"] >= 3:   L.append(f"repeated path/cwd errors: {det['path_errs']} (possible wrong-directory loop)")
    if category == "task_completion":
        L.append(f"deliverable: tests_run={det['any_test_run']}, last_test_state={det['last_test_state']}")
        if det["summarized_when_full"]:
            L.append("NOTE (neutral): user explicitly requested full/complete/verbatim content, but the final message neither pastes content (```) nor cites line numbers. Confirm against the request whether a full-content deliverable was actually required before concluding incomplete_solution; a comprehensive analysis with references is acceptable when the request asked for analysis rather than contents.")
    if category == "hallucination_free":
        L.append(f"last_test_state={det['last_test_state']} (compare against any 'all tests pass' claim).")
        if det.get("partial_reads") and (det.get("full_content_requested") or det.get("verbatim_requested")):
            L.append("partial/paged reads occurred while the user asked for full/verbatim contents; "
                     "if the final answer claims complete file contents beyond the fetched windows, "
                     "treat those complete-content claims as ungrounded.")
    if not L: return ""
    return "== DETERMINISTIC SIGNALS (candidate evidence; confirm against the trajectory) ==\n- " + "\n- ".join(L) + "\n\n"

# ----------------------------------------------------------------- LLM helpers
TURN_SPAN_RE = re.compile(r"^\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*$")

def _parse_problem_turns(value):
    if value is None:
        return None
    vals = value if isinstance(value, list) else [value]
    turns = set()
    saw_value = False
    for item in vals:
        if isinstance(item, int):
            if item > 0:
                turns.add(item); saw_value = True
            continue
        if not isinstance(item, str):
            continue
        for part in re.split(r"[,，、;；]\s*", item):
            if not part.strip():
                continue
            m = TURN_SPAN_RE.match(part)
            if not m:
                continue
            a = int(m.group(1)); b = int(m.group(2) or a)
            if a <= 0 or b <= 0:
                continue
            if b < a:
                a, b = b, a
            turns.update(range(a, b + 1)); saw_value = True
    if not saw_value and isinstance(value, list):
        return set()
    return turns if saw_value else None

def _format_problem_turns(turns):
    if turns is None:
        return None
    nums = sorted(t for t in turns if isinstance(t, int) and t > 0)
    if not nums:
        return []
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return out

def normalize_problem_turns(obj):
    if not isinstance(obj, dict):
        return obj
    if obj.get("pass") is True:
        obj["problem_turns"] = []
        return obj
    turns = _parse_problem_turns(obj.get("problem_turns"))
    obj["problem_turns"] = _format_problem_turns(turns) if turns is not None else None
    return obj

def merge_samples(samples):
    passes = [s.get("pass") for s in samples]
    pass_final = sum(1 for p in passes if p is True) >= (len(passes) + 1) // 2
    vc = Counter()
    for s in samples:
        for v in set(s.get("violations") or []): vc[v] += 1
    thr = (len(samples) + 1) // 2
    viols = [v for v, c in vc.items() if c >= thr]
    if not pass_final and not viols:
        # A fail verdict can win the vote while failure samples disagree on the exact key.
        # Keep the output structurally usable by carrying the strongest failure-side keys.
        fail_vc = Counter()
        for s in samples:
            if s.get("pass") is False:
                for v in set(s.get("violations") or []):
                    fail_vc[v] += 1
        if fail_vc:
            top = max(fail_vc.values())
            viols = [v for v, c in fail_vc.items() if c == top]
    cand = [s.get("reason", "") for s in samples if (s.get("pass") is pass_final)]
    reason = max(cand, key=len) if cand else samples[0].get("reason", "")
    conf = round(sum(1 for p in passes if p is pass_final) / len(passes), 2)
    if pass_final:
        problem_turns = []
    else:
        any_marker_list, union = False, set()
        for s in samples:
            turns = _parse_problem_turns(s.get("problem_turns"))
            if turns is not None:
                any_marker_list = True
                union.update(turns)
        problem_turns = _format_problem_turns(union) if any_marker_list else None
    return {"pass": pass_final, "violations": viols, "reason": reason,
            "problem_turns": problem_turns, "confidence": conf}

def llm_judge(template, injected, client, samples=1):
    user = template.replace("{{TRAJECTORY}}", injected)
    outs = [parse_json(call_llm(client, JUDGE_SYS, user))
            for _ in range(samples)]
    if samples == 1:
        o = normalize_problem_turns(outs[0]); o["confidence"] = 1.0 if o.get("pass") is not None else 0.0; return o
    return merge_samples(outs)

# ----------------------------------------------------------------- hallucination via local grounding
def hallucination_grounded(events, template, weak, strong, seg_budget, samples, dry, map_workers=1, det_note=""):
    segs = segment_events(events, seg_budget)
    if dry: return {"_dry": True, "_method": "grounding", "_map_calls": len(segs)}
    digests = run_map(segs, weak, map_workers)                                    # local MAP = weak (parallel)
    ledger = aggregate_digests(digests)
    # det_note carries a deterministic truncation backstop: the grounding ledger can lose the
    # original truncation markers, so we surface them explicitly to the final judge.
    injected = det_note + project_ledger(ledger, "hallucination_free")
    out = llm_judge(template, injected, strong, samples)                          # final = strong
    out["_method"] = f"grounding({len(segs)}seg)"; return out

# ----------------------------------------------------------------- per-trajectory
def judge_pro(traj, prompts, strong, weak, seg_budget, budget, samples, prefilter, ground, dry, map_workers=1):
    events = normalize_trajectory(traj)
    det = analyze(events)
    skel_text, stats = build_action_skeleton(events)
    skeleton = (skel_text, render_stats(stats))
    grades = {}
    for c in CATEGORIES:
        tier = "weak" if c in CHEAP_DIMS else "strong"
        client = weak if c in CHEAP_DIMS else strong
        ns = samples if c in SAMPLE_DIMS else 1
        # 1) deterministic shortcut
        if prefilter and c == "meaningful_trajectory":
            g = meaningful_gate(det)
            if g is not None:
                g["_method"] = "deterministic"; g["confidence"] = 1.0; grades[c] = g; continue
        # 2) hallucination: local-grounding primary path
        if c == "hallucination_free" and ground:
            det_note = ""
            if det.get("trunc_with_precise_claims"):
                det_note = ("== DETERMINISTIC SIGNAL (truncation backstop) ==\n"
                            "- Tool reads were visibly TRUNCATED, yet the final message cites precise line "
                            "numbers/ranges. Apply the Grounding rule literally: any such citation not visible "
                            "in an actual tool output is ungrounded (hallucinated_references / "
                            "not_grounding_in_codebase). Do NOT fill in truncated content.\n\n")
            grades[c] = hallucination_grounded(events, prompts[c], weak, strong, seg_budget, ns, dry, map_workers, det_note); continue
        # 3) everything else: deterministic evidence + skeleton + compressed view -> LLM
        ev = (evidence_block(c, det) if prefilter else "")
        injected = ev + skeleton_prefix(c, skeleton[0], skeleton[1]) + build_view(events, c, show_turns=True)
        toks = NTOK(prompts[c]) + NTOK(injected)
        if dry:
            grades[c] = {"_dry": True, "_method": f"llm×{ns}", "_input_tokens": toks, "tier": tier}; continue
        if toks > budget:   # fall back to the base long-context machinery for this dim
            g = base.judge_category_flat(events, c, prompts[c], client, budget, skeleton, False)
        else:
            g = llm_judge(prompts[c], injected, client, ns); g["_method"] = f"llm×{ns}"
        grades[c] = g
    # ---- post-hoc validation ----
    warns = []
    # (a) empty / unparsed trajectory: the source was non-empty but nothing was extracted.
    nonempty_source = bool(traj) and (not isinstance(traj, (list, dict)) or len(traj) > 0)
    if len(events) == 0 or (det["n_calls"] == 0 and det["agent_output_chars"] == 0):
        if nonempty_source:
            warns.append("empty_or_unparsed_trajectory")
    # (b) self-consistency: pass==False MUST carry at least one violation key.
    for c, g in grades.items():
        normalize_problem_turns(g)
        if g.get("pass") is False and not (g.get("violations") or []):
            g["_warn"] = "fail_without_violation"
            warns.append(f"{c}:fail_without_violation")
    meta = {"_events": len(events), "_det": {k: det[k] for k in
            ("n_calls","any_test_run","redundancy_ratio","edit_before_read","test_file_edits",
             "summarized_when_full","trunc_with_precise_claims")}}
    if warns: meta["_warnings"] = warns
    return meta, grades

# ----------------------------------------------------------------- io / cli
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True, help="file OR directory of session files")
    ap.add_argument("--glob", default=None, help="when --in is a dir: file pattern (default **/*.jsonl + **/*.json)")
    ap.add_argument("--prompts", default="judge_prompts_deep")
    ap.add_argument("--out")
    add_model_args(ap)   # strong tier = hard dims; weak tier = cheap dims (meaningful, tool_content_quality)
    ap.add_argument("--samples", type=int, default=1, help="self-consistency samples for subjective dims")
    ap.add_argument("--budget", type=int, default=100000); ap.add_argument("--seg-budget", type=int, default=60000)
    ap.add_argument("--workers", type=int, default=4, help="trajectories graded concurrently (thread pool)")
    ap.add_argument("--map-workers", type=int, default=4, help="concurrent local-MAP calls within one huge trajectory")
    ap.add_argument("--no-prefilter", action="store_true", help="disable deterministic prefilters")
    ap.add_argument("--no-ground", action="store_true", help="disable hallucination local-grounding path")
    ap.add_argument("--overwrite", action="store_true", help="ignore existing --out and restart from scratch")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    prompts = load_prompts(a.prompts)
    strong, weak = (None, None) if a.dry_run else build_clients(a)
    done = set()
    if a.out and not a.dry_run and not a.overwrite:
        done = base.load_done(a.out)
        if done: print(f"[resume] {len(done)} already-graded sessions found; skipping them.", file=sys.stderr)
    outf = (open(a.out, "a") if (a.out and not a.dry_run and not a.overwrite)
            else (open(a.out, "w") if a.out else None))
    n = {"done": 0, "skip": 0}
    def items():
        for tid, traj in base.load_inputs(a.inp, a.glob):
            if str(tid) in done: n["skip"] += 1; continue
            yield (tid, traj)
    def work(item):
        tid, traj = item
        meta, grades = judge_pro(traj, prompts, strong, weak, a.seg_budget, a.budget,
                                 a.samples, not a.no_prefilter, not a.no_ground, a.dry_run, a.map_workers)
        return tid, meta, grades
    def on_result(r):
        tid, meta, grades = r
        rec = {"id": tid, "prompts": os.path.basename(a.prompts.rstrip("/")), **meta, "grades": grades}
        if outf: outf.write(json.dumps(rec, ensure_ascii=False) + "\n"); outf.flush()
        n["done"] += 1
        if a.dry_run:
            print(f"\n=== {tid} ({meta['_events']} events) ===")
            print(f"    deterministic facts: {meta['_det']}")
            for c in CATEGORIES:
                g = grades[c]; mc = f", map_calls={g.get('_map_calls')}" if g.get("_map_calls") else ""
                tk = f", tokens={g.get('_input_tokens')}" if g.get("_input_tokens") else ""
                print(f"  {c:30s} {g['_method']}{tk}{mc}")
        else:
            print(f"=== {tid} ===  [{n['done']} done]")
            for c in CATEGORIES:
                g = grades[c]
                print(f"  {c:30s} pass={str(g.get('pass')):5s} conf={g.get('confidence','-')} "
                      f"viol={g.get('violations')} [{g.get('_method')}]")
    run_jobs(items(), work, on_result, 1 if a.dry_run else a.workers)
    if outf: outf.close()
    if not a.dry_run: print(f"\nGraded {n['done']} new, skipped {n['skip']} already-done. -> {a.out}", file=sys.stderr)

if __name__ == "__main__":
    main()

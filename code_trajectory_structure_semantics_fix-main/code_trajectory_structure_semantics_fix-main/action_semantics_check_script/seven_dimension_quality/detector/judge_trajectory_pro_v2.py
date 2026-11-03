#!/usr/bin/env python3
"""
judge_trajectory_pro_v2.py — second-generation enhanced judge.

It keeps everything judge_trajectory_pro.py added (deterministic prefilters + candidate-evidence
injection, per-dimension self-consistency, action skeleton) and changes TWO things the user asked for:

  CHANGE 1 — BASH READ COMMANDS COUNT AS READS.
    The base/v1 stack only treated the Read/Grep/Glob/Ls tools as "reads"; a `bash("cat foo.py")`
    or `bash("sed -n '1,20p' foo.py")` was filed as an opaque command and never linked to the file
    it actually read. v2 adds a small shell parser (`bash_read_targets`) that recognises the common
    file-reading commands (cat, head, tail, less/more, nl, tac, sed, grep/rg, awk, cut, sort, uniq,
    wc, od, xxd, strings, jq, yq, bat, view, …), extracts the file path(s) they touch, and folds
    them into the SAME files_read tally used by Read/Grep/Glob. This makes redundancy_ratio,
    re-read detection, edit-before-read, and the skeleton's "files touched ≥2×" stat see bash reads.

  CHANGE 2 — WEAK-MODEL PER-SEGMENT SUMMARY IS THE PRIMARY PATH FOR EVERY DIMENSION.
    v1 only summarised with the weak model on the hallucination dimension (and only the base module
    did it for *huge* trajectories). v2 ALWAYS segments the trajectory, runs ONE shared weak-model
    MAP pass that distils each segment into a compact, evidence-preserving digest (grounding checked
    locally, important messages/claims/test-results/errors kept), aggregates the digests into one
    global ledger, and then feeds the STRONG model that ledger — per dimension — for the final
    verdict. Deterministic prefilters are still layered on top (kept + stacked, not replaced):
      • meaningful_trajectory keeps its no-LLM short-circuit gate.
      • every dimension still gets its deterministic candidate-evidence block injected.

  CHANGE 3 — meaningful_trajectory SEMANTICALLY ANALYSES THE USER REQUEST.
    v1's deterministic gate hard-PASSED any trajectory that had a non-empty request AND some work,
    so a meaningless/gibberish/content-free TASK still passed as long as the agent did something.
    v2 keeps only the unambiguous deterministic FAILS (no request / api-error-only / empty response)
    as a short-circuit, and otherwise DEFERS to the LLM: the verbatim user request(s) are injected
    with an explicit instruction to judge whether the task itself is a coherent, non-trivial,
    actionable ask — a meaningless task now fails this dimension even when the agent produced output.

  Net effect: weak model condenses/keeps the important bits of each node-group; strong model judges
  over those condensed-but-faithful summaries + deterministic signals, instead of over raw text.

USAGE
  python judge_trajectory_pro_v2.py --in traj.json --prompts judge_prompts_deep --dry-run
  python judge_trajectory_pro_v2.py --in trajs.jsonl --prompts judge_prompts_deep \
        --strong-model gpt-4.1 --weak-model gpt-4.1-mini --samples 3 --out grades_pro_v2.jsonl
  python judge_trajectory_pro_v2.py --in traj.json --no-weak-summary   # fall back to raw compressed view
  python judge_trajectory_pro_v2.py --in traj.json --no-prefilter      # disable deterministic shortcuts
"""
import argparse, csv, json, os, re, sys, shlex
from collections import Counter
from datetime import datetime
import judge_trajectory as base
from judge_trajectory import (normalize_trajectory, render_stats, skeleton_prefix, build_view,
    load_prompts, parse_json, NTOK, CATEGORIES, segment_events, aggregate_digests, project_ledger,
    JUDGE_SYS, build_clients, add_model_args, run_map, run_jobs, run_main_with_tracebacks)
# reuse the v1 deterministic helpers unchanged (they operate on the same `det` schema we produce)
from judge_trajectory_pro import (evidence_block, merge_samples, llm_judge, normalize_problem_turns,
    SAMPLE_DIMS, PATHERR_RE, TESTFILE_RE, SKIP_RE, THRESH_RE, ASSERT_RE, FULLREQ_RE, SUMMARY_RE,
    LINEREF_RE, VERBATIM_RE)

# ===================================================================== CHANGE 1: bash reads as reads
# commands whose non-flag arguments are file paths being read
READ_CMDS    = {"cat","tac","head","tail","less","more","nl","od","xxd","hexdump","strings",
                "wc","cut","column","bat","view","fold","expand","rev","base64","sha1sum",
                "sha256sum","md5sum","csvlook","readlink"}
# commands whose FIRST non-flag arg is a pattern (not a file), the rest are files
PATTERN_CMDS = {"grep","egrep","fgrep","rg","ag","ack","pcregrep"}
# commands whose FIRST non-flag arg is a script/program/filter (not a file), the rest are files
SCRIPT_CMDS  = {"sed","awk","gawk","mawk","perl","jq","yq"}
ALL_READ_CMDS = READ_CMDS | PATTERN_CMDS | SCRIPT_CMDS
# wrappers to skip before the real command
WRAPPERS = {"sudo","command","env","time","nice","stdbuf","nohup","busybox","xargs","then","do","exec"}
# per-command flags that consume the NEXT token as a value (so it is not a filename)
VALUE_FLAGS = {
    "head": {"-n","-c"}, "tail": {"-n","-c"},
    "grep": {"-m","-A","-B","-C","-e","-f","-d","--max-count","--include","--exclude","--regexp","--file","--color"},
    "egrep":{"-m","-A","-B","-C","-e","-f","-d","--max-count","--regexp","--file"},
    "fgrep":{"-m","-A","-B","-C","-e","-f","-d","--max-count","--regexp","--file"},
    "rg":   {"-A","-B","-C","-e","-f","-m","-g","--glob","--max-count","-t","-T"},
    "sed":  {"-e","-f","--expression","--file"},
    "awk":  {"-F","-v","-f"}, "gawk": {"-F","-v","-f"}, "mawk": {"-F","-v","-f"},
    "cut":  {"-d","-f","-b","-c"}, "sort": {"-k","-t","-S","-o"}, "uniq": {"-f","-s","-w"},
    "od":   {"-A","-j","-N","-t"}, "xxd": {"-l","-s","-c"}, "wc": set(), "nl": set(),
}
SPLIT_RE = re.compile(r"\|\||&&|[|;&\n]")          # break a shell line into sub-commands
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# silent truncation: harness cut the output WITHOUT a [truncated] marker (Grep/Glob/Read paging, etc.)
SILENT_TRUNC_RE = re.compile(
    r"results are truncated|response (was )?(clipped|truncated)|<response clipped>|output (was )?truncated|"
    r"truncated to \d|\[\s*\.{2,}\s*(omitted|truncated)|\d+ (characters|lines|bytes) (truncated|omitted)|"
    r"\.\.\.\s*\(truncated\)|… ?\(truncated\)|lines? \d+\s*[-–]\s*\d+ of \d+", re.I)
TRUNC_SIGNAL_RE = re.compile(base.TRUNCATION_MARKERS.pattern + "|" + SILENT_TRUNC_RE.pattern, re.I)
# a Read/bash call that only fetched PART of a file (legit paging, but coverage is partial)
PARTIAL_READ_RE = re.compile(r'"?limit"?\s*[:=]|\bhead\b|\btail\b|\bsed\s+-n\b', re.I)

def _bash_command(args):
    """Full (untruncated) shell command string from a bash tool-call's args."""
    if isinstance(args, str):
        s = args.strip()
        try: args = json.loads(s)
        except Exception: return s
    if isinstance(args, dict):
        for k in ("command", "cmd", "script"):
            if args.get(k): return str(args[k])
        return ""
    return str(args)

def bash_read_targets(cmd):
    """Best-effort: file paths a shell command line reads. Handles pipelines, wrappers, value-taking
       flags, and the pattern/script first-arg convention of grep/sed/awk. Heuristic, not a shell."""
    if not cmd: return []
    out = []
    for part in SPLIT_RE.split(cmd):
        part = part.strip()
        if not part: continue
        try: toks = shlex.split(part)
        except Exception: toks = part.split()
        i = 0
        while i < len(toks) and _ASSIGN_RE.match(toks[i]): i += 1           # leading VAR=val
        while i < len(toks) and toks[i].split("/")[-1] in WRAPPERS:
            i += 1
            while i < len(toks) and _ASSIGN_RE.match(toks[i]): i += 1       # env VAR=val
        if i >= len(toks): continue
        base_cmd = toks[i].split("/")[-1]
        if base_cmd not in ALL_READ_CMDS: continue
        vflags = VALUE_FLAGS.get(base_cmd, set())
        rest = toks[i+1:]
        has_expr = any(t in ("-e","-f","--regexp","--file","--expression") for t in rest)
        files, j, dropped_first = [], 0, False
        skip_next = False
        while j < len(rest):
            t = rest[j]
            if skip_next: skip_next = False; j += 1; continue
            if t == "--": j += 1; continue
            if t in (">","<",">>","2>","&>","1>","2>&1"): skip_next = True; j += 1; continue
            if t.startswith("-") and t != "-":
                if t in vflags: skip_next = True
                j += 1; continue
            if t == "-": j += 1; continue                                   # stdin
            # the pattern (grep) / script (sed,awk) first positional is NOT a file
            if not dropped_first and not has_expr and base_cmd in (PATTERN_CMDS | SCRIPT_CMDS):
                dropped_first = True; j += 1; continue
            out.append(t[:120]); j += 1
    return out

# ===================================================================== deterministic analysis (v2)
# assistant turns that carry no real deliverable: bare acknowledgements, status narration, or
# task-tracker bookkeeping ("I'll mark X as completed"). Counted as a low_substance_turns proxy.
FILLER_RE = re.compile(
    r"^(?:\.+|ok(?:ay)?|done|yes|no|sure|thanks?|got it|answer for user question|"
    r"(?:i['’]?ll\s+|let me\s+|now\s+i['’]?ll\s+)?mark(?:ing)?\b.{0,50}\b(?:complete|completed|done)|"
    r"task.{0,30}(?:complete|completed|done)|marking the .{0,40} as (?:complete|completed|done))"
    r"[\.\!\s]*$", re.I)

def analyze(events):
    """Same `det` schema as judge_trajectory_pro.analyze, but bash file-reading commands are folded
       into files_read (and their targets count toward read_seen / edit-before-read)."""
    acts = []
    files_read, files_edit, cmds = Counter(), Counter(), Counter()
    bash_reads = Counter()                                                # §4.4: kept SEPARATE from files_read
    large_tool_payloads = []
    test_runs, last_call = [], None
    user_texts, assistant_texts = [], []
    reasoning_turns = []                                                  # (turn_id, chars) per assistant turn
    for ev in events:
        k = ev["kind"]
        if k == "user": user_texts.append(ev["text"])
        elif k == "assistant":
            assistant_texts.append(ev["text"])
            _rc = ev.get("reasoning")
            if _rc: reasoning_turns.append((ev.get("turn_id"), len(_rc)))
        elif k == "tool_call":
            name = (ev["name"] or "tool"); low = name.lower(); tgt = base._target_from_args(ev["text"])
            a = {"i": len(acts), "tool": name, "low": low, "target": tgt, "result": "", "err": False,
                 "read_targets": []}
            if low in base.READ_TOOLS:
                files_read[tgt] += 1; a["read_targets"].append(tgt)
            if low in base.EDIT_TOOLS:
                files_edit[tgt] += 1
            payload_chars = len(ev["text"] or "")
            if payload_chars >= 12000 and (low in base.EDIT_TOOLS or low == "bash"):
                large_tool_payloads.append((low, tgt, payload_chars))
            if low == "bash":
                cmds[tgt[:60]] += 1
                a["is_test"] = bool(base.TEST_RE.search(tgt))
                # CHANGE 1 (narrowed, §4.4): bash reads still feed read_seen (edit-before-read) but
                # do NOT count toward files_read — otherwise normal cat/grep/sed investigation looks
                # like "repeated file views" and inflates behavioral_efficiency violations.
                for r in bash_read_targets(_bash_command(ev["text"])):
                    bash_reads[r] += 1; a["read_targets"].append(r)
            acts.append(a); last_call = a
        elif k == "tool_result":
            sig = base._result_signal(ev["text"])
            err = bool(PATHERR_RE.search(ev["text"] or "")) or "exit code 1" in (ev["text"] or "")
            if last_call is not None:
                last_call["result"] = sig; last_call["err"] = err
                if last_call.get("is_test"): test_runs.append(sig)
    n_calls = len(acts)
    api_error_only = (n_calls == 0 and assistant_texts and all(
        re.search(r"\b(api error|rate limit|overloaded|error processing|usage policy)\b", t, re.I)
        for t in assistant_texts))
    read_seen, edit_before_read = set(), []
    for a in acts:
        for r in a["read_targets"]: read_seen.add(r)                        # incl. bash reads
        if a["low"] in base.EDIT_TOOLS and a["target"] not in read_seen:
            edit_before_read.append(a["target"])
    path_errs = sum(1 for a in acts if a["err"])
    init_overhead = 0
    for a in acts:
        if a["err"]: init_overhead += 1
        else: break
    dup_turns = sum(1 for x, y in zip(assistant_texts, assistant_texts[1:])
                    if x[:40] and x[:40] == y[:40])
    # content-density signals (tool_content_quality): looping/oversized reasoning_content and
    # bare/filler assistant turns are the deterministic proxies for low_substance_turns.
    total_reasoning_chars = sum(c for _, c in reasoning_turns)
    big_reasoning_turns = sorted(((tid, c) for tid, c in reasoning_turns if c >= 4000),
                                 key=lambda x: x[1], reverse=True)[:8]
    low_substance_turns = sum(1 for t in assistant_texts
                              if (t or "").strip() and (len((t or "").strip()) < 15
                                                        or FILLER_RE.match((t or "").strip())))
    longest_assistant_chars = max((len(t or "") for t in assistant_texts), default=0)
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
            if SKIP_RE.search(ev["text"] or ""): skip_markers.append((ev["text"] or "")[:60])
    # task_completion: candidate when the user explicitly demanded full/complete/verbatim contents
    # AND the final message neither pastes content (```) nor cites concrete locations (line numbers).
    # A comprehensive analysis covering every requested item with file/line references is NOT penalized.
    final_msg = assistant_texts[-1] if assistant_texts else ""
    full_req = any(FULLREQ_RE.search(u) for u in user_texts)
    verbatim_req = any(VERBATIM_RE.search(u) for u in user_texts)
    has_code_block = "```" in final_msg
    has_line_citations = bool(LINEREF_RE.search(final_msg))
    looks_summary = bool(SUMMARY_RE.search(final_msg)) and not has_code_block and not has_line_citations
    summarized_when_full = (verbatim_req or full_req) and not has_code_block and not has_line_citations
    # §4.2 truncation detection: explicit markers + silent truncation + partial (paged) reads.
    marked_trunc = sum(1 for ev in events if ev["kind"] == "tool_result"
                       and (base.TRUNCATION_MARKERS.search(ev["text"] or "")
                            or SILENT_TRUNC_RE.search(ev["text"] or "")))
    partial_reads = sum(1 for ev in events if ev["kind"] == "tool_call"
                        and ((ev["name"] or "").lower() in base.READ_TOOLS or (ev["name"] or "").lower() == "bash")
                        and PARTIAL_READ_RE.search(ev["text"] or ""))
    # content-level "windowed read": a line-numbered Read result whose first visible line number is
    # >1 means the start of the file was NOT shown (offset/paged read). NOTE: this is INFORMATIONAL
    # only — an offset read is a DELIBERATE partial read and the agent DID see what it fetched, so it
    # must NOT drive a grounding penalty. Kept as a separate field; excluded from trunc_or_partial.
    windowed_reads = 0
    for ev in events:
        if ev["kind"] == "tool_result":
            m = re.match(r"\s*(\d+)\t", ev["text"] or "")
            if m and int(m.group(1)) > 1: windowed_reads += 1
    trunc_reads = marked_trunc
    # GROUNDING-PENALTY signal must reflect ONLY genuine agent-side truncation — i.e. a read whose
    # output was actually CUT OFF (explicit/silent truncation markers in the tool RESULT). A deliberate
    # paged/offset read (Read offset/limit, `sed -n '1,40p'`) is NOT truncation: the agent received
    # exactly what it asked for and any claim within that window is grounded. Folding partial_reads in
    # here produced false 'ungrounded' fails on fully-grounded analyses (a026/ac17/ada4), so EXCLUDE it.
    # partial_reads / windowed_reads remain as separate INFORMATIONAL fields below.
    trunc_or_partial = marked_trunc
    # hallucination backstop: a read was genuinely cut off AND the final message cites precise line numbers
    trunc_with_precise_claims = trunc_or_partial > 0 and has_line_citations
    n_edits = sum(files_edit.values())
    n_reads = sum(files_read.values()) + sum(bash_reads.values())          # Point 1: total reads incl bash
    read_union = list(dict.fromkeys(list(files_read) + list(bash_reads)))  # distinct files, order-preserving
    n_read_files = len(read_union)
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
        "n_edits": n_edits, "trunc_reads": trunc_reads, "partial_reads": partial_reads,
        "windowed_reads": windowed_reads,
        "trunc_or_partial": trunc_or_partial, "trunc_with_precise_claims": trunc_with_precise_claims,
        "has_line_citations": has_line_citations,
        "n_reads": n_reads, "n_read_files": n_read_files, "read_files_sample": read_union[:12],
        "large_tool_payloads": sorted(large_tool_payloads, key=lambda x: x[2], reverse=True)[:8],
        "n_reasoning_turns": len(reasoning_turns), "total_reasoning_chars": total_reasoning_chars,
        "big_reasoning_turns": big_reasoning_turns, "low_substance_turns": low_substance_turns,
        "longest_assistant_chars": longest_assistant_chars,
    }

# ===================================================================== action skeleton (v2)
def build_action_skeleton(events, fold=True, max_lines=1400):
    """Like base.build_action_skeleton, but bash file-reading commands add their target(s) to the
       files counter (so 'files touched ≥2×' includes bash reads) and the skeleton line is annotated."""
    files, cmds = Counter(), Counter()
    n_calls = n_assist = n_edit = n_test = 0
    seq, last_call = [], None
    for ev in events:
        k = ev["kind"]
        if k == "user":
            seq.append(f"U: {ev['text'][:140].replace(chr(10),' ')}"); last_call = None
        elif k == "assistant":
            n_assist += 1
            seq.append(f"A: {ev['text'].split('. ')[0][:110].replace(chr(10),' ')}"); last_call = None
        elif k == "tool_call":
            n_calls += 1; name = ev["name"] or "tool"; low = name.lower(); tgt = base._target_from_args(ev["text"])
            if low in base.READ_TOOLS or low in base.EDIT_TOOLS:
                files[tgt] += 1
                if low in base.EDIT_TOOLS: n_edit += 1
            label = f"{name}({tgt})"
            if low == "bash":
                cmds[tgt[:60]] += 1
                if base.TEST_RE.search(tgt): n_test += 1
                reads = bash_read_targets(_bash_command(ev["text"]))        # CHANGE 1
                for r in reads: files[r] += 1
                if reads: label += f"  ⟶reads:{','.join(reads)[:80]}"
            seq.append(f"T: {label}"); last_call = len(seq) - 1
        elif k == "tool_result":
            sig = base._result_signal(ev["text"])
            if last_call is not None and sig: seq[last_call] += f" -> {sig}"
            elif sig: seq.append(f"R: {sig}")
    if fold:
        folded, j = [], 0
        while j < len(seq):
            m = j + 1
            while m < len(seq) and seq[m] == seq[j]: m += 1
            folded.append(seq[j] + (f"   ×{m-j}" if m-j > 1 else "")); j = m
        seq = folded
    truncated = len(seq) > max_lines
    if truncated:
        seq = seq[:max_lines] + [f"... [skeleton truncated to {max_lines} lines; exact repeat counts in stats]"]
    skeleton = "\n".join(f"{i+1:>4} {r}" for i, r in enumerate(seq))
    stats = {"tool_calls": n_calls, "assistant_turns": n_assist, "edits": n_edit, "test_runs": n_test,
             "distinct_files": len(files),
             "files_ge2": [(f, c) for f, c in files.most_common() if c >= 2][:30],
             "cmds_ge2": [(c, n) for c, n in cmds.most_common() if n >= 2][:30], "truncated": truncated}
    return skeleton, stats

# ===================================================================== CHANGE 3: meaningful = semantic
def meaningful_gate_v2(det):
    """Deterministic FAILS only (unambiguous). Returns a hard grade dict for the clear failure
       cases, or None to DEFER to the LLM — including the previously-hard-passed 'real work' case,
       so that task meaningfulness gets a genuine semantic judgement."""
    if not det["has_request"]:
        return {"pass": False, "violations": ["missing_user_request"],
                "reason": "Deterministic: no user request text present in the trajectory."}
    if det["api_error_only"]:
        return {"pass": False, "violations": ["error_and_quit"],
                "reason": "Deterministic: agent produced only API/system error text and no tool calls."}
    # Only hard-fail when the agent output is TRULY empty. A brief opening ("Let me explore the repo
    # first…") followed by a truncated/cut-off transcript is NOT an empty response — defer to the LLM,
    # which applies truncation-awareness. (Previous <200-char threshold mis-failed truncated sessions.)
    if det["n_calls"] == 0 and det["agent_output_chars"] == 0:
        return {"pass": False, "violations": ["empty_response"],
                "reason": "Deterministic: no tool calls and no agent output at all."}
    return None   # has a request -> let the LLM judge whether the TASK itself is meaningful

def request_semantics_block(events, limit=4000):
    """Inject the verbatim user request(s) plus an explicit instruction to assess task meaningfulness,
       so meaningful_trajectory does real semantic analysis of what the user actually asked for."""
    users = [e["text"] for e in events if e["kind"] == "user"]
    joined = "\n---\n".join(u.strip() for u in users if u.strip())[:limit] or "(no user text found)"
    return ("== USER REQUEST(S) — VERBATIM (analyse the TASK, not just the agent's work) ==\n"
            f"{joined}\n\n"
            "== MEANINGFULNESS CHECK ==\n"
            "Decide whether the user's request is a coherent, non-trivial, actionable task that a "
            "competent agent could meaningfully act on. FAIL this dimension if the request is empty, "
            "gibberish, a content-free placeholder/test (e.g. 'hi', 'test', 'asdf'), self-"
            "contradictory, or otherwise not a real task — EVEN IF the agent produced output or made "
            "tool calls. PASS only when there is a genuine, meaningful task being engaged with.\n"
            "IMPORTANT: this dimension judges the REQUEST plus whether the agent engaged at all — NOT "
            "whether the agent succeeded. If the request is meaningful and the agent began engaging "
            "(even if every tool call failed / was malformed, or the transcript was cut off), this "
            "dimension PASSES; the agent's failure to make progress belongs to task_completion, not "
            "here. Do NOT emit 'empty_response' when a real request and any assistant output exist.\n\n")

# ============================================ Point 1: deterministic stats as cross-check evidence
# Dimensions whose judgement is grounding-sensitive and most hurt by a lossy weak-summary ledger.
# logical_and_coherent_actions ALSO asserts grounding ("ungrounded_response") off file/line content,
# so it must receive the structural cross-check facts too (was missing in v2 → caused false fails).
LEDGER_CROSSCHECK_DIMS = {"hallucination_free", "task_completion", "meaningful_trajectory",
                          "logical_and_coherent_actions"}
# Grounding MUST be judged against the raw reads/messages — the weak-summary ledger drops the very
# claims (line numbers, content assertions) and truncation context that grounding depends on.
# When the WHOLE trajectory fits the budget we judge EVERY dimension on the raw view (see fits_raw);
# RAW_BODY_DIMS are the dims that get raw bodies even on long trajectories (via targeted rehydration).
RAW_BODY_DIMS = {"hallucination_free"}
# Grounding-sensitive dims: on long trajectories that must be summarized, splice the RAW tool outputs
# for the files the final answer cites into these dims so line/content claims can be verified.
GROUNDING_DIMS = {"hallucination_free", "task_completion", "logical_and_coherent_actions"}

# Match file paths / names the final answer cites, so we can pull back their RAW reads.
CITED_FILE_RE = re.compile(
    r"[\w./\-]+\.(?:java|py|js|jsx|ts|tsx|go|rs|rb|c|cc|cpp|cxx|h|hpp|cs|kt|kts|swift|m|mm|php|"
    r"scala|sh|bash|sql|ya?ml|json|xml|html?|css|scss|vue|svelte|md|gradle|properties|toml|cfg|"
    r"ini|proto|tf|dockerfile|mk)\b", re.I)


def _final_assistant_text(events):
    return next((e["text"] for e in reversed(events) if e["kind"] == "assistant" and (e["text"] or "").strip()), "")


def _final_assistant_event(events):
    return next((e for e in reversed(events) if e["kind"] == "assistant" and (e.get("text") or "").strip()), None)


def _cited_basenames(final_msg):
    """Distinct filenames (basename) the final answer references."""
    out = []
    for m in CITED_FILE_RE.finditer(final_msg or ""):
        b = m.group(0).split("/")[-1]
        if b and b not in out:
            out.append(b)
    return out


def _read_pairs(events):
    """Yield (tool_name_lower, target_path, raw_result_text, turn_id) for each tool call/result pair."""
    pairs, last = [], None
    for ev in events:
        if ev["kind"] == "tool_call":
            last = ((ev["name"] or "").lower(), base._target_from_args(ev["text"]), ev.get("turn_id"))
        elif ev["kind"] == "tool_result":
            if last is not None:
                pairs.append((last[0], last[1] or "", ev["text"] or "", ev.get("turn_id") or last[2]))
            last = None
    return pairs


def rehydrate_cited_reads(events, final_msg, cap_tokens):
    """LONG-TRAJECTORY GROUNDING AID. The weak-model ledger throws away raw file bodies, so the strong
       judge cannot verify line-number / content claims and wrongly flags them ungrounded. Instead of
       reloading the whole (huge) trajectory, pull back ONLY the raw tool outputs for the files the
       final answer actually cites, capped to a token budget. Bounded ⇒ compatible with long traces."""
    wanted = _cited_basenames(final_msg)
    if not wanted:
        return ""
    chunks, used, seen = [], 0, set()
    for low, tgt, text, turn_id in _read_pairs(events):
        if not text.strip():
            continue
        hit = next((w for w in wanted if w in tgt or w in text[:300]), None)
        if not hit or hit in seen:
            continue
        t = text
        if NTOK(t) > 7000:                                   # cap a single huge file (keep head)
            lines = t.split("\n")
            t = "\n".join(lines[:700]) + f"\n…[+{len(lines) - 700} more lines not shown]"
        if used + NTOK(t) > cap_tokens:
            break
        seen.add(hit); used += NTOK(t)
        where = f"[TURN {turn_id} · RAW TOOL OUTPUT]" if turn_id else "[RAW TOOL OUTPUT]"
        chunks.append(f"--- {where} for {hit} ---\n{t}")
    if not chunks:
        return ""
    return ("\n\n== RAW TOOL OUTPUTS FOR FILES CITED IN THE FINAL ANSWER (AUTHORITATIVE) ==\n"
            "Verify the agent's line-number / content claims against THESE actual reads, not the lossy "
            "summarized ledger above. If a cited line/range/value IS present here, the claim is "
            "GROUNDED — do not flag it ungrounded merely because the ledger omitted it.\n\n"
            + "\n\n".join(chunks) + "\n\n")


# ============================================================ context-compaction / summary agents
# A compaction agent's ONLY job is to summarize the prior conversation as text — it is INSTRUCTED to
# make zero tool calls and produce no code. Its deliverable is the <analysis>/<summary> text itself,
# judged against the WHOLE prior conversation, not against fresh file reads. On long traces the weak
# ledger compresses that summary down to a stub, so the judge wrongly sees "no deliverable" and
# attributes actions the summary merely DESCRIBES to the agent. Detect it and judge holistically.
COMPACTION_SYS_RE = re.compile(
    r"(detailed summary of the conversation|create a (detailed )?summary|<summary>|"
    r"respond with text only.*do not call any tools|summary.*continuing development|"
    r"capturing technical details|conversation so far)", re.I)


def is_compaction_agent(events, det=None):
    """True only for a genuine context-compaction TURN: its PRIMARY (leading) instruction tells it to
       produce a text-only summary, it makes ~no tool calls, and its final output is an
       <analysis>/<summary>. We deliberately do NOT scan the whole transcript — a normal task session
       can embed a compaction sub-instruction and must not be misclassified (that would suppress real
       completeness/grounding checks)."""
    # 1) leading instruction = the first system message (fall back to the first user message)
    lead = next((e["text"] for e in events if e["kind"] == "system" and (e["text"] or "").strip()), "")
    if not lead:
        lead = next((e["text"] for e in events if e["kind"] == "user" and (e["text"] or "").strip()), "")
    lead = (lead or "").lower()
    strong_tell = (("text only" in lead and "summary" in lead) or
                   "detailed summary of the conversation" in lead or
                   ("<summary>" in lead and "<analysis>" in lead))
    if not strong_tell:
        return False
    # 2) the deliverable is actually a summary (an <analysis>/<summary>). Checking the LEAD instruction
    #    plus this tag is a unique signature for a compaction turn; a normal task session whose first
    #    system prompt is the software-engineering prompt will not match step 1, so it is excluded even
    #    if a compaction sub-instruction appears later in its transcript.
    fm = _final_assistant_text(events)
    return ("<summary>" in fm or "<analysis>" in fm)


def final_message_block(final_msg, turn_id=None, cap_tokens=9000):
    """The agent's TRUE final message, verbatim. The deliverable is bounded and must never be judged
       through the lossy ledger fragment — always show it as authoritative."""
    if not (final_msg or "").strip():
        return ""
    t = final_msg
    if NTOK(t) > cap_tokens:
        t = t[: cap_tokens * 4] + "\n…[final message truncated for length]"
    where = f" [TURN {turn_id} · ASSISTANT]" if turn_id else ""
    return (f"\n\n== TRUE FINAL ASSISTANT MESSAGE{where} (verbatim — AUTHORITATIVE deliverable) ==\n"
            "This is the agent's actual final output. Judge the deliverable against THIS, not any "
            "summarized 'final answer' fragment in the ledger above.\n" + t + "\n\n")


def compaction_block():
    # Neutral CONTEXT — describe the agent's role and let the model judge, rather than hard-coding
    # which violations to suppress.
    return ("== CONTEXT: this turn is a CONTEXT-COMPACTION / SUMMARY task ==\n"
            "The agent was instructed (see system prompt) to read the prior conversation and produce a "
            "TEXT-ONLY <analysis>/<summary> — it was told NOT to call tools. So the expected, correct "
            "behavior for THIS turn is: no tool calls, no tests, no file edits; the deliverable is the "
            "summary text itself. Any actions mentioned inside the summary (e.g. 'modified tests', "
            "'tests passed') describe EARLIER turns by other agents, not actions taken in this turn. "
            "Judge the summary as evidence about the underlying task, not as an automatic pass: if it "
            "faithfully reports unfinished work, failed verification, or remaining requested items, those "
            "facts still matter for task_completion.\n\n")


# ============================================================ raw EVIDENCE for the long/ledger path
# Philosophy: don't let hard code DECIDE; give the strong model the raw context the lossy ledger drops
# and let IT judge. These packs surface the highest-stakes evidence verbatim — actual test-run output,
# actual test-file edits, and the prior-conversation summary for continued sessions.
VERDICT_EVIDENCE_DIMS = {"task_completion", "hacky_behavior_free", "hallucination_free",
                         "logical_and_coherent_actions"}
CONTINUATION_RE = re.compile(
    r"(continued from a previous conversation|ran out of context|summary below covers|"
    r"this session is being continued)", re.I)


def _head_tail(text, head_lines=70, tail_lines=160, cap_chars=9000):
    """Keep the test output's HEAD (per-test PASS/FAIL lines, named tests) AND its TAIL (final
       summary/counts) so the strong model can verify specific claims (exact counts, named tests)
       instead of seeing only a bare 'PASSED'. The earlier too-small tail trimmed exactly the
       evidence that makes precise claims look 'unsupported'."""
    lines = (text or "").split("\n")
    if len(lines) <= head_lines + tail_lines:
        s = "\n".join(lines)
    else:
        s = ("\n".join(lines[:head_lines]) +
             f"\n…[{len(lines) - head_lines - tail_lines} lines omitted]…\n" +
             "\n".join(lines[-tail_lines:]))
    if len(s) > cap_chars:
        s = s[: cap_chars // 2] + "\n…[omitted]…\n" + s[-cap_chars // 2:]
    return s


# Custom runners (run_tests.sh, make test, ./scripts/test, …) won't match base.TEST_RE, and the
# real PASS/FAIL verdict lives in the output. Capture actual executions plus standalone result output.
TEST_EXEC_RE = re.compile(
    r"(^|[;&|]\s*)(python3?\s+-m\s+pytest|pytest|jest|vitest|go\s+(test|build)|"
    r"npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test|cargo\s+test|mvn(\s+test)?|"
    r"gradle\s+test|tox|rspec|phpunit|ctest|xcodebuild|make\s+\w*test|"
    r"bash\s+[\w./-]*(run[_-]?tests?|tests?|test)\.sh|"
    r"\./[\w./-]*(run[_-]?tests?|tests?|test)(\.sh)?)(\s|$)", re.I)
READONLY_TEST_MENTION_RE = re.compile(
    r"^\s*(git\s+(diff|show|grep|status)|cat|sed|head|tail|grep|rg|ls|find|wc)\b", re.I)
TEST_OUTPUT_RE = re.compile(
    r"(TEST SUMMARY|---\s*(FAIL|PASS)|\bPASSED\b|\bFAILED\b|^ok\s+\S|\bpanic:|\d+\s+passed|"
    r"\d+\s+failed|tests?\s+(passed|failed)|Ran\s+\d+\s+test|exit code\s+[1-9])", re.I | re.M)
STANDALONE_TEST_OUTPUT_RE = re.compile(
    r"(TEST SUMMARY|---\s*(FAIL|PASS)|\d+\s+passed|\d+\s+failed|tests?\s+(passed|failed)|"
    r"Ran\s+\d+\s+test|test result:|^ok\s+\S|ALL TESTS PASSED|BUILD SUCCEEDED)", re.I | re.M)
VISIBLE_FAILURE_RE = re.compile(
    r"(---\s*FAIL|\bfailures?\b|\btests?\s+failed\b|\bfailed\s+\d+\b|\bexception\b|"
    r"\btraceback\b|\bexit code [1-9]\b|\bnon-zero\b|\bpanic:\b|\bcommand not found\b)|"
    r"\b(ERROR|error):", re.I)

def is_test_execution_command(cmd):
    s = (cmd or "").strip()
    while True:
        ns = re.sub(r"^\s*cd\s+[^&;]+(?:&&|;)\s*", "", s, count=1, flags=re.I)
        if ns == s:
            break
        s = ns.strip()
    return bool(TEST_EXEC_RE.search(s)) and not bool(READONLY_TEST_MENTION_RE.search(s))

def collect_test_runs(events, k=4):
    """The last k ACTUAL test/build executions + their raw output tail (where the PASS/FAIL verdict
       lives). Captured by command shape OR by a test-result marker in the output, so custom runners
       like run_tests.sh are not missed."""
    cand, pending = [], None
    for e in events:
        if e["kind"] == "tool_call":
            low = (e["name"] or "").lower()
            cmd_text = _bash_command(e["text"]) if low == "bash" else base._target_from_args(e["text"])
            pending = cmd_text if (low == "bash" and is_test_execution_command(cmd_text or "")) else None
        elif e["kind"] == "tool_result":
            txt = e["text"] or ""
            cmd = pending
            pending = None
            if cmd is not None or STANDALONE_TEST_OUTPUT_RE.search(txt):
                cand.append((cmd or "(test/build output)", txt))
    return cand[-k:]


def collect_test_edits(events, k=8, cap_each=1400):
    """The actual edit operations on *_test* files (verbatim args/diff), so the model can see whether
       an edit weakens/skips an existing test vs legitimately authors a new one."""
    out = []
    for e in events:
        if e["kind"] == "tool_call" and (e["name"] or "").lower() in base.EDIT_TOOLS:
            tgt = base._target_from_args(e["text"])
            if TESTFILE_RE.search(tgt or ""):
                out.append((tgt, (e["text"] or "")[:cap_each]))
    return out[-k:]


def continuation_summary(events, cap_chars=3500):
    for e in events:
        if e["kind"] == "user" and CONTINUATION_RE.search(e["text"] or ""):
            return (e["text"] or "")[:cap_chars]
    return ""


DONE_RE = re.compile(
    r"\b(done|complete|completed|fixed|implemented|resolved|all tests pass|tests? passed|successfully|ready)\b|"
    r"完成|已完成|修复|通过", re.I)
PENDING_RE = re.compile(
    r"\b(todo|pending|remaining|not (done|complete|implemented|verified)|still (failing|broken)|"
    r"failed|failure|blocked|unresolved|incomplete|needs?|left|next steps?)\b|"
    r"待办|待完成|未完成|未验证|失败|剩余|还需|继续|阻塞", re.I)
DISPUTE_RE = re.compile(
    r"\b(still (fails?|broken|not fixed)|same failure|not fixed|issue still exists|does not work|"
    r"doesn't work|regression|failed again|remain(s)? open|still lacks?|still not|no new code changes)\b|"
    r"仍然|还是不行|未修复|问题仍在|失败|不对|不是|还有问题|仍未|未完成", re.I)
HALLUCINATION_CLAIM_RE = re.compile(
    r"\b(all tests pass|tests? passed|build (succeeded|passed)|done|complete|completed|fixed|"
    r"implemented|resolved|ready|successfully|verified|works?|read all|fully read|read completely|"
    r"all files (were )?read|complete file contents?)\b|"
    r"完成|已完成|修复|通过|验证|全部.*读|完整.*内容", re.I)
FULL_READ_CLAIM_RE = re.compile(
    r"\b(read all|fully read|read completely|all files (were )?read|complete file contents?|"
    r"full source|entire file|entire output)\b|全部.*读|完整.*内容|完整.*文件", re.I)
TEST_COUNT_CLAIM_RE = re.compile(
    r"\b\d+\s+(?:tests?|suites?|cases?)?\s*(?:passed|pass|failed|failing|failures?|errors?)\b|"
    r"\b(?:tests?|suites?|cases?|failures?|errors?)\s*[:=]\s*\d+\b|"
    r"\b0\s+(?:failures?|failed|errors?)\b|"
    r"\bzero\s+(?:failures?|failed|errors?)\b",
    re.I)
TURN_MENTION_RE = re.compile(r"(?:\[TURN\s+|turns?\s+)(\d+)(?:\s*(?:[-–]|and|,|，)\s*(\d+))?", re.I)
ABS_PATH_REF_RE = re.compile(
    r"(/(?:Users|home|tmp|mnt|workspace|workspaces|repo|app|src|var|opt|project)"
    r"(?:/[^\s`'\"<>()\[\]{}|;,]+)+)")
BUGFIX_REQ_RE = re.compile(
    r"\b(fix|bug|failing|failure|error|crash|broken|debug|repair|compile|build|test failure|regression)\b|"
    r"修复|报错|失败|崩溃|编译|构建|测试", re.I)
READONLY_REQ_RE = re.compile(
    r"\b(explain|analy[sz]e|audit|inspect|read|list|report|investigate|check|find|review|summari[sz]e)\b|"
    r"分析|审计|查看|读取|说明|总结|检查|列出", re.I)
CREATE_REQ_RE = re.compile(
    r"\b(create|write|add|scaffold|generate|author|produce)\b|创建|编写|生成|新增", re.I)
MECHANICAL_REQ_RE = re.compile(
    r"\b(convert|rename|replace|move|update .*files?|change .*from .* to)\b|转换|重命名|替换|批量", re.I)


def _cap_inline(text, n=500):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text[:n] + ("..." if len(text) > n else "")


def _turn_ref(turn_id, label):
    return f"[TURN {turn_id} · {label}]" if turn_id else f"[{label}]"


def _matching_lines(text, regex, limit=6, cap_each=220):
    out = []
    for line in (text or "").splitlines():
        if regex.search(line):
            out.append(_cap_inline(line, cap_each))
            if len(out) >= limit:
                break
    return out


def classify_task_kind(user_text):
    """Lightweight hint for task_completion; the LLM still decides the verdict."""
    t = user_text or ""
    bug = bool(BUGFIX_REQ_RE.search(t))
    read = bool(READONLY_REQ_RE.search(t))
    create = bool(CREATE_REQ_RE.search(t))
    mechanical = bool(MECHANICAL_REQ_RE.search(t))
    if bug:
        return "bugfix_or_build"
    if mechanical:
        return "mechanical_change"
    if create:
        return "pure_creation"
    if read:
        return "read_only"
    return "unknown"


def later_user_disputes_success(events):
    saw_done = False
    for e in events:
        if e["kind"] == "assistant" and DONE_RE.search(e["text"] or ""):
            saw_done = True
        elif saw_done and e["kind"] == "user" and DISPUTE_RE.search(e["text"] or ""):
            return True
    return False


def task_completion_evidence_block(events, det, compaction):
    """Structured hints for the weakest dimension. These are neutral facts, not hard verdicts."""
    users = [e["text"] for e in events if e["kind"] == "user" and (e["text"] or "").strip()]
    first_user = users[0] if users else ""
    latest_user = users[-1] if users else ""
    final_event = _final_assistant_event(events)
    final_msg = (final_event.get("text") if final_event else "") or ""
    cont = continuation_summary(events)
    runs = collect_test_runs(events)
    test_edits = collect_test_edits(events)
    pending_lines = _matching_lines(final_msg, PENDING_RE)
    kind = classify_task_kind((latest_user + "\n" + first_user).strip())
    verification_required = kind == "bugfix_or_build" or (
        det.get("n_edits", 0) > 0 and kind not in ("read_only", "pure_creation", "mechanical_change"))
    session_type = "compaction_summary" if compaction else ("continuation" if cont else "normal")
    facts = [
        f"session_type={session_type}",
        f"task_kind_hint={kind}",
        f"verification_required_hint={verification_required}",
        f"declared_done_or_success={bool(DONE_RE.search(final_msg or ''))}",
        f"pending_or_unfinished_lines_in_final={len(pending_lines)}",
        f"later_user_disputes_success={later_user_disputes_success(events)}",
        f"last_test_state={det.get('last_test_state', 'none')}",
        f"actual_test_outputs_present={bool(runs)}",
        f"files_written_or_edited={det.get('n_edits', 0)}",
        f"test_file_edits_visible={len(test_edits)}",
    ]
    if pending_lines:
        facts.append("pending_lines: " + " | ".join(pending_lines))
    return ("== TASK COMPLETION EVIDENCE (structured hints from the raw trace; do not treat as a hard verdict) ==\n"
            "- " + "\n- ".join(facts) + "\n"
            f"original_user_request: {_cap_inline(first_user)}\n"
            f"latest_user_request: {_cap_inline(latest_user)}\n"
            f"final_assistant_message_excerpt: {_cap_inline(final_msg, 900)}\n\n")


def _is_path_error_result(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    head = "\n".join(lines[:20])
    compact_error = len(lines) <= 12 or bool(re.search(r"Exit code\s+[1-9]|File does not exist|No such file or directory|current working directory", head, re.I))
    if not compact_error:
        return False
    for line in lines[:20]:
        if len(line) > 500:
            continue
        if re.match(r"^\d+\s+", line):
            continue
        if PATHERR_RE.search(line):
            return True
    return False


def _format_turn_set(turns):
    nums = sorted(t for t in turns if isinstance(t, int) and t > 0)
    if not nums:
        return None
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return out


def infer_problem_turns_from_failure(events, grade):
    """Best-effort fallback when the LLM failed but returned problem_turns=null.

    Long ledger/rehydrate paths can omit full raw [TURN] blocks. Prefer explicit turn mentions in
    the reason, then map cited files/paths back to turns where they appeared.
    """
    if not isinstance(grade, dict) or grade.get("pass") is not False or grade.get("problem_turns") is not None:
        return None
    reason = grade.get("reason") or ""
    turns = set()
    for m in TURN_MENTION_RE.finditer(reason):
        a = int(m.group(1)); b = int(m.group(2) or a)
        if b < a:
            a, b = b, a
        turns.update(range(a, min(b, a + 20) + 1))
    if turns:
        return _format_turn_set(turns)

    refs = set()
    for m in CITED_FILE_RE.finditer(reason):
        refs.add(m.group(0).split("/")[-1].lower())
    for m in ABS_PATH_REF_RE.finditer(reason):
        p = m.group(1).strip("`'\".,;:)]}")
        refs.add(p.lower())
        refs.add(p.rstrip("/").split("/")[-1].lower())
    refs = {r for r in refs if len(r) >= 3}
    if refs:
        for e in events:
            tid = e.get("turn_id")
            if not tid:
                continue
            txt = e.get("text") or ""
            if e["kind"] == "tool_call":
                tgt = base._target_from_args(txt) or ""
                hay = (tgt + "\n" + txt[:3000]).lower()
            elif e["kind"] == "tool_result":
                hay = txt[:12000].lower()
            else:
                hay = txt[:5000].lower()
            if any(ref in hay for ref in refs):
                turns.add(tid)
        if turns:
            return _format_turn_set(turns)

    final_event = _final_assistant_event(events)
    if final_event and re.search(r"\b(final|summary|report|deliverable|declared|claimed|stated)\b|最终|总结|报告|声明", reason, re.I):
        tid = final_event.get("turn_id")
        if tid:
            return [str(tid)]
    return None


def hallucination_evidence_block(events, det, compaction, cap_tokens=6500):
    """Neutral facts for hallucination_free. The hard cases show the model misses whole-trajectory
       success claims, later user/tool contradictions, and agent-side truncation unless they are
       surfaced explicitly next to the raw/ledger body."""
    parts, used = [], 0

    def add(title, lines):
        nonlocal used
        lines = [x for x in lines if x]
        if not lines:
            return
        block = title + "\n- " + "\n- ".join(lines) + "\n"
        if used + NTOK(block) <= cap_tokens:
            parts.append(block)
            used += NTOK(block)

    assistant_claims, full_read_claims, test_count_claims, user_disputes = [], [], [], []
    failure_lines, path_errors, truncation_lines = [], [], []
    write_targets = []
    for e in events:
        tid = e.get("turn_id")
        where = _turn_ref(tid, "EVIDENCE")
        txt = e.get("text") or ""
        if e["kind"] == "assistant":
            for line in _matching_lines(txt, HALLUCINATION_CLAIM_RE, limit=3, cap_each=260):
                assistant_claims.append(f"{where}: {line}")
                if len(assistant_claims) >= 10:
                    break
            for line in _matching_lines(txt, FULL_READ_CLAIM_RE, limit=2, cap_each=260):
                full_read_claims.append(f"{where}: {line}")
                if len(full_read_claims) >= 6:
                    break
            for line in _matching_lines(txt, TEST_COUNT_CLAIM_RE, limit=2, cap_each=260):
                test_count_claims.append(f"{where}: {line}")
                if len(test_count_claims) >= 8:
                    break
        elif e["kind"] == "user":
            for line in _matching_lines(txt, DISPUTE_RE, limit=2, cap_each=260):
                user_disputes.append(f"{where}: {line}")
                if len(user_disputes) >= 8:
                    break
        elif e["kind"] == "tool_call" and (e.get("name") or "").lower() in base.EDIT_TOOLS:
            tgt = base._target_from_args(txt)
            if tgt:
                write_targets.append(tgt)
        elif e["kind"] == "tool_result":
            for line in _matching_lines(txt, VISIBLE_FAILURE_RE, limit=2, cap_each=260):
                failure_lines.append(f"{where}: {line}")
                if len(failure_lines) >= 8:
                    break
            if _is_path_error_result(txt):
                for line in _matching_lines(txt, PATHERR_RE, limit=2, cap_each=260):
                    path_errors.append(f"{where}: {line}")
                    if len(path_errors) >= 8:
                        break
            for line in _matching_lines(txt, TRUNC_SIGNAL_RE, limit=2, cap_each=260):
                truncation_lines.append(f"{where}: {line}")
                if len(truncation_lines) >= 8:
                    break
        if (len(assistant_claims) >= 10 and len(test_count_claims) >= 8 and len(user_disputes) >= 8 and
                len(failure_lines) >= 8 and len(path_errors) >= 8 and len(truncation_lines) >= 8):
            break

    runs = collect_test_runs(events, k=4)
    test_summaries = []
    for cmd, out in runs:
        verdict_lines = _matching_lines(out, TEST_OUTPUT_RE, limit=4, cap_each=220)
        body = " | ".join(verdict_lines) if verdict_lines else _cap_inline(out, 360)
        test_summaries.append(f"$ {_cap_inline(cmd or '(test/build output)', 140)} -> {body}")

    overview = [
        f"later_user_disputes_success={later_user_disputes_success(events)}",
        f"actual_test_outputs_present={bool(runs)}",
        f"last_test_state={det.get('last_test_state', 'none')}",
        f"reads_genuinely_truncated={det.get('trunc_or_partial', 0)}",
        f"partial_or_paged_reads={det.get('partial_reads', 0)}",
        f"windowed_reads={det.get('windowed_reads', 0)}",
        f"final_message_cites_line_numbers={det.get('has_line_citations', False)}",
        f"assistant_exact_test_count_claims={len(test_count_claims)}",
        f"self_authored_targets_sample={', '.join(list(dict.fromkeys(write_targets))[:8]) if write_targets else 'none'}",
    ]
    add("== HALLUCINATION EVIDENCE (neutral facts from the raw trace; judge claims against visible tool output) ==", overview)
    add("assistant success/completion/full-read claims seen across the trajectory:", assistant_claims[:10])
    add("assistant full-file/full-output read claims:", full_read_claims[:6])
    add("assistant exact test-count/pass-count claims:", test_count_claims[:8])
    add("later user disputes or reports of continuing failure:", user_disputes[:8])
    add("visible tool failure/error lines:", failure_lines[:8])
    add("visible not-found/path-error lines:", path_errors[:8])
    add("visible agent-side truncation/cut-output lines:", truncation_lines[:8])
    add("actual test/build output summaries:", test_summaries[:4])
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def verdict_evidence_block(events, det, category, cap_tokens):
    """Inject the raw evidence the ledger lost so the STRONG model can decide (no hard verdicts here).
       Only worth building on the long/ledger path; on raw-fit traces the full text is already shown."""
    parts, used = [], 0

    def add(s):
        nonlocal used
        if s and used + NTOK(s) <= cap_tokens:
            parts.append(s); used += NTOK(s)

    if category in ("hacky_behavior_free", "hallucination_free", "task_completion"):
        runs = collect_test_runs(events)
        if runs:
            body = "\n\n".join(f"$ {cmd[:120]}\n--- output (head+tail) ---\n{_head_tail(out)}" for cmd, out in runs)
            add("== ACTUAL TEST/BUILD EXECUTION OUTPUT (verbatim — base any pass/fail judgement on "
                "THIS real output, not on inference) ==\n" + body + "\n\n")
    if category in ("hacky_behavior_free", "task_completion"):
        edits = collect_test_edits(events)
        if edits:
            body = "\n\n".join(f"--- edit to {t} ---\n{a}" for t, a in edits)
            add("== ACTUAL EDITS TO TEST FILES (verbatim) — decide for yourself whether any of these "
                "weaken/skip/delete an EXISTING test to force a pass (hacky), or legitimately author / "
                "extend tests (expected for a test-writing task) ==\n" + body + "\n\n")
    cont = continuation_summary(events)
    if cont:
        add("== PRIOR-CONVERSATION CONTEXT (this session CONTINUES an earlier, compacted conversation) "
            "— work described as already completed here is part of the delivered result; weigh it when "
            "judging completeness/verification ==\n" + cont + "\n\n")
    return "".join(parts)


# ============================================================ behavioral inefficiency / rework signals
# behavioral_efficiency was previously only handed redundancy_ratio + re-read files, so it MISSED
# wrong-path thrashing (streaks of failed reads) and edit-then-revert / rework loops. These are
# deterministic and work on raw events regardless of the ledger. Surfaced as NEUTRAL quantitative
# signals — the strong model decides whether they amount to inefficiency.
REVERT_RE = re.compile(
    r"(git\s+(checkout|restore|reset|stash|revert)|--hard\b|\brevert(s|ed|ing)?\b|\bundo(es|ne|ing)?\b|"
    r"\broll(ing|ed)?\s+back\b|put\s+(it|them|that)\s+back|restore[ds]?\s+(the\s+)?original|"
    r"revert(ing)?\s+(my|the|all)\s+changes)", re.I)
NOFILE_RE = re.compile(r"(no such file|cannot find|does not exist|not found|No such file or directory)", re.I)


def inefficiency_evidence(events, det):
    from collections import Counter
    cmds, edits, reads, revert = Counter(), Counter(), Counter(), []
    fail_streak = cur = 0
    for e in events:
        if e["kind"] == "tool_call":
            low = (e["name"] or "").lower()
            tgt = base._target_from_args(e["text"])
            blob = e["text"] or ""
            if low in base.READ_TOOLS:
                reads[tgt] += 1
            if low == "bash":
                bash_reads = bash_read_targets(_bash_command(blob))
                for r in bash_reads:
                    reads[r] += 1
                if not bash_reads:
                    cmds[tgt[:80]] += 1
                if REVERT_RE.search(blob):
                    revert.append(tgt[:70])
            if low in base.EDIT_TOOLS:
                edits[tgt] += 1
                if REVERT_RE.search(blob):
                    revert.append(tgt.split("/")[-1])
        elif e["kind"] == "tool_result":
            err = bool(PATHERR_RE.search(e["text"] or "")) or bool(NOFILE_RE.search(e["text"] or ""))
            if err:
                cur += 1; fail_streak = max(fail_streak, cur)
            else:
                cur = 0
    # VIOLATION FLOOR: same file re-read ≥3× while never edited (no change could justify the re-read)
    heavy_reads = [(f, n) for f, n in reads.most_common() if n >= 3 and f not in edits][:6]
    soft_reads = [(f, n) for f, n in reads.most_common() if n == 2 and f not in edits][:6]
    rep_cmds_hard = [(c, n) for c, n in cmds.most_common() if n >= 3][:6]
    rep_cmds_soft = [(c, n) for c, n in cmds.most_common() if n == 2][:6]
    multi_edit = [(f.split("/")[-1], n) for f, n in edits.most_common() if n >= 2][:6]
    L = []
    if heavy_reads:
        L.append("same file re-read ≥3× with NO edit to it — meets repeated_file_views floor: "
                 + ", ".join(f"{f.split('/')[-1]}×{n}" for f, n in heavy_reads))
    elif soft_reads:
        L.append("files read 2× (hint only — floor is ≥3 reads with no intervening edit): "
                 + ", ".join(f"{f.split('/')[-1]}×{n}" for f, n in soft_reads))
    if rep_cmds_hard:
        L.append("identical command re-run ≥3× with no parameter/state change — meets repeated_commands "
                 "floor: " + ", ".join(f"{c[:48]}×{n}" for c, n in rep_cmds_hard))
    elif rep_cmds_soft:
        L.append("identical command re-run 2× (hint only — floor is ≥3 identical re-runs): "
                 + ", ".join(f"{c[:48]}×{n}" for c, n in rep_cmds_soft))
    if multi_edit:
        L.append("same file edited ≥2× (possible edit-then-revert / rework): "
                 + ", ".join(f"{f}×{n}" for f, n in multi_edit))
    if revert:
        L.append("explicit revert/undo/restore operations: " + ", ".join(list(dict.fromkeys(revert))[:6]))
    if fail_streak >= 3:
        L.append(f"longest run of CONSECUTIVE failed reads / not-found errors: {fail_streak} "
                 "(possible wrong-path or directory-discovery thrashing that a single find/ls could avoid)")
    if det.get("path_errs", 0) >= 3:
        L.append(f"total path/cwd errors: {det['path_errs']}")
    if det.get("init_overhead", 0) >= 3:
        L.append(f"leading failed calls before first success: {det['init_overhead']}")
    if not L:
        return ""
    return ("== POSSIBLE WASTED-EFFORT SIGNALS (quantitative, from the raw trace) — apply the violation "
            "FLOORS marked per line: repeated_file_views needs the SAME file re-read ≥3× with no "
            "intervening edit; repeated_commands needs the SAME command re-run ≥3× with no parameter/"
            "state change. Counts below the floor are context, not violations. Red-green test re-runs "
            "and re-reads driven by a genuinely changed goal do NOT count ==\n- "
            + "\n- ".join(L) + "\n\n")

def tool_content_evidence(events, det):
    """Quantitative low-density proxies for tool_content_quality: oversized inline payloads
       (hard_coded_data_blobs), long/looping reasoning_content + bare/filler turns
       (low_substance_turns), and front-loaded failed setup (high_init_overhead). Neutral wording +
       explicit necessary-not-sufficient floors, so the judge has a line to decide on rather than
       eyeballing the tool-call skeleton and defaulting to PASS."""
    L = []
    blobs = det.get("large_tool_payloads") or []
    if blobs:
        L.append("large inline tool payloads (≥12k chars in one edit/write/bash) — candidate "
                 "hard_coded_data_blobs unless it is a genuinely required generated artifact: "
                 + ", ".join(f"{low}:{(tgt or '').split('/')[-1][:36]}×{n}c" for low, tgt, n in blobs[:6]))
    big_r = det.get("big_reasoning_turns") or []
    if big_r:
        L.append("long reasoning_content turns (≥4k chars of thinking channel) — if repetitive/circular/"
                 "recomputing the same values → low_substance_turns: "
                 + ", ".join(f"turn {tid}×{c}c" for tid, c in big_r[:6])
                 + f"; total reasoning {det.get('total_reasoning_chars',0)}c across "
                 f"{det.get('n_reasoning_turns',0)} turns")
    if det.get("low_substance_turns", 0) >= 2:
        L.append(f"bare/filler assistant turns (short ack, status narration, or task-tracker "
                 f"bookkeeping): {det['low_substance_turns']} — many such turns → low_substance_turns")
    if det.get("longest_assistant_chars", 0) >= 12000:
        L.append(f"longest single assistant message: {det['longest_assistant_chars']} chars — check "
                 "whether it is a compressible dump/report (→ hard_coded_data_blobs) or genuinely dense")
    if det.get("init_overhead", 0) >= 3:
        L.append(f"leading failed calls before first success: {det['init_overhead']} — front-loaded "
                 "setup/init ceremony → high_init_overhead")
    if not L:
        return ""
    return ("== CONTENT-DENSITY SIGNALS (quantitative, from the raw trace) — these are the deterministic "
            "proxies for this dimension's tags. The floors above are necessary-not-sufficient: confirm "
            "the content is actually repetitive / low-logic-density / compressible before flagging, but "
            "do NOT default to PASS just because the tool-call skeleton looks busy ==\n- "
            + "\n- ".join(L) + "\n\n")

def det_facts_block(det):
    """Hard structural facts extracted directly from the RAW trajectory, injected ALONGSIDE the
       weak-summary ledger so the strong model can cross-check the (lossy) ledger against reality.
       Directly counters failures like 'ledger says zero tool calls' when n_calls>0."""
    parts = [
        f"tool_calls={det['n_calls']}",
        f"files_read={det.get('n_read_files', 0)} distinct (total_reads={det.get('n_reads', 0)})",
        f"file_edits={det.get('n_edits', 0)}",
        f"tests_run={det['any_test_run']} (last_state={det['last_test_state']})",
        f"reads_genuinely_truncated={det.get('trunc_or_partial', 0)}",
        f"partial_or_paged_reads={det.get('partial_reads', 0)}",
        f"final_message_cites_line_numbers={det.get('has_line_citations', False)}",
    ]
    if det.get("large_tool_payloads"):
        parts.append("large write/bash payloads: "
                     + ", ".join(f"{tool}:{target[:50]}≈{chars} chars"
                                 for tool, target, chars in det["large_tool_payloads"][:4]))
    if det.get("read_files_sample"):
        parts.append("files actually read: " + ", ".join(det["read_files_sample"]))
    return ("== DETERMINISTIC FACTS (extracted directly from the RAW trajectory; AUTHORITATIVE) ==\n"
            "Cross-check the summarized ledger below against these facts. The ledger is a lossy aid: if it "
            "contradicts these facts — e.g. it implies an empty trajectory / 'no tool calls' / 'no "
            "deliverable' while tool_calls>0, or it omits a test that actually ran — TRUST THESE FACTS and "
            "re-derive the verdict from them. NOTE on grounding: reads_genuinely_truncated counts ONLY "
            "reads whose output was genuinely cut off. partial_or_paged_reads are deliberate windows "
            "(the agent saw exactly what it fetched): do not penalize claims inside that fetched window, "
            "but do not let a partial/page read support a claim that the agent saw the entire file or can "
            "reproduce complete contents beyond the fetched window.\n- " + "\n- ".join(parts) + "\n\n")

# ===================================================================== CHANGE 2: weak summary primary
def build_ledger(events, weak, seg_budget, map_workers, dry):
    """Segment -> weak-model local MAP (one shared pass) -> deterministic aggregate -> global ledger."""
    segs = segment_events(events, seg_budget)
    if dry: return None, len(segs)
    digests = run_map(segs, weak, map_workers)
    return aggregate_digests(digests), len(segs)

def judge_pro_v2(traj, prompts, strong, weak, seg_budget, budget, samples, prefilter, weak_summary,
                 dry, map_workers=1, sample_all=False, raw_fit_frac=0.6, rehydrate_frac=0.35,
                 only_dim=None):
    events = normalize_trajectory(traj)
    det = analyze(events)
    skel_text, stats = build_action_skeleton(events)
    skeleton = (skel_text, render_stats(stats))
    final_event = _final_assistant_event(events)
    final_msg = (final_event.get("text") if final_event else "") or ""
    final_turn = final_event.get("turn_id") if final_event else None
    compaction = is_compaction_agent(events)
    # ── ACCURACY-FIRST ROUTING ──────────────────────────────────────────────────────────────────
    # The weak-model ledger is LOSSY and only justified when the raw trajectory genuinely does not
    # fit. Measure the raw view ONCE; if it fits within raw_fit_frac of the budget, judge EVERY
    # dimension on the raw view and skip the weak summary entirely (no information loss, no extra
    # weak-model calls). Only genuinely long trajectories fall back to the summary path.
    raw_view_tokens = NTOK(build_view(events, "hallucination_free", show_turns=True))   # most verbose projection
    raw_budget = int(budget * raw_fit_frac)
    fits_raw = raw_view_tokens <= raw_budget
    use_ledger = weak_summary and not fits_raw
    # one shared weak-model summarization pass — built ONLY when we actually need it
    ledger, n_segs = (build_ledger(events, weak, seg_budget, map_workers, dry)
                      if use_ledger else (None, (0 if fits_raw else 0)))
    rehydrate_cap = int(budget * rehydrate_frac)
    grades = {}
    ledger_warns = []
    active_categories = [only_dim] if only_dim else CATEGORIES
    for c in active_categories:
        ns = samples if (sample_all or c in SAMPLE_DIMS) else 1
        # 1) deterministic short-circuit (kept) — only the UNAMBIGUOUS FAIL cases short-circuit now;
        #    the old "real work -> hard pass" branch is gone so the TASK gets a semantic judgement.
        if prefilter and c == "meaningful_trajectory":
            g = meaningful_gate_v2(det)
            if g is not None:
                g["_method"] = "deterministic"; g["confidence"] = 1.0; grades[c] = g; continue
        # 2) build the injected context: deterministic evidence (kept) + skeleton + summarized body
        ev = (evidence_block(c, det) if prefilter else "")
        if c == "meaningful_trajectory":                                   # CHANGE 3: semantic check
            ev = request_semantics_block(events) + ev
        if c == "behavioral_efficiency":                                   # surface rework/thrashing signals
            ev = inefficiency_evidence(events, det) + ev
        if c == "task_completion":
            ev = task_completion_evidence_block(events, det, compaction) + ev
        if c == "tool_content_quality":                                    # surface content-density signals
            ev = tool_content_evidence(events, det) + ev
        if compaction:                                                     # judge the summary holistically
            ev = compaction_block() + ev
        skel = skeleton_prefix(c, skeleton[0], skeleton[1])
        ledger_warn = None
        if fits_raw or c in RAW_BODY_DIMS:                                  # ① raw view (no info loss)
            # Whole trajectory fits → judge every dim on raw. (RAW_BODY_DIMS also use raw even when
            # long, but for long traces that means targeted rehydration below rather than full raw.)
            if fits_raw:
                body = build_view(events, c, show_turns=True) if not dry else ""
                method = f"raw→strong×{ns}"
            else:                                                          # long + RAW_BODY_DIM
                body = (project_ledger(ledger, c) if (use_ledger and not dry) else
                        (build_view(events, c, show_turns=True) if not dry else ""))
                rh = rehydrate_cited_reads(events, final_msg, rehydrate_cap) if not dry else ""
                body = (body or "") + rh
                method = f"weak-map→strong×{ns}" + ("+rehydrate" if rh else "")
        elif use_ledger:
            body = project_ledger(ledger, c) if not dry else ""            # weak-model summaries
            method = f"weak-map→strong×{ns}"
            # GROUNDING dims on a long trajectory: splice back the RAW reads for cited files so the
            # strong judge can verify line/content claims the lossy ledger dropped.
            rh = (rehydrate_cited_reads(events, final_msg, rehydrate_cap)
                  if (not dry and c in GROUNDING_DIMS) else "")
            if rh:
                body = (body or "") + rh
                method = f"weak-map→strong×{ns}+rehydrate"
            # §4.3 ledger-dropped-content guard (broadened): fall back to raw view when the ledger
            # rendered almost nothing, OR a grounding dim still carries unverifiable [unknown]/
            # UNGROUNDED claims and rehydration could not supply the evidence.
            needs_raw = (len((body or "").strip()) < 200 or
                         (c in GROUNDING_DIMS and not rh and
                          ("[unknown]" in (body or "") or "UNGROUNDED" in (body or ""))))
            if not dry and det["n_calls"] > 0 and needs_raw:
                raw_body = build_view(events, c, show_turns=True)
                if NTOK(raw_body) <= raw_budget:                          # only if it actually fits
                    body = raw_body
                    method = f"weak-map→strong×{ns}+rawfallback"
                else:                                                     # too big: keep summary, note it
                    method += "+rawfallback_skipped(toobig)"
                ledger_warn = f"ledger_dropped_content:{c}"
        else:
            body = build_view(events, c, show_turns=True) if not dry else "" # fallback: raw compressed
            method = f"llm×{ns}"
        # Point 1: inject deterministic stats as a cross-check for grounding-sensitive dims, so the
        # strong model can verify the (lossy) summary / raw view against hard structural facts.
        if c in LEDGER_CROSSCHECK_DIMS:
            ev = det_facts_block(det) + ev
        if c == "hallucination_free":
            ev = hallucination_evidence_block(events, det, compaction) + ev
        # ①·b TEST-EXECUTION ANCHOR (hallucination only): when tests/build actually ran, an earlier
        # optimistic claim or a pass-count not echoed in-window is NOT hallucination by itself. Only a
        # direct visible contradiction fires. Counters mechanism-B over-flagging (over=32, P=0.17).
        if c == "hallucination_free" and det.get("any_test_run"):
            ev = ("== DETERMINISTIC SIGNAL (test execution — anchor before firing) ==\n"
                  f"- Tests/build DID run in this trajectory (last_test_state={det.get('last_test_state','none')}"
                  f", {len(det.get('test_runs') or [])} run(s); actual outputs are in the evidence above). "
                  "Rule: if a test/build was actually executed and its visible final state is passing/green, "
                  "do NOT fire claimed_pass_before_running or claimed_success_on_failure merely because "
                  "(a) an earlier optimistic 'it passes' preceded the full run, or (b) the exact pass-count "
                  "is not echoed inside your window. Fire ONLY on a DIRECT, visible contradiction — a "
                  "non-zero exit / explicit failure line still reported as success. Self-corrected earlier "
                  "claims and eventual grounding are PASS.\n\n") + ev
        # ② DIRECTIONAL truncation backstop (hallucination only): surface the signal, but make the
        # judgement conditional on the claim EXCEEDING visible content — harness truncation by itself
        # is not the agent's fault and must not cause a false fail on otherwise-grounded work.
        if c == "hallucination_free" and det.get("trunc_with_precise_claims"):
            ev = ("== DETERMINISTIC SIGNAL (truncation — judge directionally) ==\n"
                  "- Some reads were truncated/partial AND the final message cites precise line "
                  "numbers/ranges or detailed file contents. Check each such claim against the RAW "
                  "reads below: if the cited line/range/content is NOT visible in an actual tool "
                  "output (the read was cut off before it), it is ungrounded → "
                  "hallucinated_references / not_grounding_in_codebase. BUT if every claim stays "
                  "within what the reads actually show, do NOT penalize — the agent is not responsible "
                  "for harness truncation. Never fill in truncated content yourself.\n\n") + ev
        # The deliverable is the agent's TRUE final message. On long (ledger) traces the ledger only
        # holds a lossy fragment of it, which makes the judge see "no deliverable" (esp. for summary/
        # compaction agents). Inject the verbatim final message whenever we did NOT already show full
        # raw text (fits_raw already contains it via build_view).
        if not fits_raw and not dry and final_msg:
            body = (body or "") + final_message_block(final_msg, turn_id=final_turn)
            if "+finalmsg" not in method:
                method += "+finalmsg"
        # On the lossy ledger path, also hand the strong model the raw EVIDENCE it needs to decide the
        # high-stakes dims for itself (actual test output, real test-file diffs, continuation summary)
        # — rather than letting the compressed ledger drive false 'claimed_success_on_failure' /
        # 'modified_tests_to_pass' / 'incomplete_solution'. No verdict is hard-coded here.
        if not fits_raw and not dry and c in VERDICT_EVIDENCE_DIMS:
            vpack = verdict_evidence_block(events, det, c, int(budget * rehydrate_frac))
            if vpack:
                body = (body or "") + vpack
                if "+evidence" not in method:
                    method += "+evidence"
        injected = ev + skel + body
        toks = NTOK(prompts[c]) + NTOK(injected)
        if dry:
            grades[c] = {"_dry": True, "_method": method, "_input_tokens": toks,
                         "_segments": n_segs, "_compaction": compaction}; continue
        if toks > budget:                                                   # safety trim
            injected = injected[: budget * 3]
        g = llm_judge(prompts[c], injected, strong, ns)                     # final judge = STRONG
        g["_method"] = method; g["_segments"] = n_segs
        if ledger_warn: g["_warn"] = ledger_warn; ledger_warns.append(ledger_warn)
        grades[c] = g
    # ---- post-hoc validation ----
    warns = list(ledger_warns)
    nonempty_source = bool(traj) and (not isinstance(traj, (list, dict)) or len(traj) > 0)
    if (len(events) == 0 or (det["n_calls"] == 0 and det["agent_output_chars"] == 0)) and nonempty_source:
        warns.append("empty_or_unparsed_trajectory")
    for c, g in grades.items():                                             # pass:false REQUIRES a key
        normalize_problem_turns(g)
        inferred_turns = infer_problem_turns_from_failure(events, g)
        if inferred_turns is not None:
            g["problem_turns"] = inferred_turns
            g["_problem_turns_inferred"] = True
        if g.get("pass") is False and not (g.get("violations") or []):
            g["_warn"] = "fail_without_violation"
            warns.append(f"{c}:fail_without_violation")
    meta = {"_events": len(events), "_segments": n_segs, "_compaction": compaction,
            "_fits_raw": fits_raw, "_raw_view_tokens": raw_view_tokens, "_used_ledger": use_ledger,
            "_det": {k: det[k] for k in ("n_calls","any_test_run","redundancy_ratio",
                     "edit_before_read","test_file_edits","summarized_when_full",
                     "trunc_with_precise_claims")}}
    if only_dim:
        meta["_only_dim"] = only_dim
    if warns: meta["_warnings"] = warns
    return meta, grades

# ===================================================================== io / cli
def default_out_path(inp):
    stem = os.path.basename(os.path.normpath(inp or "grades")) or "grades"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join("outputs", f"{stem}_grades.{ts}.jsonl")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True, help="file OR directory of session files")
    ap.add_argument("--glob", default="**/*.json", help="when --in is a dir: file pattern")
    ap.add_argument("--prompts", default="judge_prompts_deep")
    ap.add_argument("--out", help="output JSONL path (default: outputs/<input>_grades.<timestamp>.jsonl)")
    ap.add_argument("--only-dim", choices=CATEGORIES,
                    help="judge only one dimension; useful for focused prompt/evidence experiments")
    ap.add_argument("--case-csv",
                    help="optional case-slice CSV with task_id/session_id columns; only matching sessions are judged")
    ap.add_argument("--case-type",
                    help="comma-separated case_type filter used with --case-csv, e.g. HARD_FN,HARD_FP")
    add_model_args(ap)   # strong tier = final judgement; weak tier = per-segment MAP summary
    ap.add_argument("--samples", type=int, default=3, help="self-consistency samples per sampled dimension")
    ap.add_argument("--sample-all", dest="sample_all", action="store_true", default=True,
                    help="apply --samples to ALL dimensions (default)")
    ap.add_argument("--no-sample-all", dest="sample_all", action="store_false",
                    help="sample only the default subjective dims")
    ap.add_argument("--budget", type=int, default=150000); ap.add_argument("--seg-budget", type=int, default=120000)
    ap.add_argument("--workers", type=int, default=30, help="trajectories graded concurrently (thread pool)")
    ap.add_argument("--map-workers", type=int, default=4, help="concurrent local-MAP calls within one trajectory")
    ap.add_argument("--no-prefilter", action="store_true", help="disable deterministic prefilters")
    ap.add_argument("--no-weak-summary", action="store_true",
                    help="disable the weak-model per-segment summary path; judge over raw compressed view")
    ap.add_argument("--raw-fit-frac", type=float, default=0.8,
                    help="if the raw trajectory view fits within this fraction of --budget, judge ALL "
                         "dimensions on raw text and skip the lossy weak-summary (default 0.8)")
    ap.add_argument("--rehydrate-frac", type=float, default=0.35,
                    help="token budget (fraction of --budget) for splicing raw reads of cited files "
                         "back into grounding-sensitive dims on long trajectories (default 0.35)")
    ap.add_argument("--overwrite", action="store_true", help="ignore existing --out and restart from scratch")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if not a.out and not a.dry_run:
        a.out = default_out_path(a.inp)
    if a.out:
        out_dir = os.path.dirname(a.out)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
    case_filter = None
    case_type_by_key = {}
    if a.case_csv:
        wanted_types = None
        if a.case_type:
            wanted_types = {x.strip() for x in a.case_type.split(",") if x.strip()}
        case_filter = set()
        with open(a.case_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ctype = (row.get("case_type") or "").strip()
                if wanted_types and ctype not in wanted_types:
                    continue
                task_id = (row.get("task_id") or "").strip()
                session_id = (row.get("session_id") or "").strip()
                if task_id and session_id:
                    key = (task_id, session_id)
                    case_filter.add(key)
                    if ctype:
                        case_type_by_key[key] = ctype
        print(f"[case-filter] loaded {len(case_filter)} sessions from {a.case_csv}", file=sys.stderr)
    prompts = load_prompts(a.prompts)
    strong, weak = (None, None) if a.dry_run else build_clients(a)
    done = set()
    if a.out and not a.dry_run and not a.overwrite:
        done = base.load_done(a.out)
        if done: print(f"[resume] {len(done)} already-graded sessions found; skipping them.", file=sys.stderr)
    outf = (open(a.out, "a") if (a.out and not a.dry_run and not a.overwrite)
            else (open(a.out, "w") if a.out else None))
    n = {"done": 0, "skip": 0}
    def session_id_of(src, fallback):
        """Full GT-shaped session id: path under .../sessions/ without .json, so a subagent file
        <TASK>/sessions/<parent>/subagents/agent-X.json becomes '<parent>/subagents/agent-X'
        (a bare stem fallback only when src carries no /sessions/ segment)."""
        norm = (src or "").replace("\\", "/")
        if "/sessions/" in norm:
            sid = norm.split("/sessions/", 1)[1]
            return sid[:-5] if sid.endswith(".json") else sid
        return str(fallback)
    def task_id_of(src):
        norm = (src or "").replace("\\", "/")
        return os.path.basename(norm.split("/sessions/")[0]) if "/sessions/" in norm else None
    def items():
        for tid, traj, src in base.load_inputs(a.inp, a.glob, with_src=True):
            sid = session_id_of(src, tid)
            task_id = task_id_of(src)
            if case_filter is not None and (task_id, sid) not in case_filter:
                n["skip"] += 1
                continue
            if sid in done or str(tid) in done: n["skip"] += 1; continue  # skip across both id formats
            yield (sid, traj, src)
    def work(item):
        tid, traj, src = item
        meta, grades = judge_pro_v2(traj, prompts, strong, weak, a.seg_budget, a.budget, a.samples,
                                    not a.no_prefilter, not a.no_weak_summary, a.dry_run, a.map_workers,
                                    a.sample_all, a.raw_fit_frac, a.rehydrate_frac, a.only_dim)
        return tid, meta, grades, src
    def on_result(r):
        tid, meta, grades, src = r
        norm = (src or "").replace("\\", "/")
        task_id = task_id_of(src)
        rec = {"task_id": task_id, "session_id": tid,
               "prompts": os.path.basename(a.prompts.rstrip("/")), **meta, "grades": grades}
        ctype = case_type_by_key.get((task_id, tid))
        if ctype:
            rec["case_type"] = ctype
        if outf: outf.write(json.dumps(rec, ensure_ascii=False) + "\n"); outf.flush()
        n["done"] += 1
        if a.dry_run:
            print(f"\n=== {tid} ({meta['_events']} events, {meta['_segments']} segments) ===")
            print(f"    deterministic facts: {meta['_det']}")
            for c in (a.only_dim and [a.only_dim] or CATEGORIES):
                g = grades[c]; tk = f", tokens={g.get('_input_tokens')}" if g.get("_input_tokens") else ""
                print(f"  {c:30s} {g['_method']}{tk}")
        else:
            print(f"=== {tid} ===  [{n['done']} done]")
            for c in (a.only_dim and [a.only_dim] or CATEGORIES):
                g = grades[c]
                print(f"  {c:30s} pass={str(g.get('pass')):5s} conf={g.get('confidence','-')} "
                      f"viol={g.get('violations')} [{g.get('_method')}]")
    run_jobs(items(), work, on_result, 1 if a.dry_run else a.workers)
    if outf: outf.close()
    if not a.dry_run: print(f"\nGraded {n['done']} new, skipped {n['skip']} already-done. -> {a.out}", file=sys.stderr)

if __name__ == "__main__":
    run_main_with_tracebacks(main)

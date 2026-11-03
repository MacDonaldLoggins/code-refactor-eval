#!/usr/bin/env python3
"""
judge_trajectory.py — run the 7 fitted judges over agent trajectories and emit, per category,
{pass, violations, reason}. Built to survive trajectories from a few KB up to TENS OF MILLIONS of
tokens that cannot fit in any context window.

TWO EXECUTION MODES (auto-selected by size; override with --mode):

  FLAT  (small / medium trajectories)
    Tier A  fits as-is                 -> one judge call per category.
    Tier B  structure-aware compression-> fits -> one judge call.
            keep user msgs + assistant text in full; compress tool OUTPUTS (head+tail+evidence
            lines: error|FAIL|passed|exit code|"File unchanged since last read"|truncation marks);
            PER-CATEGORY PROJECTION: efficiency/quality/meaningful drop output bodies (they only
            need the call sequence); hallucination/task/hacky/logical keep compressed bodies.
    Tier C  still too big              -> single-category map-reduce.

  HIERARCHICAL  (huge trajectories: local -> aggregate -> global)
    Stage 0  segment the event stream into ~seg_budget chunks at turn boundaries (no LLM).
    Stage 1  LOCAL MAP (cheap model, ONE shared pass for all 7 dimensions): each segment ->
             a compact JSON digest. CRUCIALLY, grounding is checked LOCALLY here — for each agent
             claim, is it supported by tool output visible IN THIS SEGMENT? (After aggregation the
             raw output is gone, so hallucination MUST be judged where the evidence still exists.)
             Also records files_read / commands_run (for cross-segment repetition tallies),
             test/build results, errors, truncation, candidate per-dimension signals.
    Stage 2  AGGREGATE (deterministic tree): merge segment digests into one global evidence
             ledger — first user request, last final message, claims+grounding, cross-segment
             read/command counts, final test state, observed signals per dimension. Lists are
             deduped and capped so the ledger always fits.
    Stage 3  GLOBAL REDUCE (strong model, 7 calls total, independent of trajectory size): for each
             category, feed the ORIGINAL fitted prompt with {{TRAJECTORY}} = verbatim request +
             verbatim final message + the dimension's slice of the ledger -> final JSON.
    Cost ~ N_segments cheap calls + 7 strong calls. A 10M-token trajectory @80k/seg ~= 125 + 7.

Compression/aggregation are for the JUDGE. The agent's own truncation markers are preserved
verbatim; our omissions are tagged [JUDGE-SIDE ...] so the judge is never misled about what the
agent actually saw.

INPUT (auto-detected): OpenAI/Anthropic message list or {"messages":[...]}, {"events":[...]},
{"session_id","trajectory":"<text>"}, a bare list, or a .txt transcript. .json (obj/list) or
.jsonl (one trajectory per line). For extreme inputs you can also pre-convert to an events JSONL
and point --in at it. Use --dry-run to see the plan + token/segment/call accounting with NO LLM.

MODELS (OpenAI SDK; two independently-configurable tiers — base_url + api_key + model each):
  strong tier (final judgement, FLAT dims): --strong-model/-base-url/-api-key  or STRONG_* env
  weak   tier (HIERARCHICAL local MAP)    : --weak-model/-base-url/-api-key      or WEAK_* env (falls back to strong)
  generic fallback: OPENAI_BASE_URL / OPENAI_API_KEY / MODEL. base_url works with any
  OpenAI-compatible server (vLLM, DeepSeek, Together, local, ...).
  pip install openpyxl tiktoken openai   (tiktoken optional)
"""
import argparse, faulthandler, json, os, re, signal, sys, time, random, traceback
import concurrent.futures as cf
from collections import Counter, defaultdict

CATEGORIES = ["meaningful_trajectory","task_completion","behavioral_efficiency",
              "tool_content_quality","hacky_behavior_free","hallucination_free",
              "logical_and_coherent_actions"]
NEEDS_OUTPUT_BODIES = {"task_completion","hacky_behavior_free","hallucination_free",
                       "logical_and_coherent_actions"}
BIG_OUTPUT = {"hallucination_free"}
# Verbatim reasoning_content (thinking channel) is surfaced ONLY for the dimension that scores content
# density — GT fails tool_content_quality on looping/low-substance reasoning the judge otherwise can't
# see. It is deliberately withheld from every other dimension (esp. hallucination_free, which is the
# routing-measure view and where speculative in-thought claims would drive false over-flags).
REASONING_VISIBLE_DIMS = {"tool_content_quality"}
EVIDENCE_RE = re.compile(
    r"(error|traceback|exception|\bfail(ed|ing|ures?)?\b|\bpassed?\b|\d+\s+(passing|failing|passed|failed|skipped)"
    r"|exit\s*(code|status)|File unchanged since last read|No newline|\boffset\b|\blimit\b"
    r"|Permission denied|does not exist|command not found|panic|assert)", re.I)
TRUNCATION_MARKERS = re.compile(r"(\.\.\.\s*$|\[truncated\]|<truncated>|… )", re.I)

# ----------------------------------------------------------------- tokens
def make_token_counter():
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return lambda s: len(enc.encode(s, disallowed_special=()))
    except Exception:
        return lambda s: max(1, len(s) // 4)
NTOK = make_token_counter()
def est_tokens_fast(events):          # cheap routing estimate, no encoding of huge text
    return sum(len(e.get("text") or "") for e in events) // 4 + 4 * len(events)

# ----------------------------------------------------------------- LLM (OpenAI SDK; per-tier config)
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
RETRYABLE_NAMES  = {"RateLimitError","APITimeoutError","APIConnectionError","InternalServerError",
                    "APIError","Timeout","ServiceUnavailableError","OverloadedError",
                    "MalformedModelResponse"}

class MalformedModelResponse(RuntimeError):
    pass

def _response_preview(r, limit=1200):
    try:
        return r.model_dump_json()[:limit]
    except Exception:
        return repr(r)[:limit]

def _obj_get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)

def _response_text(r):
    text = _obj_get(r, "output_text")
    if isinstance(text, str) and text:
        return text

    pieces = []
    for item in _obj_get(r, "output", []) or []:
        for content in _obj_get(item, "content", []) or []:
            for key in ("text", "refusal"):
                value = _obj_get(content, key)
                if isinstance(value, str) and value:
                    pieces.append(value)
    return "".join(pieces)

def _status_of(e):
    return getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)

def _is_retryable(e):
    if _status_of(e) in RETRYABLE_STATUS: return True
    return type(e).__name__ in RETRYABLE_NAMES

def _retry_after(e):
    h = getattr(getattr(e, "response", None), "headers", None)
    if h:
        v = h.get("retry-after") or h.get("Retry-After")
        if v:
            try: return float(v)
            except Exception: pass
    return None

class Client:
    """One model endpoint = independent base_url + api_key + model. Uses the OpenAI SDK, so it works
       with OpenAI and any OpenAI-compatible server (vLLM, DeepSeek, Together, Azure-compatible,
       local, ...). Has built-in retry with exponential backoff + jitter on rate limits / transient
       errors (respects Retry-After). 'strong' and 'weak' tiers are separate Client instances."""
    def __init__(self, model, base_url=None, api_key=None, name="", max_retries=6,
                 retry_base=1.0, retry_cap=60.0):
        from openai import OpenAI
        if not model:
            sys.exit(f"No model for the '{name or 'llm'}' tier: set --{name}-model / "
                     f"{name.upper()}_MODEL / MODEL.")
        self.model = model; self.name = name
        self.max_retries = max_retries; self.retry_base = retry_base; self.retry_cap = retry_cap
        self._c = OpenAI(base_url=base_url or None, api_key=api_key or "EMPTY", max_retries=0,
                         timeout=600)
    def call(self, system, user, max_tokens=900):
        delay, last = self.retry_base, None
        for attempt in range(self.max_retries + 1):
            try:
                r = self._c.responses.create(
                    model=self.model,
                    instructions=system,
                    input=user,
                    max_output_tokens=max_tokens,
                    store=False)
                content = _response_text(r)
                if content is None:
                    raise MalformedModelResponse(f"empty output text in model response: {_response_preview(r)}")
                if not content:
                    raise MalformedModelResponse(f"empty output text in model response: {_response_preview(r)}")
                return content
            except Exception as e:
                last = e
                if attempt == self.max_retries or not _is_retryable(e):
                    raise
                ra = _retry_after(e)
                sleep = ra if ra is not None else min(self.retry_cap, delay) * (1.0 + random.random() * 0.3)
                print(f"[retry {attempt+1}/{self.max_retries}] {self.name} {type(e).__name__}"
                      f"{(' '+str(_status_of(e))) if _status_of(e) else ''}; sleeping {sleep:.1f}s",
                      file=sys.stderr, flush=True)
                print(f"  Error details: {str(e)}", file=sys.stderr)
                print(f"  Traceback:\n{''.join(traceback.format_tb(e.__traceback__))}",
                      file=sys.stderr, flush=True)
                time.sleep(sleep); delay = min(self.retry_cap, delay * 2)
        raise last

def resolve_tier(tier, args):
    """Resolve (base_url, api_key, model) for a tier. Priority: CLI > <TIER>_* env > OPENAI_*/MODEL."""
    g = lambda k: getattr(args, k, None)
    P = tier.upper()
    base_url = g(f"{tier}_base_url") or os.getenv(f"{P}_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    api_key  = g(f"{tier}_api_key")  or os.getenv(f"{P}_API_KEY")  or os.getenv("OPENAI_API_KEY")
    model    = g(f"{tier}_model")    or os.getenv(f"{P}_MODEL")    or os.getenv("MODEL")
    return base_url, api_key, model

def build_clients(args):
    """Return (strong, weak) Clients. The weak tier falls back to strong when not configured."""
    mr = getattr(args, "max_retries", 6)
    sb, sk, sm = resolve_tier("strong", args)
    strong = Client(sm, sb, sk, name="strong", max_retries=mr)
    wb, wk, wm = resolve_tier("weak", args)
    weak = strong if not wm else Client(wm, wb or sb, wk or sk, name="weak", max_retries=mr)
    return strong, weak

def add_model_args(ap):
    """Shared CLI flags for both tiers (used by both scripts)."""
    ap.add_argument("--strong-model"); ap.add_argument("--strong-base-url"); ap.add_argument("--strong-api-key")
    ap.add_argument("--weak-model");   ap.add_argument("--weak-base-url");   ap.add_argument("--weak-api-key")
    ap.add_argument("--max-retries", type=int, default=6, help="retries per LLM call on 429/transient errors")

def call_llm(client, system, user, max_tokens=900):
    return client.call(system, user, max_tokens)

# ----------------------------------------------------------------- prompts
def load_prompts(prompt_dir):
    out = {}
    for c in CATEGORIES:
        p = os.path.join(prompt_dir, f"{c}.md")
        if not os.path.exists(p): sys.exit(f"Missing prompt: {p}")
        out[c] = open(p).read()
    return out

# ----------------------------------------------------------------- normalize
def _block_text(rc):
    if isinstance(rc, str): return rc
    if isinstance(rc, list):
        return "\n".join((x.get("text","") if isinstance(x, dict) else str(x)) for x in rc)
    return str(rc)

def _content_to_text(content):
    if content is None: return "", [], []
    if isinstance(content, str): return content, [], []
    text, calls, results = [], [], []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict): text.append(str(b)); continue
            t = b.get("type")
            if t in ("text", None) and ("text" in b or "content" in b):
                text.append(b.get("text") or b.get("content") or "")
            elif t == "tool_use":
                calls.append({"name": b.get("name",""), "args": b.get("input", {})})
            elif t == "tool_result":
                results.append({"name": b.get("tool_use_id",""), "text": _block_text(b.get("content"))})
            else:
                text.append(json.dumps(b)[:500])
    return "\n".join(t for t in text if t), calls, results

def normalize_trajectory(obj):
    if isinstance(obj, str): return [{"kind":"raw","text":obj,"name":""}]
    msgs = None
    if isinstance(obj, dict):
        for k in ("messages","events","conversation","turns","trajectory"):
            if k in obj and isinstance(obj[k], list): msgs = obj[k]; break
        if msgs is None:
            for k in ("trajectory","text","transcript","content"):
                if isinstance(obj.get(k), str): return [{"kind":"raw","text":obj[k],"name":""}]
            return [{"kind":"raw","text":json.dumps(obj, ensure_ascii=False)[:200000],"name":""}]
    elif isinstance(obj, list): msgs = obj
    else: return [{"kind":"raw","text":str(obj),"name":""}]
    events = []
    for m in msgs:
        if isinstance(m, str): events.append({"kind":"raw","text":m,"name":""}); continue
        role = m.get("role") or m.get("kind") or "assistant"
        if role == "tool":
            events.append({"kind":"tool_result","text":_block_text(m.get("content")),
                           "name":m.get("name") or m.get("tool_call_id","")}); continue
        text, calls, results = _content_to_text(m.get("content"))
        rc = m.get("reasoning_content")
        rc = _block_text(rc) if isinstance(rc, list) else ("" if rc is None else rc if isinstance(rc, str) else str(rc))
        for tc in (m.get("tool_calls") or []):
            fn = (tc.get("function") or {}); calls.append({"name": fn.get("name",""), "args": fn.get("arguments","")})
        rk = {"user":"user","system":"system","assistant":"assistant","human":"user","model":"assistant"}.get(role,"assistant")
        has_reasoning = rk == "assistant" and rc.strip() != ""
        emitted_assistant = False
        if text.strip() or has_reasoning:
            ev = {"kind":rk,"text":text,"name":""}
            if has_reasoning: ev["reasoning"] = rc
            events.append(ev); emitted_assistant = True
        for i, c in enumerate(calls):
            a = c["args"]; a = a if isinstance(a,str) else json.dumps(a, ensure_ascii=False)
            events.append({"kind":"tool_call","text":a,"name":c["name"],
                           "_new_assistant_turn": rk == "assistant" and not emitted_assistant and i == 0})
        for r in results:
            events.append({"kind":"tool_result","text":r["text"],"name":r.get("name","")})
    return assign_turn_ids(events)

def assign_turn_ids(events):
    """Number local user/assistant turns. Tool calls/results belong to the assistant turn that
       issued them. Raw/system events are intentionally unnumbered."""
    out, turn_id, current_assistant_turn = [], 0, None
    for ev in events:
        ev = dict(ev)
        k = ev.get("kind")
        if k == "user":
            turn_id += 1
            current_assistant_turn = None
            ev["turn_id"] = turn_id
        elif k == "assistant":
            turn_id += 1
            current_assistant_turn = turn_id
            ev["turn_id"] = turn_id
        elif k in ("tool_call", "tool_result"):
            new_assistant_turn = ev.pop("_new_assistant_turn", False)
            if k == "tool_call" and (current_assistant_turn is None or new_assistant_turn):
                turn_id += 1
                current_assistant_turn = turn_id
            if current_assistant_turn is not None:
                ev["turn_id"] = current_assistant_turn
        elif k in ("system", "raw"):
            current_assistant_turn = None
        out.append(ev)
    return out

# ----------------------------------------------------------------- compression
def compress_output(text, head=40, tail=15):
    lines = text.split("\n"); n = len(lines)
    if n <= head + tail + 10: return text
    keep = set(range(head)) | set(range(n - tail, n))
    for i, ln in enumerate(lines):
        if EVIDENCE_RE.search(ln) or TRUNCATION_MARKERS.search(ln): keep.add(i)
    out, gap = [], 0
    for i, ln in enumerate(lines):
        if i in keep:
            if gap: out.append(f"   …[JUDGE-SIDE COMPRESSION: {gap} lines omitted]…"); gap = 0
            out.append(ln)
        else: gap += 1
    if gap: out.append(f"   …[JUDGE-SIDE COMPRESSION: {gap} lines omitted]…")
    return "\n".join(out)

def turn_label(ev, label, show_turns=False):
    tid = ev.get("turn_id")
    return f"[TURN {tid} · {label}]" if show_turns and tid else f"[{label}]"

def _reasoning_snippet(ev, category):
    """Surface a bounded sample of an assistant turn's reasoning_content (thinking channel), but ONLY
       for REASONING_VISIBLE_DIMS. The char count is the load-bearing signal for low_substance/circular-
       reasoning detection; the head/tail sample lets the judge confirm looping without paying for the
       full block, and without leaking speculative in-thought claims into grounding dimensions."""
    if category not in REASONING_VISIBLE_DIMS: return ""
    rc = ev.get("reasoning")
    if not rc: return ""
    n = len(rc)
    samp = rc if n <= 4000 else rc[:3200] + f"\n…[reasoning_content truncated · {n} chars total]…\n" + rc[-600:]
    return f"\n[REASONING_CONTENT · {n} chars]\n{samp}"

def render_event(ev, category, show_turns=False):
    k = ev["kind"]
    if k == "user":      return f"\n{turn_label(ev, 'USER', show_turns)}\n{ev['text']}"
    if k == "system":    return f"\n[SYSTEM]\n{ev['text']}"
    if k == "assistant": return f"\n{turn_label(ev, 'ASSISTANT', show_turns)}\n{ev['text']}{_reasoning_snippet(ev, category)}"
    if k == "raw":       return ev["text"]
    if k == "tool_call":
        a = ev["text"];  a = a if len(a) <= 600 else a[:600] + " …[args truncated]"
        return f"\n{turn_label(ev, 'ASSISTANT TOOL CALL', show_turns)} {ev['name']}({a})"
    if k == "tool_result":
        body = ev["text"] or ""; nlines = body.count("\n") + 1
        if category not in NEEDS_OUTPUT_BODIES:
            ev_lines = [l for l in body.split("\n") if EVIDENCE_RE.search(l)][:6]
            d = f"\n{turn_label(ev, 'ASSISTANT TOOL RESULT', show_turns)} {ev['name']} ({nlines} lines, {len(body)} chars; body omitted for this dimension)"
            if ev_lines: d += "\n  key: " + " | ".join(s.strip()[:120] for s in ev_lines)
            return d
        head, tail = (120, 40) if category in BIG_OUTPUT else (40, 15)
        return f"\n{turn_label(ev, 'ASSISTANT TOOL RESULT', show_turns)} {ev['name']} ({nlines} lines)\n{compress_output(body, head, tail)}"
    return ""

def build_view(events, category, per_event_cap=8000, show_turns=False):
    parts = []
    for ev in events:
        s = render_event(ev, category, show_turns=show_turns)
        if NTOK(s) > per_event_cap and ev["kind"] == "tool_result":
            s = render_event({**ev, "text": "\n".join(ev["text"].split("\n")[:400])}, category, show_turns=show_turns)
        parts.append(s)
    return "\n".join(parts)

# ----------------------------------------------------------------- action skeleton
TOOL_TARGET_KEYS = ("file_path","filePath","path","file","command","cmd","pattern","query",
                    "notebook_path","url","old_string")
READ_TOOLS  = {"read","grep","glob","ls","notebookread"}
EDIT_TOOLS  = {"edit","write","multiedit","notebookedit","apply_patch"}
TEST_RE = re.compile(r"\b(pytest|jest|vitest|go test|npm (run )?test|cargo test|mvn test|gradle test|tox|rspec|phpunit|ctest|build)\b", re.I)

def _target_from_args(args):
    if isinstance(args, str):
        s = args.strip()
        try: args = json.loads(s)
        except Exception: return s[:100]
    if isinstance(args, dict):
        for k in TOOL_TARGET_KEYS:
            if args.get(k): return str(args[k])[:120]
        return json.dumps(args, ensure_ascii=False)[:80]
    return str(args)[:80]

def _result_signal(text):
    if not text: return ""
    for l in text.split("\n"):
        if EVIDENCE_RE.search(l): return l.strip()[:80]
    return f"{text.count(chr(10))+1} ln"

def build_action_skeleton(events, fold=True, max_lines=1400):
    """Deterministic, no-LLM ordered 'what the agent did' skeleton + a stats footer.
       One record per node; tool results are attached to their call; consecutive identical
       records are run-length folded (so tight loops show as ×N)."""
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
            n_calls += 1; name = ev["name"] or "tool"; low = name.lower(); tgt = _target_from_args(ev["text"])
            if low in READ_TOOLS or low in EDIT_TOOLS:
                files[tgt] += 1
                if low in EDIT_TOOLS: n_edit += 1
            if low == "bash":
                cmds[tgt[:60]] += 1
                if TEST_RE.search(tgt): n_test += 1
            seq.append(f"T: {name}({tgt})"); last_call = len(seq) - 1
        elif k == "tool_result":
            sig = _result_signal(ev["text"])
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
    stats = {"tool_calls":n_calls,"assistant_turns":n_assist,"edits":n_edit,"test_runs":n_test,
             "distinct_files":len(files),
             "files_ge2":[(f,c) for f,c in files.most_common() if c>=2][:30],
             "cmds_ge2":[(c,n) for c,n in cmds.most_common() if n>=2][:30],"truncated":truncated}
    return skeleton, stats

def render_stats(stats):
    L = [f"tool_calls={stats['tool_calls']}, assistant_turns={stats['assistant_turns']}, "
         f"edits={stats['edits']}, test_runs={stats['test_runs']}, distinct_files={stats['distinct_files']}"]
    if stats["files_ge2"]:
        L.append("files touched ≥2×: " + ", ".join(f"{f}×{c}" for f, c in stats["files_ge2"]))
    if stats["cmds_ge2"]:
        L.append("commands run ≥2×: " + ", ".join(f"{c[:50]}×{n}" for c, n in stats["cmds_ge2"]))
    return "\n".join(L)

SKELETON_FULL  = {"behavioral_efficiency", "logical_and_coherent_actions"}
SKELETON_STATS = {"task_completion", "tool_content_quality", "hacky_behavior_free"}

def skeleton_prefix(category, skel_text, stats_text):
    if category in SKELETON_FULL and skel_text:
        return (f"== ACTION SKELETON (ordered; consecutive repeats folded ×N) ==\n{skel_text}\n\n"
                f"== ACTION STATS ==\n{stats_text}\n\n")
    if category in SKELETON_STATS and stats_text:
        return f"== ACTION STATS ==\n{stats_text}\n\n"
    return ""

# ----------------------------------------------------------------- parsing
def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m: return {"pass":None,"violations":[],"reason":"unparseable model output"}
    try: o = json.loads(m.group(0))
    except Exception: return {"pass":None,"violations":[],"reason":"unparseable model output"}
    o.setdefault("violations", []); o.setdefault("reason",""); return o

def parse_json_loose(text, default):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m: return dict(default)
    try: return json.loads(m.group(0))
    except Exception: return dict(default)

JUDGE_SYS = "You are a strict evaluator. Follow the rubric exactly and output ONLY the JSON object."

def extract_decisive(events, limit_each=6000):
    users  = [e["text"] for e in events if e["kind"] == "user"]
    finals = [e["text"] for e in events if e["kind"] == "assistant" and e["text"].strip()]
    return ("\n---\n".join(users)[:limit_each], (finals[-1][:limit_each] if finals else ""))

# ================================================================= FLAT mode
MAP_SYS = ("You are scanning ONE chunk of a long agent trajectory to help judge the dimension '{cat}'. "
           "You see only part of it. Using the rubric, list concrete observations relevant to this "
           "dimension. Output ONLY JSON: "
           '{{"observations":[{{"violation_key":"<key or none>","evidence":"<short, specifics>"}}],'
           '"facts":"<=2 sentences a final judge needs"}}. No final pass/fail.')

def judge_category_flat(events, category, template, client, budget, skeleton=("",""), dry_run=False):
    view = skeleton_prefix(category, skeleton[0], skeleton[1]) + build_view(events, category, show_turns=True)
    toks = NTOK(template) + NTOK(view)
    if toks <= budget:
        if dry_run: return {"_dry":True,"_method":"single","_input_tokens":toks}
        out = parse_json(call_llm(client, JUDGE_SYS, template.replace("{{TRAJECTORY}}", view)))
        out["_method"]="single"; out["_input_tokens"]=toks; return out
    # single-category map-reduce
    chunk_budget = max(4000, budget - NTOK(template) - 2000)
    chunks, cur, ct = [], [], 0
    for ev in events:
        s = render_event(ev, category); st = NTOK(s)
        if cur and ct + st > chunk_budget: chunks.append("\n".join(cur)); cur, ct = [], 0
        cur.append(s); ct += st
    if cur: chunks.append("\n".join(cur))
    if dry_run: return {"_dry":True,"_method":"mapreduce","_input_tokens":toks,"_chunks":len(chunks)}
    rubric = template.replace("{{TRAJECTORY}}", "(provided in chunks)")
    merged = []
    for i, ch in enumerate(chunks):
        obs = parse_json_loose(call_llm(client, MAP_SYS.format(cat=category),
              f"RUBRIC '{category}':\n{rubric}\n\nCHUNK {i+1}/{len(chunks)}:\n{ch}", max_tokens=700),
              {"observations":[],"facts":""})
        merged.append({"chunk":i+1, **obs})
    req, fin = extract_decisive(events)
    digest = (skeleton_prefix(category, skeleton[0], skeleton[1]) +
              f"[Too long to read at once; scanned in {len(chunks)} chunks.]\n\n== USER REQUEST ==\n{req}\n\n"
              f"== FINAL ASSISTANT MESSAGE ==\n{fin}\n\n== CONSOLIDATED OBSERVATIONS ==\n"
              f"{json.dumps(merged, ensure_ascii=False, indent=1)}")
    out = parse_json(call_llm(client, JUDGE_SYS, template.replace("{{TRAJECTORY}}", digest)))
    out["_method"]="mapreduce"; out["_input_tokens"]=toks; out["_chunks"]=len(chunks); return out

# ================================================================= HIERARCHICAL mode
def map_render_event(ev):
    """Single rendering for the local MAP pass: keep tool-output bodies (compressed) so grounding
       can be checked locally; head/tail moderate."""
    k = ev["kind"]
    if k in ("user","system","assistant","raw"):
        tag = {"user":"USER","system":"SYSTEM","assistant":"ASSISTANT","raw":"TEXT"}[k]
        return f"\n[{tag}]\n{ev['text']}"
    if k == "tool_call":
        a = ev["text"]; a = a if len(a) <= 400 else a[:400] + " …"
        return f"\n[TOOL CALL] {ev['name']}({a})"
    if k == "tool_result":
        body = ev["text"] or ""; nlines = body.count("\n")+1
        return f"\n[TOOL RESULT {ev['name']}] ({nlines} lines)\n{compress_output(body, 60, 20)}"
    return ""

def render_atom(atom):           # atom = list of events kept together
    return "\n".join(map_render_event(e) for e in atom)
def atom_tokens(atom):
    return sum(NTOK(map_render_event(e)) for e in atom)

def group_atoms(events):
    """Group events into atomic 'steps' that must never be split:
       a tool_call stays with its tool_result, and a tool_result stays with the assistant turn
       that immediately interprets it. A user message always starts a fresh atom."""
    atoms, cur = [], []
    def flush():
        nonlocal cur
        if cur: atoms.append(cur); cur = []
    for ev in events:
        k = ev["kind"]
        if k == "user":
            flush(); atoms.append([ev]); continue
        if k in ("tool_call", "tool_result"):
            cur.append(ev); continue
        # assistant / system / raw
        if cur and cur[-1]["kind"] == "tool_result":
            cur.append(ev); flush()          # this turn interprets the result -> closes the step
        else:
            flush(); cur = [ev]              # a fresh agent action/thought -> new atom
    flush()
    return atoms

def split_big_atom(atom, seg_budget):
    """Last-resort split of a single oversized atom, by events; hard-cap a monster output."""
    pieces, cur, ct = [], [], 0
    for ev in atom:
        st = NTOK(map_render_event(ev))
        if st > seg_budget:
            ev = {**ev, "text": "\n".join((ev["text"] or "").split("\n")[:1200])}
            st = NTOK(map_render_event(ev))
        if cur and ct + st > seg_budget: pieces.append(cur); cur, ct = [], 0
        cur.append(ev); ct += st
    if cur: pieces.append(cur)
    return pieces

def carry_header(prev_atoms):
    """Small continuity header from the tail of the previous segment. Tagged so the MAP pass
       uses it only for context and does NOT re-count its files/commands/claims."""
    evs = [e for a in prev_atoms for e in a]
    last_assist = next((e["text"] for e in reversed(evs) if e["kind"] == "assistant" and e["text"].strip()), "")
    last_call   = next((e for e in reversed(evs) if e["kind"] == "tool_call"), None)
    last_res    = next((e for e in reversed(evs) if e["kind"] == "tool_result"), None)
    L = ["[CONTEXT FROM PREVIOUS SEGMENT — continuity only; already analyzed, do NOT re-report "
         "its files_read/commands_run/claims/signals]"]
    if last_assist: L.append("prev last note: " + last_assist[:200].replace("\n", " "))
    if last_call:   L.append(f"prev last tool: {last_call['name']}({last_call['text'][:120]})")
    if last_res:
        sig = next((l for l in (last_res["text"] or "").split("\n") if EVIDENCE_RE.search(l)), "")
        if sig: L.append("prev last result signal: " + sig.strip()[:160])
    return "\n".join(L)

def segment_events(events, seg_budget, carry=True):
    """Priority-based segmentation: prefer to start a new segment at a USER boundary; otherwise
       cut at atomic STEP boundaries; only split a step internally as a last resort. Each segment
       after the first gets a small carry-over header for continuity (not counted in tallies)."""
    atoms = group_atoms(events)
    seg_atomlists, cur, ct = [], [], 0
    def flush():
        nonlocal cur, ct
        if cur: seg_atomlists.append(cur); cur, ct = [], 0
    for atom in atoms:
        is_user = (len(atom) == 1 and atom[0]["kind"] == "user")
        at = atom_tokens(atom)
        if at > seg_budget:                       # oversized step -> its own internal splits
            flush()
            for piece in split_big_atom(atom, seg_budget):
                seg_atomlists.append([piece])
            continue
        if is_user and cur:                       # user message starts a fresh segment
            flush()
        if cur and ct + at > seg_budget:          # size-based cut at a step boundary
            flush()
        cur.append(atom); ct += at
    flush()
    out = []
    for i, alist in enumerate(seg_atomlists):
        body = "\n".join(render_atom(a) for a in alist)
        if carry and i > 0:
            body = carry_header(seg_atomlists[i - 1]) + "\n" + body
        out.append(body)
    return out

MAP_DIGEST_SYS = (
"You perform LOCAL analysis of ONE segment of a very long AI coding-agent trajectory (segment "
"{i}/{n}) so that 7 separate judges can later decide globally WITHOUT re-reading raw text. "
"Read the segment and output ONLY this JSON (keep every field short, quote specifics):\n"
'{{"user_request":"<verbatim if a user request appears here, else empty>",'
'"final_answer_fragment":"<assistant text that reads like a final summary/deliverable, else empty>",'
'"claims":[{{"claim":"<a substantive factual claim the agent makes: line numbers, file contents, '
'test results, success/failure>","grounded":"yes|no|unknown","evidence":"<what tool output in THIS '
'segment supports or contradicts it; note if the supporting read was truncated>"}}],'
'"files_read":["<path>"],"commands_run":["<cmd>"],'
'"tests":{{"ran":true_or_false,"result":"pass|fail|mixed|none","detail":"<short>"}},'
'"errors_or_truncation":["<short notes: errors, exit codes, wrong-path loops, \'File unchanged since '
'last read\', truncated outputs>"],'
'"signals":[{{"category":"<one of: meaningful_trajectory, task_completion, behavioral_efficiency, '
'tool_content_quality, hacky_behavior_free, hallucination_free, logical_and_coherent_actions>",'
'"evidence":"<concrete behavior relevant to that dimension>"}}],'
'"produced_deliverable":true_or_false}}\n'
"If a [CONTEXT FROM PREVIOUS SEGMENT] header is present, use it ONLY to understand continuity — do "
"NOT list its files/commands/claims as occurring in this segment.\n"
"CRITICAL: judge grounding LOCALLY — only this segment's tool outputs can support its claims.")

MAP_DEFAULT = {"user_request":"","final_answer_fragment":"","claims":[],"files_read":[],
               "commands_run":[],"tests":{"ran":False,"result":"none","detail":""},
               "errors_or_truncation":[],"signals":[],"produced_deliverable":False}

def map_segment(seg_text, i, n, client, max_tokens=1100):
    raw = call_llm(client, MAP_DIGEST_SYS.format(i=i+1, n=n), seg_text, max_tokens=max_tokens)
    d = parse_json_loose(raw, MAP_DEFAULT)
    for k, v in MAP_DEFAULT.items(): d.setdefault(k, v if not isinstance(v, (list,dict)) else type(v)())
    return d

def aggregate_digests(digests, cap=50):
    req = next((d["user_request"] for d in digests if d.get("user_request")), "")
    fin = ""
    for d in digests:
        if d.get("final_answer_fragment"): fin = d["final_answer_fragment"]
    claims, files, cmds, errs = [], Counter(), Counter(), []
    sig = defaultdict(list); produced = False; last_state = "none"
    for d in digests:
        for c in d.get("claims") or []:
            if isinstance(c, dict): claims.append(c)
        for f in d.get("files_read") or []: files[str(f)] += 1
        for c in d.get("commands_run") or []: cmds[str(c)] += 1
        for e in d.get("errors_or_truncation") or []: errs.append(str(e))
        for s in d.get("signals") or []:
            if isinstance(s, dict) and s.get("category") in CATEGORIES:
                sig[s["category"]].append(str(s.get("evidence",""))[:240])
        t = d.get("tests") or {}
        if t.get("ran"): last_state = t.get("result", last_state)
        if d.get("produced_deliverable"): produced = True
    # ungrounded claims first
    claims.sort(key=lambda c: 0 if str(c.get("grounded")).lower()=="no" else 1)
    def dedup(lst, k):
        seen, out = set(), []
        for x in lst:
            key = x[:120]
            if key in seen: continue
            seen.add(key); out.append(x)
            if len(out) >= k: break
        return out
    return {
        "request": req, "final_answer": fin, "produced_deliverable": produced,
        "final_test_state": last_state,
        "claims": claims[:cap],
        "repeated_files": [(f, n) for f, n in files.most_common() if n >= 2][:30],
        "n_files_read": sum(files.values()), "n_distinct_files": len(files),
        "repeated_commands": [(c, n) for c, n in cmds.most_common() if n >= 2][:30],
        "n_commands": sum(cmds.values()),
        "errors": dedup(errs, 30),
        "signals": {c: dedup(sig[c], 40) for c in sig},
    }

def project_ledger(ledger, category):
    L = [f"[The trajectory was too large to read directly; it was analyzed in segments via "
         f"local→global aggregation. Raw text is not shown. Grounding was verified LOCALLY per "
         f"segment. Consolidated evidence ledger for dimension '{category}':]",
         f"\n== USER REQUEST (verbatim) ==\n{ledger['request']}",
         f"\n== FINAL ASSISTANT MESSAGE (verbatim) ==\n{ledger['final_answer']}",
         f"\n== GLOBAL FACTS ==",
         f"deliverable_produced: {ledger['produced_deliverable']}",
         f"final_test_state: {ledger['final_test_state']}",
         f"tool calls: {ledger['n_commands']} commands, {ledger['n_files_read']} file reads "
         f"({ledger['n_distinct_files']} distinct)"]
    if category in ("behavioral_efficiency","logical_and_coherent_actions","tool_content_quality"):
        if ledger["repeated_files"]:
            L.append("files read >=2x: " + ", ".join(f"{f}×{n}" for f, n in ledger["repeated_files"]))
        if ledger["repeated_commands"]:
            L.append("commands run >=2x: " + ", ".join(f"{c[:60]}×{n}" for c, n in ledger["repeated_commands"]))
    if ledger["errors"]:
        L.append("\n== ERRORS / TRUNCATION EVENTS (sample) ==\n- " + "\n- ".join(ledger["errors"]))
    if category in ("hallucination_free","task_completion","logical_and_coherent_actions") and ledger["claims"]:
        L.append("\n== AGENT CLAIMS & LOCAL GROUNDING (ungrounded first) ==")
        for c in ledger["claims"]:
            g = str(c.get("grounded","?")).lower()
            tag = "UNGROUNDED" if g == "no" else ("grounded" if g == "yes" else "unknown")
            L.append(f"- [{tag}] {str(c.get('claim',''))[:200]} — {str(c.get('evidence',''))[:200]}")
    sigs = ledger["signals"].get(category, [])
    if sigs:
        L.append(f"\n== OBSERVED SIGNALS FOR THIS DIMENSION ==\n- " + "\n- ".join(sigs))
    return "\n".join(L)

def judge_hierarchical(events, prompts, strong, weak, seg_budget, budget, skeleton=("",""),
                       map_workers=1, dry_run=False, verbose=False):
    segs = segment_events(events, seg_budget)
    meta = {"_events": len(events), "_segments": len(segs), "_method": "hierarchical"}
    if dry_run:
        meta["_map_calls"] = len(segs); meta["_final_calls"] = len(CATEGORIES)
        return meta, {c: {"_dry":True,"_method":"hierarchical","_segments":len(segs)} for c in CATEGORIES}
    if verbose: print(f"    [map {len(segs)} segments × {map_workers} workers]", file=sys.stderr)
    digests = run_map(segs, weak, map_workers)
    ledger = aggregate_digests(digests)
    grades = {}
    for c in CATEGORIES:
        digest = skeleton_prefix(c, skeleton[0], skeleton[1]) + project_ledger(ledger, c)
        # safety: if a dimension's ledger slice is huge, hard-trim
        if NTOK(prompts[c]) + NTOK(digest) > budget:
            digest = digest[: budget * 3]
        out = parse_json(call_llm(strong, JUDGE_SYS, prompts[c].replace("{{TRAJECTORY}}", digest)))
        out["_method"] = "hierarchical"; out["_segments"] = len(segs)
        grades[c] = out
    return meta, grades

# ================================================================= parallelism
def run_map(segs, weak, workers):
    """Run the per-segment local MAP calls, concurrently when workers>1, preserving segment order."""
    n = len(segs)
    if workers <= 1:
        return [map_segment(s, i, n, weak) for i, s in enumerate(segs)]
    out = [None] * n
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(map_segment, s, i, n, weak): i for i, s in enumerate(segs)}
        for f in cf.as_completed(futs):
            out[futs[f]] = f.result()
    return out

def _item_id(it):
    return it[0] if isinstance(it, (tuple, list)) and it else "?"

def _log_item_error(item, exc):
    print(f"[error] session {_item_id(item)} failed, skipped (resume will retry): "
          f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    print("[traceback]", file=sys.stderr, flush=True)
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    sys.stderr.flush()

def run_jobs(item_iter, work_fn, on_result, workers):
    """Process items concurrently with a bounded thread pool (≤ workers in flight at once, to cap
       memory for huge trajectories). Heavy work runs in worker threads; on_result is called in the
       main thread one result at a time, so output writing needs no lock. A single item that fails
       after all retries is logged and skipped (NOT written), so a later resume re-attempts it."""
    if workers <= 1:
        for it in item_iter:
            try: res = work_fn(it)
            except BaseException as e:
                _log_item_error(it, e)
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                continue
            on_result(res)
        return
    it = iter(item_iter); futs = {}; exhausted = False
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        def fill():
            nonlocal exhausted
            while not exhausted and len(futs) < workers:
                try: nxt = next(it)
                except StopIteration: exhausted = True; break
                futs[ex.submit(work_fn, nxt)] = nxt
        fill()
        while futs:
            done_set, _ = cf.wait(futs, return_when=cf.FIRST_COMPLETED)
            for f in done_set:
                item = futs.pop(f)
                try: res = f.result()
                except BaseException as e:
                    _log_item_error(item, e)
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
                else:
                    on_result(res)
            fill()

def install_runtime_diagnostics():
    """Make nohup/background logs useful even without `python -u`.

    `PYTHONFAULTHANDLER=1` only helps for fatal interpreter crashes. Most failures here are caught
    Python exceptions inside worker threads or top-level code, so we explicitly line-buffer logs and
    install a top-level traceback wrapper below.
    """
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except Exception:
        pass
    for sig_name in ("SIGUSR1", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            faulthandler.register(sig, file=sys.stderr, all_threads=True, chain=True)
        except Exception:
            pass

def run_main_with_tracebacks(main_fn):
    install_runtime_diagnostics()
    try:
        return main_fn()
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"[fatal] top-level SystemExit: {e.code}", file=sys.stderr, flush=True)
            print("[traceback]", file=sys.stderr, flush=True)
            traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
            sys.stderr.flush()
        raise
    except BaseException as e:
        print(f"[fatal] unhandled top-level exception: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        print("[traceback]", file=sys.stderr, flush=True)
        traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
        sys.stderr.flush()
        raise

# ================================================================= dispatch
def judge_trajectory(traj_obj, prompts, strong, weak, budget, mode="auto", seg_budget=60000,
                     map_workers=1, hier_threshold=400000, dry_run=False, verbose=False):
    events = normalize_trajectory(traj_obj)
    est = est_tokens_fast(events)
    skel_text, stats = build_action_skeleton(events)
    skeleton = (skel_text, render_stats(stats))
    use_hier = (mode == "hierarchical") or (mode == "auto" and est > hier_threshold)
    if use_hier:
        if verbose: print(f"  mode=hierarchical (~{est} tok)", file=sys.stderr)
        return judge_hierarchical(events, prompts, strong, weak, seg_budget, budget,
                                  skeleton, map_workers, dry_run, verbose)
    if verbose: print(f"  mode=flat (~{est} tok)", file=sys.stderr)
    grades = {}
    for c in CATEGORIES:
        if verbose: print(f"  [{c}]", file=sys.stderr)
        grades[c] = judge_category_flat(events, c, prompts[c], strong, budget, skeleton, dry_run)
    return {"_events": len(events), "_method": "flat"}, grades

# ----------------------------------------------------------------- io / cli
def load_inputs(path, glob_pat=None, with_src=False):
    """Accept a file (.json/.jsonl/.txt) OR a directory. For a directory, recursively pick up
       session files (default: **/*.jsonl and **/*.json; override with glob_pat) and treat each
       .json file as one trajectory, each .jsonl line as one trajectory.
       with_src=True additionally yields the source file path so callers can recover context the
       JSON itself doesn't carry (e.g. the enclosing TASK-* directory): items become
       (tid, traj, src_path) instead of (tid, traj)."""
    import glob as _glob
    if os.path.isdir(path):
        pats = [glob_pat] if glob_pat else ["**/*.jsonl", "**/*.json"]
        seen = set()
        for pat in pats:
            for f in sorted(_glob.glob(os.path.join(path, pat), recursive=True)):
                if f in seen or os.path.isdir(f): continue
                seen.add(f); yield from load_inputs(f, with_src=with_src)
        return
    stem = os.path.splitext(os.path.basename(path))[0]
    emit = (lambda tid, obj: (tid, obj, path)) if with_src else (lambda tid, obj: (tid, obj))
    if path.endswith(".jsonl"):
        for i, line in enumerate(open(path)):
            line = line.strip()
            if not line: continue
            o = json.loads(line)
            sid = (o.get("session_id") or o.get("id")) if isinstance(o, dict) else None
            yield emit(str(sid or f"{stem}:{i}"), o)
    elif path.endswith(".json"):
        o = json.load(open(path))
        if isinstance(o, list):
            for i, x in enumerate(o):
                sid = (x.get("session_id") or x.get("id")) if isinstance(x, dict) else None
                yield emit(str(sid or f"{stem}:{i}"), x)
        else:
            sid = (o.get("session_id") or o.get("id")) if isinstance(o, dict) else None
            yield emit(str(sid or stem), o)
    else:
        yield emit(stem, open(path).read())

def load_done(out_path):
    """Checkpoint: return the set of ids already present in an existing JSONL output, and rewrite
       the file to drop any corrupt/partial trailing line, so an interrupted run resumes cleanly."""
    done, valid = set(), []
    if out_path and os.path.exists(out_path):
        with open(out_path) as f:
            for line in f:
                s = line.rstrip("\n")
                if not s.strip(): continue
                try: o = json.loads(s)
                except Exception: continue          # drop partial line from a killed write
                key = (o.get("session_id") or o.get("id")) if isinstance(o, dict) else None
                if key is not None:
                    done.add(str(key)); valid.append(s)
        with open(out_path, "w") as f:
            for s in valid: f.write(s + "\n")
    return done

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True, help="file OR directory of session files")
    ap.add_argument("--glob", default=None, help="when --in is a dir: file pattern (default **/*.jsonl + **/*.json)")
    ap.add_argument("--prompts", default="judge_prompts_deep")
    ap.add_argument("--out")
    ap.add_argument("--mode", choices=["auto","flat","hierarchical"], default="auto")
    add_model_args(ap)   # --strong-model/-base-url/-api-key, --weak-model/-base-url/-api-key
    ap.add_argument("--budget", type=int, default=100000, help="max input tokens per judge call")
    ap.add_argument("--seg-budget", type=int, default=60000, help="tokens per segment (hierarchical)")
    ap.add_argument("--hier-threshold", type=int, default=400000, help="auto: go hierarchical above this many est tokens")
    ap.add_argument("--workers", type=int, default=4, help="trajectories graded concurrently (thread pool)")
    ap.add_argument("--map-workers", type=int, default=4, help="concurrent local-MAP calls within one huge trajectory")
    ap.add_argument("--overwrite", action="store_true", help="ignore existing --out and restart from scratch")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    prompts = load_prompts(a.prompts)
    strong, weak = (None, None) if a.dry_run else build_clients(a)
    done = set()
    if a.out and not a.dry_run and not a.overwrite:
        done = load_done(a.out)
        if done: print(f"[resume] {len(done)} already-graded sessions found; skipping them.", file=sys.stderr)
    outf = (open(a.out, "a") if (a.out and not a.dry_run and not a.overwrite)
            else (open(a.out, "w") if a.out else None))
    n = {"done": 0, "skip": 0}
    def items():
        for tid, traj in load_inputs(a.inp, a.glob):
            if str(tid) in done: n["skip"] += 1; continue
            yield (tid, traj)
    def work(item):
        tid, traj = item
        meta, grades = judge_trajectory(traj, prompts, strong, weak, a.budget, a.mode, a.seg_budget,
                                        a.map_workers, a.hier_threshold, a.dry_run, a.verbose)
        return tid, meta, grades
    def on_result(r):
        tid, meta, grades = r
        rec = {"id": tid, "prompts": os.path.basename(a.prompts.rstrip("/")), **meta, "grades": grades}
        if outf: outf.write(json.dumps(rec, ensure_ascii=False) + "\n"); outf.flush()
        n["done"] += 1
        if a.dry_run:
            extra = f", segments={meta.get('_segments')}, map_calls={meta.get('_map_calls')}" if meta.get("_segments") else ""
            print(f"\n=== {tid}  ({meta['_events']} events, mode={meta['_method']}{extra}) ===")
            for c in CATEGORIES:
                g = grades[c]; ch = f", chunks={g.get('_chunks')}" if g.get("_chunks") else ""
                tk = f" input_tokens={g.get('_input_tokens')}" if g.get("_input_tokens") else ""
                print(f"  {c:30s} method={g['_method']}{tk}{ch}")
        else:
            print(f"=== {tid}  (mode={meta['_method']}) ===  [{n['done']} done]")
            for c in CATEGORIES:
                g = grades[c]
                print(f"  {c:30s} pass={str(g.get('pass')):5s} viol={g.get('violations')} [{g.get('_method')}]")
    run_jobs(items(), work, on_result, 1 if a.dry_run else a.workers)
    if outf: outf.close()
    if not a.dry_run: print(f"\nGraded {n['done']} new, skipped {n['skip']} already-done. -> {a.out}", file=sys.stderr)

if __name__ == "__main__":
    run_main_with_tracebacks(main)

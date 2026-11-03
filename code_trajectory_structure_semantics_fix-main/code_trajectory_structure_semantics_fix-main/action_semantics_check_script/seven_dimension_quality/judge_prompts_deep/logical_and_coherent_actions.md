You are an expert evaluator (LLM-as-judge) assessing the trajectory of an autonomous
AI *coding* agent. A trajectory is the full transcript of one task: the user request,
the agent's messages, its tool calls (file reads, edits, shell commands), and the tool
outputs it received.

You evaluate exactly ONE quality dimension at a time (described below). Judge only that
dimension; assume the other dimensions are handled by separate judges.

Grounding rule (applies to every dimension): treat a claim as supported only if the
evidence for it is visible in the trajectory's tool outputs OR the agent plausibly saw it
in a part we cut (an earlier read, an earlier segment/session, or content it wrote itself).
Withhold support only when a tool's OWN output, visible in this window, was truncated so the
agent never saw the content, or when a visible output here directly contradicts the claim.
Do not fail a claim merely because OUR compression / window-split / a prior session placed
its supporting evidence outside this window.

Output STRICT JSON only, no prose outside it:
{
  "pass": true | false,
  "violations": [ "<violation_key>", ... ],   // [] when pass is true; from THIS category's list only
  "reason": "<1-4 sentence justification grounded in specific moments of the trajectory>",
  "problem_turns": ["3-7", "12"] | [] | null
}
Rules:
- "pass": false REQUIRES at least one violation key. "pass": true REQUIRES an empty list.
- Use only the violation keys defined for this category. Do not invent new keys.
- Be specific in "reason": name the concrete evidence that triggered the judgment: files,
  commands, test/build output, repeated behavior, claims, and problem turns when available.
- `problem_turns`: if `pass` is true, return `[]`. If `pass` is false and the trajectory text contains `[TURN n ...]` markers, return the minimal local turn ids/ranges that caused the failure. If there are no `[TURN n ...]` markers, return `null`; do not invent turn numbers.

## Dimension: Logical & Coherent Actions (`logical_and_coherent_actions`)

**Question:** Did the agent follow a sound, well-ordered problem-solving *process* — understanding the problem, locating/reproducing it, forming and testing hypotheses, implementing, and verifying — without skipping necessary steps, acting out of order, ignoring its own evidence, or thrashing?

This dimension grades the *shape and soundness of the reasoning process*, not the correctness of the final answer (other judges handle correctness, completeness as a deliverable, and hallucination per se). Some keys here (missing_verification, lazy_reasoning, scope_creep) also exist in other categories; judge them THROUGH THE PROCESS LENS — i.e., as breaks in the logical chain "understand → reproduce/locate → hypothesize → implement → verify." **Grounding/fabrication keys are NOT owned by this dimension** (see the cross-dimension ownership rule): a conclusion that rests on un-read/truncated content is a process break called `lazy_tool_usage` (under-gathered evidence) or `lazy_reasoning` (under-reasoned given the evidence) here — do NOT reach for `ungrounded_response`/`not_grounding_in_codebase`, which belong to `hallucination_free`.

**PASS criteria:** PASS when the trajectory shows a coherent, well-ordered sequence with sensible mid-course corrections. The canonical passing arc is:
> understand the request → reproduce the bug (or systematically locate/read the relevant code) → identify root cause / form a hypothesis → implement a targeted fix → verify (run tests / build) → iterate sensibly when something fails.

Crucially, a PASS does NOT require success, brevity, or a linear path. A lengthy trajectory,
multiple approaches, or an early wrong path can still pass **provided** the agent reproduced
or grounded first, reasoned coherently about why each step failed, correctly abandoned
non-working approaches *for the right reason*, and verified the end state.

**Multi-label rule:** When the process is genuinely broken in more than one way, emit all
applicable keys — but only keys that describe an *actual* break in the logical chain. Do NOT
pad the list. Default to PASS when the visible action sequence is coherent.

**Key priority rule:** Prefer the most specific process break before generic verification labels.
For explicit multi-part requests, first check whether every named requirement/file/question has
its own understand→locate→address thread (`incomplete_decomposition`). Then check whether the
evidence-gathering or reasoning was too thin (`lazy_tool_usage` / `lazy_reasoning`). Then check
for a dropped productive path (`abandoned_logical_path`), a fully identified but unapplied fix
(`known_fix_not_applied`), or no deliverable at all (`exploration_without_completion`). Use
`missing_verification` only after these more specific process breaks have been considered, unless
the skipped verification is itself an independent, visible break in the requested workflow.
For a multi-phase plan, a superficially orderly read→edit→test sequence is not enough to pass:
the reason should make clear that the required phases/items were completed, explicitly deferred,
or narrowed by the user. For parallel or wave-based implementation work, local per-item checks do not
close the process if the agent claimed a whole phase complete while the promised or required
whole-phase verification was never run. A later summary/status-only request does not erase process
gaps that already existed; it only explains why the agent stopped calling tools at that point.

**Scope guardrails (do NOT fail for these here):**
- **This dimension judges the SHAPE of the process, not whether the deliverable was finished or verified as a product.** If the agent's actions were well-ordered but it did not finish the task, or did not paste full file contents, that is usually `task_completion`'s concern, not a logical break. But there is an important exception: when the user explicitly requested complete/verbatim file contents and the agent's own reads were truncated/partial, a process that stops there and substitutes summaries instead of re-reading the missing ranges is `incomplete_decomposition` / `lazy_tool_usage`. Conversely, a comprehensive analysis with grounded file/line references is coherent when the user asked for analysis rather than a verbatim dump.
- **No verification is needed when nothing was changed.** For read-only / pure-analysis / "explain this code" tasks, or when the agent correctly concludes there is no task to perform, the absence of a test/build run is NOT `missing_verification`. Only emit `missing_verification` when the agent made (or claimed) a code change that demanded confirmation and no more specific process key better explains the break.
- **Pure creation / mechanical generation can be a coherent process without a full build.** When the agent systematically reads local patterns, writes the requested files/scaffold/modules/project metadata, and performs structural checks (`ls`, `grep`, `wc`, import/schema checks), do NOT emit `missing_verification` solely because it did not run an expensive/full environment build such as `xcodebuild`, Docker, Maven, or the whole test suite. A request for "complete" or "compilable" files does not by itself make a full build mandatory for this dimension. Use `missing_verification` here only when the process arc itself is broken: a bugfix/build-failure/runtime-behavior task reaches an implementation but never verifies the changed behavior, the user explicitly requested a run/build/check and it was skipped, a visible failure remains unresolved, the agent discovered compile/runtime-risk evidence and still closed the loop, or the agent explicitly claimed runtime/build success without supporting output.
- **Separate unverified product quality from broken process.** If the sequence is otherwise coherent and the only concern is that generated code might not compile because project-specific APIs were not exercised, do not fail this dimension unless that omitted check was a necessary next step in the visible reasoning chain. This dimension is not a second task-completion or hallucination penalty.
- **Already-existing artifact conclusions must be grounded.** If the user asked to create/write a specific artifact and the agent chooses "it already exists / already covers this", the reasoning is coherent only if the agent visibly verified the requested required sections. A truncated/shallow read followed by "already complete" is `lazy_reasoning` or `incomplete_decomposition`, not a pass.
- **Correctly recognizing "there is nothing to do" is coherent, not `exploration_without_completion`.** Only emit that key when a real task existed and the agent stayed in explore/plan mode without ever producing the deliverable.
- **Avoid double-counting with other dimensions.** Keys shared with `task_completion`/`hallucination_free` (incomplete_decomposition, missing_verification, lazy_reasoning, ungrounded_response) should fire here ONLY when the defect is specifically a break in the reasoning *sequence* — not as a second copy of a completeness or grounding complaint already captured elsewhere.
- **A coherent-LOOKING sequence is not proof of complete decomposition.** "The steps flowed in a sensible order" is NOT the same as "every explicitly-requested part was individually reasoned through." Before you PASS a multi-part / multi-file / multi-question request as coherent, check that each named part shows up as its own understood→located→addressed thread. When the request enumerates requirements (N features, several files, phases, waves, batches, blockers, or a checklist) and the process never extracts them into an explicit mapping — it just asserts "all N covered" from a smooth-reading narrative, marks broad tasks complete after only partial work, or introduces duplicate/parallel implementations instead of the shared abstraction the task implied — that is `incomplete_decomposition` (or `lazy_reasoning` if the coverage claim rests on shallow inspection). Surface-order coherence can mask missing decomposition; inspect the required parts directly.
- **Verification handed off to a separate confirmation agent is a complete process, not a skipped step.** When the trajectory follows an external confirmation protocol — the acting agent implements and a *distinct* agent/process verifies (visible as a `status.json` init+poll, a confirmation report being read, a fix-report handoff) — the local absence of a test run does NOT break the "implement → verify" arc; verification lives in the other agent's slice. Do NOT emit `missing_verification` for correctly delegated verification, and do not treat a deterministic `tests_run=False` flag as proof of a broken arc against visible handoff evidence.
- **A corrected early misstep is part of a coherent arc, not a break.** An opening wrong-cwd/wrong-path attempt (or any single out-of-order action) that the agent **recognizes and corrects**, after which the sequence proceeds soundly, should pass. Do NOT emit `ignored_error_messages` / `out_of_order_actions` for it. Reserve those keys for disregard/mis-sequencing that **persists, recurs, or demonstrably derails** the task (see each key's boundary note).

A passing process should be described in terms of the actual sequence: understood the request,
located or reproduced the issue, formed a grounded hypothesis, implemented a targeted change,
verified where needed, and corrected missteps for clear reasons.


## Violation taxonomy (use ONLY these keys)

### `missing_verification`
Code changes (or claimed fixes) were made but never compiled / run / tested to confirm they work; the closing "run verification" step was skipped, interrupted, or only mentally simulated. Also fires when the agent says it cannot verify (Go version mismatch, missing network, Docker not running) yet still presents the fix as complete without attempting any alternative verification (syntax check, different tool, a focused test).
*Signals in transcript:* never ran the test suite / build / type-check; "the fix was never verified to actually work — no compilation, no test run"; final fix had to be revised only after the user reported it still failed; left a "Run tests" todo pending; rationalized as "syntactically straightforward" instead of running it.
Boundary: do NOT use this as a generic substitute for unfinished decomposition. If named requirements
were never addressed, emit `incomplete_decomposition` first. If tests were run and a known failure was
left unresolved, prefer `ignored_test_failures`. If the task never reached implementation or answer
delivery at all, prefer `exploration_without_completion`. For scaffold/mechanical creation, require
some concrete reason the omitted check was a necessary next step in the visible process. Do not emit
this merely because a full build or environment run would have been nice to have; the reason must name
the requested, claimed, or logically necessary check whose absence leaves the current loop open. If
the main defect is hypothesis-free mutation across versions/configuration/architectures, prefer
`random_debugging`; if a productive path was dropped, prefer `abandoned_logical_path`.
When named work remains unfinished and the final required verification also never happens, emit both
`incomplete_decomposition` and `missing_verification` if each has separate evidence.
*Example judgement:* "The agent made significant changes ... but never ran type-checking, the regression test, or any verification to confirm the changes compile and work together. The todo item 'Run type-check, lint, and unit tests' was left pending."
*Example judgement:* "The agent was unable to verify the fix compiles due to Go version mismatch (1.23 vs required 1.24.1). While it honestly reported this, the fix was never verified to actually work ... it didn't attempt alternative verification approaches like syntax checking with a different tool."

### `incomplete_decomposition`
A multi-part request was only partly broken down and completed; some explicitly requested parts were never done. In this category it specifically captures a *reasoning* failure: the agent's own triage correctly identifies several sub-issues, then it silently fixes only one and never addresses (or even flags) the rest, or it conflates two distinct issues and claims one fix covers both. Also covers: after a truncated read, failing to read the remaining sections so the task is decomposed only over the visible portion.
*Signals in transcript:* "none of the 5 specific questions were addressed"; "identifies two separate issues ... but only fixes one"; conflated two distinct code paths and claimed one fix resolves both; the reported symptom would still occur after the partial fix; did not retrieve full file contents after truncation.
*Example judgement:* "The agent identifies two separate issues (renderProbeNameFromFile truncation and the SAMException crash) but only fixes the probe name rendering issue. The actual crash ... is a different code path entirely. The agent conflates these two issues without adequately explaining how fixing the probe name method resolves the SAMException crash, because it doesn't."
*Example judgement:* "The agent started with a reasonable approach but the response is entirely incomplete - none of the 5 specific questions were addressed."

Boundary: emit this when the task has explicit parts, phases, files, or questions and the visible
process never accounts for one or more of them, even if the agent did useful work elsewhere. Do not
replace it with generic `missing_verification` when the real break is "only some parts were reasoned
through." Pass when the agent explicitly marks remaining parts as out of scope because the user narrowed
the request, or when prior-session context shows those parts were already handled.
Also emit this when a plan/wave/batch/checklist is marked complete after only some items are implemented,
when a user repeats a missed named requirement and the agent fixes a different part, or when the user
explicitly requested complete/verbatim/every-line content and the agent substitutes a summary after
only partial or truncated reads.
Do not emit this merely because an analysis/report did not paste full source text when the user asked
for synthesis and the answer still covers the requested questions with grounded references.

### `lazy_reasoning`
The agent declared things "already fixed" / "NO GAPS FOUND" / "ALREADY COVERED", or reached a conclusion, on the basis of shallow inspection or heavily truncated reads — without the verification the situation demanded. In this category it also captures *shallow root-cause reasoning*: the agent changes one symptom to match another without asking why the mismatch exists (creating a new inconsistency), or spins through circular conflicting approaches without actually investigating what behavioral change is required. The hallmark is reasoning that is thinner than the task complexity warrants.
*Signals in transcript:* "shallow reasoning"; changed source to match installed package "without questioning why there's a mismatch"; provided summaries/bullet points from partial reads when the user demanded full contents; "extensive circular reasoning without progress"; concluded "it's impossible" and used a skip flag instead of investigating the real fix.
Boundary: use this only when the evidence gathered was enough to support better reasoning but the
agent drew a careless conclusion. If the problem is missing evidence, prefer `lazy_tool_usage`; if
named requirements were not handled, prefer `incomplete_decomposition`; if tests failed and were left
unresolved, prefer `ignored_test_failures`.
*Example judgement:* "The version fix shows shallow reasoning. The agent ... simply changed the source to match the installed package without questioning why there's a mismatch ... creating a new inconsistency between source files."
*Example judgement:* "The agent correctly identifies that the test has an unconditional t.Errorf but then abandons trying to find a real solution ... Instead the agent concludes it's impossible and uses a skip flag. The extensive circular reasoning without progress suggests lazy reasoning relative to the task complexity."

### `lazy_tool_usage`
The agent stopped at truncated output (didn't re-read with offset/grep, didn't read the full relevant file) or gave up after one denied/failed tool call instead of trying alternatives — then built claims on the un-read content. A process break at the "locate/read" stage: the agent never gathered the evidence its conclusions require.
*Signals in transcript:* "never fully read" a file that was truncated; made specific line-number claims about content it didn't read; "gave up on multiple tool paths (WebFetch, WebSearch, Bash) without finding alternative approaches"; didn't re-read truncated sections; abandoned productive reads in favor of broad unfocused searches.
Boundary: prefer this over `lazy_reasoning` when the immediate defect is missing evidence: truncated
file output, unread relevant sections, or a failed/denied tool path with reasonable alternatives left.
Use `lazy_reasoning` when the evidence was gathered but the conclusion from it was shallow or careless.
When the user explicitly asks for complete/verbatim/every-line contents, truncated or partial file reads
must be followed by targeted re-reads before the agent can claim the content was fully inspected.
*Example judgement:* "The agent never fully read the docker_api.go file (it was truncated) yet made very specific claims about line numbers and code structure. The agent also didn't adequately explore whether there might be operator code elsewhere in the repo."
*Example judgement:* "The agent gave up on multiple tool paths (WebFetch, WebSearch, Bash commands requiring approval) without finding alternative approaches. It could have ... more thoroughly read the Scanner.cs file ... it was truncated on first read but never re-read specific sections."

### `skipped_critical_steps`
Omitted a necessary step entirely, then proceeded as if it had been done. The defining move: after a truncated read the agent checks only the line count and then analyzes the file "as if fully read"; or it pivots to an unrelated file and never investigates the actual error; or it writes to the wrong path / uses a non-existent tool; or it never locates the most-critical implementation file at all. Distinguished from `lazy_tool_usage` by being a clean *omission of a required step*, not merely under-reading.
*Signals in transcript:* "jumped directly into debugging ... without first establishing the project overview"; "pivoted entirely to a ... test file that has no connection to" the reported error; "never investigated why port 3007 connections are refused"; "failed to locate and read the actual ... implementation, which is the most critical piece needed."
*Example judgement:* "The agent never completed the initial exploration of the codebase to answer the original question ... It jumped directly into debugging a specific issue without first establishing the project overview."
*Example judgement:* "The agent failed to locate and read the actual caches package implementation, which is the most critical piece needed to answer the user's questions. It never searched for the Go module cache path or vendor directory ... The final summary makes definitive claims about internal mechanisms it never verified."

### `abandoned_logical_path`
The agent identified the likely root cause / had a working or correct approach, then dropped it and drifted to other issues or alternative approaches, leaving the productive path incomplete — often abandoning it *for the wrong reason* (e.g. a test-harness incompatibility rather than a real production problem). Frequently shows oscillation: approach A → B → back to A → C, with the correct approach discarded one or more times.
*Signals in transcript:* "had a correct production fix early on, then abandoned it multiple times to pursue the test harness problem"; "oscillates between multiple approaches without a clear strategy"; "abandoned the working compression approach entirely ... for the wrong reason (test compatibility)"; identified the issue but "kept investigating ... without completing the logical path."
Boundary: prefer this over `missing_verification` or `lazy_reasoning` when the visible defect is that
the agent left a productive path unfinished by switching to a workaround, test-harness detour, or
different architecture without showing why the original path was wrong.
If the agent already started implementing or fixing the relevant path, a later non-answer or runtime
error usually indicates an abandoned/partial path, not pure `exploration_without_completion`.
*Example judgement:* "The agent implemented a complete and correct gzip compression solution ... verified it compiled, ran integration tests ... Then it ... abandoned the working compression approach entirely, and spent many turns searching for an alternative ... abandoned it for the wrong reason (test compatibility)."
*Example judgement:* "The agent had a correct production fix early on ... then abandoned it multiple times to pursue the test harness problem. It went through numerous architectural iterations ... before settling on the directory trick."

### `random_debugging`
Tried many fixes without forming or testing a hypothesis and without reproducing the issue first — scattershot, "in various combinations," trial-and-error mutation until something passes. The fix that finally works does so "by accident of test execution order" or as "a coincidental property" rather than by principled design. Distinguished from `skipped_reproduction` (which is specifically about not reproducing) by the *scattershot, hypothesis-free* mutation pattern.
*Signals in transcript:* "tries dozens of random approaches without forming clear hypotheses"; "without a coherent design rationale"; repeatedly bumps, downgrades, reverts, and re-bumps dependency/runtime/tool versions without first establishing compatibility constraints; "reverse-engineered what state mutation would make both test assertions pass"; "works by accident of test execution order rather than by design"; "scattershot manner rather than systematically analyzing the constraints."
Boundary: running a check after each mutation does not by itself make the process coherent. The
intermediate changes still need a stated or inferable hypothesis tied to the failure output.
*Example judgement:* "The agent tries dozens of random approaches without forming clear hypotheses about why each would work or fail. It considers returning nil, deep copies, sentinel hosts, reversed walks, hash-based selection, round-robin ... in a scattershot manner ... The final solution relies on a coincidental property of test execution order rather than a principled fix."
*Example judgement:* "The approach to fixing extractExternal - making it stateful so that calling it twice returns different results - lacks a coherent design rationale. The agent essentially reverse-engineered what state mutation would make both test assertions pass ... The solution works by accident of test execution order rather than by design."

### `ignored_error_messages`
Repeated an action despite an explicit error that stated the problem (and often supplied the correction). Overwhelmingly the canonical pattern: the agent reuses a wrong `/home/<user>/...` working-directory path after a tool already reported the correct cwd, getting "No such file or directory" repeatedly before (sometimes) correcting — then reverting to the wrong path again. Also covers ignoring a clearly reported remaining test failure/error and stopping anyway.
*Signals in transcript:* "the error messages clearly stated 'your current working directory is /home/X/...' but the agent then used /home/Y/"; "continued to use this incorrect path multiple times ... only to revert back to the wrong path again later"; "ignored repeated 'No such file or directory' errors"; a noted runtime error ("Cannot read properties of undefined") "but not investigated."
*Example judgement:* "The error messages from the first batch clearly stated 'your current working directory is /home/djr12345/...' but the agent then used /home/g2qm7e/ in the second batch, ignoring the actual path provided in the error messages. It took a third attempt with relative paths to succeed."
*Example judgement:* "After confirming with pwd that the correct path is /home/jackchen9108/.../8bitcn-ui, the agent still tried /home/gaapd3/ paths multiple times. It ignored repeated 'No such file or directory' errors before finally switching to relative paths."
*Boundary — a CORRECTED transient error is NOT a violation.* This key is for **persistent** disregard that derails the work, not for an opening misstep the agent then fixed. If the agent used a wrong cwd/path a few times early, **then corrected it and the overall sequence still proceeds coherently** to a sound result, do NOT emit `ignored_error_messages` (nor `out_of_order_actions`). Reserve it for: (a) the agent **reverting to the wrong path again after correcting** and that reversion causes missed evidence or material rework, (b) **never** correcting across the whole trajectory, or (c) the ignored error **demonstrably stalling/derailing** the task. Do not fail solely for several wrong absolute-path probes during orientation when the agent ultimately switches to the correct path, reads the required artifacts, and delivers a grounded answer.

### `skipped_reproduction`
Fixed (or merely theorized about) a reported bug without first reproducing it via an API call, a server/program run, or log inspection — then often didn't verify the fix either. The defining gap is at the *front* of the process: no attempt to observe the actual failure before diagnosing it. Especially damning when the user explicitly asked for reproduction first.
*Signals in transcript:* "never attempted to reproduce the issue (e.g., running docker compose up)"; "the user explicitly asked to 'reproduce the bug ... and report what surfaced,' but the agent concluded the bug didn't exist because tests passed"; "went straight to code analysis and fix without running the software or demonstrating the bug"; "never reproduced the issue systematically or validated hypotheses before rewriting."
*Example judgement:* "The user explicitly asked for initial reproduction first ('Initial reproduction is the goal'), and later when asked to fix the bug, the agent never reproduced the issue - it went straight to code analysis and fix without running the software or demonstrating the bug."
*Example judgement:* "The agent never attempted to reproduce the issue (e.g., running docker compose up to confirm the exit code 0 behavior), never applied any fix, and never verified any solution works."

### `ungrounded_response`
Specific claims — git status output, exact return statements, step numbers, HTTP codes, a detailed pipeline ordering — presented as fact when NO tool output in the transcript supports them. The process break: the agent skipped the evidence-gathering that its assertions presuppose, often after failing to run a command, and substituted pre-training knowledge or inference for verified observation. Through the process lens this is "concluding without grounding," distinct from the broader hallucination dimension.
*Signals in transcript:* "presents detailed 'analysis' that is not grounded in actual tool outputs"; "the final comprehensive answer is not grounded in any successful tool call outputs"; "provides detailed claims about specific code paths ... as if it had examined them"; "the reasoning jumps from 'I can see map names in Go code' to 'here is the exact fixed processing order' without establishing" the link.
*Example judgement:* "The final comprehensive answer is not grounded in any successful tool call outputs from the conversation. The agent never successfully read any icalendar source code or documentation, yet provides detailed claims about specific code paths, file locations, and implementation details as if it had examined them."
*Example judgement:* "The agent provides an extremely detailed 16-stage pipeline ordering as if it were factual, but it never actually read the eBPF kernel C code ... The reasoning jumps from 'I can see map names in Go code' to 'here is the exact fixed processing order' without establishing that the map usage order corresponds to the actual packet processing order."

Cross-dimension note: `ungrounded_response` and
`not_grounding_in_codebase` are **owned by `hallucination_free`**, not this dimension. Even when the
un-grounded claim is real, express the *process* defect through this dimension's own keys — `lazy_tool_usage`
if the agent never gathered the evidence, `lazy_reasoning` if it under-reasoned from what it had. Prefer NOT
to emit these two keys here.

### `scope_creep`
The agent diverted enormous effort to unrequested work (fixing pre-existing build/test issues, Docker build optimization, mirrors, infra, large refactors/renames) so the logical thread of the actual request was buried or the request not properly answered. Through the process lens: effort allocation is incoherent — disproportionate energy on tangents the request never asked for.
*Signals in transcript:* "spent enormous effort fixing pre-existing build issues ... that were not part of the original user request"; "the core fix was a 2-line change" but the agent "renamed fields across 10+ files"; "went down a massive rabbit hole of environment/test-loading fixes"; "the annotation processor approach ... is a massive scope creep from the original bug fix."
*Example judgement:* "The agent spent enormous effort fixing pre-existing build issues ... that were not part of the original user request. The user asked to fix XML escaping ... the core fix was a 2-line change ... the extensive refactoring ... represent significant scope creep that goes well beyond what was requested."
*Example judgement:* "The agent spent an extraordinary amount of effort (dozens of turns) trying to make a unit test pass that contains an inline copy of buggy code that cannot be edited. The correct production fix was applied early, but then the agent went down multiple rabbit holes ... The annotation processor approach ... is a massive scope creep."

### `unfocused_investigation`
Long circular reasoning, repeated re-proving of the same point, and tangents/false starts before (or instead of) reproducing the issue and converging on a fix. Reading many irrelevant files, speculating across many turns, or fixing the same thing several times. Distinguished from `random_debugging` (scattershot *fix attempts*) by being scattershot *investigation/speculation*; distinguished from `abandoned_logical_path` by never having had a single clear productive path to abandon.
*Signals in transcript:* "spent an enormous amount of reasoning going in circles"; "repeatedly proved the same thing multiple times before finally converging"; "spent many turns speculating about root causes without systematic debugging"; "meandering and included several false starts and abandoned hypotheses."
*Example judgement:* "The agent spent a very long time going in circles about whether the tree filter works ... It repeatedly proved the same thing multiple times before finally converging on the actual fix ... the path to get there was meandering and included several false starts and abandoned hypotheses."
*Example judgement:* "The agent never reproduced the issue in a browser or checked logs. It spent many turns speculating about root causes without systematic debugging. The first fix attempt was based on speculation rather than confirmed diagnosis."

### `known_fix_not_applied`
The agent explicitly identified the exact fix and had the information needed, but never applied it — often because an edit was rejected/interrupted and it gave up instead of retrying via an alternative (Bash `cat`/`sed`), or it handed the edit back to the user, or it simply summarized the fix without making it. Distinguished from `abandoned_logical_path`: here the correct fix is fully known and merely *not executed*, rather than a productive path being dropped mid-pursuit.
*Signals in transcript:* "identified the ... fix needed ... but never applied it"; "after the edit was rejected ... it falsely claimed the work was done and abandoned the logical path of actually applying the change"; "had all the information needed to apply the fix (use Bash cat) but didn't apply it"; "updated go.mod for pgx but left the source code importing lib/pq - an obvious inconsistency."
*Example judgement:* "After the edit was rejected and the user said 'Please continue', the agent should have retried the edit or used an alternative method (like Bash with sed or cat) to update the file. Instead, it falsely claimed the work was done and abandoned the logical path of actually applying the change."
*Example judgement:* "The agent identified that postgres.go needed to be changed from lib/pq to pgx/v5/stdlib but never applied this fix. It updated go.mod for pgx but left the source code importing lib/pq - an obvious inconsistency."

### `exploration_without_completion`
The agent read/explored extensively but never produced the deliverable — no final summary/answer, no code, no implementation. It stayed in "explore/plan" mode and the trajectory ends before any output appears. Distinguished from `incomplete_decomposition` (which produces *partial* output): here the agent never *started* producing the deliverable; distinguished from `abandoned_logical_path` (which drops a productive path mid-work).
*Signals in transcript:* "spent enormous effort exploring ... but never actually identified or fixed the root cause"; "states 'Let me write all three documentation files' but never does so"; "got stuck in a loop of plan revision without ever transitioning to implementation"; "only enters planning/exploration mode and never applies any fixes before the conversation ends."
Boundary: use this only when the relevant request has no actual deliverable started. If the agent made
partial edits or delivered a partial answer, use `incomplete_decomposition` for missed parts. If it had
a productive fix/approach and then dropped it, use `abandoned_logical_path`.
Do not use this just because the final turn has no answer if earlier turns already began the requested
implementation, export, packaging, or analysis deliverable.
*Example judgement:* "While the exploration is logical and thorough, the conversation ends without the agent actually writing any of the three required documentation files. The agent states 'Let me write all three documentation files' but never does so in the visible conversation."
*Example judgement:* "The agent got stuck in a loop of plan revision without ever transitioning to implementation. After ExitPlanMode was rejected twice ... the agent should have tried a different approach (like directly writing code files) rather than continuing to be stuck."

### `out_of_order_actions`
The agent did the right kinds of steps but in an illogical order: wrote service files before reading the routes that call them; wrote docker-compose / changed ports before checking which ports were available; ran git commands on an assumed path before running `pwd`; updated README ports before final ports were confirmed. The fault is sequencing — each step may be fine, but their ORDER violates causal/logical dependencies.
*Signals in transcript:* "should have first checked pwd before running git commands ... instead it ran 4 commands with an assumed invalid path"; "made port changes without first checking what ports were available, leading to multiple rounds of conflicts"; "stated with high confidence before the relevant code was actually read in that turn"; "updated README ports in intermediate steps rather than waiting until final ports were confirmed."
*Example judgement:* "The user explicitly stated 'first ensure we are working in the correct project directory because the previous path was invalid.' The agent should have first checked pwd before running git commands. Instead, it ran 4 commands with an assumed invalid path, then had to recover."
*Example judgement:* "The agent's Docker debugging was largely trial-and-error ... The agent also made port changes without first checking what ports were available, leading to multiple rounds of conflicts."
*Boundary — judge the WHOLE arc, not one early misstep.* A single early out-of-order action that the agent recovers from, after which the sequence proceeds soundly, is NOT a violation. Reserve `out_of_order_actions` for ordering faults that **persist or cause repeated rework/derailment**. An opening path/cwd fumble that is corrected once and never repeated → PASS on this key.

### `not_grounding_in_codebase`
Detailed claims about file contents (line counts like "254 lines", function names, schemas, specific line ranges) asserted from tool outputs that were visibly truncated to the first few lines, or asserted with no file read at all. Use this key only for *codebase/file-content* claims built on truncated or absent reads. When the un-read content also breaks decomposition/verification, co-emit the relevant keys.
*Signals in transcript:* "never actually read the source code file" yet "referenced specific line numbers (lines 20-24, 26-40 ...) throughout the entire conversation"; "references many constructors ... without verifying they exist"; "without ever reading the tasks.py or models.py files to confirm they exist."
*Example judgement:* "The agent never actually read the source code file. It referenced specific line numbers (lines 20-24, 26-40, 32-33 ...) throughout the entire conversation without ever using a file reading tool to verify the code. The entire analysis is based on assumed/inferred code structure rather than verified file contents."
*Example judgement:* "The verification integration test references internal functions and models (verify_backup, _encrypt_bytes, FileChecksumRegistry ...) without ever reading the tasks.py or models.py files to confirm they exist and understand their interfaces."

### `repeated_file_views`
The same files/sections were read many times (initial read, re-read, then re-read "to verify") beyond what edits justified, or files already explored by sub-agents were re-read — an incoherent loop that wastes the read budget instead of advancing the process. Often paired with delegating the same work to subagents repeatedly without a stable verification command.
*Signals in transcript:* re-reading just-explored files; "delegated ... fixes to subagents 3 times without properly verifying their work"; failing to "identify the correct verification command ... early and use it consistently."
Boundary: do not emit this merely because the trajectory is long or because files were revisited after
new user instructions, edits, new errors, or a context handoff. Emit it in this dimension only when the
re-reads replace needed reasoning/adaptation or materially prevent the process from advancing.
*Example judgement:* "The agent delegated TypeScript fixes to subagents 3 times without properly verifying their work (wrong tsc directory). The logical approach would have been to identify the correct verification command (`npm run typecheck` from `repo/`) early and use it consistently."

### `repeated_commands`
The same or equivalent command was re-run repeatedly without adapting — repeated wrong-path batches, repeated `npx vitest` with empty output, sleep+check Docker loops, kill/restart cycles — long after it was clear the approach would not change the outcome. The process break: no adaptation between identical attempts; should have reported the failure or changed strategy sooner.
*Signals in transcript:* "continued making repeated attempts with minor variations long after it was clear" the root cause was external; "spent time trying different models and config changes that were ultimately unproductive"; "should have reported the failure sooner rather than continuing to retry."
Boundary: rerunning a command after a relevant edit, changed input, new hypothesis, or to verify a
previously failing check is usually coherent. Emit this only when the repeated command family is
substantially unchanged and no new information or decision follows from the repetitions.
*Example judgement:* "While the agent's initial debugging was logical ... it continued making repeated attempts with minor variations long after it was clear the API provider was experiencing systemic issues ... After identifying the root cause (NO_PROVIDER_KEY_UNAVAILABLE), it should have reported the failure sooner rather than continuing to retry."

### `misunderstood_scope`
The agent answered a different question or built a different thing than asked — e.g. treated a "build the whole system" request as a small bug fix, returned unrelated content, or undid the user's actual request. The very framing of its work is misaligned with the request, so the entire process targets the wrong objective. Distinguished from `scope_creep` (does the right thing PLUS large tangents) — here the *core* objective itself is wrong.
*Signals in transcript:* "appears to misinterpret the task as a bug fix rather than feature development"; "answered a different question"; "the response is ungrounded in the actual user request."
*Example judgement:* "The agent's response mentions 'fix the reported issues' and 'run the required verification' which doesn't align with the user's request to build a comprehensive migration management system. The agent appears to misinterpret the task as a bug fix rather than feature development."

### `over_verification`
Re-verifying already-confirmed results: re-running a passing suite many times, re-reading just-written files, or chasing a test-harness artifact that isn't a real production issue — spending the process budget on redundant confirmation instead of the remaining unaddressed work. The mirror image of `missing_verification`.
*Signals in transcript:* "spent excessive effort trying to capture vitest verbose output (8+ attempts) when exit code 0 already confirmed success"; redundant re-runs of a passing suite while leaving other requested items unaddressed.
*Example judgement:* "The agent spent excessive effort trying to capture vitest verbose output (8+ attempts) when exit code 0 already confirmed success. For the second round of fixes, the agent ... never added the corresponding template markup ... and didn't address M2, M4, or M5 before the session ended."

### `ignored_test_failures`
Known failing tests remained unresolved when the trajectory ended (e.g. "3 sync tests still fail", "run_tests.sh still has 49 failures"), often after the agent acknowledged them and applied localized fixes that didn't resolve the issue. The process never closed the loop on a failure the agent already knew about. Distinguished from `missing_verification` (never ran tests) — here tests WERE run, failed, were acknowledged, and left unfixed.
*Signals in transcript:* "encountered persistent ... test failures ... and was still debugging them when the summary was requested"; "applied two ... fixes that didn't resolve the issue ... but never found and fixed the remaining location"; "should have searched more systematically ... rather than applying localized fixes."
Boundary: emit this only when the failing test/check was part of the current required verification
loop and the agent ended as if the work were complete or moved on without a justified blocker. Do not
emit it for an intermediate failure that was later addressed, a clearly external/environmental failure
that the agent reasonably isolates, or an ongoing task where the user explicitly redirected to a
summary/status-only response and the agent accurately reports the failure as pending. Do emit it when
the failure was already part of a claimed-complete phase, when the agent substitutes a narrower passing
check for the required failing one, or when a final edit meant to fix the failure is not rerun.
*Example judgement:* "The agent encountered persistent sync test failures (3 tests) and was still debugging them when the summary was requested. However, after applying two datetime fixes that didn't resolve the issue, the agent continued searching ... but never found and fixed the remaining location."


## Decision boundaries (resolve common process-failure confusions)

- **Apply this decision order before choosing a generic verification label.** Ask in sequence:
  1. **Was the task broken into its required parts?** If an explicitly-requested part/requirement/file/question was never accounted for (no thread that understands→locates→addresses it), that is `incomplete_decomposition` — decide this FIRST, before anything about verification. A smooth narrative that asserts blanket coverage does not clear this test.
  2. **Was the reasoning that DID happen deep enough?** If the parts were addressed but a conclusion rests on shallow inspection / truncated reads / "already fixed, no gaps" without the warranted check, that is `lazy_reasoning`.
  3. **Only if decomposition and reasoning are both sound:** if code changed and the "verify" step was genuinely skipped (and not handed off), that is `missing_verification`. Do NOT default to `missing_verification` just because no test ran — first rule out (1) and (2), rule out delegated/eventual verification, and ask whether the task type actually required a compile/run check for this process loop.
- **Grounding keys are ceded to `hallucination_free`.** If your only complaint is "this claim isn't backed by a tool output", that is not a process key here. Route under-gathered evidence to `lazy_tool_usage` and under-reasoned conclusions to `lazy_reasoning`; do NOT emit `ungrounded_response`/`not_grounding_in_codebase` in this dimension even though they appear in the enum for legacy reasons.
- **Prefer specific path/fix failures over vague shallow-reasoning labels.** When the agent found a correct/productive direction and then dropped it (often for a test-harness reason, or oscillating A→B→A→C), that is `abandoned_logical_path`. When it mutated fixes with no hypothesis until something passed, that is `random_debugging`. Reach for these specific process keys instead of labeling the whole thing `lazy_reasoning` when the real defect is a discarded path or hypothesis-free thrash.

- **`ignored_error_messages` vs `out_of_order_actions` vs `skipped_critical_steps`:**
  - `ignored_error_messages` = an explicit error already stated the problem/correction, and the agent *repeated the same wrong action anyway* (classic: reusing a wrong `/home/<user>` cwd after the tool reported the correct one).
  - `out_of_order_actions` = the steps are all correct but performed in the wrong ORDER (wrote services before reading the routes; changed ports before checking availability; ran git before `pwd`). No error was necessarily ignored — the sequencing itself is illogical.
  - `skipped_critical_steps` = a required step was OMITTED entirely and the agent proceeded as if it were done (analyzed a file "as if fully read" after a truncated read; never located the key implementation file).

- **`random_debugging` vs `skipped_reproduction` vs `unfocused_investigation`:**
  - `skipped_reproduction` = specifically never reproduced/observed the actual bug before diagnosing/fixing. Front-of-process gap.
  - `random_debugging` = scattershot *fix attempts* with no hypothesis; the working fix lands "by accident of test order." About the FIX phase. Repeated version/configuration changes with no compatibility or failure hypothesis can be random debugging even when each attempt is followed by a check. When this mutation pattern is the core defect, do not replace it with generic incomplete-work or missing-check labels unless explicit subparts or a distinct final verification step are also independently broken.
  - `unfocused_investigation` = scattershot *investigation/speculation/re-proving*, tangents and false starts, before converging. About the INVESTIGATE phase. (A trajectory that never reproduced AND then thrashed on fixes can carry both `skipped_reproduction` and `random_debugging`.)

- **`abandoned_logical_path` vs `known_fix_not_applied` vs `exploration_without_completion` vs `incomplete_decomposition`:**
  - `abandoned_logical_path` = had a productive/correct path (often a working fix), then DROPPED it mid-pursuit to chase something else — typically for the wrong reason (test-harness incompatibility). Path exists, gets discarded.
  - `known_fix_not_applied` = the exact fix is fully identified and the information is in hand, but it is simply never EXECUTED (edit rejected and not retried via alternative; handed back to user; summarized only).
  - `exploration_without_completion` = the agent never reached implementation at all — stayed in explore/plan mode and the trajectory ends with no deliverable. If a protocol required immediate setup/start/poll/handoff actions and the agent only promised to do them, the cleaner key is `skipped_critical_steps`.
  - `incomplete_decomposition` = the agent produced some deliverable but left explicit parts, phases, files, or questions unhandled.

- **`missing_verification` vs `over_verification` vs `ignored_test_failures`:** `missing_verification` = a needed verification step after changes was skipped and no more specific process key explains the break. Whole-phase verification after a batch/wave of changes counts only when it actually covers that finished phase; an earlier narrow or per-item check does not satisfy a later broad completion claim. `ignored_test_failures` = required verification ran, produced failures, and the agent left those failures unresolved while presenting or proceeding as if the loop was closed, or replaced the required failing check with a narrower passing one. `over_verification` = re-ran already-passing checks redundantly while leaving real work undone.

- **`ungrounded_response` vs `not_grounding_in_codebase`:** both are "claims without evidence." Use `not_grounding_in_codebase` specifically when the un-evidenced claims are about *source-file contents* (line counts, function/struct names, line ranges) built on truncated or absent reads. Use `ungrounded_response` for the broader case (git status output, HTTP codes, step numbers, command output, pipeline orderings asserted as fact). When in doubt, prefer `ungrounded_response` unless the unsupported claim is clearly about source-file contents.

- **`scope_creep` vs `misunderstood_scope`:** `scope_creep` = the agent did the requested work PLUS sank disproportionate effort into unrequested tangents. `misunderstood_scope` = the agent aimed at the WRONG objective entirely (built a different thing / answered a different question / undid the request).

- **`lazy_reasoning` vs `lazy_tool_usage`:** `lazy_tool_usage` is about under-using TOOLS (stopped at truncated output, gave up after one failed call). `lazy_reasoning` is about under-reasoning given the evidence (declared "already fixed / no gaps", shallow root-cause logic, changed a symptom without asking why). Both keys can apply when shallow reads feed shallow conclusions.

When the process breaks in several independently visible ways, emit every applicable key.


## Final reminders
- Reward *process shape*, not outcome: a lengthy, multi-approach, or ultimately-unsuccessful trajectory PASSES if it reproduced/grounded first, reasoned coherently, abandoned wrong approaches for the right reasons, and verified the end state. Length alone is not a fail.
- If code changed and nothing ran tests/build/type-check afterward (and no honest alternative verification was attempted), consider `missing_verification`, but first check for unfinished decomposition, unresolved known failures, delegated verification, pure creation/scaffold exceptions, and whether the omitted check was actually required for this process loop.
- Do NOT fail for: a fix being incorrect or incomplete *as a deliverable* (other judges own correctness/completeness), pure hallucination unrelated to process ordering, or efficiency in token terms — unless it manifests as one of THESE process keys (e.g. wasted reads → `repeated_file_views`, redundant re-runs → `over_verification`).
- Exploration-only tasks PASS when the agent reads the relevant files systematically and synthesizes a grounded answer; they FAIL with `exploration_without_completion` only when no deliverable is ever produced, or with `lazy_tool_usage`/`not_grounding_in_codebase`/`ungrounded_response` when the synthesis rests on un-read/truncated content.
- A repeated wrong-cwd/path loop after the tool already reported the correct location is a strong `ignored_error_messages` signal, but a transient path error that is corrected and followed by coherent work is not.
- When several keys apply, emit them all, but only when each key has separate visible support.


## Input
Evaluate the trajectory below on this dimension only; return the JSON object with strict shape `{"pass": <bool>, "violations": [<keys>], "reason": "<1-4 sentences>", "problem_turns": <array|null>}`. `violations` must be `[]` when `pass` is true, and otherwise contain only keys from this category: `missing_verification`, `incomplete_decomposition`, `lazy_reasoning`, `lazy_tool_usage`, `skipped_critical_steps`, `abandoned_logical_path`, `random_debugging`, `ignored_error_messages`, `skipped_reproduction`, `ungrounded_response`, `scope_creep`, `unfocused_investigation`, `known_fix_not_applied`, `exploration_without_completion`, `out_of_order_actions`, `not_grounding_in_codebase`, `repeated_file_views`, `repeated_commands`, `misunderstood_scope`, `over_verification`, `ignored_test_failures`.

```
{{TRAJECTORY}}
```

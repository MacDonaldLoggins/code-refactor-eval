# Unit Test RL Rules 0.10.2

## 1. Version and flow

The formal 0.10.2 structure consists of three independent flows:

```text
Prepare Flow
Candidate Generation Flow
Candidate Evaluation Flow
```

The three flows connect only through immutable artifacts and explicit IDs. They must not be chained automatically or select `latest` implicitly.
Versions 0.8.0–0.8.3 may be migrated by read-only copy; 0.9.1 accepts only a published and verified Prepared
Package; a 0.10.1 preparation package may only be copied to a 0.10.2 draft and semantically upgraded. Historical candidates may migrate their test
source and original trajectories, but historical scores must not be inherited; all validation and scoring must run again.

The formal inheritance identity for this version is:

```text
0.10.1 / engine-revision-015 / contract-v38
→ 0.10.2 / engine-revision-016 / contract-v39
```

The incorrect-baseline `0.10.0 / contract-v24` belongs only to the experimental task sequence and is not a formal predecessor. Existing experimental
tasks remain bound to their original snapshots, but their preparation packages, scoring results, and contracts must not be marked as 0.10.2/v39
artifacts under these rules.

## 2. Prepared Package

S00–S24 write only to `active/`. On completion, create:

```text
prepared_runs/drafts/prepared-vXXX
```

The Prepared Package contains the Repository, Workspace, Behavior, fault pool, E2-v2 obligations,
English Prompt, English J1–J5 Rubrics, Verifier, environment lock, HC, and Reward configuration.

The Prepared Package is not bound to a Candidate or Judge model and must declare:

```text
model_binding=none
```

A Draft may be edited manually, but every edit must record its path, operator, reason, sequence number, and component revision.
A Published Prepared Package is read-only and must not be overwritten or edited in place. When the Prompt, Rubrics, Verifier, or
any other content changes, create the next Prepared ID.

When accepting a preparation package from 0.9.1, its source must be exactly `0.9.1 / engine-revision-013 /
contract-v36`, published, verified, and read-only as a complete tree. Acceptance may use only `copy` mode; Repository,
Candidate Input, Evaluation Assets, Runtime, and Environment must remain byte-for-byte identical, with only provenance tracking and the target task/engine/contract identity rewritten. The source and target business-payload hashes and the Task Core,
Generation Bundle, and Scoring Bundle must match, after which D00 must pass again.

D00 validates structure, the English Prompt/Rubric text, the Verifier, dependency closure, skip policy, and runtime fingerprint.
At publication, compute the Task Core, Generation Bundle, Scoring Bundle, and complete-content fingerprints.

## 3. Environment

After S02 admits the PR, S02E must discover dependencies from the repository's formal configuration, run the relevant official tests in a clean environment,
audit skip reasons, install the formally declared dependencies, and then lock Python, distributions, import probes, and the runtime tree hash.

After freezing, do not install dependencies temporarily during Candidate or Evaluation. The import name supplied to `pytest.importorskip()`
must be genuinely available; exit code 0 alone is not sufficient to validate the environment.

## 4. Prompt and Rubrics

Candidate input contains only the deterministically synthesized English `user_prompt.md`, public summary, Workspace constraints, and manifest;
the Repository is provided through an independent read-only snapshot. Behavior summaries, Rubrics, faults, Oracles, reference tests,
answer-like evidence, other candidates, sealed assets, and credentials must not enter candidate input.

The Prompt must contain only:

```text
## 1. Repository and PR information
## 2. Rules
```

When the public summary is not English, an independent translation model may normalize it, but the final Prompt must be rendered deterministically by a fixed template.

Each J1–J5 Rubric may contain only the four fields `rubric_id`, `direction`,
`score_1_condition`, and `score_0_condition`; the root of each dimension file is an array of scoring items.
The two conditions describe the complete observable state for the item and must not use templated scoring language such as “score zero,” “score one,” “no points,” or
“otherwise.” J5 applicability is maintained by an independent candidate-preparation frozen mapping and must not be put back into the
Rubric. The Rubric validation report must bind the current file SHA-256; a non-English explanatory sentence, a missing or stale report, or a forged report fails closed.

## 5. Candidate Generation Flow

Each run explicitly selects one Published Prepared Package. The owner-only registry stores shared connection information in
`providers[]`, and each Model references a Provider through `provider_key`; one Provider may register multiple Candidate/Judge Models.

Candidate Flow accepts an ordered model array: repeat `--candidate-model-key` on the CLI or use
`UNITTEST_RL_CANDIDATE_MODEL_KEYS`. Explicit CLI values take precedence; the singular variable is compatibility-only. The array must be non-empty and model keys must be unique. Each model in the array runs independently, once by default; with
`--runs-per-model=N`, create `number of models × N` Candidate Runs in total. Freeze the Provider, URL, model name/revision, request parameters, and system prompt separately for each Run; do not share Session,
Workspace, or Submission.

Directory names:

```text
candidate_runs/<prepared-id>/
  candidate--<model-artifact-name>--<sequence>/
```

Within a Task, sequence numbers increase globally by model artifact name, do not reset across Prepared versions, and are never reused after retirement.
Candidate generation models and parameters are for run provenance only and do not enter the Task Core or Candidate Set compatibility Gate.

Every Candidate must save:

- Prepared binding and the actual Candidate Input;
- a redacted model snapshot of the generation policy;
- Workspace before/after;
- the complete observable Session request/response, tool input/output, and command results;
- boundary validation;
- an answer-leakage report;
- a read-only Submission.

A candidate may read only its own input snapshot, Workspace, and declared runtime, and may write only
`candidate_tests/` and `.candidate_tmp/`. All file access goes through the proxy and commands through the physical sandbox;
the network is disabled by default. Any out-of-bounds attempt fails the Candidate, even when the attempt is blocked.

## 6. Answer leakage

Before freezing the Submission, S27L reads the candidate's actual system/user input, model requests, tool results, command
output, and complete trajectory and compares them with sealed reference facts. The report must identify the location, source, leakage type, and exact information.

Only the following may freeze a Submission and enter formal scoring:

```text
answer_leakage_status=clean
official_score_eligible=true
```

`suspected`, `confirmed`, `not_auditable`, or an incomplete trajectory all fail closed.

## 7. Candidate Set

Before Candidate Evaluation Flow begins, explicitly freeze the Candidate Set:

```text
candidate_sets/candidate-set--<name>--<sequence>/
```

The set records member order, Candidate ID, generation model, generated Prepared version, and Submission SHA-256.
Members may come from different Prompts, Candidate models, and parameters, but must share the same
`task_core_fingerprint`. After publication, the set cannot gain or lose members; even a single candidate must use a singleton set.
Task Core excludes generation/scoring models, the semantic content of `user_prompt`, and J1–J5 Rubrics, and includes Repository,
Workspace constraints, Behavior, faults, E2 obligations, Verifier, runtime, dependencies, skip policy,
Reward, and other sealed facts. Therefore only Prompt, Rubric, and model differences may be compared freely; every other input change
requires a separate Candidate Set.

## 8. Candidate Evaluation Flow

Each scoring run explicitly selects:

- the Published Prepared Package used for scoring;
- the Candidate Set;
- the currently selected ordered Judge model array.

Directory names:

```text
evaluation_runs/
  evaluation--<candidate-set-slug>--<judge-model-slug>--<sequence>/
```

Repeat `--judge-model-key` on the CLI or use `UNITTEST_RL_JUDGE_MODEL_KEYS`. Each Judge Model in the array creates an independent Evaluation Batch; models do not share J results, Reward,
or batch state. Freeze scoring-model parameters only for their own Evaluation Batch. The same Candidate Set
may be scored multiple times with one model or with any other model. The Prepared package used for scoring may differ from the generation
Prepared package, but the Task Core must match.

Within a batch, each Candidate uses its own `input_snapshot/`, Workspace, HC, E1, E2-v2, J1–J5,
Reward, and Trace. All members of a set use the same Judge snapshot, so results can be ranked directly within the batch.
Results from different Judge-model batches are not averaged, compensated, or merged automatically. Cross-model comparisons require an explicit comparison of the batch results.

## 9. Hard gates, E1, and E2-v2

HC-04 recomputes the environment contract and runtime fingerprint for the Evaluation Member snapshot. HC-03 must
read the pytest JUnit XML; it passes only when exit=0, failed=0, errors=0, and unexpected_skipped=0.

An `importorskip` caused by a missing formal dependency is `environment_invalid`; an unapproved
candidate skip in a complete environment is `candidate_fail`; a skip during fault replay must not be classified as survived.

Stop scoring that member when the Candidate cause fails. Record environment, tool, or evidence anomalies as `invalid`; they cannot be converted to a score of 0.

`SC-E2-v2` is the only formal E2:

Each obligation is judged only from three positive candidate-test executions on the target version. A formal execution witness consists of a complete parameterized
pytest nodeid, frozen preconditions, the target production call, a behavior-result event, and the same test execution to which they belong; all three runs must witness the same complete nodeid. A return, exception, or warning proves only that an observable event
occurred; E2 does not judge its exact value, type, message, or candidate assertion correctness.

Formal E2 input must not include candidate source text, AST or token matches, fault-replay classification, SC-E1 results,
or SC-J1–SC-J5 results. E2 output must not be used as J1–J5 input. Record `not_activated` when the target call never occurs;
record `execution_incomplete` when the call occurs but no complete stable witness forms; record `satisfied` when all three stable witnesses are complete. Observation, collection, or reporting anomalies are `evaluation_invalid` and stop
formal Reward calculation.

```text
q(b,c) = Σ obligation_weight(o) × score(o) / Σ obligation_weight(o)
behavior_score(b) = mean(q(b,c)) over applicable criteria
SC-E2-v2 = Σ behavior_risk_weight(b) × behavior_score(b)
           / Σ behavior_risk_weight(b)
```

Do not generate or reference `SC-E2-v1`.

## 10. J1–J5

At Evaluation Batch creation, convert the Judge model into a frozen panel. Start five independent Agents for every Candidate Member:

```text
J1 Agent
J2 Agent
J3 Agent
J4 Agent
J5 Agent
```

Each Agent scores only one dimension; a single call must not return all five dimensions or read another dimension's result. J1–J4
receive only candidate source, facts for their dimension, the corresponding Rubric, evidence policy, and positive-baseline execution records. J5 receives only
candidate source, J5 facts, the J5 applicability mapping, SC-E1 results, and replay logs for faults detected stably. Do not provide SC-E2
results to any J dimension or SC-E1 results to J1–J4. Preserve every input snapshot in the result directory. After all five results, identities, and child Traces pass validation, the judge barrier may generate formal J1–J5 components.

## 11. Reward

Compute the single formal fault-joint reward from the verified component snapshot:

```text
λ = 0.25
R_fault_B = SC-E1 × ((1 - λ) + λ × SC-J5)

R_B
= 0.40 × R_fault_B
+ 0.10 × SC-E2-v2
+ 0.10 × SC-J1
+ 0.25 × SC-J2
+ 0.05 × SC-J3
+ 0.10 × SC-J4
```

`R_total` is always equal to `R_B`. `R_A` is retired and must not be computed,
reported, selected, or included in statistics. An independent Reward Validator
must recompute `R_fault_B`, `R_B`, and `R_total`.

## 12. Historical migration

0.9.1 opens only Prepared Package acceptance, not automatic whole-task migration. The acceptance report must record source and target paths,
engine/contract, Tree SHA-256, business-payload hash, and preparation-package fingerprint; do not migrate Candidate,
Candidate Set, Evaluation, or historical Reward. The accepted new preparation package may generate entirely new 0.10.2 candidates.

Versions 0.8.0–0.8.3 allow only `copy` migration, and the source task must not be modified:

- Delivery → Prepared Package;
- Candidate → Candidate named with its model name and sequence number;
- historical Candidate → singleton Candidate Set;
- Evaluation → Evaluation Batch member snapshot.

0.8.0 defaults to `rescore_required`; 0.8.1 defaults to
`compatibility_review_required`; when 0.8.2–0.8.3 artifacts are complete, use
`fully_migrated`. Migration does not automatically translate, recompute, fabricate, or overwrite historical content.

## 13. Retired artifacts

Do not delete a Prepared package, Candidate, Candidate Set, Evaluation, Session, or Judge run confirmed as retired. Atomically rename its directory to:

```text
<stable-id> -- retired (reason)
```

Also write the structured reason, original status, and relocation. Stable IDs and sequence numbers remain permanently reserved.

## 14. Compatibility commands

The 0.8.3 Delivery → Candidate → Evaluation commands remain only for reading and migrating historical structures. New
0.10.2 tasks must use `prepare`, `generate`, `create-candidate-set`, and `evaluate`.

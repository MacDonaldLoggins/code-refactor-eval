# Reverse-engineering the judge prompts behind your evaluation data

Your `all_categories.xlsx` is the output of an **LLM-as-judge** system that grades AI coding-agent
trajectories on **7 quality dimensions**. This package reconstructs the judge prompts behind it —
both to **re-use them for new evaluation** and to **document the rubric**.

## The key insight

You worried the trajectories were too large to work with. For *reconstructing the prompts*, you
mostly don't need them: the grades themselves leak the rubric.

- Each `category` is a **separate judge** with its own **fixed violation vocabulary** — those
  violation names are the failure criteria the judge was told to check.
- The `reason` texts reveal each judge's standards, tone, and boundaries.

So the heavy lifting is done from the cheap, structured grade data. The large trajectories are
only needed for the final **validation** step, and only for a small sample.

## What's in here

```
taxonomy.json              All 7 categories, every violation, exact counts & pass rates (from your data)
evidence.json              Mined signal: distinctive terms + example judgements per violation
RUBRIC_DOCUMENTATION.md    Human-readable spec of the whole evaluation system  ← the "understand/document" deliverable
FITTING_METHODOLOGY.md     How the prompts were fitted from all 193k reasons (transparency)
judge_prompts/             7 evidence-grounded judge prompts (one per category) ← the "reuse for evaluation" deliverable
  _shared_system.md        Common preamble shared by all judges
  meaningful_trajectory.md  task_completion.md  behavioral_efficiency.md  tool_content_quality.md
  hacky_behavior_free.md    hallucination_free.md  logical_and_coherent_actions.md
mine_corpus.py             Mine the xlsx -> evidence.json (log-odds distinctive terms, examples, co-occurrence)
build_prompts_v2.py        Regenerate judge_prompts/ from taxonomy.json + evidence.json
induce_and_validate.py     LLM-based (re)induction + validation against ground truth
```

Each judge prompt was **fitted from the full corpus**, not hand-guessed: it contains the dimension
definition, PASS criteria (from distinctive PASS vocabulary), every violation with a fitted
definition + the transcript *signals* that flag it + a real example judgement, decision boundaries
between the most-confused violations, and a strict JSON output schema. A `{{TRAJECTORY}}`
placeholder marks where you inject the trajectory text. See `FITTING_METHODOLOGY.md` for how this
was derived and what the corpus revealed.

## How the dataset is structured

193,452 grades · 3,912 tasks · 26,844 sessions. Every session is graded on all 7 categories.

| category | pass rate | violation types |
|---|:--:|:--:|
| meaningful_trajectory | 99.8% | 3 |
| task_completion | 59.7% | 14 |
| behavioral_efficiency | 55.9% | 9 |
| tool_content_quality | 84.4% | 3 |
| hacky_behavior_free | 88.2% | 5 |
| hallucination_free | 43.1% | 11 |
| logical_and_coherent_actions | 71.3% | 21 |

## Use case A — re-run the evaluation on new trajectories

Run the 7 judges **independently** (one call per category) so each lens is applied cleanly,
exactly as the original data was produced.

```python
import json, re
from openai import OpenAI            # or: import anthropic
client = OpenAI()

CATS = ["meaningful_trajectory","task_completion","behavioral_efficiency",
        "tool_content_quality","hacky_behavior_free","hallucination_free",
        "logical_and_coherent_actions"]

def grade(trajectory_text):
    out = {}
    for c in CATS:
        prompt = open(f"judge_prompts/{c}.md").read().replace("{{TRAJECTORY}}", trajectory_text)
        r = client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role":"system","content":"You are a strict evaluator. Output only JSON."},
                      {"role":"user","content":prompt}])
        out[c] = json.loads(re.search(r"\{.*\}", r.choices[0].message.content, re.S).group(0))
    return out   # {category: {pass, violations[], reason}}
```

Feed each trajectory with the **same truncation the agent actually experienced** — many
`hallucination_free` judgements depend on whether a file's content was visible or truncated.

## Use case B — understand / document the rubric

Read `RUBRIC_DOCUMENTATION.md`. It defines all 7 dimensions, every violation, the pass rates,
and explains why some violations intentionally recur across dimensions (the same behavior judged
under different lenses).

## Refining and trusting the prompts: `induce_and_validate.py`

The prompts here were induced by hand from a stratified sample of `reason` texts. To tighten them
or prove fidelity against ground truth:

```bash
pip install openpyxl openai          # or: anthropic
export OPENAI_API_KEY=...            # or ANTHROPIC_API_KEY=...

# 1) (re)sample reasons per category/violation
python induce_and_validate.py sample --xlsx all_categories.xlsx --out samples.json

# 2) let an LLM re-synthesize a category's prompt from those samples (optional refinement)
python induce_and_validate.py induce --category hallucination_free \
       --samples samples.json --out judge_prompts/hallucination_free.md

# 3) validate: does the prompt reproduce the real grades?
python induce_and_validate.py validate --category task_completion \
       --prompt judge_prompts/task_completion.md \
       --xlsx all_categories.xlsx --trajectories trajectories.jsonl --limit 100
```

`validate` reports **pass/fail accuracy** and **violation precision/recall/F1** against your
ground-truth grades. Iterate on a prompt until agreement is high enough for your purposes, then
it's production-ready.

`trajectories.jsonl` is the only place you need the raw trajectories — one JSON object per line:

```json
{"session_id": "1-d3336332-...", "trajectory": "<full transcript text>"}
```

Only sessions present in both the xlsx and this file are used, so a few hundred lines is enough to
validate.

## If you want to go further (optional)

For the strictest reproduction of the original judge, wrap each prompt in a **DSPy** signature
(`trajectory -> {pass, violations, reason}`) and let `MIPROv2` optimize it, using the validation
agreement above as the metric. The prompts here are already a strong starting point; DSPy is only
worth it when you need to squeeze out the last few points of agreement.

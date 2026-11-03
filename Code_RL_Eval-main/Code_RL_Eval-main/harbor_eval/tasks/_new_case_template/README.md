# Harbor Refactor Task Starter v1

这是一个可直接复制的新 case 骨架。通常只改 4 个 hook：
`tests/hard_gates/hard_gates.py`、`tests/objective/objective_metrics.py`、
`tests/rubric/judge.toml`、`tests/rubric/prompt.md`。

固定框架文件 `tests/test.sh`、`tests/verifier.py`、`tests/patch_export.py`、
`tests/rubric/rubric.py` 和 `tests/evaluation.toml` 不应在单个 case 中分叉。

## 快速开始

```bash
cp -R harbor_eval/templates/refactor_task_v1 \
  harbor_eval/tasks/Org__repo__<base_commit>__<case_id>

# case description:
# - task.toml
# - instruction.md
# - behavior_contract.md
# - environment/Dockerfile
# - environment/repo

# evaluation hooks:
# - tests/hard_gates/hard_gates.py
# - tests/objective/objective_metrics.py
# - tests/rubric/judge.toml
# - tests/rubric/prompt.md
# - tests/oracles/ and tests/fixtures/ when needed

python harbor_eval/templates/validate_task.py \
  harbor_eval/tasks/Org__repo__<base_commit>__<case_id>
```

## 修改边界

评测逻辑只改这些 hook：

- `tests/hard_gates/hard_gates.py`
- `tests/objective/objective_metrics.py`
- `tests/rubric/judge.toml`
- `tests/rubric/prompt.md`
- `tests/oracles/`
- `tests/fixtures/`

任务描述和运行环境仍然是 case material，不属于 verifier 框架：

- `task.toml`
- `instruction.md`
- `behavior_contract.md`
- `environment/Dockerfile` 和依赖配置

固定框架文件：

- `tests/evaluation.toml`
- `tests/test.sh`
- `tests/verifier.py`
- `tests/patch_export.py`
- `tests/rubric/rubric.py`

如果确实需要改变固定框架，必须先升级模板版本，而不是在单个 case 中私有分叉。

## 最小可运行契约

```text
hard_gates.run_all(repo) -> gate result
objective_metrics.collect_metrics(repo) -> objective evidence
rubric.atoms() -> judge atoms
RewardKit(tests/rubric) -> rubric reward
verifier.py -> refactor.patch + repo/ + repo.json + reward.json + reward-details.json + reward.txt
```

模板的详细规范见 [EVALUATION_SPEC.md](../../templates/EVALUATION_SPEC.md)。

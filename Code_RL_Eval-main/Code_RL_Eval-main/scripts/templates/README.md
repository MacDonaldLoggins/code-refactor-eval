# Verifier 模板化使用指南

## 概述

通过模板化，verifier.py 变成了一个**完全通用**的脚本，只需修改一个参数（TASK_ID），评分逻辑完全由 rubric.py 实现。

> **注意（rubric.py 来源已变更）**：`rubric.py` 不再手工从模板复制编写。它由生成器
> `scripts/data_synthesis/02_generate_eval_system.py` 基于固定的 **C1–C12** 维度库
> （`scripts/resources/rubrics/c*.json`）自动生成，评分逻辑走 Atom 层聚合。旧的
> `rubric_template.py` 通用占位模板及 D1–D4 / 自定义命名的手写 rubric 均已废弃，不要再使用。
> 本文档中关于"复制 rubric 模板并手写 calculate_rubric"的说明仅作历史接口参考保留。

## 架构设计

```
verifier.py (通用，几乎不需要修改)
    |
    |-- 调用 --> hard_gates.py (任务特定)
    |-- 调用 --> objective_metrics.py (任务特定)
    |-- 调用 --> rubric.calculate_rubric() (任务特定)
                    |
                    在 rubric.py 中实现
```

## 三步流程

### 1. 硬门控检查（Hard Gates）
- 在 `hard_gates.py` 中实现
- 一票否决项
- 如果失败，返回 reward=0，不继续后续步骤

### 2. 客观指标采集（Objective Metrics）
- 在 `objective_metrics.py` 中实现
- 采集可量化的质量指标（复杂度、重复度、lint等）
- 计算 baseline 和 candidate 的差值

### 3. Rubric 评分（Rubric）
- 在 `rubric.py` 中实现 `calculate_rubric()` 函数
- 基于客观指标和硬门控结果进行主观质量判断
- 返回 0.0 到 1.0 的评分

## 使用模板

### 步骤 1：生成评测体系（推荐）

`rubric.py`、`hard_gates.py`、`objective_metrics.py`、`verifier.py` 统一由生成器产出，无需手工复制模板：

```bash
python scripts/data_synthesis/02_generate_eval_system.py --case <task_dir>
# A 线：基于 C1-C12 维度库生成 rubric 骨架
# B 线（默认启用）：LLM 基于骨架 + 任务上下文补全完整 rubric.py
# 关闭 B 线仅走 A 线：追加 --no-enable-llm-enrichment
```

verifier.py 仍复用通用模板（生成器会自动写入并填好 TASK_ID）：

```bash
cp scripts/templates/verifier_template.py <task_dir>/verifier.py  # 如需单独重置
```

### 步骤 2：verifier.py 的 TASK_ID

verifier.py 是通用脚本，唯一任务相关的是：

```python
TASK_ID = "your-task-id-here"  # 生成器会自动填写
```

其他代码**完全不需要修改**！

### 步骤 3：rubric.py 接口（由生成器产出，以下为接口参考）

生成的 `rubric.py` 实现 `calculate_rubric()`，按 C1–C12 维度的 Atom 层聚合打分。以下手写示例仅用于理解接口契约，实际不需要手写：

```python
def calculate_rubric(metrics: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    """
    根据客观指标和硬门控结果计算 rubric 评分
    """
    metric_map = metrics.get("metrics", {})
    score = 0.0
    items = {}

    # 评分项 1：复杂度改进
    complexity = metric_map.get("cyclomatic_complexity", {})
    if complexity.get("candidate", 999) < complexity.get("baseline", 0):
        items["complexity_reduced"] = {"points": 0.4, "earned": 0.4}
        score += 0.4
    else:
        items["complexity_reduced"] = {"points": 0.4, "earned": 0.0}

    # 评分项 2：代码重复减少
    duplication = metric_map.get("code_duplication", {})
    if duplication.get("candidate", 999) < duplication.get("baseline", 0):
        items["duplication_reduced"] = {"points": 0.3, "earned": 0.3}
        score += 0.3
    else:
        items["duplication_reduced"] = {"points": 0.3, "earned": 0.0}

    # 评分项 3：提取方法
    extracted = metric_map.get("extracted_methods", {})
    if extracted.get("candidate", 0) > extracted.get("baseline", 0):
        items["method_extraction"] = {"points": 0.3, "earned": 0.3}
        score += 0.3
    else:
        items["method_extraction"] = {"points": 0.3, "earned": 0.0}

    return {
        "score": round(score, 4),
        "max_score": 1.0,
        "items": items
    }
```

### 步骤 4：测试

```bash
# 直接运行 verifier
python verifier.py ./repo

# 或通过第三步脚本进行校准
python scripts/03_calibrate_with_pr.py --case <task_dir>
```

## 接口规范

### rubric.py 接口

```python
def calculate_rubric(
    metrics: dict[str, Any],
    gates: dict[str, Any]
) -> dict[str, Any]:
    """
    Args:
        metrics: {
            "metrics": {
                "metric_name": {
                    "baseline": <value>,
                    "candidate": <value>,
                    "delta": <value>,
                    "applicable": bool
                },
                ...
            }
        }

        gates: {
            "gate_name": {
                "status": "pass" | "fail" | "na",
                "passed": bool,
                "applicable": bool,
                "reason": str,
                "details": {...}
            },
            ...
        }

    Returns:
        {
            "score": float,        # 0.0 到 1.0
            "max_score": 1.0,
            "items": {
                "item_name": {
                    "points": float,
                    "earned": float,
                    "reason": str  # 可选
                },
                ...
            }
        }
    """
```

### verifier.py 输出格式

```json
{
  "task_id": "task-name",
  "reward": 0.85,
  "invalid": false,
  "status": "pass",
  "hard_gates": {
    "failed_gates": [],
    "gates": { ... },
    "passed": true,
    "invalid": false
  },
  "objective_metrics": {
    "metrics": { ... }
  },
  "rubric": {
    "score": 0.85,
    "max_score": 1.0,
    "items": { ... }
  }
}
```

### Exit Codes

- **0**: 通过所有硬门控且 reward > 0
- **1**: 通过硬门控但 reward = 0，或未通过硬门控
- **2**: invalid（L2 作弊）

## 评分原则

### Rubric 应该评什么

✅ **应该评**：
- 代码清晰度和可读性
- 设计合理性
- 模块化程度
- 重构模式的应用（提取方法、消除重复等）
- 命名质量
- 注释和文档的改进

❌ **不应该评**（已由其他部分覆盖）：
- 测试通过（hard_gates 已检查）
- 行为保持（hard_gates 已检查）
- 复杂度数值（objective_metrics 已采集）
- Lint 问题数量（objective_metrics 已采集）
- 类型检查（hard_gates 已检查）

### 评分项设计建议

1. **具体到任务**
   ```python
   # 好：具体到当前重构目标
   "extracted_long_methods": {...}

   # 不好：过于通用
   "code_quality": {...}
   ```

2. **可区分质量**
   ```python
   # 能区分真重构、表面改动和伪重构
   if complexity_reduced > 20%:
       earned = full_points
   elif complexity_reduced > 10%:
       earned = half_points
   else:
       earned = 0
   ```

3. **组合多个信号**
   ```python
   # 同时检查多个维度
   if complexity_improved and duplication_reduced and methods_extracted:
       earned = full_points
   ```

## 示例：现有 FastAPI Case

查看实际的 rubric.py 实现：

```bash
cat manual_tasks/fastapi__fastapi__c48e67b__16013/rubric.py
```

这个实现检查了：
- `no_source_route_mutation` - 没有直接修改源路由对象
- `thread_safety_refactor` - 引入了线程安全机制
- `preserved_route_order_and_behavior` - 保持路由顺序和行为

## 优势

### 对比原来的内联方式

**原来（verifier.py 包含 _rubric 函数）**：
- ❌ 每个任务的 verifier.py 都不一样
- ❌ 修改评分逻辑需要编辑 verifier.py
- ❌ 难以复用和测试

**现在（模板化）**：
- ✅ verifier.py 完全通用，所有任务共享
- ✅ 评分逻辑集中在 rubric.py，清晰分离
- ✅ rubric.py 可以独立测试
- ✅ 更容易维护和迭代

### 对 LLM 生成的影响

**更容易指导 LLM**：
- verifier.py 使用固定模板，不需要 LLM 生成
- 只需要 LLM 生成 rubric.py 的 `calculate_rubric()` 函数
- 减少了 LLM 出错的可能性

**生成流程简化**：
```
原来：生成 hard_gates.py + objective_metrics.py + verifier.py（包含评分逻辑）
现在：02_generate_eval_system.py 一次性生成
      hard_gates.py + objective_metrics.py + rubric.py（C1-C12），并写入通用 verifier.py
```

## 迁移现有任务

如果已经有旧格式的 verifier.py（包含 _rubric 函数）：

1. 提取 `_rubric()` 函数到新的 rubric.py：
   ```python
   # rubric.py
   def calculate_rubric(metrics, gates):
       # 复制原来的 _rubric() 函数体
       ...
   ```

2. 替换 verifier.py：
   ```bash
   cp scripts/templates/verifier_template.py verifier.py
   # 修改 TASK_ID
   ```

3. 测试：
   ```bash
   python verifier.py ./repo
   ```

## 下一步优化

考虑进一步模板化其他文件：

- **hard_gates.py** - 定义标准的硬门控接口
- **objective_metrics.py** - 定义标准的指标接口

这样整个评测体系都可以模板化，LLM 只需要填充具体的规则和指标。

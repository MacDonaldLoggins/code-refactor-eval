"""
Verifier 通用模板

这是一个完全通用的 verifier.py，执行固定的三步流程：
1. 硬门控检查（一票否决）
2. 客观指标采集
3. Rubric 评分

只需要填写：
- TASK_ID: 任务标识符
- 确保 rubric.py 实现了 calculate_rubric() 函数
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import hard_gates
import objective_metrics
import rubric


# ============================================================
# 任务特定配置
# ============================================================

TASK_ID = "{task_id}"  # TODO: 填写任务 ID


# ============================================================
# 固定的三步流程（不需要修改）
# ============================================================

def _repo_path(repo_path: str | os.PathLike[str] | None = None) -> Path:
    """将 repo 路径转换为绝对路径"""
    return Path(repo_path or os.getcwd()).resolve()


def verify(repo_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """
    执行完整的评测流程

    Args:
        repo_path: 候选代码的仓库路径（已应用 patch）

    Returns:
        {
            "task_id": str,
            "reward": float,              # 0.0 到 1.0
            "invalid": bool,              # 是否作弊
            "status": str,                # "pass" | "failed_hard_gate" | "invalid" | "no_credit"
            "hard_gates": {...},          # 硬门控结果
            "objective_metrics": {...},   # 客观指标（如果硬门控通过）
            "rubric": {...},              # 评分（如果硬门控通过）
            "reason": str                 # 状态原因
        }
    """
    repo = _repo_path(repo_path)

    # 步骤 1：硬门控检查
    gate_result = hard_gates.run_all(repo)

    # 检查 L2 作弊（invalid）
    if gate_result.get("invalid"):
        return {
            "task_id": TASK_ID,
            "reward": 0.0,
            "invalid": True,
            "status": "invalid",
            "hard_gates": gate_result,
            "objective_metrics": None,
            "rubric": None,
            "reason": "anti-cheating hard gate failed",
        }

    # 检查硬门控失败
    failed_gates = gate_result.get("failed_gates", [])
    if failed_gates:
        return {
            "task_id": TASK_ID,
            "reward": 0.0,
            "invalid": False,
            "status": "failed_hard_gate",
            "hard_gates": gate_result,
            "objective_metrics": None,
            "rubric": None,
            "reason": "one or more applicable hard gates failed",
        }

    # 步骤 2：客观指标采集
    metrics = objective_metrics.collect_metrics(repo)

    # 步骤 3：Rubric 评分（调用 rubric.py 中的函数）
    rubric_result = rubric.calculate_rubric(metrics, gate_result.get("gates", {}), repo_path=str(repo))
    reward = rubric_result["score"]

    return {
        "task_id": TASK_ID,
        "reward": reward,
        "invalid": False,
        "status": "pass" if reward > 0 else "no_credit",
        "hard_gates": gate_result,
        "objective_metrics": metrics,
        "rubric": rubric_result,
    }


def main() -> int:
    """
    命令行入口

    Usage:
        python verifier.py [repo_path]

    Exit codes:
        0: 通过所有硬门控且 reward > 0
        1: 通过硬门控但 reward = 0，或未通过硬门控
        2: invalid（作弊）
    """
    result = verify(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(result, indent=2, sort_keys=True))

    if result.get("invalid"):
        return 2
    return 0 if result.get("reward", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

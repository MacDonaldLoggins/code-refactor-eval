"""通用 Rubric 评分器（固定脚本，逐字节拷贝到每个任务目录）。

职责固定：解析同目录的 rubric.md（评分标准，唯一 source of truth）→ 按维度调用
Codex agent 进入仓库自行读取 git diff / 源码 / 测试证据打分 → 聚合成任务级分数。

不再为每个任务生成一份 rubric.py。任务差异全部体现在 rubric.md 里。

路径契约（重要）：
- `calculate_rubric(metrics, gates, repo_path)` 的 `repo_path` 传入的是【repo 目录】
  （例如评估时的 `{workspace}/repo`）。函数内部用 `Path(repo_path).parent` 得到工作空间
  根目录（含 agent_session/、patches/、refactor_result.json），codex 在该根目录执行，
  这样它能看到完整上下文。**不要把工作空间根直接当 repo_path 传进来。**
- rubric.md 通过 `Path(__file__).parent / "rubric.md"` 定位——本脚本与 rubric.md 始终被
  一起放到同一目录（评估时的 /workspace/、harbor 的 tests/），因此可靠命中；而 repo 不在
  本脚本旁边，故 repo 位置只能靠传入的 repo_path，绝不用 __file__ 推断。
"""

import json
import os
import re
import sys
from pathlib import Path

TASK_KIND = "refactor"


# ============================================================
# rubric.md 解析
# ============================================================

# 维度标题：### <维度id>: <名称>（维度 id 形如「字母+数字」，如 C1 / E5，不绑定具体维度）
_DIM_RE = re.compile(r"^###\s+(?P<id>[A-Za-z]+\d+)\s*[:：]\s*(?P<name>.+?)\s*$")
# atom 行：- **<atom_id>** (hard|soft, <weight>): 文本
#   atom id 形如「字母+数字」再跟一个或多个 .数字（兼容 C1.1.1 三段与 C3.1 两段）。
#   真正的区分特征是 (type, weight) 结构，故不依赖任何具体维度前缀。
_ATOM_RE = re.compile(
    r"^\s*-\s+\*\*(?P<id>[A-Za-z]+\d+(?:\.\d+)+)\*\*\s*\(\s*"
    r"(?P<type>hard|soft)\s*,\s*(?P<weight>\d+(?:\.\d+)?)\s*\)\s*[:：]\s*(?P<text>.+?)\s*$"
)

_RUBRIC_CACHE = None


def _find_rubric_md(repo_path=None):
    """定位 rubric.md。只用 __file__ 找 md（与本脚本同目录），绝不用它推 repo。"""
    candidates = [Path(__file__).resolve().parent / "rubric.md"]
    if repo_path:
        rp = Path(repo_path)
        candidates.append(rp.parent / "rubric.md")  # 工作空间根兜底
        candidates.append(rp / "rubric.md")
    for path in candidates:
        if path.exists():
            return path
    return None


def parse_rubric_md(md_path: Path) -> dict:
    """把 rubric.md 解析成与旧 RUBRIC 同构的 dict。

    Returns:
        {"dimensions": {dim_id: {"id", "name", "weight": 1.0,
                                 "atoms": [{"id","text","type","weight"}]}}}
        维度与 atom 均保持在文档中出现的顺序（依赖 dict 有序性）。

    只认维度标题行与 atom 行；`#### 分组标题`、`**维度描述**`、空行等一律忽略。
    遇到 level-2 段落头（`## `，例如末尾的 `## 评分说明`）即停止收集，
    避免把「问题类型 / 评分范围」等说明段误当作维度。
    """
    dimensions: dict = {}
    current_id = None

    for raw_line in md_path.read_text(encoding="utf-8").splitlines():
        # level-2 段落头（`## ` 且非 `### `/`#### `）：维度区结束，停止。
        if raw_line.startswith("## ") and not raw_line.startswith("### "):
            if dimensions:
                break
            # 尚未进入任何维度（如开头的 `## 任务背景`/`## 评分维度`），跳过即可。
            continue

        dim_match = _DIM_RE.match(raw_line)
        if dim_match:
            current_id = dim_match.group("id")
            dimensions[current_id] = {
                "id": current_id,
                "name": dim_match.group("name").strip(),
                "weight": 1.0,
                "atoms": [],
            }
            continue

        atom_match = _ATOM_RE.match(raw_line)
        if atom_match and current_id is not None:
            dimensions[current_id]["atoms"].append(
                {
                    "id": atom_match.group("id"),
                    "text": atom_match.group("text").strip(),
                    "type": atom_match.group("type"),
                    "weight": float(atom_match.group("weight")),
                }
            )

    return {"dimensions": dimensions}


def _get_rubric(repo_path=None) -> dict:
    """定位并解析 rubric.md，缓存结果。找不到或解析为空时返回空维度（由上层判空处理）。"""
    global _RUBRIC_CACHE
    if _RUBRIC_CACHE is not None:
        return _RUBRIC_CACHE
    md_path = _find_rubric_md(repo_path)
    if md_path is None:
        _RUBRIC_CACHE = {"dimensions": {}}
        return _RUBRIC_CACHE
    _RUBRIC_CACHE = parse_rubric_md(md_path)
    return _RUBRIC_CACHE


# ============================================================
# Codex Agent 评估引擎
# 由 codex agent 进入仓库自行读取完整 git diff / 源码 / 测试输出评分，
# 不再由脚本预截断 diff 塞进无状态 LLM prompt。
# ============================================================


def _agent_evaluate_dimension(
    dimension_id: str,
    dimension_data: dict,
    workspace_path: Path,
    metrics: dict,
    gates: dict,
    rubric_file: Path,
) -> dict:
    """用 Codex agent 评估单个维度：agent 自行进入仓库读 diff / 源码 / 测试证据。"""
    from subprocess import run, TimeoutExpired

    dimension_name = dimension_data["name"]
    atoms = dimension_data["atoms"]

    atoms_list = [
        {
            "id": atom["id"],
            "question": atom["text"],
            "type": atom["type"],
            "weight": atom["weight"],
        }
        for atom in atoms
    ]
    example_id = atoms_list[0]["id"] if atoms_list else f"{dimension_id}.1"

    workspace_root = workspace_path.parent
    metrics_summary = metrics.get("candidate", {}) if isinstance(metrics, dict) else {}
    delta_summary = metrics.get("delta", {}) if isinstance(metrics, dict) else {}

    prompt = f"""你是代码重构评审专家。请评估工作空间中的重构在「{dimension_name}」({dimension_id}) 维度的表现。

# 当前工作目录
你当前位于工作空间根目录：{workspace_root}

# 评分标准来源
完整的维度定义与问题清单在评分标准文档：{rubric_file}
可用 `cat {rubric_file}` 查看本维度的全部问题与上下文。

# 工作空间结构（以下资源可能存在，也可能缺失；缺失时按下方降级方式取证，不要中断评估）
- **repo/**：重构后的代码。
  - **优先**：若 repo 是 git 仓库，用 `cd repo && git diff HEAD`（或 `git -C repo/ diff HEAD`）查看完整改动，`git -C repo/ show HEAD` 看最近提交。
  - **若 repo 无 .git**（例如 harbor 评测环境）：改用工作空间根的 `patches/refactor.patch`（若存在）了解改动；否则直接阅读 repo/ 下的当前源码。
- **patches/refactor.patch**：重构 diff 补丁（可能不存在）。
- **agent_session/trajectory.jsonl**：Claude Code 执行重构的会话轨迹（可选证据，存在时 `cat` 可参考决策过程，不存在则跳过）。
- **refactor_result.json**：重构执行的元数据（可选）。

以上所有辅助文件都是可选证据；任一缺失都不影响你给分，但你**不得凭空猜测**，只能基于实际读到的代码与下方指标判断。

# 客观指标摘要（仅供参考，完整证据请自行用工具核对）
## candidate
{json.dumps(metrics_summary, indent=2, ensure_ascii=False)[:2000]}
## delta
{json.dumps(delta_summary, indent=2, ensure_ascii=False)[:2000]}

# 硬门控结果摘要
{json.dumps({k: (v.get('status') if isinstance(v, dict) else v) for k, v in list(gates.items())[:15]}, indent=2, ensure_ascii=False)}

# 评估问题列表（共 {len(atoms_list)} 个）
{json.dumps(atoms_list, indent=2, ensure_ascii=False)}

# 你的任务
1. 用 Bash 工具读取改动前后代码，收集证据（按上面的优先/降级方式取证）。
2. 对每个问题给出评分并写明依据（基于实际读到的代码，不要猜测）：
   - 1.0: 完全满足，有明确证据
   - 0.5: 部分满足，或有可接受的小问题
   - 0.0: 不满足，或有明显问题
   - na: 不适用于本任务（仅 soft 类型可用）
3. hard 类型问题：证据不足默认 0.0；soft 类型问题：证据不足可标记 na（不计入分母）。

# 输出格式
在最后严格输出一个 JSON 数组（可用 ```json 包裹），每个问题一个对象：
[
  {{"id": "{example_id}", "score": 1.0, "status": "scored", "reasoning": "读了 git diff，具体依据……"}},
  {{"id": "...", "score": null, "status": "na", "reasoning": "本任务不涉及……"}}
]

开始评估。
"""

    output = ""
    try:
        print(f"  🤖 Codex Agent 评估维度 {dimension_id}({dimension_name})...", flush=True)
        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt,
        ]
        result = run(
            cmd,
            cwd=str(workspace_root),  # 在工作空间根目录执行，让 codex 能访问 agent_session/、patches/ 等
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            print(f"  ⚠️  Codex Agent 调用失败({dimension_id}): {result.stderr[:300]}", file=sys.stderr)
            return _fallback_scoring(atoms)

        output = result.stdout.strip()

        # 从 agent 输出中提取最后一个 JSON 数组（agent 输出常夹杂思考过程）
        atom_results = _extract_json_array(output)
        if atom_results is None:
            print(f"  ⚠️  Codex Agent 未返回可解析的 JSON 数组({dimension_id})", file=sys.stderr)
            print(f"  原始输出尾部: {output[-500:]}", file=sys.stderr)
            return _fallback_scoring(atoms)

        scored_weight = 0.0
        scored_denominator = 0.0
        for atom_result in atom_results:
            if atom_result.get("status") == "scored" and atom_result.get("score") is not None:
                atom_id = atom_result.get("id")
                atom_weight = next((a["weight"] for a in atoms if a["id"] == atom_id), 1.0)
                scored_weight += float(atom_result["score"]) * atom_weight
                scored_denominator += atom_weight
        dimension_score = scored_weight / scored_denominator if scored_denominator > 0 else 0.0

        return {"atoms": atom_results, "dimension_score": dimension_score}

    except TimeoutExpired:
        print(f"  ⚠️  Codex Agent 评估超时({dimension_id})", file=sys.stderr)
        return _fallback_scoring(atoms)
    except Exception as e:
        print(f"  ⚠️  Codex Agent 评估失败({dimension_id}): {e}", file=sys.stderr)
        return _fallback_scoring(atoms)


def _extract_json_array(text: str):
    """从可能夹杂思考过程/markdown 的 agent 输出中提取最后一个 JSON 数组。"""
    # 优先取 ```json ... ``` 代码块中的内容
    blocks = re.findall(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    candidates = list(blocks)

    # 兜底：扫描裸的 [ ... ] 片段（取最长的，通常是完整结果）
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    candidates.append(text[start : i + 1])
                    start = -1

    for snippet in sorted(candidates, key=len, reverse=True):
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, list) and parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _fallback_scoring(atoms: list) -> dict:
    """codex agent 失败时的回退：hard 默认 0.0，soft 标记 na。"""
    atom_results = []
    for atom in atoms:
        is_hard = atom["type"] == "hard"
        atom_results.append(
            {
                "id": atom["id"],
                "score": 0.0 if is_hard else None,
                "status": "scored" if is_hard else "na",
                "reasoning": "Codex Agent 评估失败：hard 约束默认 0.0，soft 约束标记 na。",
            }
        )
    return {"atoms": atom_results, "dimension_score": 0.0}


def calculate_rubric(metrics: dict, gates: dict, repo_path: str = None) -> dict:
    """任务级入口：解析同目录 rubric.md，逐维度用 Codex agent 评估并聚合。

    Args:
        metrics: 客观指标（objective_metrics.collect_metrics 的返回）
        gates: 硬门控结果
        repo_path: 【repo 目录】路径；函数内部取 .parent 作为工作空间根供 codex 执行
    """
    metrics = metrics or {}
    gates = gates or {}

    if not repo_path:
        return {
            "score": 0.0,
            "dimensions": [],
            "notes": ["缺少 repo_path，无法进行 codex agent 评估"],
            "evidence": {"error": "no repo_path"},
        }

    workspace_path = Path(repo_path)
    if not workspace_path.exists():
        return {
            "score": 0.0,
            "dimensions": [],
            "notes": [f"仓库路径不存在: {workspace_path}"],
            "evidence": {"error": "workspace not found"},
        }

    rubric_file = _find_rubric_md(repo_path)
    if rubric_file is None:
        return {
            "score": 0.0,
            "dimensions": [],
            "notes": ["找不到 rubric.md，无法确定评分标准"],
            "evidence": {"error": "rubric.md not found"},
        }

    rubric = _get_rubric(repo_path)
    if not rubric["dimensions"]:
        return {
            "score": 0.0,
            "dimensions": [],
            "notes": [f"rubric.md 未解析出任何维度: {rubric_file}"],
            "evidence": {"error": "empty rubric", "rubric_file": str(rubric_file)},
        }

    notes = []
    dimension_results = []
    total_weighted = 0.0
    total_denominator = 0.0

    gate_failed = any(
        isinstance(v, dict) and (v.get("status") == "fail" or v.get("passed") is False)
        for v in gates.values()
    )
    if gate_failed:
        notes.append("hard gates 中存在 failed 项，总分按契约直接置 0。")

    print(f"  🚀 开始 codex agent rubric 评估（仓库: {workspace_path}，标准: {rubric_file}）", flush=True)

    for dim_id, dim_data in rubric["dimensions"].items():
        result = _agent_evaluate_dimension(
            dim_id, dim_data, workspace_path, metrics, gates, rubric_file
        )
        dim_score = result["dimension_score"]
        atom_results = result["atoms"]

        dimension_results.append(
            {
                "id": dim_id,
                "name": dim_data["name"],
                "weight": dim_data["weight"],
                "score": dim_score,
                "atoms": atom_results,
            }
        )
        total_weighted += dim_score * dim_data["weight"]
        total_denominator += dim_data["weight"]

        for atom_result in atom_results:
            if atom_result.get("status") == "scored" and atom_result.get("score", 1.0) < 1.0:
                notes.append(f"{atom_result.get('id')} 扣分: {atom_result.get('reasoning', 'no reason')}")

    score = total_weighted / total_denominator if total_denominator > 0 else 0.0
    if gate_failed:
        score = 0.0

    return {
        "score": max(0.0, min(1.0, float(score))),
        "dimensions": dimension_results,
        "notes": notes,
        "evidence": {
            "gate_failed": gate_failed,
            "workspace_path": str(workspace_path),
            "rubric_file": str(rubric_file),
            "engine": "codex_agent",
        },
    }


def get_rubric_documentation() -> dict:
    """返回解析出的 rubric 结构（供人工/调试；全仓库无自动 caller）。"""
    return _get_rubric()


def main() -> int:
    """本地调试入口：python rubric.py [repo_path]。"""
    repo_path = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    result = calculate_rubric({}, {}, repo_path=repo_path)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


_MINIMUM_UNIVERSAL = ["C1", "C2", "C3", "C5", "C11", "C12"]

_RUBRICS_DIR = Path(__file__).parent.parent / "resources" / "rubrics"
_SCENARIO_INDEX_PATH = _RUBRICS_DIR / "scenario_index.json"

_DIMENSION_NAMES = {
    "C1": "行为保持与安全性", "C2": "坏味道识别与目标命中", "C3": "可理解性",
    "C4": "职责与内聚性", "C5": "变化隔离与可修改性", "C6": "重复与知识集中",
    "C7": "数据封装与状态安全", "C8": "API 与调用契约", "C9": "条件逻辑与控制流",
    "C10": "抽象、继承与委托", "C11": "测试与验证质量", "C12": "成本、复杂性与演化收益",
}

_DIMENSION_LAYERS = {"C1": "univ", "C2": "univ", "C3": "univ", "C5": "univ", "C11": "univ", "C12": "univ"}

_EQUIVALENCE_NAMES = {
    "E1": "抽取与分解",
    "E2": "内联与合并",
    "E3": "迁移与职责重分配",
    "E4": "符号与契约迁移",
    "E5": "共享抽象与去重",
}

# E1..E5 → scenario_id 白名单（依据 docs/B-P/重构等价类分类Rubrics方案.md）
_EQUIVALENCE_TO_SCENARIOS: dict[str, tuple[str, ...]] = {
    "E1": (
        "extract_function",
        "extract_class",
        "split_phase",
        "split_loop",
        "split_variable",
        "decompose_conditional",
    ),
    "E2": (
        "inline_function_variable_class",
        "remove_middle_man",
        "collapse_hierarchy",
    ),
    "E3": (
        "combine_functions_into_class",
        "combine_functions_into_transform",
        "encapsulate_variable",
        "encapsulate_collection",
        "replace_primitive_with_object",
        "move_function",
        "move_field",
        "hide_delegate",
        "preserve_whole_object",
        "pull_up_push_down_member",
        "replace_hierarchy_with_delegate",
    ),
    "E4": (
        "rename_extract_variable",
        "introduce_parameter_object",
        "change_reference_to_value",
        "separate_query_from_modifier",
        "remove_flag_argument",
        "replace_constructor_with_factory_function",
        "replace_function_with_command",
    ),
    "E5": (
        "replace_loop_with_pipeline",
        "consolidate_conditional",
        "replace_nested_conditional_with_guard_clauses",
        "replace_conditional_with_polymorphism",
        "introduce_special_case",
        "replace_type_code_with_subclasses",
        "remove_dead_code",
        "introduce_assertion",
    ),
}

_SCENARIO_TO_EQUIVALENCE: dict[str, str] = {
    scenario: e_class
    for e_class, scenarios in _EQUIVALENCE_TO_SCENARIOS.items()
    for scenario in scenarios
}


def _load_scenario_index() -> dict[str, dict[str, Any]]:
    if not _SCENARIO_INDEX_PATH.exists():
        return {}
    data = json.loads(_SCENARIO_INDEX_PATH.read_text(encoding="utf-8"))
    return {
        entry["scenario_id"]: {
            "name": entry.get("name", entry["scenario_id"]),
            "aliases": tuple(entry.get("aliases", ())),
            "must_dimensions": tuple(entry.get("must_dimensions", ())),
            "recommended_dimensions": tuple(entry.get("recommended_dimensions", ())),
        }
        for entry in data.get("scenarios", [])
    }


_SCENARIO_INDEX = _load_scenario_index()


def _load_dimension_definitions() -> dict[str, Any]:
    definitions = {}
    for path in sorted(_RUBRICS_DIR.glob("c*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        dim_id = data["dimension_id"]
        definitions[dim_id] = {
            "name": data["name"],
            "layer": data["layer"],
            "description": data["description"],
            "questions": data["questions"],
            "aggregation": data["aggregation"],
        }
    return definitions


_DIMENSION_DEFINITIONS = _load_dimension_definitions()


def _sort_dimensions(values: Iterable[str]) -> list[str]:
    return sorted(set(values), key=lambda value: int(value[1:]))


def _task_text(metadata: dict[str, Any]) -> str:
    task_value = metadata.get("task")
    source_value = metadata.get("source")
    task: dict[str, Any] = task_value if isinstance(task_value, dict) else {}
    source: dict[str, Any] = source_value if isinstance(source_value, dict) else {}
    values = [
        metadata.get("title"), metadata.get("body"), metadata.get("refactor_type"),
        metadata.get("issue_sub_intent"), task.get("title"), task.get("intent"),
        task.get("refactor_type"), source.get("title"), source.get("raw_description"),
    ]
    return "\n".join(str(value) for value in values if value).lower()


def select_dimensions(task_description: str) -> dict[str, Any]:
    """兜底路径：从任务描述里做 alias 关键词匹配拿到 scenario 集合。"""
    text = re.sub(r"[_-]+", " ", (task_description or "").lower())
    must: set[str] = set()
    recommended: set[str] = set()
    scenarios: list[str] = []
    matches: list[str] = []
    for scenario_id, info in _SCENARIO_INDEX.items():
        hit = next((alias for alias in info["aliases"] if alias.lower() in text), None)
        if hit is None:
            continue
        scenarios.append(scenario_id)
        matches.append(hit)
        must.update(info["must_dimensions"])
        recommended.update(info["recommended_dimensions"])
    if not scenarios:
        must.update(_MINIMUM_UNIVERSAL)
    recommended -= must
    selected = _sort_dimensions((*must, *recommended))
    return {
        "scenarios": scenarios,
        "matched_aliases": matches,
        "fallback": not scenarios,
        "must": _sort_dimensions(must),
        "recommended": _sort_dimensions(recommended),
        "selected": selected,
    }


def select_dimensions_by_equivalence(
    equivalence_class: str | None,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    """A 线主路径：按 (E 类, scenario) 决定 must / recommended 维度。

    - 若 scenario_id 命中 scenario_index.json：直接用该 scenario 的 must/recommended。
    - 若 scenario_id 缺失但 E 类合法：取 E 类下所有 scenario 的 must 并集 + recommended 并集。
    - 若都非法：退回 _MINIMUM_UNIVERSAL。
    """
    e_class = equivalence_class if equivalence_class in _EQUIVALENCE_TO_SCENARIOS else None
    scenario_hit = scenario_id if scenario_id in _SCENARIO_INDEX else None

    must: set[str] = set()
    recommended: set[str] = set()
    scenarios: list[str] = []
    fallback = False

    if scenario_hit is not None:
        info = _SCENARIO_INDEX[scenario_hit]
        must.update(info["must_dimensions"])
        recommended.update(info["recommended_dimensions"])
        scenarios.append(scenario_hit)
        # 若 scenario 对应的 E 类与传入的 E 类不一致，只信 scenario
        if e_class is None:
            e_class = _SCENARIO_TO_EQUIVALENCE.get(scenario_hit)
    elif e_class is not None:
        for candidate in _EQUIVALENCE_TO_SCENARIOS[e_class]:
            info = _SCENARIO_INDEX.get(candidate)
            if info is None:
                continue
            must.update(info["must_dimensions"])
            recommended.update(info["recommended_dimensions"])
            scenarios.append(candidate)
    else:
        must.update(_MINIMUM_UNIVERSAL)
        fallback = True

    recommended -= must
    selected = _sort_dimensions((*must, *recommended))
    return {
        "equivalence_class": e_class,
        "scenario_id": scenario_hit,
        "scenarios": scenarios,
        "fallback": fallback,
        "must": _sort_dimensions(must),
        "recommended": _sort_dimensions(recommended),
        "selected": selected,
    }


def _dimension_definition(dimension_id: str) -> dict[str, Any]:
    if dimension_id in _DIMENSION_DEFINITIONS:
        defn = _DIMENSION_DEFINITIONS[dimension_id]
        return {
            "dimension_id": dimension_id,
            "name": defn["name"],
            "layer": defn["layer"],
            "weight": 1.0,
            "description": defn["description"],
            "questions": defn["questions"],
            "aggregation": defn["aggregation"],
        }
    layer = _DIMENSION_LAYERS.get(dimension_id, "eq")
    return {
        "dimension_id": dimension_id,
        "name": _DIMENSION_NAMES.get(dimension_id, f"未知维度 {dimension_id}"),
        "layer": layer,
        "weight": 1.0,
        "questions": [
            {
                "id": f"{dimension_id}.1",
                "text": f"{_DIMENSION_NAMES.get(dimension_id, dimension_id)}是否满足本任务的目标？",
                "type": "soft" if dimension_id != "C1" else "hard",
                "weight": 1.0,
                "anchor": "0=不满足；0.5=部分满足；1=充分满足",
            }
        ],
    }


def build_rubric(metadata: dict[str, Any], task_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """A 线核心：由 (E 类, scenario) 或任务描述兜底选维度，装载 c*.json 完整 Atom。"""
    task_value = metadata.get("task")
    task: dict[str, Any] = task_value if isinstance(task_value, dict) else {}
    task_context = task_context or {}
    description = str(task_context.get("task_description") or _task_text(metadata))
    equivalence_class = task_context.get("equivalence_class")
    scenario_id = task_context.get("scenario_id") or task_context.get("scenario")

    if equivalence_class or scenario_id:
        selection = select_dimensions_by_equivalence(
            str(equivalence_class) if equivalence_class else None,
            str(scenario_id) if scenario_id else None,
        )
    else:
        selection = select_dimensions(description)
        # 补 equivalence_class 字段以便下游知道走的是兜底路径
        selection.setdefault("equivalence_class", None)
        selection.setdefault("scenario_id", selection["scenarios"][0] if selection["scenarios"] else None)

    return {
        "schema_version": "2.0-compatible",
        "task_id": str(metadata.get("task_id") or task.get("task_id") or "unknown"),
        "task_description": description,
        "task_context": task_context,
        "scenario_selection": selection,
        "dimension_selection": {
            "must": selection["must"],
            "recommended": selection["recommended"],
            "selected": selection["selected"],
        },
        "dimensions": {
            dimension_id: _dimension_definition(dimension_id)
            for dimension_id in selection["selected"]
        },
        "scoring_policy": {
            "hard_gate_failures_are_not_scored": True,
            "objective_metrics_are_not_repeated": True,
            "missing_task_score_is_skipped": True,
        },
        "metadata": {
            "task_id": str(metadata.get("task_id") or task.get("task_id") or "unknown"),
            "version": "2.0-compatible",
        },
    }


def build_rubric_markdown(
    metadata: dict[str, Any],
    task_context: dict[str, Any] | None = None,
) -> str:
    """生成 rubric.md 文档（人类可读的评分标准说明）。"""
    rubric = build_rubric(metadata, task_context)

    # 提取任务信息
    repo_name = metadata.get("repo", "unknown")
    task_type = metadata.get("refactor_type", "refactor")
    task_title = metadata.get("title", "")
    task_body = metadata.get("body", "")[:200]

    # 开始构建 markdown
    lines = [
        "# Rubric 评分标准",
        "",
        "本文档定义了代码重构评估的标准和问题列表。",
        "",
        "## 任务背景",
        "",
        f"- **仓库**: {repo_name}",
        f"- **重构类型**: {task_type}",
    ]

    if task_title:
        lines.append(f"- **任务标题**: {task_title}")
    if task_body:
        lines.append(f"- **任务描述**: {task_body}...")

    lines.extend([
        "",
        "## 评分维度",
        "",
        "评估分为 {} 个维度，每个维度包含多个具体问题。每个问题有类型（hard/soft）和权重。".format(
            len(rubric["dimensions"])
        ),
        "",
    ])

    # 遍历每个维度
    for dim_id, dim_data in rubric["dimensions"].items():
        dim_name = dim_data["name"]
        lines.append(f"### {dim_id}: {dim_name}")
        lines.append("")

        # 维度描述（如果有的话）
        if "description" in dim_data and dim_data["description"]:
            lines.append(f"**维度描述**: {dim_data['description']}")
            lines.append("")

        # 按问题分组（提取子分类）
        questions_by_group = {}
        for question in dim_data["questions"]:
            qid = question["id"]
            # 提取分组编号（如 C1.1 从 C1.1.1 提取）
            parts = qid.split(".")
            if len(parts) >= 3:
                group_key = f"{parts[0]}.{parts[1]}"
            else:
                group_key = qid

            if group_key not in questions_by_group:
                questions_by_group[group_key] = []
            questions_by_group[group_key].append(question)

        # 输出问题（按分组）
        for group_key in sorted(questions_by_group.keys()):
            questions = questions_by_group[group_key]

            # 尝试生成分组标题（从第一个问题推断）
            first_q = questions[0]
            first_text = first_q["text"]

            # 如果有多个问题，显示分组标题
            if len(questions) > 1:
                # 尝试从问题文本中提取主题
                if "是否" in first_text:
                    topic = first_text.split("是否")[0].rstrip("，,、")
                    lines.append(f"#### {group_key} - {topic}")
                else:
                    lines.append(f"#### {group_key}")
                lines.append("")

            # 输出该组的所有问题
            for question in questions:
                qid = question["id"]
                text = question["text"]
                qtype = question.get("type", "soft")
                weight = question.get("weight", 1.0)

                lines.append(f"- **{qid}** ({qtype}, {weight}): {text}")

            lines.append("")

        lines.append("")

    # 添加评分说明
    lines.extend([
        "## 评分说明",
        "",
        "### 问题类型",
        "",
        "- **hard**: 硬约束，必须满足，否则该问题得分为 0",
        "- **soft**: 软约束，可以部分满足，或标记为 na（不适用）",
        "",
        "### 评分范围",
        "",
        "- **1.0**: 完全满足，有明确证据",
        "- **0.5**: 部分满足，或有小问题但可接受",
        "- **0.0**: 不满足，或有明显问题",
        "- **na**: 不适用于本任务（仅 soft 类型问题可用）",
        "",
        "### 维度得分计算",
        "",
        "维度得分 = Σ(问题得分 × 问题权重) / Σ(已评分问题权重)",
        "",
        "- na 标记的问题不计入分母",
        "- hard 类型问题如果得分 < 1.0，会产生 critical failure",
        "",
        "### 总分计算",
        "",
        "总分 = Σ(维度得分 × 维度权重) / Σ(维度权重)",
        "",
        "- 如果任何 hard gate 失败，总分直接置为 0",
        "",
    ])

    return "\n".join(lines)


__all__ = [
    "build_rubric",
    "build_rubric_markdown",
    "select_dimensions",
    "select_dimensions_by_equivalence",
    "_EQUIVALENCE_TO_SCENARIOS",
    "_SCENARIO_TO_EQUIVALENCE",
    "_SCENARIO_INDEX",
    "_EQUIVALENCE_NAMES",
]

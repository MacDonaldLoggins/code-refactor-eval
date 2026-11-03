"""A 线第一步：让 LLM 判定重构任务的等价类 E1..E5 与 scenario_id。

LLM 只负责分类，不生成任何 Rubric 内容。分类结果驱动 rubric_builder 从
scenario_index.json + c*.json 装载对应的 must/recommended 维度骨架。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from utils.llm_client import call_llm
from utils.prompt_loader import load_prompt
from utils.rubric_builder import (
    _EQUIVALENCE_NAMES,
    _EQUIVALENCE_TO_SCENARIOS,
    _SCENARIO_INDEX,
    _SCENARIO_TO_EQUIVALENCE,
)


def _extract_json_block(response: str) -> str:
    """从 LLM 响应中提取最外层 JSON 对象文本，兼容 markdown 代码块。"""
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    depth = 0
    start = -1
    best: str | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                block = text[start : i + 1]
                if best is None or len(block) > len(best):
                    best = block
                start = -1
    if best is None:
        raise ValueError("LLM 响应中未找到 JSON 对象")
    return best


def _build_scenario_catalog() -> str:
    """把 E→scenario 白名单渲染成给 LLM 看的表格，避免 LLM 幻觉出无效 id。"""
    lines: list[str] = []
    for e_class, scenarios in _EQUIVALENCE_TO_SCENARIOS.items():
        lines.append(f"- {e_class} {_EQUIVALENCE_NAMES[e_class]}:")
        for sid in scenarios:
            info = _SCENARIO_INDEX.get(sid)
            display = info["name"] if info else sid
            lines.append(f"    - {sid}  # {display}")
    return "\n".join(lines)


def classify_to_equivalence_class(
    metadata: dict[str, Any],
    repo_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """让 LLM 判定 (E 类, scenario_id)。

    返回:
      {
        "equivalence_class": "E1"..."E5" 或 None,
        "scenario_id": scenario_index.json 里的合法 id 或 None,
        "confidence": 0.0..1.0,
        "reasoning": str,
        "raw": LLM 原始 JSON,
      }

    结果由代码强校验：
    - equivalence_class 必须 ∈ {E1..E5}；否则置 None。
    - scenario_id 必须存在于 scenario_index.json；否则置 None。
    - 若两者都合法，还会校验 scenario_id ∈ _EQUIVALENCE_TO_SCENARIOS[e_class]，
      不一致时相信 scenario_id 并修正 e_class。
    """
    repo_context = repo_context or {}
    template = load_prompt("equivalence_classification")
    prompt = template.format(
        metadata_json=json.dumps(metadata, indent=2, ensure_ascii=False),
        directory_tree=repo_context.get("directory_tree", "(无)"),
        refactor_md=repo_context.get("refactor_md", "(无)"),
        scenario_catalog=_build_scenario_catalog(),
    )
    response = call_llm(prompt)
    try:
        raw = json.loads(_extract_json_block(response))
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"等价类分类响应 JSON 解析失败: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("等价类分类响应必须是 JSON 对象")

    e_class = raw.get("equivalence_class")
    scenario_id = raw.get("scenario_id")

    if scenario_id in _SCENARIO_INDEX:
        canonical_e = _SCENARIO_TO_EQUIVALENCE.get(scenario_id)
        if canonical_e and canonical_e != e_class:
            print(
                f"⚠️  LLM 判定 E 类 {e_class} 与 scenario {scenario_id} 归属的 {canonical_e} 不一致，"
                f"以 scenario 为准修正为 {canonical_e}"
            )
            e_class = canonical_e
    else:
        if scenario_id is not None:
            print(f"⚠️  LLM 返回的 scenario_id={scenario_id!r} 不在 scenario_index，置 None")
        scenario_id = None

    if e_class not in _EQUIVALENCE_TO_SCENARIOS:
        if e_class is not None:
            print(f"⚠️  LLM 返回的 equivalence_class={e_class!r} 非 E1-E5，置 None")
        e_class = None

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "equivalence_class": e_class,
        "scenario_id": scenario_id,
        "confidence": max(0.0, min(1.0, confidence)),
        "reasoning": str(raw.get("reasoning", "")),
        "raw": raw,
    }


__all__ = ["classify_to_equivalence_class"]

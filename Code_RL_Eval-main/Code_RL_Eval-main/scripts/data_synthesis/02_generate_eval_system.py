#!/usr/bin/env python3
"""
第二步：使用大模型编写评测体系

为指定任务生成完整评测体系：
1. metadata.json
2. hard_gates.py + objective_metrics.py + verifier.py
3. rubric.py

用法：
    python scripts/02_generate_eval_system.py --case manual_tasks/milvus-io__pymilvus__d36851e__2764
"""

import json
import argparse
import sys
from pathlib import Path

# 添加 scripts 到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.llm_client import call_llm
from utils.context_collector import collect_repo_context
from utils.prompt_loader import load_prompt
from utils.rubric_builder import build_rubric, build_rubric_markdown
from utils.equivalence_classifier import classify_to_equivalence_class


def format_config_files_section(config_files: dict) -> str:
    """格式化配置文件部分"""
    if not config_files:
        return "(无配置文件)"

    sections = []
    for filename, content in config_files.items():
        sections.append(f"#### {filename}\n```\n{content}\n```")

    return "\n\n".join(sections)


def extract_json_from_response(response: str) -> str:
    """
    从响应中提取有效的 JSON 对象

    处理模型可能输出的多种情况：
    - 纯 JSON
    - Markdown 代码块包裹的 JSON
    - 前面有额外文本或多个小 JSON 对象的情况
    """
    response = response.strip()

    # 如果是 Markdown 代码块，提取内容
    if response.startswith('```'):
        lines = response.split('\n')
        # 去掉第一行的 ```json 或 ```
        lines = lines[1:]
        # 去掉最后的 ```
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        response = '\n'.join(lines).strip()

    # 查找所有可能的 JSON 对象（{ ... }）
    candidates = []
    depth = 0
    start_idx = -1

    for i, char in enumerate(response):
        if char == '{':
            if depth == 0:
                start_idx = i
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0 and start_idx != -1:
                # 找到一个完整的 JSON 对象
                candidates.append(response[start_idx:i + 1])
                start_idx = -1

    if not candidates:
        raise ValueError("响应中未找到有效的 JSON 对象")

    # 返回最长的候选（通常是完整的 metadata.json）
    return max(candidates, key=len)


def generate_metadata(task_dir: Path) -> dict:
    """生成 metadata.json"""
    print("\n[1/4] 生成 metadata.json")

    context = collect_repo_context(task_dir)

    # 加载提示词模板
    template = load_prompt("metadata_generation")

    prompt = template.format(
        directory_tree=context.get("directory_tree", "(无)"),
        refactor_md=context.get("refactor_md", "(无)"),
        config_files_section=format_config_files_section(context.get("config_files", {}))
    )

    max_retries = 3
    last_response = None

    for attempt in range(1, max_retries + 1):
        print(f"调用 LLM (尝试 {attempt}/{max_retries})...", flush=True)
        response = call_llm(prompt)
        last_response = response

        # 提取 JSON
        try:
            json_str = extract_json_from_response(response)
            metadata = json.loads(json_str)
            # 注入确定性字段（commit SHA、url 等），覆盖 LLM 无法得知而填的 "unknown"
            inject_deterministic_fields(task_dir, metadata)
            print("✓ metadata.json 生成成功")
            return metadata
        except (ValueError, json.JSONDecodeError) as e:
            if attempt < max_retries:
                print(f"  ⚠️  JSON 解析失败: {e}，重试中...")
            else:
                print(f"❌ JSON 解析失败: {e}")
                print(f"\n原始输出 (前 2000 字符):\n{last_response[:2000]}")
                raise

    # 理论上不会到这里
    raise ValueError("生成 metadata.json 失败")


def _first_nonempty(item: dict, *keys):
    """按顺序取第一个存在且非空的字段值。"""
    for k in keys:
        v = item.get(k)
        if v not in (None, "", "unknown"):
            return v
    return None


def inject_deterministic_fields(task_dir: Path, metadata: dict) -> None:
    """用第 1 步保存的 source_item.json 注入确定性字段，覆盖 LLM 生成的占位值。

    commit SHA、仓库 url、PR url 等属于确定性事实，LLM 无从得知（只拿到目录树和
    refactor.md），会填成 "unknown"。这里从原始数据源回填真实值。
    """
    source_path = task_dir / "source_item.json"
    if not source_path.exists():
        print(f"  ⚠️  未找到 source_item.json，跳过确定性字段注入（commit SHA 可能为 unknown）", file=sys.stderr)
        return

    with open(source_path, encoding="utf-8") as f:
        item = json.load(f)

    repo = metadata.setdefault("repository", {})
    base_sha = _first_nonempty(item, "before_commit_sha", "pr_base_sha", "commit_id")
    pr_sha = _first_nonempty(item, "pr_head_sha", "after_commit_sha", "pr_commit_sha")
    merge_sha = _first_nonempty(item, "merge_commit_sha")
    repo_url = _first_nonempty(item, "repository_url")
    if base_sha:
        repo["base_commit_sha"] = base_sha
    if pr_sha:
        repo["pr_commit_sha"] = pr_sha
    if merge_sha:
        repo["merge_commit_sha"] = merge_sha
    if repo_url:
        repo["url"] = repo_url

    # source.url：优先 PR url
    pr_url = _first_nonempty(item, "pr_url")
    if pr_url:
        metadata.setdefault("source", {})["url"] = pr_url

    # diff_download_url：用于第 3 步校准时下载官方 PR diff
    diff_url = _first_nonempty(item, "diff_download_url")
    if diff_url:
        metadata["diff_download_url"] = diff_url

    injected = [k for k, v in (
        ("base_commit_sha", base_sha), ("pr_commit_sha", pr_sha), ("diff_download_url", diff_url),
    ) if v]
    if injected:
        print(f"  ✓ 已注入确定性字段: {', '.join(injected)}")


def generate_verifier_files(task_dir: Path, metadata: dict) -> dict:
    """生成 hard_gates.py、objective_metrics.py、verifier.py"""
    print("\n[2/4] 生成 verifier 三件套 (hard_gates.py / objective_metrics.py / verifier.py)")

    context = collect_repo_context(task_dir)

    # 加载提示词模板
    template = load_prompt("verifier_generation")

    prompt = template.format(
        metadata_json=json.dumps(metadata, indent=2, ensure_ascii=False),
        directory_tree=context.get("directory_tree", "(无)"),
        config_files_section=format_config_files_section(context.get("config_files", {}))
    )

    max_retries = 3
    last_response = None

    for attempt in range(1, max_retries + 1):
        print(f"调用 LLM (尝试 {attempt}/{max_retries})...", flush=True)
        response = call_llm(prompt)
        last_response = response

        # 解析三个文件
        # 支持两种格式：
        # 1. ===== FILE: xxx.py =====
        # 2. # FILE: xxx.py 或 ## xxx.py
        files = {}
        current_file = None
        current_content = []

        lines = response.split('\n')
        for line in lines:
            # 检测文件分隔符
            if '===== FILE:' in line or line.startswith('# FILE:') or line.startswith('## FILE:'):
                # 保存上一个文件
                if current_file and current_content:
                    # 清理开头和结尾的空行
                    content = '\n'.join(current_content).strip()
                    # 如果内容被代码块包裹，去掉
                    if content.startswith('```python'):
                        content = content[9:].strip()
                    if content.startswith('```'):
                        content = content[3:].strip()
                    if content.endswith('```'):
                        content = content[:-3].strip()
                    files[current_file] = content

                # 提取文件名
                if '===== FILE:' in line:
                    current_file = line.split('FILE:')[1].split('=====')[0].strip()
                else:
                    # # FILE: xxx.py 格式
                    current_file = line.split('FILE:')[1].strip()
                current_content = []
            elif current_file:
                current_content.append(line)

        # 保存最后一个文件
        if current_file and current_content:
            content = '\n'.join(current_content).strip()
            if content.startswith('```python'):
                content = content[9:].strip()
            if content.startswith('```'):
                content = content[3:].strip()
            if content.endswith('```'):
                content = content[:-3].strip()
            files[current_file] = content

        # 检查是否有三个文件
        expected = ['hard_gates.py', 'objective_metrics.py', 'verifier.py']
        missing = [f for f in expected if f not in files]

        if not missing:
            print(f"✓ 生成了 {len(files)} 个文件")
            return files

        # 解析失败
        if attempt < max_retries:
            print(f"  ⚠️  解析失败，缺少文件: {', '.join(missing)}，重试中...")
        else:
            # 最后一次失败，打印详细信息
            print(f"❌ 缺少文件: {', '.join(missing)}")
            print(f"找到的文件: {list(files.keys())}")
            print(f"\n原始响应 (前 1500 字符):\n{last_response[:1500]}")
            raise ValueError(f"❌ 缺少文件: {', '.join(missing)}")

    # 理论上不会到这里
    raise ValueError("生成 verifier 文件失败")


def generate_rubric(task_dir: Path, metadata: dict) -> tuple[str, str]:
    """Rubric 生成：

    A 线：LLM 分类 (E 类, scenario_id) → 从 scenario_index.json + c*.json 装载骨架。
    然后基于骨架用 Codex Agent 版本模板生成 rubric.py 源码
    （评分时由 codex agent 进仓库自行读取完整 diff/源码/测试）。

    Returns:
        (rubric_py_source, rubric_md_source): rubric.py 和 rubric.md 的源码
    """
    print("\n[3/4] 收集仓库上下文")
    context = collect_repo_context(task_dir)

    print("[3/4] A 线：LLM 分类等价类与场景")
    classification = classify_to_equivalence_class(metadata, context)
    print(
        f"✓ 分类结果: equivalence_class={classification['equivalence_class']!r} "
        f"scenario_id={classification['scenario_id']!r} "
        f"confidence={classification['confidence']:.2f}"
    )
    if classification["reasoning"]:
        print(f"  理由: {classification['reasoning']}")

    print("[3/4] A 线：装载 must/recommended 维度骨架")
    task_context = {
        "equivalence_class": classification["equivalence_class"],
        "scenario_id": classification["scenario_id"],
        "classification_confidence": classification["confidence"],
        "classification_reasoning": classification["reasoning"],
    }
    base_rubric = build_rubric(metadata, task_context)
    selected = base_rubric["dimension_selection"]["selected"]
    total_atoms = sum(len(d["questions"]) for d in base_rubric["dimensions"].values())
    print(f"✓ A 线骨架: 维度 {selected}, 共 {total_atoms} 个 Atom")

    print("[3/4] 装载固定 rubric.py（通用评分器，逐字节拷贝）")
    fixed_rubric_path = Path(__file__).parent.parent / "templates" / "rubric.py"
    rubric_py_source = fixed_rubric_path.read_text(encoding="utf-8")
    print(f"✓ 固定 rubric.py 已装载（{fixed_rubric_path}）")

    # 生成 rubric.md 文档
    print("[3/4] 生成 rubric.md 文档")
    rubric_md_source = build_rubric_markdown(metadata, task_context)
    print("✓ rubric.md 生成完成")

    return rubric_py_source, rubric_md_source


def save_outputs(task_dir: Path, metadata: dict, verifier_files: dict, rubric_py: str, rubric_md: str):
    """保存所有生成的文件"""
    print("\n[4/4] 保存文件")

    # metadata.json
    metadata_path = task_dir / "metadata.json"
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    print(f"✓ {metadata_path}")

    # verifier 三件套
    for filename, content in verifier_files.items():
        file_path = task_dir / filename
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✓ {file_path}")

    # rubric.py
    rubric_py_path = task_dir / "rubric.py"
    with open(rubric_py_path, 'w', encoding='utf-8') as f:
        f.write(rubric_py)
    print(f"✓ {rubric_py_path}")

    # rubric.md
    rubric_md_path = task_dir / "rubric.md"
    with open(rubric_md_path, 'w', encoding='utf-8') as f:
        f.write(rubric_md)
    print(f"✓ {rubric_md_path}")


def main():
    parser = argparse.ArgumentParser(description='使用大模型生成评测体系')
    parser.add_argument('--case', type=str, required=True,
                        help='任务目录路径，如: manual_tasks/milvus-io__pymilvus__d36851e__2764')

    args = parser.parse_args()

    try:
        # 解析任务目录
        task_dir = Path(args.case)
        if not task_dir.exists():
            print(f"❌ 任务目录不存在: {task_dir}")
            sys.exit(1)

        if not task_dir.is_dir():
            print(f"❌ 不是有效的目录: {task_dir}")
            sys.exit(1)

        repo_dir = task_dir / "repo"
        if not repo_dir.exists():
            print(f"❌ repo/ 目录不存在: {repo_dir}")
            sys.exit(1)

        print(f"任务目录: {task_dir}\n")

        # 生成各个组件
        metadata = generate_metadata(task_dir)
        verifier_files = generate_verifier_files(task_dir, metadata)
        rubric_py, rubric_md = generate_rubric(task_dir, metadata)

        # 保存文件
        save_outputs(task_dir, metadata, verifier_files, rubric_py, rubric_md)

        print(f"\n{'='*60}")
        print(f"✅ 评测体系生成完成")
        print(f"输出目录: {task_dir.absolute()}")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

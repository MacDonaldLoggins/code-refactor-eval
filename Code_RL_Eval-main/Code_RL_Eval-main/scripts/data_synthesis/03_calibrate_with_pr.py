#!/usr/bin/env python3
"""
第三步：用真实 PR 验证评测体系（校准）

从 metadata.json 读取 PR commit 信息，导出 patch，运行 verifier 3 次，生成校准报告。
如果得分 < 6，调用 LLM 分析并优化 verifier 脚本，最多重复 3 轮。

用法：
    python scripts/03_calibrate_with_pr.py --case manual_tasks/fastapi__fastapi__c48e67b__16013
    python scripts/03_calibrate_with_pr.py --case <case_dir> --no-optimize  # 禁用优化循环
"""

import json
import argparse
import sys
import subprocess
import os
from pathlib import Path
from typing import Dict, Any, List

# 添加 scripts 到 path
sys.path.insert(0, str(Path(__file__).parent.parent))


def load_metadata(task_dir: Path) -> Dict[str, Any]:
    """加载 metadata.json"""
    metadata_path = task_dir / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"metadata.json 不存在: {metadata_path}")

    with open(metadata_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def export_patch_from_bundle(task_dir: Path, metadata: Dict[str, Any]):
    """从 diff_download_url 下载 PR diff bundle，提取 raw.diff 作为 reference_pr.patch

    bundle 为 zip，内含 raw.diff（官方 verified_complete 的完整 PR diff）。
    成功返回 patch 路径；若无 diff_download_url 或下载/解析失败则返回 None（交由本地 git diff 兜底）。
    """
    import os
    import urllib.request
    import zipfile
    import tempfile

    diff_url = metadata.get("diff_download_url")
    if not diff_url:
        return None

    print("尝试从 diff_download_url 获取官方 PR diff bundle...")
    tmp_zip = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_zip = tmp.name
        with urllib.request.urlopen(diff_url, timeout=60) as resp:
            with open(tmp_zip, "wb") as f:
                f.write(resp.read())

        with zipfile.ZipFile(tmp_zip) as zf:
            names = zf.namelist()
            if "raw.diff" not in names:
                print(f"⚠️  bundle 中未找到 raw.diff，实际内容: {names}")
                return None
            raw = zf.read("raw.diff").decode("utf-8")
    except Exception as e:
        print(f"⚠️  下载/解析 diff bundle 失败，将回退到本地 git diff: {e}")
        return None
    finally:
        if tmp_zip and os.path.exists(tmp_zip):
            os.remove(tmp_zip)

    patch_path = task_dir / "reference_pr.patch"
    with open(patch_path, "w", encoding="utf-8") as f:
        f.write(raw)

    print(f"✓ 已从 bundle 导出 patch: {patch_path}（{raw.count(chr(10)) + 1} 行, {len(raw)} 字节）")
    return patch_path


def export_pr_patch(task_dir: Path, metadata: Dict[str, Any]) -> Path:
    """导出真实 PR 的 patch"""
    print("=== 步骤 1: 检查真实 PR patch ===")

    # 检查是否已经存在 reference_pr.patch
    existing_patch = task_dir / "reference_pr.patch"
    if existing_patch.exists():
        print(f"✓ 已存在 patch 文件: {existing_patch}")
        with open(existing_patch, 'r') as f:
            lines = f.readlines()
            print(f"  行数: {len(lines)}")
        return existing_patch

    # 优先：从 diff_download_url 下载官方 PR diff bundle，取其中的 raw.diff
    # （快照通常没有 .git，无法本地 git diff；bundle 是 verified_complete 的完整 diff）
    bundle_patch = export_patch_from_bundle(task_dir, metadata)
    if bundle_patch:
        return bundle_patch

    # 兜底：本地 git diff（要求 repo 保留 .git 历史）
    # 支持两种 metadata 格式
    if "repository" in metadata:
        # 格式2: 详细格式
        base_commit = metadata.get("repository", {}).get("base_commit_sha")
        pr_commit = metadata.get("repository", {}).get("pr_commit_sha")
    else:
        # 格式1: 简单格式
        base_commit = metadata.get("commit_id")
        pr_commit = metadata.get("pr_commit_sha")

    if not base_commit or base_commit == "unknown":
        raise ValueError("metadata.json 缺少有效的 base_commit_sha")

    if not pr_commit or pr_commit == "unknown":
        print("❌ metadata.json 缺少有效的 pr_commit_sha")
        print("这是一个 issue case，没有真实 PR 可以校准")
        print("提示：需要先手动准备 reference_pr.patch")
        sys.exit(1)

    repo_dir = task_dir / "repo"
    if not repo_dir.exists():
        raise FileNotFoundError(f"repo/ 目录不存在: {repo_dir}")

    # 检查是否有 .git 目录
    git_dir = repo_dir / ".git"
    if not git_dir.exists():
        print("⚠️  repo/ 没有 .git 目录，无法自动生成 patch")
        raise FileNotFoundError("需要手动准备 reference_pr.patch")

    print(f"Base commit: {base_commit[:7]}")
    print(f"PR commit: {pr_commit[:7]}")

    # 生成 patch
    patch_path = task_dir / "reference_pr.patch"
    try:
        result = subprocess.run(
            ["git", "diff", f"{base_commit}..{pr_commit}"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True
        )

        with open(patch_path, 'w', encoding='utf-8') as f:
            f.write(result.stdout)

        # 统计改动
        stats = subprocess.run(
            ["git", "diff", "--stat", f"{base_commit}..{pr_commit}"],
            cwd=repo_dir,
            capture_output=True,
            text=True
        )

        print(f"✓ 已导出 patch: {patch_path}")
        print(f"\n改动统计:")
        print(stats.stdout)

        return patch_path

    except subprocess.CalledProcessError as e:
        print(f"❌ git diff 失败: {e}")
        print(f"stdout: {e.stdout}")
        print(f"stderr: {e.stderr}")
        raise


def apply_patch_to_temp_workspace(repo_dir: Path, patch_path: Path, run_id: int) -> Path:
    """
    将 patch 应用到临时工作空间

    Returns:
        临时工作空间路径
    """
    import tempfile
    import shutil

    # 创建临时工作空间
    temp_dir = tempfile.mkdtemp(prefix=f"verifier_run_{run_id}_")
    temp_repo = Path(temp_dir) / "repo"

    # 复制 repo 到临时目录
    shutil.copytree(repo_dir, temp_repo, symlinks=True)

    # 应用 patch
    result = subprocess.run(
        ["git", "apply", str(patch_path.absolute())],
        cwd=temp_repo,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"⚠️  git apply 失败: {result.stderr}")
        raise RuntimeError(f"Failed to apply patch: {result.stderr}")

    return temp_repo


def run_verifier(task_dir: Path, patch_path: Path, run_id: int, output_dir: Path) -> Dict[str, Any]:
    """运行 verifier 一次"""
    print(f"\n--- 运行 {run_id}/3 ---")

    verifier_path = task_dir / "verifier.py"
    if not verifier_path.exists():
        raise FileNotFoundError(f"verifier.py 不存在: {verifier_path}")

    repo_dir = task_dir / "repo"
    output_path = output_dir / f"run_{run_id}.json"

    try:
        # 创建临时工作空间并应用 patch
        print(f"创建临时工作空间...")
        temp_repo = apply_patch_to_temp_workspace(repo_dir, patch_path, run_id)

        print(f"运行 verifier.py...")

        # 调用 verifier.py
        result = subprocess.run(
            [sys.executable, verifier_path.name, str(temp_repo)],
            cwd=verifier_path.parent,
            capture_output=True,
            text=True,
            timeout=300
        )

        # 解析输出
        if result.returncode in [0, 1, 2]:
            output = result.stdout.strip()

            # 如果 stdout 为空，检查 stderr
            if not output and result.stderr:
                print(f"⚠️  stdout 为空，stderr 内容:")
                print(result.stderr[:1000])
                raise RuntimeError("Verifier 没有输出 JSON 结果")

            try:
                # 尝试从输出中提取最后一个完整的 JSON 对象
                # verifier 可能输出多行日志 + 最终的 JSON 结果

                # 方法 1: 尝试直接解析（如果输出就是纯 JSON）
                try:
                    verifier_result = json.loads(output)
                except json.JSONDecodeError:
                    # 方法 2: 查找所有可能的 JSON 对象
                    import re
                    # 找到所有 { ... } 块
                    json_candidates = []
                    depth = 0
                    start_idx = -1

                    for i, char in enumerate(output):
                        if char == '{':
                            if depth == 0:
                                start_idx = i
                            depth += 1
                        elif char == '}':
                            depth -= 1
                            if depth == 0 and start_idx != -1:
                                json_str = output[start_idx:i+1]
                                try:
                                    obj = json.loads(json_str)
                                    # 检查是否是 verifier 的结果（必须有 hard_gates 和 status）
                                    if isinstance(obj, dict) and 'hard_gates' in obj and 'status' in obj:
                                        json_candidates.append(obj)
                                except json.JSONDecodeError:
                                    pass
                                start_idx = -1

                    if not json_candidates:
                        print(f"❌ 无法在输出中找到有效的 verifier JSON 结果")
                        print(f"stdout 前 1000 字符:\n{output[:1000]}")
                        raise RuntimeError("输出中没有有效的 verifier JSON")

                    # 使用最后一个找到的完整 verifier 结果
                    verifier_result = json_candidates[-1]

                # 保存结果
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(verifier_result, f, indent=2, ensure_ascii=False)

                print(f"✓ reward: {verifier_result.get('reward', 0):.2f}")
                print(f"  status: {verifier_result.get('status', 'unknown')}")

                # 显示失败的硬门控
                failed_gates = verifier_result.get("hard_gates", {}).get("failed_gates", [])
                if failed_gates:
                    print(f"  ⚠️  失败的硬门控: {', '.join(failed_gates)}")

                # 清理临时目录
                import shutil
                shutil.rmtree(temp_repo.parent)

                return verifier_result

            except json.JSONDecodeError as e:
                print(f"❌ JSON 解析失败: {e}")
                print(f"尝试提取的 JSON 字符串（前 500 字符）:\n{json_str[:500] if 'json_str' in locals() else 'N/A'}")
                print(f"完整 stdout 前 1000 字符:\n{output[:1000]}")
                print(f"stderr 前 500 字符:\n{result.stderr[:500]}")
                raise
        else:
            print(f"❌ verifier 执行失败 (exit code {result.returncode})")
            print(f"stdout: {result.stdout[:500]}")
            print(f"stderr: {result.stderr[:500]}")
            raise RuntimeError(f"Verifier failed with exit code {result.returncode}")

    except subprocess.TimeoutExpired:
        print(f"❌ verifier 执行超时（>5分钟）")
        raise
    except Exception as e:
        print(f"❌ 运行失败: {e}")
        raise


def generate_calibration_report(
    task_dir: Path,
    metadata: Dict[str, Any],
    patch_path: Path,
    runs: List[Dict[str, Any]],
    output_dir: Path
) -> Dict[str, Any]:
    """生成校准报告"""
    print("\n=== 步骤 3: 生成校准报告 ===")

    # 检查一致性
    rewards = [run.get("reward", 0) for run in runs]
    reward_variance = max(rewards) - min(rewards) if rewards else 0

    # 检查硬门控一致性
    hard_gates_consistent = True
    if len(runs) > 1:
        first_gates = runs[0].get("hard_gates", {}).get("gates", {})
        for run in runs[1:]:
            current_gates = run.get("hard_gates", {}).get("gates", {})
            if first_gates != current_gates:
                hard_gates_consistent = False
                break

    # 提取 PR 信息（支持两种格式）
    if "repository" in metadata:
        pr_url = metadata.get("source", {}).get("url")
        base_commit = metadata.get("repository", {}).get("base_commit_sha")
        pr_commit = metadata.get("repository", {}).get("pr_commit_sha")
        merge_commit = metadata.get("repository", {}).get("merge_commit_sha")
    else:
        pr_url = metadata.get("url")
        base_commit = metadata.get("commit_id")
        pr_commit = metadata.get("pr_commit_sha")
        merge_commit = metadata.get("merge_commit_sha")

    report = {
        "reference_pr_patch": str(patch_path.name),
        "pr_info": {
            "url": pr_url,
            "base_commit": base_commit,
            "pr_commit": pr_commit,
            "merge_commit": merge_commit
        },
        "runs": runs,
        "consistency": {
            "reward_variance": round(reward_variance, 6),
            "hard_gates_consistent": hard_gates_consistent,
            "objective_metrics_consistent": True,  # TODO: 详细检查
            "rubric_consistent": True
        },
        "summary": {
            "all_hard_gates_passed": all(
                not run.get("hard_gates", {}).get("failed_gates", [])
                for run in runs
            ),
            "average_reward": round(sum(rewards) / len(rewards), 4) if rewards else 0,
            "issues_found": []
        }
    }

    # 检测问题
    if reward_variance > 0.01:
        report["summary"]["issues_found"].append(
            f"reward 方差较大 ({reward_variance:.4f})，评测可能不稳定"
        )

    if not hard_gates_consistent:
        report["summary"]["issues_found"].append("硬门控结果不一致")

    # 保存 JSON 报告
    report_path = output_dir / "calibration_report.json"
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"✓ 已保存 JSON 报告: {report_path}")

    # 生成人类可读的分析文档
    analysis_path = output_dir / "calibration_analysis.md"
    with open(analysis_path, 'w', encoding='utf-8') as f:
        f.write("# 真实 PR 校准分析\n\n")
        f.write("## 基本信息\n")
        f.write(f"- PR: {pr_url or 'unknown'}\n")
        f.write(f"- Base commit: {base_commit[:7] if base_commit else 'unknown'}\n")
        f.write(f"- PR commit: {pr_commit[:7] if pr_commit else 'unknown'}\n")
        f.write(f"- 评测运行次数: {len(runs)}\n\n")

        f.write("## 评测结果\n\n")

        # 硬门控
        f.write("### 硬门控\n")
        if runs:
            gates = runs[0].get("hard_gates", {}).get("gates", {})
            for gate_name, gate_info in gates.items():
                status = gate_info.get("status", "unknown")
                symbol = "✓" if status == "pass" else "✗" if status == "fail" else "○"
                f.write(f"- {gate_name}: {symbol} {status}（3/3 一致）\n")
        f.write("\n")

        # 客观指标
        f.write("### 客观指标\n")
        if runs and runs[0].get("objective_metrics"):
            metrics = runs[0].get("objective_metrics", {}).get("metrics", {})
            if metrics:
                for metric_name, metric_data in metrics.items():
                    baseline = metric_data.get("baseline", "N/A")
                    candidate = metric_data.get("candidate", "N/A")
                    delta = metric_data.get("delta", "N/A")
                    f.write(f"- {metric_name}: {baseline} → {candidate} (Δ {delta})\n")
            else:
                f.write("- 无客观指标数据\n")
        else:
            f.write("- 未计算（硬门控失败时跳过）\n")
        f.write("\n")

        # Rubric 评分
        f.write("### Rubric 评分\n")
        if runs and runs[0].get("rubric"):
            rubric = runs[0].get("rubric", {})
            items = rubric.get("items", {})
            if items:
                for item_name, item_data in items.items():
                    earned = item_data.get("earned", 0)
                    points = item_data.get("points", 0)
                    f.write(f"- {item_name}: {earned}/{points}\n")
                f.write(f"\n**总分**: {rubric.get('score', 0):.2f}/{rubric.get('max_score', 1.0)}\n\n")
            else:
                f.write("- 无评分项\n\n")
        else:
            f.write("- 未计算（硬门控失败时跳过）\n\n")

        # 一致性检查
        f.write("## 一致性检查\n")
        f.write(f"- reward 方差: {reward_variance:.6f}\n")
        if reward_variance < 0.01:
            f.write("- 3 次运行结果一致 ✓\n\n")
        else:
            f.write("- ⚠️  3 次运行结果存在差异\n\n")

        # 发现的问题
        f.write("## 发现的问题\n")
        if report["summary"]["issues_found"]:
            for issue in report["summary"]["issues_found"]:
                f.write(f"- ⚠️  {issue}\n")
        else:
            f.write("- 无\n")
        f.write("\n")

        # 结论
        f.write("## 结论\n")
        if report['summary']['all_hard_gates_passed']:
            f.write("✓ 评测体系能够正确识别真实 PR 的重构。\n")
        else:
            f.write("⚠️  真实 PR 未通过所有硬门控，需要检查评测体系。\n")

    print(f"✓ 已保存分析文档: {analysis_path}")

    return report
    """生成校准报告"""
    print("\n=== 步骤 3: 生成校准报告 ===")

    # 检查一致性
    rewards = [run.get("reward", 0) for run in runs]
    reward_variance = max(rewards) - min(rewards) if rewards else 0

    # 检查硬门控一致性
    hard_gates_consistent = True
    if len(runs) > 1:
        first_gates = runs[0].get("hard_gates", {}).get("gates", {})
        for run in runs[1:]:
            current_gates = run.get("hard_gates", {}).get("gates", {})
            if first_gates != current_gates:
                hard_gates_consistent = False
                break

    # 提取 PR 信息（支持两种格式）
    if "repository" in metadata:
        pr_url = metadata.get("source", {}).get("url")
        base_commit = metadata.get("repository", {}).get("base_commit_sha")
        pr_commit = metadata.get("repository", {}).get("pr_commit_sha")
        merge_commit = metadata.get("repository", {}).get("merge_commit_sha")
    else:
        pr_url = metadata.get("url")
        base_commit = metadata.get("commit_id")
        pr_commit = metadata.get("pr_commit_sha")
        merge_commit = metadata.get("merge_commit_sha")

    report = {
        "reference_pr_patch": str(patch_path.name),
        "pr_info": {
            "url": pr_url,
            "base_commit": base_commit,
            "pr_commit": pr_commit,
            "merge_commit": merge_commit
        },
        "runs": runs,
        "consistency": {
            "reward_variance": round(reward_variance, 6),
            "hard_gates_consistent": hard_gates_consistent,
            "objective_metrics_consistent": True,  # TODO: 详细检查
            "rubric_consistent": True
        },
        "summary": {
            "all_hard_gates_passed": all(
                not run.get("hard_gates", {}).get("failed_gates", [])
                for run in runs
            ),
            "average_reward": round(sum(rewards) / len(rewards), 4) if rewards else 0,
            "issues_found": []
        }
    }

    # 检测问题
    if reward_variance > 0.01:
        report["summary"]["issues_found"].append(
            f"reward 方差较大 ({reward_variance:.4f})，评测可能不稳定"
        )

    if not hard_gates_consistent:
        report["summary"]["issues_found"].append("硬门控结果不一致")

    # 保存 JSON 报告
    report_path = task_dir / "reference_pr_calibration_report.json"
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"✓ 已保存 JSON 报告: {report_path}")

    # 生成人类可读的分析文档
    analysis_path = task_dir / "reference_pr_calibration_analysis.md"
    with open(analysis_path, 'w', encoding='utf-8') as f:
        f.write("# 真实 PR 校准分析\n\n")
        f.write("## 基本信息\n")
        f.write(f"- PR: {pr_url or 'unknown'}\n")
        f.write(f"- Base commit: {base_commit[:7] if base_commit else 'unknown'}\n")
        f.write(f"- PR commit: {pr_commit[:7] if pr_commit else 'unknown'}\n")
        f.write(f"- 评测运行次数: {len(runs)}\n\n")

        f.write("## 评测结果\n\n")

        # 硬门控
        f.write("### 硬门控\n")
        if runs:
            gates = runs[0].get("hard_gates", {}).get("gates", {})
            for gate_name, gate_info in gates.items():
                status = gate_info.get("status", "unknown")
                symbol = "✓" if status == "pass" else "✗" if status == "fail" else "○"
                f.write(f"- {gate_name}: {symbol} {status}（3/3 一致）\n")
        f.write("\n")

        # 客观指标
        f.write("### 客观指标\n")
        if runs and runs[0].get("objective_metrics"):
            metrics = runs[0].get("objective_metrics", {}).get("metrics", {})
            if metrics:
                for metric_name, metric_data in metrics.items():
                    baseline = metric_data.get("baseline", "N/A")
                    candidate = metric_data.get("candidate", "N/A")
                    delta = metric_data.get("delta", "N/A")
                    f.write(f"- {metric_name}: {baseline} → {candidate} (Δ {delta})\n")
            else:
                f.write("- 无客观指标数据\n")
        else:
            f.write("- 未计算（硬门控失败时跳过）\n")
        f.write("\n")

        # Rubric 评分
        f.write("### Rubric 评分\n")
        if runs and runs[0].get("rubric"):
            rubric = runs[0].get("rubric", {})
            items = rubric.get("items", {})
            if items:
                for item_name, item_data in items.items():
                    earned = item_data.get("earned", 0)
                    points = item_data.get("points", 0)
                    f.write(f"- {item_name}: {earned}/{points}\n")
                f.write(f"\n**总分**: {rubric.get('score', 0):.2f}/{rubric.get('max_score', 1.0)}\n\n")
            else:
                f.write("- 无评分项\n\n")
        else:
            f.write("- 未计算（硬门控失败时跳过）\n\n")

        # 一致性检查
        f.write("## 一致性检查\n")
        f.write(f"- reward 方差: {reward_variance:.6f}\n")
        if reward_variance < 0.01:
            f.write("- 3 次运行结果一致 ✓\n\n")
        else:
            f.write("- ⚠️  3 次运行结果存在差异\n\n")

        # 发现的问题
        f.write("## 发现的问题\n")
        if report["summary"]["issues_found"]:
            for issue in report["summary"]["issues_found"]:
                f.write(f"- ⚠️  {issue}\n")
        else:
            f.write("- 无\n")
        f.write("\n")

        # 结论
        f.write("## 结论\n")
        if report['summary']['all_hard_gates_passed']:
            f.write("✓ 评测体系能够正确识别真实 PR 的重构。\n")
        else:
            f.write("⚠️  真实 PR 未通过所有硬门控，需要检查评测体系。\n")

    print(f"✓ 已保存分析文档: {analysis_path}")

    return report


def calculate_progress_metrics(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """计算进步指标

    Returns:
        包含各种指标的字典
    """
    if not runs:
        return {
            'reward': 0.0,
            'failed_count': 0,
            'passed_count': 0,
            'failed_gates': [],
            'passed_gates': []
        }

    avg_reward = sum(r.get('reward', 0) for r in runs) / len(runs)
    failed_gates = runs[0].get('hard_gates', {}).get('failed_gates', [])
    all_gates = runs[0].get('hard_gates', {}).get('gates', {})

    passed_gates = [
        name for name, gate in all_gates.items()
        if gate.get('applicable', True) and gate.get('status') == 'pass'
    ]

    return {
        'reward': avg_reward,
        'failed_count': len(failed_gates),
        'passed_count': len(passed_gates),
        'failed_gates': failed_gates,
        'passed_gates': passed_gates
    }


def compare_progress(current: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    """比较两次迭代的进步

    Returns:
        包含进步分析的字典
    """
    reward_delta = current['reward'] - previous['reward']
    failed_delta = current['failed_count'] - previous['failed_count']

    # 找出新通过的硬门控
    newly_passed = set(previous['failed_gates']) - set(current['failed_gates'])
    # 找出新失败的硬门控
    newly_failed = set(current['failed_gates']) - set(previous['failed_gates'])
    # 仍然失败的硬门控
    still_failed = set(previous['failed_gates']) & set(current['failed_gates'])

    return {
        'reward_delta': reward_delta,
        'failed_delta': failed_delta,
        'newly_passed': list(newly_passed),
        'newly_failed': list(newly_failed),
        'still_failed': list(still_failed),
        'has_improvement': failed_delta < 0 or reward_delta > 0.01,
        'has_regression': len(newly_failed) > 0
    }


def analyze_and_optimize_verifier(
    task_dir: Path,
    metadata: Dict[str, Any],
    patch_path: Path,
    runs: List[Dict[str, Any]],
    iteration: int,
    previous_metrics: Dict[str, Any] = None
) -> tuple[Dict[str, str], str]:
    """分析 verifier 失分原因并生成优化后的脚本

    Args:
        previous_metrics: 上一轮的指标（用于进步分析）

    Returns:
        (optimized_files, analysis): 优化后的文件字典和分析报告
    """
    from utils.llm_client import call_llm
    from utils.context_collector import collect_repo_context

    print(f"\n=== 优化迭代 {iteration}: 分析失分原因 ===")

    # 收集当前 verifier 脚本
    verifier_files = {}
    for filename in ['hard_gates.py', 'objective_metrics.py', 'verifier.py']:
        file_path = task_dir / filename
        if file_path.exists():
            verifier_files[filename] = file_path.read_text(encoding='utf-8')

    # 收集仓库上下文
    context = collect_repo_context(task_dir)

    # 读取 reference_pr.patch
    patch_content = patch_path.read_text(encoding='utf-8')

    # 应用 patch 后读取关键文件（提供真实执行环境）
    print("应用 patch 到临时工作区以获取真实代码状态...")
    repo_dir = task_dir / "repo"
    try:
        temp_repo = apply_patch_to_temp_workspace(repo_dir, patch_path, 0)

        # 尝试读取关键文件
        applied_files_context = ""
        for root, dirs, files in os.walk(temp_repo):
            for file in files:
                if file.endswith('.py'):
                    file_path = Path(root) / file
                    rel_path = file_path.relative_to(temp_repo)
                    # 只读取可能包含目标类的文件
                    if 'filter' in file.lower() or 'group_by' in str(rel_path).lower():
                        try:
                            content = file_path.read_text(encoding='utf-8')
                            if 'GroupByItemSetFilter' in content or 'class ' in content:
                                applied_files_context += f"\n### {rel_path}\n```python\n{content[:2000]}\n```\n"
                        except Exception:
                            pass

        # 清理临时目录
        import shutil
        shutil.rmtree(temp_repo.parent)
    except Exception as e:
        print(f"⚠️  无法应用 patch 到临时工作区: {e}")
        applied_files_context = "(无法获取应用 patch 后的代码状态)"

    # 计算当前指标
    current_metrics = calculate_progress_metrics(runs)

    # 进步分析
    progress_analysis = ""
    if previous_metrics:
        progress = compare_progress(current_metrics, previous_metrics)
        progress_analysis = f"""
# 进步分析（相比上一轮）

- Reward 变化: {previous_metrics['reward']:.2f} → {current_metrics['reward']:.2f} ({'+' if progress['reward_delta'] >= 0 else ''}{progress['reward_delta']:.2f})
- 失败硬门控: {previous_metrics['failed_count']} → {current_metrics['failed_count']} ({'+' if progress['failed_delta'] >= 0 else ''}{progress['failed_delta']})

{'✅ **有进步！**' if progress['has_improvement'] else '⚠️ **没有明显进步**'}

"""
        if progress['newly_passed']:
            progress_analysis += f"- ✅ 新通过: {', '.join(progress['newly_passed'])}\n"
        if progress['newly_failed']:
            progress_analysis += f"- ❌ 新失败: {', '.join(progress['newly_failed'])}\n"
        if progress['still_failed']:
            progress_analysis += f"- ↔️ 仍失败: {', '.join(progress['still_failed'])}\n"

    # 构建分析 prompt
    avg_reward = current_metrics['reward']
    failed_gates = current_metrics['failed_gates']

    prompt = f"""你是一个代码重构评测系统的专家。需要你分析为什么真实的 PR 没有通过评测，并优化 verifier 脚本。

# 任务背景
{json.dumps(metadata, indent=2, ensure_ascii=False)[:2000]}

{progress_analysis}

# 当前评测结果
- 平均得分: {avg_reward:.2f} / 10.0
- 失败的硬门控: {', '.join(failed_gates) if failed_gates else '无'}
- 通过的硬门控: {current_metrics['passed_count']} 个

# 详细评测结果
```json
{json.dumps(runs[0], indent=2, ensure_ascii=False)[:4000]}
```

# 真实 PR 的改动（reference_pr.patch 前 8000 字符）
```diff
{patch_content[:8000]}
```

# 应用 patch 后的关键代码文件
{applied_files_context[:3000]}

# 当前 verifier 脚本

## hard_gates.py（前 6000 字符）
```python
{verifier_files.get('hard_gates.py', '')[:6000]}
```

## objective_metrics.py（前 3000 字符）
```python
{verifier_files.get('objective_metrics.py', '')[:3000]}
```

## verifier.py（前 3000 字符）
```python
{verifier_files.get('verifier.py', '')[:3000]}
```

# 重要约束条件

⚠️ **你必须遵守以下约束，否则优化无效**：

1. **不能删除硬门控**
   - 不能删除 GATE_FUNCTIONS 中的任何函数
   - 不能将硬门控改为 `return _gate("na", False, ...)`

2. **不能降低评测标准**
   - 不能将 fail 改成 pass（如：`if not fields: return pass` 是错误的）
   - 不能跳过必要的检查（如：`return pass  # TODO: implement later`）
   - 不能放宽检查条件让所有情况都通过

3. **只能修复逻辑错误**
   - ✅ 正确：扩大搜索路径（从只搜索 query/ 改为搜索整个仓库）
   - ✅ 正确：使用 AST 精确检查（区分注释和实际代码）
   - ✅ 正确：修复错误的正则表达式或路径匹配
   - ❌ 错误：删除检查逻辑
   - ❌ 错误：将所有检查改为 pass

# 优化示例

❌ **错误的优化（降低标准）**：
```python
def public_api_pass(repo: Path):
    # 跳过检查，直接通过
    return _gate("pass", True, "已优化")
```

✅ **正确的优化（修复逻辑）**：
```python
def _find_class_info(repo: Path) -> ClassInfo | None:
    # 扩大搜索范围，不只搜索 query/
    search_roots = [
        repo / "metricflow-semantics" / "metricflow_semantics" / "model" / "semantics",  # 新增
        repo / "metricflow-semantics" / "metricflow_semantics" / "query",
    ]
    for root in search_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            info = _extract_class_info_from_source(repo, path)
            if info is not None:
                return info
    return None
```

# 你的任务

1. **分析失分原因**：
   - 为什么真实 PR 没有通过硬门控？
   - 是 verifier 脚本的逻辑错误（路径、检查逻辑）还是真实 PR 确实有问题？
   - 参考"应用 patch 后的关键代码文件"了解真实状态

2. **生成优化后的 verifier 脚本**：
   - 修正 hard_gates.py、objective_metrics.py、verifier.py
   - 必须输出**完整的文件内容**，不能省略任何部分
   - 保持评测标准的合理性

3. **输出格式**：

首先输出分析报告（markdown 格式）：

```markdown
# 失分原因分析

## 问题识别
[详细说明发现的问题]

## 根本原因
[解释为什么会出现这些问题]

## 优化方案
[说明如何修复，强调遵守了约束条件]
```

然后输出优化后的文件（**必须是完整文件，不能省略**）：

===== FILE: hard_gates.py =====
[完整的优化后代码，包含所有 import、所有函数、所有变量]

===== FILE: objective_metrics.py =====
[完整的优化后代码]

===== FILE: verifier.py =====
[完整的优化后代码]

注意：
- 必须输出完整的文件内容，包括所有 import 语句
- 不能只输出修改的部分
- 确保代码的正确性和健壮性
- 遵守所有约束条件
"""

    print("调用 LLM 进行分析和优化...", flush=True)
    response = call_llm(prompt)

    # 解析响应：分离分析报告和文件
    analysis = ""
    files = {}

    # 提取分析报告（markdown 部分）
    if "# 失分原因分析" in response or "## 问题识别" in response:
        parts = response.split("===== FILE:", 1)
        if len(parts) > 0:
            analysis = parts[0].strip()

    # 解析文件
    current_file = None
    current_content = []
    lines = response.split('\n')

    for line in lines:
        if '===== FILE:' in line:
            # 保存上一个文件
            if current_file and current_content:
                content = '\n'.join(current_content).strip()
                # 清理代码块标记
                if content.startswith('```python'):
                    content = content[9:].strip()
                if content.startswith('```'):
                    content = content[3:].strip()
                if content.endswith('```'):
                    content = content[:-3].strip()
                files[current_file] = content

            # 提取文件名
            current_file = line.split('FILE:')[1].split('=====')[0].strip()
            current_content = []
        elif current_file and not line.startswith('#') or (current_file and len(current_content) > 0):
            # 跳过文件头部的注释，直到遇到实际代码
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

    expected = ['hard_gates.py', 'objective_metrics.py', 'verifier.py']
    missing = [f for f in expected if f not in files]

    if missing:
        print(f"⚠️  LLM 未生成所有文件，缺少: {', '.join(missing)}")
        print(f"生成的文件: {list(files.keys())}")
        # 使用原始文件填充缺失的部分
        for f in missing:
            if f in verifier_files:
                files[f] = verifier_files[f]

    print(f"✓ LLM 分析完成，生成了 {len(files)} 个优化文件")

    return files, analysis


def validate_verifier_code(filename: str, content: str) -> tuple[bool, str]:
    """验证生成的 verifier 代码

    Returns:
        (is_valid, error_message)
    """
    import ast

    # 1. 语法检查
    try:
        ast.parse(content)
    except SyntaxError as e:
        return False, f"语法错误: {e.msg} at line {e.lineno}"

    # 2. 基本结构检查
    if filename == "hard_gates.py":
        required_patterns = [
            "def run_all(",
            "GATE_FUNCTIONS",
        ]
        for pattern in required_patterns:
            if pattern not in content:
                return False, f"缺少必需的结构: {pattern}"

        # hard_gates.py 应该比较长（至少 1000 字符）
        if len(content.strip()) < 1000:
            return False, f"代码太短（{len(content)} 字符），hard_gates.py 通常需要数千字符"

    elif filename == "verifier.py":
        required_patterns = [
            "def main(",
            "import hard_gates",
        ]
        for pattern in required_patterns:
            if pattern not in content:
                return False, f"缺少必需的结构: {pattern}"

        # verifier.py 应该至少 500 字符
        if len(content.strip()) < 500:
            return False, f"代码太短（{len(content)} 字符），verifier.py 通常需要数百字符"

    elif filename == "objective_metrics.py":
        required_patterns = [
            "def run_all(",
        ]
        for pattern in required_patterns:
            if pattern not in content:
                return False, f"缺少必需的结构: {pattern}"

        # objective_metrics.py 应该至少 300 字符
        if len(content.strip()) < 300:
            return False, f"代码太短（{len(content)} 字符），objective_metrics.py 通常需要数百字符"

    return True, ""


def save_optimized_verifier(task_dir: Path, files: Dict[str, str], iteration: int) -> bool:
    """保存优化后的 verifier 脚本（带验证和回滚）

    Returns:
        True if saved successfully, False if validation failed and rolled back
    """
    print(f"\n保存优化后的 verifier 脚本（迭代 {iteration}）...")

    # 1. 先验证所有文件
    print("验证生成的代码...")
    validation_errors = {}
    for filename, content in files.items():
        is_valid, error_msg = validate_verifier_code(filename, content)
        if not is_valid:
            validation_errors[filename] = error_msg
            print(f"  ❌ {filename}: {error_msg}")

    # 如果有验证错误，不保存，回滚
    if validation_errors:
        print(f"\n⚠️  代码验证失败，共 {len(validation_errors)} 个文件有问题")
        print("保持使用上一个版本的 verifier")
        return False

    print("✓ 所有文件验证通过")

    # 2. 备份并保存
    import shutil
    for filename, content in files.items():
        file_path = task_dir / filename

        # 备份原文件
        if file_path.exists():
            backup_path = task_dir / f"{filename}.iter{iteration-1}.bak"
            shutil.copy2(file_path, backup_path)
            print(f"  备份: {backup_path.name}")

        # 写入优化后的内容
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"  ✓ {filename}")

    return True


def optimization_loop(
    task_dir: Path,
    metadata: Dict[str, Any],
    patch_path: Path,
    output_dir: Path,
    max_iterations: int = 3,
    target_score: float = 6.0
) -> Dict[str, Any]:
    """优化循环：运行 verifier -> 分析 -> 优化 -> 重新运行

    Returns:
        最终的校准报告
    """
    print(f"\n{'='*80}")
    print(f"开始优化循环（最多 {max_iterations} 轮，目标得分: {target_score}）")
    print(f"{'='*80}")

    best_report = None
    best_score = 0.0
    best_iteration = 0
    previous_metrics = None

    for iteration in range(1, max_iterations + 1):
        print(f"\n{'='*80}")
        print(f"迭代 {iteration}/{max_iterations}")
        print(f"{'='*80}")

        # 创建本次迭代的输出目录
        iter_output_dir = output_dir / f"iteration_{iteration}"
        iter_output_dir.mkdir(exist_ok=True)

        # 运行 verifier 3 次
        print(f"\n运行 verifier 3 次（迭代 {iteration}）...")
        runs = []
        for i in range(1, 4):
            result = run_verifier(task_dir, patch_path, i, iter_output_dir)
            runs.append(result)

        # 生成校准报告
        report = generate_calibration_report(task_dir, metadata, patch_path, runs, iter_output_dir)

        # 计算当前指标
        current_metrics = calculate_progress_metrics(runs)
        avg_score = current_metrics['reward']

        # 进步分析
        print(f"\n迭代 {iteration} 结果:")
        print(f"  平均得分: {avg_score:.2f}")
        print(f"  失败硬门控: {current_metrics['failed_count']} 个")
        print(f"  通过硬门控: {current_metrics['passed_count']} 个")

        if previous_metrics:
            progress = compare_progress(current_metrics, previous_metrics)
            print(f"\n  相比上一轮:")
            print(f"    得分变化: {previous_metrics['reward']:.2f} → {avg_score:.2f} ({'+' if progress['reward_delta'] >= 0 else ''}{progress['reward_delta']:.2f})")
            print(f"    失败变化: {previous_metrics['failed_count']} → {current_metrics['failed_count']} ({'+' if progress['failed_delta'] >= 0 else ''}{progress['failed_delta']})")

            if progress['newly_passed']:
                print(f"    ✅ 新通过: {', '.join(progress['newly_passed'])}")
            if progress['newly_failed']:
                print(f"    ❌ 新失败: {', '.join(progress['newly_failed'])}")
            if progress['still_failed']:
                print(f"    ↔️  仍失败: {', '.join(progress['still_failed'])}")

            if progress['has_improvement']:
                print(f"\n  🎉 有进步！")
            elif progress['has_regression']:
                print(f"\n  ⚠️  有退化（新增失败）")
            else:
                print(f"\n  ➡️  无明显变化")

        # 更新最佳结果
        if avg_score > best_score:
            best_score = avg_score
            best_report = report
            best_iteration = iteration
            print(f"\n  ⭐ 新的最佳得分！")

        # 检查是否达到目标
        if avg_score >= target_score:
            print(f"\n🎉 达到目标得分 {target_score}！停止优化。")
            break

        # 如果还没达到目标且不是最后一轮，进行优化
        if iteration < max_iterations:
            print(f"\n得分 {avg_score:.2f} < {target_score}，需要优化...")

            # 调用 LLM 分析和优化
            try:
                optimized_files, analysis = analyze_and_optimize_verifier(
                    task_dir, metadata, patch_path, runs, iteration, previous_metrics
                )

                # 保存分析报告
                analysis_path = iter_output_dir / "optimization_analysis.md"
                with open(analysis_path, 'w', encoding='utf-8') as f:
                    f.write(f"# 优化迭代 {iteration} 分析\n\n")
                    f.write(analysis)
                print(f"✓ 分析报告: {analysis_path}")

                # 保存优化后的 verifier（带验证）
                save_success = save_optimized_verifier(task_dir, optimized_files, iteration + 1)

                if not save_success:
                    print(f"⚠️  代码验证失败，继续使用迭代 {iteration} 的版本")
                    # 不再继续优化，直接使用当前最佳结果
                    print("提前结束优化循环")
                    break

            except Exception as e:
                print(f"⚠️  优化过程出错: {e}")
                import traceback
                traceback.print_exc()
                print(f"继续使用迭代 {iteration} 的 verifier")
                break

            # 更新 previous_metrics 用于下一轮比较
            previous_metrics = current_metrics
        else:
            print(f"\n已达到最大迭代次数 {max_iterations}，停止优化。")

    print(f"\n{'='*80}")
    print(f"优化循环完成")
    print(f"最佳得分: {best_score:.2f}（迭代 {best_iteration}）")
    print(f"{'='*80}")

    return best_report if best_report else report


def main():
    parser = argparse.ArgumentParser(description='用真实 PR 验证评测体系（校准）')
    parser.add_argument('--case', type=str, required=True,
                        help='任务目录路径，如: manual_tasks/fastapi__fastapi__c48e67b__16013')
    parser.add_argument('--no-optimize', action='store_true',
                        help='禁用优化循环，只运行一次评测')
    parser.add_argument('--max-iterations', type=int, default=3,
                        help='最大优化迭代次数（默认: 3）')
    parser.add_argument('--target-score', type=float, default=6.0,
                        help='目标得分（默认: 6.0）')

    args = parser.parse_args()

    try:
        # 解析任务目录
        task_dir = Path(args.case)
        if not task_dir.exists():
            print(f"❌ 任务目录不存在: {task_dir}")
            sys.exit(1)

        print(f"任务目录: {task_dir}\n")

        # 加载 metadata
        metadata = load_metadata(task_dir)

        # 导出 PR patch
        patch_path = export_pr_patch(task_dir, metadata)

        # 创建校准输出目录（统一在 calibration/ 下）
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        calibration_base = task_dir / "calibration"
        calibration_base.mkdir(exist_ok=True)

        output_dir = calibration_base / timestamp
        output_dir.mkdir(exist_ok=True)

        print(f"\n校准输出目录: {output_dir}")
        print(f"  (相对路径: calibration/{timestamp})")

        if args.no_optimize:
            # 只运行一次评测，不优化
            print("\n=== 步骤 2: 运行 verifier 3 次 ===")
            runs = []
            for i in range(1, 4):
                result = run_verifier(task_dir, patch_path, i, output_dir)
                runs.append(result)

            # 生成校准报告
            report = generate_calibration_report(task_dir, metadata, patch_path, runs, output_dir)
        else:
            # 运行优化循环
            report = optimization_loop(
                task_dir, metadata, patch_path, output_dir,
                max_iterations=args.max_iterations,
                target_score=args.target_score
            )

        print(f"\n{'='*60}")
        print("✅ 校准完成")
        print(f"平均 reward: {report['summary']['average_reward']:.2f}")
        print(f"reward 方差: {report['consistency']['reward_variance']:.4f}")
        print(f"输出目录: {output_dir.absolute()}")
        print(f"  (相对路径: calibration/{timestamp})")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

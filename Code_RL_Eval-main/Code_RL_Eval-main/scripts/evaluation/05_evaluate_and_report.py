#!/usr/bin/env python3
"""
步骤 5: 评估与报告生成

设计思路：
    对 05 执行完的重构结果进行完整评估，并生成对比报告。

功能：
    1. 读取所有重构结果
    2. 对每个工作空间运行：
       - 测试验证（make unittest, make lint）
       - verifier.py（基础检查）
       - hard_gates.py（硬性要求）
       - rubric.py（评分标准）
    3. 汇总所有结果
    4. 生成 Markdown 对比报告

用法：
    python scripts/evaluation/05_evaluate_and_report.py --case manual_tasks/xxx
"""

import sys
import json
import os
import argparse
import subprocess
from pathlib import Path
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

# 尝试从 .env 文件加载环境变量
def _load_env_file():
    """加载 .env 文件中的环境变量(如果存在)"""
    env_file = Path(__file__).parent.parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    # 去掉值两端成对的引号（.env 里常写成 KEY="value"，
                    # 若不剥离，传进容器的会是含字面引号的值，导致 API 认证失败）。
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                        value = value[1:-1]
                    # 只设置尚未设置的环境变量
                    if key and not os.getenv(key):
                        os.environ[key] = value

_load_env_file()
from datetime import datetime

# 导入配置加载器
from config_loader import load_config


def _extract_last_json(text: str) -> Dict:
    """从可能混入进度信息的 stdout 中提取最后一个完整 JSON 对象。

    verifier 正常只输出纯 JSON；但若有进度信息意外混入 stdout，
    直接 json.loads 会失败。此处优先整体解析，失败则从末尾反向
    扫描出最后一个括号平衡的 {...} 再解析，保证健壮。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:end + 1])
                    except json.JSONDecodeError:
                        break
        end = text.rfind("}", 0, end)
    # 兜底：仍无法解析则抛出原始错误，交由上层记录
    return json.loads(text)


class EvaluationReporter:
    """评估与报告生成器"""

    def __init__(self, case_path: Path, config):
        self.case_path = case_path
        self.config = config

        self.metadata = self._load_metadata()
        self.container_info = self._load_container_info()
        self.workspace_info = self._load_workspace_info()

        self.container_id = self.container_info["container_id"]
        self.container_name = self.container_info["container_name"]

        # 运行批次时间戳（区分多次评估，避免互相覆盖）
        self.run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 结果目录（评估结果按批次时间戳隔离）
        self.refactor_results_dir = self.case_path / "results" / "refactor"
        self.evaluation_results_dir = self.case_path / "results" / "evaluation" / self.run_ts
        self.evaluation_results_dir.mkdir(parents=True, exist_ok=True)

    def _load_metadata(self) -> Dict:
        metadata_file = self.case_path / "metadata.json"
        if not metadata_file.exists():
            raise FileNotFoundError(f"未找到 metadata.json: {metadata_file}")
        with open(metadata_file) as f:
            return json.load(f)

    def _load_container_info(self) -> Dict:
        info_file = self.case_path / ".container_info.json"
        if not info_file.exists():
            raise FileNotFoundError(
                f"未找到容器信息: {info_file}\n"
                f"请先运行步骤 02"
            )
        with open(info_file) as f:
            return json.load(f)

    def _load_workspace_info(self) -> Dict:
        info_file = self.case_path / ".workspace_info.json"
        if not info_file.exists():
            raise FileNotFoundError(
                f"未找到工作空间信息: {info_file}\n"
                f"请先运行步骤 04"
            )
        with open(info_file) as f:
            return json.load(f)

    def run(self) -> int:
        print("=" * 80)
        print(f"步骤 5: 评估与报告生成 - {self.case_path.name}")
        print("=" * 80)

        try:
            # 0. 同步评测脚本到容器（确保用的是 case 目录里的最新版，而非镜像里 COPY 的旧版）
            print("\n[0] 同步评测脚本到容器 ...")
            self._sync_eval_scripts()

            workspaces = self.workspace_info["workspaces"]
            print(f"\n将评估 {len(workspaces)} 个工作空间")

            # 1. 对每个工作空间运行评估
            print("\n[1/3] 运行评估 ...")
            evaluations = []
            for ws in workspaces:
                print(f"\n  评估: {ws['name']}")
                evaluation = self._evaluate_workspace(ws)
                evaluations.append(evaluation)

            # 2. 汇总结果
            print("\n[2/3] 汇总结果 ...")
            summary = self._summarize_results(evaluations)

            # 3. 生成报告
            print("\n[3/3] 生成报告 ...")
            report_file = self._generate_report(evaluations, summary)
            print(f"  ✅ 报告已生成: {report_file}")

            return 0

        except Exception as e:
            print(f"\n❌ 评估失败: {e}")
            return 1

    # 需同步到容器 /workspace 的评测脚本（与 Dockerfile COPY 的一致）。
    # 这样每次评估都用 case 目录里的最新门控/verifier，改完无需重建镜像。
    EVAL_SCRIPTS = ["verifier.py", "hard_gates.py", "objective_metrics.py", "objective_scoring.py", "rubric.py", "rubric.md"]

    def _sync_eval_scripts(self):
        """把 case 目录里的评测脚本 docker cp 进容器 /workspace，覆盖镜像内旧版。"""
        for name in self.EVAL_SCRIPTS:
            src = self.case_path / name
            if not src.exists():
                print(f"      ⚠️  跳过（case 内不存在）: {name}")
                continue
            result = subprocess.run(
                ["docker", "cp", str(src), f"{self.container_name}:/workspace/{name}"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print(f"      ✅ {name}")
            else:
                raise RuntimeError(
                    f"同步评测脚本失败: {name}\n{result.stderr.strip()}"
                )

    def _load_refactor_result(self, ws_name: str, ws_path: str) -> Dict | None:
        """加载重构结果，优先从容器内读取，fallback 到宿主机。

        Args:
            ws_name: 工作空间名称
            ws_path: 容器内工作空间路径

        Returns:
            重构结果字典，如果两处都不存在则返回 None
        """
        # 方案1: 从容器内读取 refactor_result.json
        container_result_path = f"{ws_path}/refactor_result.json"
        result = subprocess.run(
            ["docker", "exec", self.container_name, "cat", container_result_path],
            capture_output=True,
            text=True
        )

        if result.returncode == 0 and result.stdout:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                print(f"      ⚠️  容器内 {container_result_path} 格式错误，尝试从宿主机读取")

        # 方案2: fallback 到宿主机的 results/refactor/
        host_result_file = self.refactor_results_dir / f"{ws_name}.json"
        if host_result_file.exists():
            with open(host_result_file) as f:
                return json.load(f)

        # 两处都不存在
        return None

    def _evaluate_workspace(self, workspace: Dict) -> Dict:
        """评估单个工作空间"""
        ws_name = workspace["name"]
        ws_path = workspace["path"]

        # 优先从容器内读取重构结果，fallback 到宿主机
        refactor_result = self._load_refactor_result(ws_name, ws_path)

        if refactor_result is None:
            return {
                "workspace": ws_name,
                "model": workspace["model"],
                "iteration": workspace["iteration"],
                "status": "skipped",
                "error": "未找到重构结果（容器内和宿主机都不存在）"
            }

        evaluation = {
            "workspace": ws_name,
            "model": workspace["model"],
            "iteration": workspace["iteration"],
            "refactor_status": refactor_result.get("status"),
            "timestamp": datetime.now().isoformat()
        }

        # 如果重构失败，跳过评估
        if refactor_result.get("status") != "success":
            evaluation["status"] = "skipped"
            evaluation["error"] = "重构失败，跳过评估"
            return evaluation

        try:
            # 1. 运行测试
            print(f"    [1/2] 运行测试 ...")
            test_result = self._run_tests(ws_path)
            evaluation["tests"] = test_result
            print(f"          测试: {test_result['passed']}/{test_result['total']} 通过")

            # 2. 运行 verifier（会自动调用 hard_gates、objective_metrics、rubric）
            print(f"    [2/2] 运行 verifier ...")
            verifier_result = self._run_verifier(ws_path)
            evaluation["verifier"] = verifier_result

            # 提取各部分结果
            reward = verifier_result.get("reward", 0)
            status = verifier_result.get("status", "unknown")

            print(f"          状态: {status}")
            print(f"          得分: {reward}")

            # 如果 verifier 返回了详细结果，标记各部分状态
            if "hard_gates" in verifier_result:
                hard_gates_passed = len(verifier_result["hard_gates"].get("failed_gates", [])) == 0
                print(f"          hard_gates: {'✅' if hard_gates_passed else '❌'}")

            if "rubric" in verifier_result and verifier_result["rubric"] is not None:
                print(f"          rubric: ✅")
            elif "rubric" in verifier_result and verifier_result["rubric"] is None:
                print(f"          rubric: - (跳过,hard gates 失败)")

            evaluation["status"] = "completed"

        except Exception as e:
            evaluation["status"] = "failed"
            evaluation["error"] = str(e)
            print(f"          ❌ 评估失败: {e}")

        # 保存评估结果
        output_file = self.evaluation_results_dir / f"{ws_name}.json"
        with open(output_file, "w") as f:
            json.dump(evaluation, f, indent=2, ensure_ascii=False)

        return evaluation

    def _run_tests(self, ws_path: str) -> Dict:
        """在工作空间里运行测试"""
        env = self.metadata.get("environment", {})
        test_commands = env.get("test_commands", [])

        results = []
        for cmd in test_commands:
            result = subprocess.run(
                ["docker", "exec", "-w", f"{ws_path}/repo", self.container_name, "sh", "-c", cmd],
                capture_output=True,
                text=True
            )
            results.append({
                "command": cmd,
                "exit_code": result.returncode,
                "success": result.returncode == 0,
                "output": result.stdout[-1000:] if result.stdout else ""
            })

        passed = sum(1 for r in results if r["success"])
        return {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": passed / len(results) if results else 0,
            "commands": results
        }

    def _run_verifier(self, ws_path: str) -> Dict:
        """运行 verifier.py，固定跑满 N 次（并行执行）。

        每次原始输出都落盘（带 run 序号，互不覆盖）。返回值取第一个成功解析的
        结果供下游打分；若 N 次全部失败，返回最后一次错误。
        """
        total_runs = self.config.verifier_runs

        def run_single_verifier(run_idx: int) -> Dict:
            """运行单次 verifier"""
            try:
                # 构建环境变量参数(传递 LLM 相关环境变量到容器)
                env_vars = []
                for key in ["OPENAI_API_KEY", "OPENAI_BASE_URL", "TEXT_MODEL", "LLM_TIMEOUT"]:
                    value = os.getenv(key)
                    if value:
                        env_vars.extend(["-e", f"{key}={value}"])

                # 运行 verifier.py
                # 为每次并行运行分配独立的 mypy 缓存根目录,避免共享
                # /tmp/.mypy_cache 造成的竞态(FileNotFoundError 误判用例失败)
                cmd = ["docker", "exec"] + env_vars + [
                    "-e", f"MYPY_CACHE_DIR=/tmp/.mypy_cache_run{run_idx}",
                    "-w", f"{ws_path}/repo", self.container_name,
                    "python", "/workspace/verifier.py", "."]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True
                )

                # verifier.py 会输出 JSON 格式的结果
                if result.stdout:
                    parsed = _extract_last_json(result.stdout)
                    # 每次原始输出都落盘（带 run 序号），供步骤 06 导出
                    self._save_verifier_output(ws_path, result.stdout, run_idx)
                    return {
                        "success": True,
                        "run_idx": run_idx,
                        "result": parsed
                    }
                else:
                    return {
                        "success": False,
                        "run_idx": run_idx,
                        "error": {
                            "exit_code": result.returncode,
                            "error": "verifier.py 无输出",
                            "stderr": result.stderr[-1000:] if result.stderr else ""
                        }
                    }
            except json.JSONDecodeError as e:
                return {
                    "success": False,
                    "run_idx": run_idx,
                    "error": {
                        "exit_code": result.returncode,
                        "error": f"verifier.py 输出格式错误: {e}",
                        "stdout": result.stdout[-1000:] if result.stdout else "",
                        "stderr": result.stderr[-1000:] if result.stderr else ""
                    }
                }
            except Exception as e:
                return {
                    "success": False,
                    "run_idx": run_idx,
                    "error": {
                        "error": f"运行 verifier 失败: {e}"
                    }
                }

        # 并行运行 N 次
        if total_runs > 1:
            print(f"          并行运行 verifier {total_runs} 次 ...")

        first_success = None
        last_error = None

        with ThreadPoolExecutor(max_workers=total_runs) as executor:
            # 提交所有任务
            futures = {executor.submit(run_single_verifier, i): i for i in range(1, total_runs + 1)}

            # 收集结果
            for future in as_completed(futures):
                run_idx = futures[future]
                try:
                    run_result = future.result()
                    if run_result["success"]:
                        if first_success is None:
                            first_success = run_result["result"]
                        if total_runs > 1:
                            print(f"          verifier 第 {run_result['run_idx']}/{total_runs} 次完成 ✓")
                    else:
                        last_error = run_result["error"]
                        if total_runs > 1:
                            print(f"          verifier 第 {run_result['run_idx']}/{total_runs} 次失败 ✗")
                except Exception as e:
                    last_error = {"error": f"Future 异常: {e}"}
                    if total_runs > 1:
                        print(f"          verifier 第 {run_idx}/{total_runs} 次异常 ✗")

        # 跑满 N 次后：优先返回首个成功结果，否则返回最后一次错误
        return first_success if first_success is not None else last_error

    def _save_verifier_output(self, ws_path: str, raw_output: str, run_idx: int = 1):
        """将 verifier 原始输出写入容器工作空间（带运行批次时间戳 + run 序号，多次运行不覆盖）"""
        container_path = f"{ws_path}/verifier_output_{self.run_ts}_run{run_idx}.json"
        subprocess.run(
            ["docker", "exec", "-i", self.container_name, "sh", "-c",
             f"cat > {container_path}"],
            input=raw_output.encode(),
            check=False
        )

    def _run_script(self, ws_path: str, script_name: str) -> Dict:
        """运行验证脚本"""
        script_file = f"/workspace/{script_name}"
        result = subprocess.run(
            ["docker", "exec", "-w", ws_path, self.container_name,
             "python", script_file],
            capture_output=True,
            text=True
        )
        return {
            "exit_code": result.returncode,
            "output": result.stdout[-2000:] if result.stdout else "",
            "error": result.stderr[-1000:] if result.stderr else ""
        }

    def _summarize_results(self, evaluations: List[Dict]) -> Dict:
        """汇总所有评估结果"""
        summary = {
            "total": len(evaluations),
            "by_model": {},
            "by_status": {
                "completed": 0,
                "skipped": 0,
                "failed": 0
            }
        }

        for eval in evaluations:
            # 按模型统计
            model = eval["model"]
            if model not in summary["by_model"]:
                summary["by_model"][model] = {
                    "total": 0,
                    "completed": 0,
                    "avg_test_pass_rate": 0
                }

            summary["by_model"][model]["total"] += 1

            # 按状态统计
            status = eval.get("status", "unknown")
            if status in summary["by_status"]:
                summary["by_status"][status] += 1

            if status == "completed":
                summary["by_model"][model]["completed"] += 1
                test_pass_rate = eval.get("tests", {}).get("pass_rate", 0)
                summary["by_model"][model]["avg_test_pass_rate"] += test_pass_rate

        # 计算平均值
        for model_data in summary["by_model"].values():
            if model_data["completed"] > 0:
                model_data["avg_test_pass_rate"] /= model_data["completed"]

        return summary

    def _generate_report(self, evaluations: List[Dict], summary: Dict) -> Path:
        """生成 Markdown 报告(完整版:含任务背景、逐迭代分析、Rubric剖析、结论建议)"""
        report_file = self.case_path / "results" / f"evaluation_report_{self.run_ts}.md"

        # === 准备数据 ===
        metadata = self.metadata
        task_id = metadata.get("task_id", "N/A")
        repo_info = metadata.get("repo_info", {})
        repo_name = repo_info.get("full_name", "N/A")
        pr_url = metadata.get("pr_url", "")
        pr_num = pr_url.split("/")[-1] if pr_url else "N/A"

        total_workspaces = summary["total"]
        completed = summary["by_status"].get("completed", 0)
        completed_evals = [e for e in evaluations if e.get("status") == "completed"]
        avg_pass_rate = sum(e["tests"]["pass_rate"] for e in completed_evals) / len(completed_evals) if completed_evals else 0
        avg_reward = sum(e["verifier"].get("reward", 0) for e in completed_evals) / len(completed_evals) if completed_evals else 0

        # === 开始生成报告(分段拼接避免超长字符串) ===
        lines = []

        # 标题与元信息
        lines.extend([
            "# 评估报告",
            "",
            f"**Case**: {self.case_path.name}",
            f"**任务 ID**: {task_id}",
            f"**运行批次**: {self.run_ts}",
            f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"**模型**: {list(summary['by_model'].keys())[0] if summary['by_model'] else 'N/A'}（{total_workspaces} 次独立迭代）",
            "",
            "---",
            ""
        ])

        # 一、任务背景
        lines.extend([
            "## 一、任务背景",
            "",
            f"- **仓库**: [{repo_name}](https://github.com/{repo_name})",
            f"- **来源 PR**: [#{pr_num}]({pr_url if pr_url else 'N/A'})",
            f"- **任务描述**: {metadata.get('description', '见 prompt.md')}",
            f"- **等价类**: {metadata.get('equivalence_class', 'N/A')}",
            "",
            "---",
            ""
        ])

        # 二、总体概况
        lines.extend([
            "## 二、总体概况",
            "",
            f"- **总工作空间数**: {total_workspaces}",
            f"- **评估完成**: {completed}",
            f"- **跳过**: {summary['by_status'].get('skipped', 0)}",
            f"- **失败**: {summary['by_status'].get('failed', 0)}",
            f"- **平均测试通过率**: {avg_pass_rate:.1%}",
            f"- **平均 Reward**: {avg_reward:.4f}",
            "",
            "### 模型对比",
            ""
        ])

        for model, stats in summary["by_model"].items():
            lines.extend([
                f"#### {model}",
                "",
                f"- **迭代次数**: {stats['total']}",
                f"- **成功次数**: {stats['completed']}",
                f"- **平均测试通过率**: {stats['avg_test_pass_rate']:.1%}",
                ""
            ])

        lines.extend(["---", ""])

        # 三、详细结果表格
        lines.extend([
            "## 三、详细结果",
            "",
            "| 工作空间 | 模型 | 迭代 | 重构 | 测试通过率 | Reward | Verifier | Hard Gates | Rubric |",
            "|---------|------|------|------|-----------|--------|----------|-----------|--------|"
        ])

        for eval_item in evaluations:
            ws_name = eval_item["workspace"]
            model = eval_item["model"]
            iteration = eval_item["iteration"]
            refactor_status = eval_item.get("refactor_status", "unknown")

            if eval_item.get("status") == "completed":
                test_pass_rate = eval_item["tests"]["pass_rate"]
                verifier_result = eval_item["verifier"]
                v_status = verifier_result.get("status")
                # pass 与 capped 都算 verifier 有效产出（capped 说明 rubric 跑了、只是被封顶）
                verifier_ok = v_status in ("pass", "capped")
                verifier = {"pass": "✅", "capped": "🔶"}.get(v_status, "❌")
                hg = verifier_result.get("hard_gates", {})
                hard_gates = "✅" if len(hg.get("failed_gates", [])) == 0 else "❌"
                rubric = "✅" if verifier_result.get("rubric") else "❌"
                reward = verifier_result.get("reward", 0)

                lines.append(f"| {ws_name} | {model} | {iteration} | {refactor_status} | {test_pass_rate:.1%} | {reward:.2f} | {verifier} | {hard_gates} | {rubric} |")
            else:
                lines.append(f"| {ws_name} | {model} | {iteration} | {refactor_status} | - | - | - | - |")

        lines.extend(["", "---", ""])

        # 四、逐迭代分析
        lines.extend(["## 四、逐迭代分析", ""])

        for eval_item in completed_evals:
            ws_name = eval_item["workspace"]
            iteration = eval_item["iteration"]
            tests = eval_item["tests"]
            verifier = eval_item["verifier"]

            lines.extend([
                f"### 迭代 {iteration} ({ws_name})",
                "",
                "**测试结果**:",
                f"- 总用例数: {tests.get('total', 0)}",
                f"- 通过: {tests.get('passed', 0)}",
                f"- 失败: {tests.get('failed', 0)}",
                f"- 通过率: {tests['pass_rate']:.1%}",
                "",
                "**Verifier 评估**:",
                f"- 状态: {verifier.get('status', 'unknown')}",
                f"- Reward: {verifier.get('reward', 0):.4f}",
                f"- Hard Gates: {'✅ 全部通过' if not verifier.get('hard_gates', {}).get('failed_gates') else '❌ 失败: ' + str(verifier.get('hard_gates', {}).get('failed_gates'))}",
            ])

            # 分层封顶信息（如果 verifier 产出了 score_cap 相关字段）
            hg = verifier.get("hard_gates", {}) or {}
            score_cap = verifier.get("score_cap", hg.get("score_cap"))
            verdict = hg.get("verdict")
            if score_cap is not None and verdict and verdict != "clean":
                verdict_label = {
                    "invalid": "作弊 (invalid)",
                    "blocker": "阻断级 (blocker)",
                    "major": "重大缺陷 (major)",
                    "minor": "轻微瑕疵 (minor)",
                }.get(verdict, verdict)
                lines.append(f"- 封顶级别: {verdict_label} → score_cap = {score_cap}")
                if verifier.get("capped"):
                    lines.append(
                        f"- 分数裁剪: 原始分 {verifier.get('rubric_raw', 0):.4f} → 封顶后 {verifier.get('reward', 0):.4f}"
                    )
                diag = hg.get("diagnostics", {}) or {}
                diag_parts = []
                for level in ("blockers", "majors", "minors"):
                    if diag.get(level):
                        diag_parts.append(f"{level}={diag[level]}")
                if diag_parts:
                    lines.append(f"- 门槛诊断: {'; '.join(diag_parts)}")

            lines.extend([
                "",
                "**改动文件**:"
            ])

            obj_metrics = verifier.get("objective_metrics", {})
            files_changed = obj_metrics.get("files_changed", []) if obj_metrics else []
            if files_changed:
                for f in files_changed[:10]:
                    lines.append(f"- `{f}`")
                if len(files_changed) > 10:
                    lines.append(f"- ... 共 {len(files_changed)} 个文件")
            else:
                lines.append("- (无改动或数据缺失)")

            lines.extend(["", "---", ""])

        # 五、Rubric 维度剖析
        lines.extend(["## 五、Rubric 维度剖析", ""])

        first_completed = completed_evals[0] if completed_evals else None
        if first_completed and first_completed["verifier"].get("rubric"):
            rubric = first_completed["verifier"]["rubric"]
            dimensions = rubric.get("dimensions", [])
            lines.append(f"以下基于第一个 workspace 的 rubric 结构展开分析（共 {len(dimensions)} 个维度）。")
            lines.append("")

            for dim in dimensions[:6]:
                dim_id = dim.get("dimension_id", "N/A")
                name = dim.get("name", "N/A")
                weight = dim.get("weight", 0)
                score = dim.get("score", 0)
                weighted_score = dim.get("weighted_score", 0)

                lines.extend([
                    f"### {dim_id}: {name}",
                    f"- **权重**: {weight:.2f}",
                    f"- **得分**: {score:.2f}/1.0",
                    f"- **加权分**: {weighted_score:.4f}",
                    ""
                ])

                questions = dim.get("questions", [])
                if questions:
                    lines.append("**评分细节**:")
                    for q in questions[:3]:
                        q_text = q.get("question", "N/A")
                        q_score = q.get("score", 0)
                        lines.append(f"- {q_text[:80]}{'...' if len(q_text) > 80 else ''}: {q_score:.2f}")
                    if len(questions) > 3:
                        lines.append(f"- ... 共 {len(questions)} 个评分点")
                    lines.append("")

            if len(dimensions) > 6:
                lines.append(f"... 共 {len(dimensions)} 个维度，此处仅展示前 6 个。")
                lines.append("")
        else:
            lines.append("**Rubric 数据缺失或未计算。**")
            lines.append("")

        lines.extend(["---", ""])

        # 五.五、分数溯源分析（新增）
        if len(completed_evals) >= 2:
            lines.extend(["## 五.五、分数溯源分析", ""])
            lines.append("对比不同迭代的得分差异，帮助理解评分的组成和变化原因。")
            lines.append("")

            # 按 reward 排序
            sorted_evals = sorted(completed_evals, key=lambda e: e["verifier"].get("reward", 0), reverse=True)

            # 取最高分和最低分进行对比
            if len(sorted_evals) >= 2:
                best = sorted_evals[0]
                worst = sorted_evals[-1]

                best_name = best["workspace"]
                worst_name = worst["workspace"]
                best_reward = best["verifier"].get("reward", 0)
                worst_reward = worst["verifier"].get("reward", 0)
                reward_diff = best_reward - worst_reward

                lines.extend([
                    f"### 对比：最高分 vs 最低分",
                    "",
                    f"- **最高分**: `{best_name}` - Reward: {best_reward:.4f}",
                    f"- **最低分**: `{worst_name}` - Reward: {worst_reward:.4f}",
                    f"- **差距**: {reward_diff:.4f}",
                    ""
                ])

                # 维度分解对比
                # rubric/objective_metrics 在 hard gates 失败时可能是显式 None，
                # 故 .get(..., {}) 无法兜底（键存在时返回 None），需再 or {}。
                best_rubric = best["verifier"].get("rubric") or {}
                worst_rubric = worst["verifier"].get("rubric") or {}
                best_dims = {d.get("dimension_id"): d for d in best_rubric.get("dimensions", [])}
                worst_dims = {d.get("dimension_id"): d for d in worst_rubric.get("dimensions", [])}

                if best_dims and worst_dims:
                    lines.extend([
                        "### 维度分解",
                        "",
                        "| 维度 | 最高分 | 最低分 | 差距 | 主要原因 |",
                        "|------|--------|--------|------|----------|"
                    ])

                    total_diff_explained = 0.0
                    dimension_diffs = []

                    for dim_id in sorted(best_dims.keys()):
                        if dim_id in worst_dims:
                            best_dim = best_dims[dim_id]
                            worst_dim = worst_dims[dim_id]

                            best_score = best_dim.get("score", 0)
                            worst_score = worst_dim.get("score", 0)
                            weight = best_dim.get("weight", 1.0)
                            dim_name = best_dim.get("name", dim_id)

                            score_diff = best_score - worst_score
                            weighted_diff = score_diff * weight

                            dimension_diffs.append({
                                "id": dim_id,
                                "name": dim_name,
                                "diff": score_diff,
                                "weighted_diff": weighted_diff,
                                "best_score": best_score,
                                "worst_score": worst_score,
                            })

                            total_diff_explained += weighted_diff

                    # 按加权差距排序，找出影响最大的维度
                    dimension_diffs.sort(key=lambda d: abs(d["weighted_diff"]), reverse=True)

                    for dim_diff in dimension_diffs[:6]:  # 显示前 6 个
                        dim_id = dim_diff["id"]
                        dim_name = dim_diff["name"]
                        best_score = dim_diff["best_score"]
                        worst_score = dim_diff["worst_score"]
                        score_diff = dim_diff["diff"]

                        # 判断原因
                        reason = "得分差异"
                        if abs(score_diff) < 0.01:
                            reason = "基本一致"
                        elif best_score == 0 and worst_score > 0:
                            reason = "最高分异常（可能评估失败）"
                        elif worst_score == 0 and best_score > 0:
                            reason = "最低分异常（可能评估失败）"
                        elif score_diff > 0.3:
                            reason = "显著差异"

                        lines.append(
                            f"| {dim_id} ({dim_name[:15]}) | {best_score:.2f} | {worst_score:.2f} | "
                            f"{score_diff:+.2f} | {reason} |"
                        )

                    lines.append("")

                    # 关键发现
                    lines.extend([
                        "### 关键发现",
                        ""
                    ])

                    # 检测异常（如 Codex Agent 崩溃）
                    anomalies_found = False
                    for dim_diff in dimension_diffs:
                        dim_id = dim_diff["id"]
                        best_score = dim_diff["best_score"]
                        worst_score = dim_diff["worst_score"]

                        # 检测崩溃特征：分数为 0 且差距大
                        if worst_score == 0 and best_score > 0.5 and dim_diff["diff"] > 0.5:
                            anomalies_found = True
                            lines.append(
                                f"- ⚠️ **{dim_id} 维度异常**: 最低分为 0.0，但最高分为 {best_score:.2f}，"
                                f"可能是评估系统问题（如 Codex Agent 崩溃）而非代码质量问题。"
                            )

                    if not anomalies_found:
                        lines.append("- ✅ 未发现明显的评估系统异常。")

                    lines.append("")

                    # 排除异常后的预估
                    if anomalies_found:
                        lines.extend([
                            "### 排除异常后的预估分数",
                            ""
                        ])

                        # 简单预估：如果异常维度取平均值
                        adjusted_worst_score = worst_reward
                        adjustment_details = []

                        for dim_diff in dimension_diffs:
                            if dim_diff["worst_score"] == 0 and dim_diff["best_score"] > 0.5:
                                dim_id = dim_diff["id"]
                                # 假设取中间值
                                estimated_score = dim_diff["best_score"] * 0.7
                                weight = best_dims[dim_id].get("weight", 1.0)
                                adjustment = estimated_score * weight
                                adjusted_worst_score += adjustment
                                adjustment_details.append(
                                    f"  - {dim_id}: 0.0 → {estimated_score:.2f} (估算)"
                                )

                        lines.append(f"如果将异常维度调整为合理估算值：")
                        lines.extend(adjustment_details)
                        lines.append("")
                        lines.append(f"- **调整后最低分**: {adjusted_worst_score:.4f} (原: {worst_reward:.4f})")
                        lines.append(f"- **与最高分差距**: {best_reward - adjusted_worst_score:.4f} (原: {reward_diff:.4f})")
                        lines.append("")

                else:
                    lines.append("**无法获取维度详情进行对比。**")
                    lines.append("")

        lines.extend(["---", ""])

        # 六、结论与建议
        lines.extend([
            "## 六、结论与建议",
            "",
            "### 1. 评估完成情况",
            ""
        ])

        if completed == total_workspaces:
            lines.append(f"- 所有 {total_workspaces} 个 workspace 均完成评估。")
        else:
            lines.append(f"- {completed}/{total_workspaces} 个 workspace 完成评估，{total_workspaces - completed} 个未完成。")

        lines.extend([
            "",
            "### 2. 测试通过率",
            "",
            f"- 平均通过率: {avg_pass_rate:.1%}"
        ])

        pass_rates = [e["tests"]["pass_rate"] for e in completed_evals]
        if pass_rates and (max(pass_rates) - min(pass_rates) > 0.3):
            lines.append("- **差异较大**: 不同迭代的测试通过率差异明显，建议查看失败迭代的测试日志。")

        lines.extend([
            "",
            "### 3. Reward 分析",
            "",
            f"- 平均 Reward: {avg_reward:.4f}"
        ])

        if avg_reward < 0.1:
            lines.extend([
                "- **Reward 极低**: 说明 rubric 各维度得分普遍偏低，可能原因:",
                "  - Agent 重构质量不佳（改动范围不足、实现不完整等）",
                "  - Rubric 判定标准过严",
                "  - 建议深入查看 rubric 各维度的评分细节"
            ])

        lines.extend([
            "",
            "### 4. 后续优化方向",
            "",
            "- **提升 Agent 能力**: 加强 prompt 引导，明确任务要求和质量标准",
            "- **Rubric 校准**: 如判定标准过严，调整评分阈值或权重",
            "- **测试稳定性**: 排查测试失败原因，确保评估结果可靠",
            "",
            "---",
            ""
        ])

        # 附注
        lines.extend([
            "## 附注",
            "",
            "- ✅ = 通过　❌ = 失败　🔶 = 分数被封顶(capped)　- = 未运行",
            "- **分层封顶**: hard gate 失败按严重度设定分数上限 —— blocker→0.0(一票否决)、major→0.4、minor→0.7；最终分 = min(rubric 原始分, score_cap)",
            f"- 详细日志: `results/evaluation/{self.run_ts}/`",
            "- 每个 workspace 的 verifier 原始输出可在评估 JSON 文件中查看",
            "- 导出的 verifier_output 文件可由步骤 06 从容器导出至 `results/workspaces/`",
            ""
        ])

        # 写入文件
        with open(report_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return report_file


def main():
    parser = argparse.ArgumentParser(
        description="评估与报告生成"
    )
    parser.add_argument("--case", required=True, help="Case 目录路径")
    parser.add_argument("--config", help="配置文件路径（默认使用 scripts/evaluation/config.yaml）")
    parser.add_argument("--verifier-runs", type=int, help="verifier 固定运行次数（覆盖配置文件）")

    args = parser.parse_args()

    case_path = Path(args.case)
    if not case_path.exists():
        print(f"❌ Case 目录不存在: {case_path}")
        return 1

    # 加载配置
    config = load_config(
        config_file=Path(args.config) if args.config else None,
        verifier_runs=args.verifier_runs
    )

    print(f"配置: verifier 运行次数={config.verifier_runs}")

    reporter = EvaluationReporter(
        case_path=case_path,
        config=config
    )
    return reporter.run()


if __name__ == "__main__":
    sys.exit(main())

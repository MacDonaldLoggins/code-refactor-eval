#!/usr/bin/env python3
"""
评测流水线主脚本

功能：串联执行评测的所有步骤（01-06），全流程基于 Docker 容器。

步骤：
1. 校验 case 完整性
2. 构建镜像并启动容器
3. 使用 Claude Code 在容器里搭建并验证环境
4. 创建工作空间并执行重构任务
5. 评估重构结果并生成报告
6. 导出容器产物到宿主机

用法：
    # 完整流水线（模型/迭代次数由 config.yaml 决定）
    python scripts/evaluation_pipeline.py --case manual_tasks/xxx

    # 覆盖配置里的模型和迭代次数
    python scripts/evaluation_pipeline.py --case manual_tasks/xxx --models claude-opus-4-8 --iterations 3

    # 只执行特定步骤
    python scripts/evaluation_pipeline.py --case manual_tasks/xxx --steps 4,5,6
"""

import sys
import os
import subprocess
import argparse
from pathlib import Path
from typing import List, Optional


class EvaluationPipeline:
    """评测流水线"""

    def __init__(
        self,
        case_path: str,
        models: Optional[List[str]] = None,
        iterations: Optional[int] = None,
        config: Optional[str] = None,
        steps: Optional[List[int]] = None,
        parallel: int = 1,
        verifier_runs: Optional[int] = None,
        force_rebuild: bool = False,
        interactive: bool = False,
        include_agent_logs: bool = False,
    ):
        self.case_path = Path(case_path)
        self.models = models
        self.iterations = iterations
        self.config = config
        self.steps = steps or [1, 2, 3, 4, 5, 6]
        self.parallel = parallel
        self.verifier_runs = verifier_runs
        self.force_rebuild = force_rebuild
        self.interactive = interactive
        self.include_agent_logs = include_agent_logs

        self.script_dir = Path(__file__).parent / "evaluation"
        # 结果按 case 隔离，直接落在任务目录下的 results/
        self.results_dir = self.case_path / "results"

        # 步骤定义
        self.step_definitions = {
            1: {
                "name": "校验 Case 完整性",
                "script": "01_prepare_dataset.py",
                "description": "验证 case 是否具备评测条件（检查必要文件）"
            },
            2: {
                "name": "构建镜像并启动容器",
                "script": "02_build_image.py",
                "description": "使用 case 自带 Dockerfile 构建镜像并启动长期运行容器"
            },
            3: {
                "name": "搭建并验证环境",
                "script": "03_setup_environment.py",
                "description": "在容器里用 Claude Code 执行环境搭建并运行测试验证"
            },
            4: {
                "name": "创建工作空间并执行重构",
                "script": "04_run_refactor.py",
                "description": "为每个模型/迭代创建工作空间并调用 agent 执行重构"
            },
            5: {
                "name": "评估与报告生成",
                "script": "05_evaluate_and_report.py",
                "description": "运行测试 / verifier，汇总结果并生成 Markdown 报告"
            },
            6: {
                "name": "导出容器产物",
                "script": "06_export_artifacts.py",
                "description": "将容器里的 patch、结果、日志导出到宿主机"
            },
        }

    def validate(self) -> bool:
        """验证环境和参数"""
        # 检查脚本目录
        if not self.script_dir.exists():
            print(f"❌ 脚本目录不存在: {self.script_dir}")
            return False

        # 检查步骤编号
        for step in self.steps:
            if step not in self.step_definitions:
                print(f"❌ 无效的步骤编号: {step}")
                return False

        # 检查 case 是否存在
        if not self.case_path.exists():
            print(f"❌ Case 目录不存在: {self.case_path}")
            return False

        return True

    def run_step(self, step: int) -> bool:
        """执行单个步骤"""
        step_info = self.step_definitions[step]
        script_path = self.script_dir / step_info['script']

        print("\n" + "=" * 80)
        print(f"步骤 {step}: {step_info['name']}")
        print("=" * 80)

        # 所有步骤都以 --case 打头
        cmd = [sys.executable, str(script_path), "--case", str(self.case_path)]

        if step == 1:
            # 步骤 1: 校验 case 完整性（无额外参数）
            pass

        elif step == 2:
            # 步骤 2: 构建镜像并启动容器
            if self.force_rebuild:
                cmd.append("--force-rebuild")

        elif step == 3:
            # 步骤 3: 搭建并验证环境
            if self.interactive:
                cmd.append("--interactive")

        elif step == 4:
            # 步骤 4: 创建工作空间并执行重构
            if self.config:
                cmd.extend(["--config", self.config])
            if self.models:
                cmd.extend(["--models", *self.models])
            if self.iterations is not None:
                cmd.extend(["--iterations", str(self.iterations)])
            # 仅在显式指定时透传 --parallel，覆盖 config.yaml；否则交给 04 读配置
            if self.parallel is not None:
                cmd.extend(["--parallel", str(self.parallel)])

        elif step == 5:
            # 步骤 5: 评估与报告生成
            if self.config:
                cmd.extend(["--config", self.config])
            if self.verifier_runs is not None:
                cmd.extend(["--verifier-runs", str(self.verifier_runs)])

        elif step == 6:
            # 步骤 6: 导出容器产物
            if self.include_agent_logs:
                cmd.append("--include-agent-logs")

        else:
            print(f"❌ 未知的步骤: {step}")
            return False

        return self._execute_command(cmd)

    def _execute_command(self, cmd: List[str]) -> bool:
        """执行命令"""
        try:
            subprocess.run(
                cmd,
                cwd=Path.cwd(),
                check=True,
                text=True,
                capture_output=False,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
            return True

        except subprocess.CalledProcessError as e:
            print(f"\n❌ 命令执行失败: {e}")
            return False

        except Exception as e:
            print(f"\n❌ 执行出错: {e}")
            return False

    def run(self):
        """运行评测流水线"""
        # 1. 验证
        if not self.validate():
            return 1

        # 2. 显示执行计划
        print("\n" + "=" * 80)
        print("评测流水线")
        print("=" * 80)

        print(f"\nCase 路径: {self.case_path}")
        if self.models:
            print(f"模型（覆盖配置）: {', '.join(self.models)}")
        else:
            print(f"模型: 由 config.yaml 决定")
        if self.iterations is not None:
            print(f"迭代次数（覆盖配置）: {self.iterations}")

        print(f"\n将执行以下步骤:")
        for step in self.steps:
            step_info = self.step_definitions[step]
            print(f"  {step}. {step_info['name']}")
            print(f"     {step_info['description']}")

        print("\n" + "=" * 80)

        # 3. 执行步骤
        completed_steps = []
        failed_step = None

        for step in self.steps:
            success = self.run_step(step)

            if success:
                completed_steps.append(step)
                print(f"\n✅ 步骤 {step} 完成")
            else:
                failed_step = step
                break

        # 4. 输出总结
        print("\n" + "=" * 80)
        print("流水线执行总结")
        print("=" * 80)

        if completed_steps:
            print(f"\n✅ 已完成步骤: {', '.join(map(str, completed_steps))}")

        if failed_step:
            print(f"\n❌ 失败步骤: {failed_step}")
            print(f"\n提示：可以修复问题后，使用 --steps {failed_step} 重新运行该步骤")
            return 1

        print("\n🎉 所有步骤执行完成！")

        # 5. 显示输出位置
        print("\n生成的文件：")
        print(f"  📁 重构结果: {self.results_dir / 'refactor'}/")
        print(f"  📁 评估结果: {self.results_dir / 'evaluation'}/")
        print(f"  📁 导出产物: {self.results_dir / 'workspaces'}/")
        print(f"  📄 评估报告: {self.results_dir}/evaluation_report_<时间戳>.md")

        print("\n下一步：")
        print(f"  查看评估报告: ls {self.results_dir}/evaluation_report_*.md")

        return 0


def main():
    parser = argparse.ArgumentParser(
        description='评测流水线',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 完整流水线（模型/迭代次数由 config.yaml 决定）
  python scripts/evaluation_pipeline.py --case manual_tasks/xxx

  # 覆盖配置里的模型和迭代次数
  python scripts/evaluation_pipeline.py --case manual_tasks/xxx --models claude-opus-4-8 --iterations 3

  # 只执行特定步骤
  python scripts/evaluation_pipeline.py --case manual_tasks/xxx --steps 4,5,6

  # 强制重建镜像 + 重构并行度 3
  python scripts/evaluation_pipeline.py --case manual_tasks/xxx --force-rebuild --parallel 3

步骤说明：
  1 - 校验 Case 完整性（检查必要文件）
  2 - 构建镜像并启动容器
  3 - 搭建并验证环境（Claude Code）
  4 - 创建工作空间并执行重构
  5 - 评估与报告生成
  6 - 导出容器产物
        """
    )

    parser.add_argument('--case', type=str, required=True, help='Case 路径（必需），如: manual_tasks/xxx')
    parser.add_argument('--models', nargs='+', help='AI 模型列表（覆盖 config.yaml），如: claude-opus-4-8')
    parser.add_argument('--iterations', type=int, help='每个模型的迭代次数（覆盖 config.yaml）')
    parser.add_argument('--config', type=str, help='配置文件路径（默认使用 scripts/evaluation/config.yaml）')
    parser.add_argument('--steps', type=str, help='要执行的步骤，逗号分隔（默认: 1,2,3,4,5,6）')
    parser.add_argument('--parallel', type=int, default=None, help='步骤 4 重构并行度（覆盖 config.yaml agent.parallel；未指定则用配置值）')
    parser.add_argument('--verifier-runs', type=int, help='步骤 5 verifier 固定运行次数（覆盖 config.yaml）')
    parser.add_argument('--force-rebuild', action='store_true', help='步骤 2 强制重新构建镜像（--no-cache）')
    parser.add_argument('--interactive', action='store_true', help='步骤 3 交互模式（手动在容器里操作）')
    parser.add_argument('--include-agent-logs', action='store_true', help='步骤 6 导出时包含 agent_session/ 日志')

    args = parser.parse_args()

    # 解析步骤
    steps = None
    if args.steps:
        try:
            steps = [int(s.strip()) for s in args.steps.split(',')]
        except ValueError:
            print("❌ 无效的步骤格式，请使用逗号分隔的数字，如: 4,5,6")
            return 1

    # 运行流水线
    pipeline = EvaluationPipeline(
        case_path=args.case,
        models=args.models,
        iterations=args.iterations,
        config=args.config,
        steps=steps,
        parallel=args.parallel,
        verifier_runs=args.verifier_runs,
        force_rebuild=args.force_rebuild,
        interactive=args.interactive,
        include_agent_logs=args.include_agent_logs,
    )

    return pipeline.run()


if __name__ == '__main__':
    sys.exit(main())

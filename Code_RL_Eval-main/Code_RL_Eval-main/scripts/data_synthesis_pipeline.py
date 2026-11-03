#!/usr/bin/env python3
"""
数据合成流水线脚本

功能：串联执行数据合成的所有步骤（02-07）

步骤：
1. 生成评测体系（metadata.json, verifier.py, hard_gates.py, rubric.py）
2. 用真实 PR 校准评测体系
3. 生成 prompt.md
4. 审计并补充单元测试
5. 生成 setup.py
6. 生成 Dockerfile

用法：
    python scripts/data_synthesis_pipeline.py --case manual_tasks/xxx [--steps 2,3,4,5,6,7] [--skip-confirmation]
"""

import sys
import os
import subprocess
from pathlib import Path
from typing import List, Optional


class DataSynthesisPipeline:
    """数据合成流水线"""

    def __init__(self, case_path: str = None, number: str = None, steps: Optional[List[int]] = None,
                 source: str = None):
        if number:
            # 如果提供了 number，先执行步骤 1 获取 case_path
            self.number = number
            self.case_path = None  # 稍后从步骤 1 的输出中获取
        elif case_path:
            self.case_path = Path(case_path)
            self.number = None
        else:
            raise ValueError("必须提供 --case 或 --number 参数之一")

        self.steps = steps or [1, 2, 3, 4, 5, 6, 7]
        self.source = source  # 步骤 1 的数据源 JSON 文件路径，None 表示使用脚本默认值
        self.script_dir = Path(__file__).parent / "data_synthesis"

        # 可选步骤：失败时跳过并继续执行后续步骤，而不是中断整条流水线
        # 步骤 3（用真实 PR 校准）对 issue case 无 PR 可用，允许失败跳过
        self.optional_steps = {3}

        # 定义步骤
        self.step_definitions = {
            1: {
                "name": "下载代码仓库",
                "script": "01_clone_repos.py",
                "description": "从 GitHub 下载代码仓库和任务描述",
                "special_args": True  # 该步骤需要特殊参数
            },
            2: {
                "name": "生成评测体系",
                "script": "02_generate_eval_system.py",
                "description": "生成 metadata.json, verifier.py, hard_gates.py, rubric.py"
            },
            3: {
                "name": "校准评测体系",
                "script": "03_calibrate_with_pr.py",
                "description": "用真实 PR 验证并校准评测体系"
            },
            4: {
                "name": "生成 prompt.md",
                "script": "04_generate_prompt.py",
                "description": "生成 refactor 任务提示文档"
            },
            5: {
                "name": "审计并补充测试",
                "script": "05_audit_tests.py",
                "description": "审计测试覆盖并生成测试补丁"
            },
            6: {
                "name": "生成 setup.py",
                "script": "06_generate_setup.py",
                "description": "生成评测工作空间准备脚本 setup.py"
            },
            7: {
                "name": "生成 Dockerfile",
                "script": "07_generate_dockerfile.py",
                "description": "生成固化评测环境的 Dockerfile"
            }
        }

    def validate(self):
        """验证环境和参数"""
        # 如果提供了 number，必须包含步骤 1
        if self.number and 1 not in self.steps:
            print(f"❌ 使用 --number 参数时，必须包含步骤 1")
            return False

        # 如果包含步骤 1，则不检查 case 目录是否存在（步骤 1 会创建）
        if self.case_path and 1 not in self.steps and not self.case_path.exists():
            print(f"❌ 任务目录不存在: {self.case_path}")
            return False

        # 检查脚本目录
        if not self.script_dir.exists():
            print(f"❌ 脚本目录不存在: {self.script_dir}")
            return False

        # 检查步骤编号
        for step in self.steps:
            if step not in self.step_definitions:
                print(f"❌ 无效的步骤编号: {step}")
                return False

        return True

    def run_step(self, step: int) -> bool:
        """执行单个步骤"""
        step_info = self.step_definitions[step]
        script_path = self.script_dir / step_info['script']

        print("\n" + "=" * 80)
        print(f"步骤 {step}: {step_info['name']}")
        print("=" * 80)

        # 构建命令
        if step == 1:
            # 步骤 1 使用 --number 参数
            if self.number:
                cmd = [
                    sys.executable,
                    str(script_path),
                    "--number", self.number
                ]
            elif self.case_path:
                # 从 case 路径中提取 number
                case_name = self.case_path.name
                if '__' in case_name:
                    parts = case_name.split('__')
                    if len(parts) >= 4 and parts[-1].isdigit():
                        number = parts[-1]
                        cmd = [
                            sys.executable,
                            str(script_path),
                            "--number", number
                        ]
                    else:
                        print(f"❌ 无法从 case 名称中提取编号: {case_name}")
                        return False
                else:
                    print(f"❌ 无效的 case 名称格式: {case_name}")
                    return False
        else:
            # 其他步骤使用 --case 参数
            cmd = [
                sys.executable,
                str(script_path),
                "--case", str(self.case_path)
            ]

        # 步骤 1 透传数据源文件路径
        if step == 1 and self.source:
            cmd += ["--source", self.source]

        # 执行命令
        try:
            result = subprocess.run(
                cmd,
                cwd=Path.cwd(),
                check=True,
                text=True,
                capture_output=False,  # 改为 False，让子进程直接输出到终端
                env={**os.environ, "PYTHONUNBUFFERED": "1"}  # 禁用 Python 缓冲
            )

            # 如果是步骤 1 且使用了 number，需要找到生成的 case 目录
            if step == 1 and self.number and not self.case_path:
                # 从 manual_tasks 目录中查找包含该 number 的目录
                manual_tasks = Path("manual_tasks")
                if manual_tasks.exists():
                    for case_dir in manual_tasks.iterdir():
                        if case_dir.is_dir() and case_dir.name.endswith(f"__{self.number}"):
                            self.case_path = case_dir
                            print(f"\n✅ 找到生成的 case 目录: {self.case_path}")
                            break

                if not self.case_path:
                    print(f"\n❌ 未找到生成的 case 目录")
                    return False

            print(f"\n✅ 步骤 {step} 完成")
            return True

        except subprocess.CalledProcessError as e:
            print(f"\n❌ 步骤 {step} 失败: {e}")
            return False

        except Exception as e:
            print(f"\n❌ 步骤 {step} 执行出错: {e}")
            return False

    def run(self):
        """运行流水线"""
        # 1. 验证
        if not self.validate():
            return 1

        # 2. 显示执行计划
        print("\n" + "=" * 80)
        print("数据合成流水线")
        print("=" * 80)

        if self.number:
            print(f"\n任务编号: {self.number}")
        else:
            print(f"\n任务目录: {self.case_path}")

        print(f"\n将执行以下步骤:")

        for step in self.steps:
            step_info = self.step_definitions[step]
            print(f"  {step}. {step_info['name']}")
            print(f"     {step_info['description']}")

        print("\n" + "=" * 80)

        # 3. 执行步骤
        completed_steps = []
        skipped_steps = []
        failed_step = None

        for step in self.steps:
            success = self.run_step(step)

            if success:
                completed_steps.append(step)
            elif step in self.optional_steps:
                # 可选步骤失败：跳过并继续
                skipped_steps.append(step)
                print(f"\n⏭️  步骤 {step} 失败，但为可选步骤，已跳过并继续")
            else:
                failed_step = step
                break

        # 4. 输出总结
        print("\n" + "=" * 80)
        print("流水线执行总结")
        print("=" * 80)

        if completed_steps:
            print(f"\n✅ 已完成步骤: {', '.join(map(str, completed_steps))}")

        if skipped_steps:
            print(f"\n⏭️  已跳过步骤（可选，执行失败）: {', '.join(map(str, skipped_steps))}")

        if failed_step:
            print(f"\n❌ 失败步骤: {failed_step}")
            print(f"\n提示：可以修复问题后，使用 --steps {failed_step} 重新运行该步骤")
            return 1

        print("\n🎉 所有步骤执行完成！")

        # 5. 输出生成的文件
        print("\n生成的文件：")
        output_files = [
            "metadata.json",
            "verifier.py",
            "hard_gates.py",
            "objective_metrics.py",
            "rubric.py",
            "rubric.md",
            "prompt.md",
            "tests_audit.md",
            "candidate_tests.patch",
            "setup.py",
            "Dockerfile"
        ]

        for filename in output_files:
            filepath = self.case_path / filename
            if filepath.exists():
                print(f"  ✅ {filename}")
            else:
                print(f"  ⚠️  {filename} (未生成)")

        print("\n下一步：")
        print(f"1. 查看生成的文件并进行人工复核")
        print(f"2. 如有需要，应用测试补丁: cd {self.case_path}/repo && git apply ../candidate_tests.patch")
        print(f"3. 创建评测工作空间: python {self.case_path}/setup.py --case {self.case_path} --models <model-list>")

        return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='数据合成流水线',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 使用 number 参数（推荐）
  python scripts/data_synthesis_pipeline.py --number 2764

  # 使用 case 路径
  python scripts/data_synthesis_pipeline.py --case manual_tasks/milvus-io__pymilvus__d36851e__2764

  # 只执行特定步骤
  python scripts/data_synthesis_pipeline.py --number 2764 --steps 2,3,4

步骤说明：
  1 - 下载代码仓库（从 GitHub 克隆仓库）
  2 - 生成评测体系（metadata.json, verifier.py, hard_gates.py, rubric.py）
  3 - 用真实 PR 校准评测体系
  4 - 生成 prompt.md
  5 - 审计并补充单元测试
  6 - 生成 setup.py
  7 - 生成 Dockerfile
        """
    )

    parser.add_argument('--number', type=str, help='任务编号，如: 2764')
    parser.add_argument('--case', type=str, help='任务目录路径，如: manual_tasks/fastapi__fastapi__c48e67b__16013')
    parser.add_argument('--steps', type=str, help='要执行的步骤，用逗号分隔，如: 2,3,4 (默认: 1,2,3,4,5,6,7)')
    parser.add_argument('--source', type=str, help='步骤 1 的数据源 JSON 文件路径（透传给 01_clone_repos.py，默认使用其内置文件）')

    args = parser.parse_args()

    # 验证参数
    if not args.number and not args.case:
        print("❌ 必须提供 --number 或 --case 参数之一")
        return 1

    if args.number and args.case:
        print("❌ --number 和 --case 参数不能同时使用")
        return 1

    # 解析步骤
    steps = None
    if args.steps:
        try:
            steps = [int(s.strip()) for s in args.steps.split(',')]
        except ValueError:
            print("❌ 无效的步骤格式，请使用逗号分隔的数字，如: 2,3,4")
            return 1

    # 运行流水线
    pipeline = DataSynthesisPipeline(case_path=args.case, number=args.number, steps=steps, source=args.source)
    return pipeline.run()


if __name__ == '__main__':
    sys.exit(main())

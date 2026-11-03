#!/usr/bin/env python3
"""
步骤 3: 在容器中使用 Claude Code 搭建和验证环境

设计思路：
    使用 Claude Code CLI 在 Docker 容器里智能地：
    1. 理解项目结构和依赖
    2. 执行 setup_commands（处理可能的错误）
    3. 运行项目（如果需要）
    4. 运行单元测试，验证环境就绪

    Claude Code 能够：
    - 自动分析错误日志并修复常见问题
    - 理解项目配置文件（pyproject.toml, requirements.txt 等）
    - 智能处理依赖冲突、版本不兼容等问题

功能：
    1. 启动容器（基于 02 构建的镜像）
    2. 在容器里调用 Claude Code CLI 执行环境搭建任务
    3. 验证：运行测试套件
    4. 记录结果和日志

用法：
    python scripts/evaluation/03_setup_environment.py --case manual_tasks/xxx
    python scripts/evaluation/03_setup_environment.py --case manual_tasks/xxx --interactive
"""

import sys
import json
import argparse
import subprocess
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime


class EnvironmentSetup:
    """使用 Claude Code 在容器里搭建和验证环境"""

    def __init__(self, case_path: Path, interactive: bool = False):
        self.case_path = case_path
        self.interactive = interactive

        self.metadata = self._load_metadata()

        # 从 02 保存的信息中读取容器信息
        self.container_info = self._load_container_info()
        self.container_id = self.container_info["container_id"]
        self.container_name = self.container_info["container_name"]
        # 镜像名由步骤 02 写入 .container_info.json，供保存结果时记录
        self.image_tag = self.container_info.get("image")

        # 结果目录
        self.results_dir = self.case_path / "results" / "environment"
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _load_metadata(self) -> Dict:
        metadata_file = self.case_path / "metadata.json"
        if not metadata_file.exists():
            raise FileNotFoundError(f"未找到 metadata.json: {metadata_file}")
        with open(metadata_file) as f:
            return json.load(f)

    def _load_container_info(self) -> Dict:
        """从 02 保存的文件中读取容器信息"""
        info_file = self.case_path / ".container_info.json"
        if not info_file.exists():
            raise FileNotFoundError(
                f"未找到容器信息: {info_file}\n"
                f"请先运行: python scripts/evaluation/02_build_image.py --case {self.case_path}"
            )
        with open(info_file) as f:
            return json.load(f)

    def _validate(self):
        """验证前置条件"""
        # 检查容器是否在运行
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.container_id],
            capture_output=True,
            text=True
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise RuntimeError(
                f"容器未运行: {self.container_id}\n"
                f"请先运行: python scripts/evaluation/02_build_image.py --case {self.case_path}"
            )

    def run(self) -> int:
        print("=" * 80)
        print(f"步骤 3: 使用 Claude Code 搭建环境 - {self.case_path.name}")
        print("=" * 80)

        try:
            self._validate()
            print(f"\n使用容器: {self.container_name} ({self.container_id[:12]})")

            print("\n[1/4] 准备 Claude Code 任务提示词 ...")
            task_prompt = self._generate_task_prompt()

            # 将提示词写入容器（自动和交互模式都需要）
            prompt_file = "/workspace/setup_task.md"
            self._write_prompt_to_container(task_prompt, prompt_file)
            print(f"  ✅ 任务提示词已生成: {prompt_file}")

            if self.interactive:
                print("\n[2/4] 交互模式：手动在容器里使用 Claude Code ...")
                self._interactive_mode()
            else:
                print("\n[2/4] 自动模式：调用 Claude Code 执行环境搭建 ...")
                setup_result = self._run_claude_setup(task_prompt)

                # 检查搭建结果
                status = setup_result.get("status", "failed")
                if status == "success":
                    print(f"  ✅ 环境搭建成功")
                elif status == "partial":
                    print(f"  ⚠️  环境部分可用（部分测试失败）")
                    print(f"     原因: {setup_result.get('final_status', 'unknown')}")
                else:
                    print(f"  ❌ 环境搭建失败")
                    print(f"     错误: {setup_result.get('error', 'unknown')}")
                    # 环境搭建失败应该中断流程
                    raise RuntimeError(
                        f"环境搭建失败: {setup_result.get('error', 'unknown')}\n"
                        f"Claude Code 无法自动修复环境问题，需要人工介入。"
                    )

                print("\n[3/4] 运行测试验证 ...")
                test_result = self._run_tests()

                print("\n[4/4] 保存结果 ...")
                self._save_results(setup_result, test_result)

            return 0

        except Exception as e:
            print(f"\n❌ 环境搭建失败: {e}")
            return 1

    def _interactive_mode(self):
        """交互模式：提示用户手动操作"""
        print("\n" + "=" * 80)
        print("交互模式")
        print("=" * 80)
        print(f"\n容器 ID: {self.container_id}")
        print(f"容器名称: {self.container_name}")
        print("\n请在另一个终端执行以下命令进入容器：")
        print(f"\n  docker exec -it {self.container_name} bash")
        print("\n然后在容器里运行：")
        print("\n  # 查看任务提示词")
        print("  cat /workspace/setup_task.md")
        print("\n  # 使用 Claude Code CLI（如果已安装）")
        print("  claude @/workspace/setup_task.md")
        print("\n  # 或手动执行 setup 命令")
        for cmd in self.metadata.get("environment", {}).get("setup_commands", []):
            print(f"  {cmd}")
        print("\n完成后，按 Enter 继续...")
        input()

    def _generate_task_prompt(self) -> str:
        """生成给 Claude Code 的任务提示词（从模板文件填充）"""
        # 读取模板
        template_file = Path(__file__).parent.parent / "prompts" / "setup_environment.md"
        if not template_file.exists():
            raise FileNotFoundError(f"未找到提示词模板: {template_file}")

        template = template_file.read_text()

        # 准备变量
        env = self.metadata.get("environment", {})
        setup_commands = env.get("setup_commands", [])
        test_commands = env.get("test_commands", [])

        # 格式化命令列表
        setup_commands_text = "\n".join(
            f"{i}. `{cmd}`" for i, cmd in enumerate(setup_commands, 1)
        )
        test_commands_text = "\n".join(
            f"{i}. `{cmd}`" for i, cmd in enumerate(test_commands, 1)
        )

        # 填充模板
        prompt = template.format(
            project_name=self.metadata.get("repository", {}).get("name", "unknown"),
            language=self.metadata.get("repository", {}).get("language", "Python"),
            setup_commands=setup_commands_text,
            test_commands=test_commands_text
        )

        return prompt

    def _write_prompt_to_container(self, prompt: str, target_path: str):
        """将提示词写入容器"""
        subprocess.run(
            ["docker", "exec", "-i", self.container_name, "sh", "-c",
             f"cat > {target_path}"],
            input=prompt.encode(),
            check=True
        )

    def _run_claude_setup(self, task_prompt: str) -> Dict:
        """在容器里调用 Claude Code 执行环境搭建"""
        prompt_file = "/workspace/setup_task.md"

        # 调用 Claude Code（非交互模式，跳过权限确认）
        cmd = [
            "docker", "exec",
            "-e", "IS_SANDBOX=1",  # 允许 --dangerously-skip-permissions 在 root 下运行
            "-w", "/workspace/repo",
            self.container_name,
            "claude", "--dangerously-skip-permissions", f"@{prompt_file}"
        ]

        print(f"    执行: {' '.join(cmd[3:])}")  # 只显示容器内的命令

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800  # 30 分钟超时
            )

            print(f"    Claude Code 退出码: {result.returncode}")

            # 读取 Claude 生成的结果文件
            result_json = subprocess.run(
                ["docker", "exec", self.container_name, "cat", "/workspace/setup_result.json"],
                capture_output=True,
                text=True
            )

            if result_json.returncode == 0:
                try:
                    return json.loads(result_json.stdout)
                except json.JSONDecodeError as e:
                    return {
                        "status": "failed",
                        "error": f"setup_result.json 格式错误: {e}",
                        "claude_output": result.stdout[-2000:],
                        "claude_stderr": result.stderr[-2000:]
                    }
            else:
                return {
                    "status": "failed",
                    "error": "未找到 setup_result.json",
                    "claude_output": result.stdout[-2000:],
                    "claude_stderr": result.stderr[-2000:]
                }

        except subprocess.TimeoutExpired:
            return {"status": "failed", "error": "Claude Code 执行超时（30分钟）"}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def _run_tests(self) -> Dict:
        """运行测试套件"""
        env = self.metadata.get("environment", {})
        test_commands = env.get("test_commands", [])

        results = []
        for cmd in test_commands:
            print(f"    执行: {cmd}")
            result = subprocess.run(
                ["docker", "exec", "-w", "/workspace/repo", self.container_name, "sh", "-c", cmd],
                capture_output=True,
                text=True
            )
            results.append({
                "command": cmd,
                "exit_code": result.returncode,
                "stdout": result.stdout[-2000:],  # 只保留最后 2000 字符
                "stderr": result.stderr[-2000:],
                "success": result.returncode == 0
            })

            if result.returncode == 0:
                print(f"      ✅ 通过")
            else:
                print(f"      ❌ 失败（退出码 {result.returncode}）")

        passed = sum(1 for r in results if r["success"])
        total = len(results)

        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "commands": results
        }

    def _save_results(self, setup_result: Dict, test_result: Dict):
        """保存结果到宿主机"""
        report = {
            "timestamp": datetime.now().isoformat(),
            "case": self.case_path.name,
            "image": self.image_tag,
            "container": self.container_id,
            "setup": setup_result,
            "tests": test_result
        }

        output_file = self.results_dir / "setup_report.json"
        with open(output_file, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        print(f"  ✅ 结果已保存: {output_file}")

        # 打印摘要
        print("\n" + "=" * 80)
        print("环境搭建摘要")
        print("=" * 80)
        print(f"状态: {setup_result.get('status', 'unknown')}")
        if test_result:
            print(f"测试: {test_result['passed']}/{test_result['total']} 通过")

def main():
    parser = argparse.ArgumentParser(
        description="在容器中使用 Claude Code 搭建和验证环境"
    )
    parser.add_argument("--case", required=True, help="Case 目录路径")
    parser.add_argument("--interactive", action="store_true",
                       help="交互模式：手动在容器里使用 Claude Code")

    args = parser.parse_args()

    case_path = Path(args.case)
    if not case_path.exists():
        print(f"❌ Case 目录不存在: {case_path}")
        return 1

    setup = EnvironmentSetup(
        case_path=case_path,
        interactive=args.interactive
    )
    return setup.run()


if __name__ == "__main__":
    sys.exit(main())

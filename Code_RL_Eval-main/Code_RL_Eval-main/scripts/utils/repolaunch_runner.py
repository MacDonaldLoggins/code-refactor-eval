#!/usr/bin/env python3
"""
RepoLaunch 集成工具类

提供统一的 RepoLaunch 接口，用于评测流程
"""

import asyncio
import json
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime


class RepoLaunchRunner:
    """RepoLaunch 运行器"""

    def __init__(self, case_path: Path, metadata: Dict):
        self.case_path = case_path
        self.metadata = metadata
        self.repo_path = case_path / "repo"

    async def run_in_container(self, setup_commands: list = None) -> Dict:
        """
        在 RepoLaunch 容器中运行评测

        Args:
            setup_commands: 额外的设置命令列表

        Returns:
            包含所有执行结果的字典
        """
        try:
            # 导入 RepoLaunch (根据文档调整)
            from repolaunch import RepoLaunch, InstanceConfig

            # 配置容器
            config = InstanceConfig(
                repo_path=str(self.repo_path),
                base_commit=self.metadata.get("base_commit", "HEAD"),
                instance_image="python:3.11",  # 根据项目需要选择
                timeout=1800,  # 30分钟超时
            )

            result = {
                "timestamp": datetime.now().isoformat(),
                "execution_mode": "repolaunch",
                "status": "success",
            }

            # 启动容器
            async with RepoLaunch(config) as instance:
                # 1. 执行额外的设置命令
                if setup_commands:
                    for cmd in setup_commands:
                        await instance.exec(cmd)

                # 2. 安装依赖
                install_result = await self._install_dependencies(instance)
                result["install"] = install_result

                # 3. 运行测试
                test_result = await self._run_tests(instance)
                result["tests"] = test_result

                # 4. 收集质量指标
                quality_result = await self._collect_quality_metrics(instance)
                result["metrics"] = {"quality": quality_result}

                # 5. 运行 verifier
                verifier_result = await self._run_verifier(instance)
                result["verifier"] = verifier_result

            return result

        except Exception as e:
            return {
                "timestamp": datetime.now().isoformat(),
                "execution_mode": "repolaunch",
                "status": "error",
                "error": str(e)
            }

    async def _install_dependencies(self, instance) -> Dict:
        """在容器中安装依赖"""
        test_directives = self.metadata.get("test_directives", {})
        install_cmd = test_directives.get("install", "pip install -e .")

        try:
            output = await instance.exec(install_cmd)
            return {
                "success": True,
                "command": install_cmd,
                "output": output[-1000:] if output else ""  # 只保留最后1000字符
            }
        except Exception as e:
            return {
                "success": False,
                "command": install_cmd,
                "error": str(e)
            }

    async def _run_tests(self, instance) -> Dict:
        """在容器中运行测试"""
        test_directives = self.metadata.get("test_directives", {})
        test_cmd = test_directives.get("test", "pytest")

        try:
            # 运行测试并生成 JSON 报告
            full_cmd = f"{test_cmd} --json-report --json-report-file=test_report.json || true"
            output = await instance.exec(full_cmd)

            # 读取测试报告
            try:
                report_content = await instance.read_file("test_report.json")
                test_report = json.loads(report_content) if report_content else None
            except:
                test_report = None

            return {
                "success": True,
                "command": test_cmd,
                "report": test_report,
                "output": output[-2000:] if output else ""
            }
        except Exception as e:
            return {
                "success": False,
                "command": test_cmd,
                "error": str(e)
            }

    async def _collect_quality_metrics(self, instance) -> Dict:
        """在容器中收集代码质量指标"""
        metrics = {}

        # Pylint
        try:
            pylint_cmd = "pylint --output-format=json --exit-zero $(find . -name '*.py' | head -20)"
            output = await instance.exec(pylint_cmd)
            if output:
                try:
                    metrics["pylint"] = json.loads(output)
                except:
                    metrics["pylint"] = {"raw_output": output[:1000]}
        except:
            pass

        # Radon (复杂度)
        try:
            radon_cmd = "radon cc . -j"
            output = await instance.exec(radon_cmd)
            if output:
                try:
                    metrics["complexity"] = json.loads(output)
                except:
                    pass
        except:
            pass

        return metrics

    async def _run_verifier(self, instance) -> Dict:
        """在容器中运行 verifier"""
        verifier_file = self.case_path / "verifier.py"

        if not verifier_file.exists():
            return {"skipped": True, "reason": "verifier.py 不存在"}

        try:
            # 将 verifier.py 复制到容器中
            await instance.copy_to_container(
                str(verifier_file),
                "verifier.py"
            )

            # 执行 verifier
            output = await instance.exec("python verifier.py")

            return {
                "success": True,
                "output": output
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    async def run_with_refactor(self, instance, refactor_changes: Dict) -> Dict:
        """
        在容器中应用重构并评测

        Args:
            instance: RepoLaunch 实例
            refactor_changes: 重构修改内容

        Returns:
            评测结果
        """
        # TODO: 应用重构修改
        # 然后执行测试等步骤
        pass

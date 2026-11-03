#!/usr/bin/env python3
"""
步骤 6: 导出容器产物

设计思路：
    将 Docker 容器里的所有重要产物导出到宿主机，方便归档、分析和分享。

导出内容：
    - patches/*.patch（核心：代码变更）
    - refactor_result.json（重构结果报告）
    - refactor_task.md（使用的提示词）
    - test_results/（测试日志）
    - *_output.json（验证脚本的详细输出）
    - agent_session/（可选：agent 工作日志）

不导出：
    - repo/（太大，可以用 patch 重建）

用法：
    python scripts/evaluation/06_export_artifacts.py --case manual_tasks/xxx
    python scripts/evaluation/06_export_artifacts.py --case manual_tasks/xxx --include-agent-logs
"""

import sys
import argparse
import subprocess
from pathlib import Path
from typing import Dict, List


class ArtifactExporter:
    """产物导出器"""

    def __init__(self, case_path: Path, include_agent_logs: bool = True):
        self.case_path = case_path
        self.include_agent_logs = include_agent_logs

        self.container_info = self._load_container_info()
        self.workspace_info = self._load_workspace_info()

        self.container_id = self.container_info["container_id"]
        self.container_name = self.container_info["container_name"]

        # 导出目录
        self.export_dir = self.case_path / "results" / "workspaces"
        self.export_dir.mkdir(parents=True, exist_ok=True)

    def _load_container_info(self) -> Dict:
        info_file = self.case_path / ".container_info.json"
        if not info_file.exists():
            raise FileNotFoundError(
                f"未找到容器信息: {info_file}\n"
                f"请先运行步骤 02"
            )
        import json
        with open(info_file) as f:
            return json.load(f)

    def _load_workspace_info(self) -> Dict:
        info_file = self.case_path / ".workspace_info.json"
        if not info_file.exists():
            raise FileNotFoundError(
                f"未找到工作空间信息: {info_file}\n"
                f"请先运行步骤 04"
            )
        import json
        with open(info_file) as f:
            return json.load(f)

    def run(self) -> int:
        print("=" * 80)
        print(f"步骤 6: 导出容器产物 - {self.case_path.name}")
        print("=" * 80)

        try:
            workspaces = self.workspace_info["workspaces"]
            print(f"\n将导出 {len(workspaces)} 个工作空间的产物")
            print(f"导出目录: {self.export_dir}\n")

            # 1. 导出环境搭建产物
            print("[1/2] 导出环境搭建产物 ...")
            self._export_environment_artifacts()

            # 2. 导出每个工作空间的产物
            print("\n[2/2] 导出工作空间产物 ...")
            for ws in workspaces:
                self._export_workspace_artifacts(ws)

            print("\n" + "=" * 80)
            print("导出完成")
            print("=" * 80)
            print(f"所有产物已保存到: {self.export_dir}")

            return 0

        except Exception as e:
            print(f"\n❌ 导出失败: {e}")
            return 1

    def _export_environment_artifacts(self):
        """导出环境搭建的产物"""
        env_dir = self.case_path / "results" / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)

        # 导出 setup_result.json
        self._export_file(
            "/workspace/setup_result.json",
            env_dir / "setup_result.json",
            optional=True
        )

        # 导出 setup_task.md
        self._export_file(
            "/workspace/setup_task.md",
            env_dir / "setup_task.md",
            optional=True
        )

        print(f"  ✅ 环境产物已导出到: {env_dir}")

    def _export_workspace_artifacts(self, workspace: Dict):
        """导出单个工作空间的产物"""
        ws_name = workspace["name"]
        ws_path = workspace["path"]
        ws_export_dir = self.export_dir / ws_name
        ws_export_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n  导出: {ws_name}")

        # 核心产物
        exported_count = 0

        # 1. patches/
        if self._export_directory(f"{ws_path}/patches", ws_export_dir / "patches"):
            print(f"    ✅ patches/")
            exported_count += 1

        # 2. refactor_result.json
        if self._export_file(f"{ws_path}/refactor_result.json", ws_export_dir / "refactor_result.json"):
            print(f"    ✅ refactor_result.json")
            exported_count += 1

        # 3. refactor_task.md
        if self._export_file(f"{ws_path}/refactor_task.md", ws_export_dir / "refactor_task.md"):
            print(f"    ✅ refactor_task.md")
            exported_count += 1

        # 4. test_results/
        if self._export_directory(f"{ws_path}/test_results", ws_export_dir / "test_results"):
            print(f"    ✅ test_results/")
            exported_count += 1

        # 5. verifier 输出（每次运行带时间戳，可能有多份；hard_gates/rubric 结果已内含其中）
        listing = subprocess.run(
            ["docker", "exec", self.container_name, "sh", "-c",
             f"ls {ws_path}/verifier_output_*.json 2>/dev/null"],
            capture_output=True, text=True
        )
        verifier_files = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
        for container_file in verifier_files:
            fname = container_file.rsplit("/", 1)[-1]
            if self._export_file(container_file, ws_export_dir / fname):
                print(f"    ✅ {fname}")
                exported_count += 1

        # 6. agent_session/（可选）
        if self.include_agent_logs:
            if self._export_directory(f"{ws_path}/agent_session", ws_export_dir / "agent_session"):
                print(f"    ✅ agent_session/")
                exported_count += 1

        if exported_count == 0:
            print(f"    ⚠️  未找到任何产物")

    def _export_file(self, container_path: str, host_path: Path, optional: bool = True) -> bool:
        """从容器导出单个文件"""
        # 先检查文件是否存在
        check_result = subprocess.run(
            ["docker", "exec", self.container_name, "test", "-f", container_path],
            capture_output=True
        )

        if check_result.returncode != 0:
            if not optional:
                print(f"    ⚠️  文件不存在: {container_path}")
            return False

        # 导出文件
        try:
            subprocess.run(
                ["docker", "cp", f"{self.container_name}:{container_path}", str(host_path)],
                check=True,
                capture_output=True
            )
            return True
        except subprocess.CalledProcessError as e:
            print(f"    ❌ 导出失败: {container_path} -> {e}")
            return False

    def _export_directory(self, container_path: str, host_path: Path) -> bool:
        """从容器导出目录"""
        # 先检查目录是否存在
        check_result = subprocess.run(
            ["docker", "exec", self.container_name, "test", "-d", container_path],
            capture_output=True
        )

        if check_result.returncode != 0:
            return False

        # 确保宿主机目标目录存在
        host_path.mkdir(parents=True, exist_ok=True)

        # 导出目录（注意：docker cp 的目录语法）
        # 如果源路径以 /. 结尾，会复制目录内容；否则复制整个目录
        try:
            subprocess.run(
                ["docker", "cp", f"{self.container_name}:{container_path}/.", str(host_path)],
                check=True,
                capture_output=True
            )
            return True
        except subprocess.CalledProcessError as e:
            print(f"    ❌ 导出失败: {container_path} -> {e}")
            return False


def main():
    parser = argparse.ArgumentParser(
        description="导出容器产物到宿主机"
    )
    parser.add_argument("--case", required=True, help="Case 目录路径")
    parser.add_argument("--no-agent-logs", action="store_true",
                       help="跳过 agent_session/ 日志导出")

    args = parser.parse_args()

    case_path = Path(args.case)
    if not case_path.exists():
        print(f"❌ Case 目录不存在: {case_path}")
        return 1

    exporter = ArtifactExporter(
        case_path=case_path,
        include_agent_logs=not args.no_agent_logs
    )
    return exporter.run()


if __name__ == "__main__":
    sys.exit(main())

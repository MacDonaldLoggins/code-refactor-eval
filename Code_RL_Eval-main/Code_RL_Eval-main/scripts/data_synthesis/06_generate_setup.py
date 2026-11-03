#!/usr/bin/env python3
"""
Step 6: 生成 setup.py

功能：
1. 读取 metadata.json
2. 生成 setup.py 脚本用于准备评测工作空间

说明：Dockerfile 的生成已拆分到 07_generate_dockerfile.py。
"""

import json
import sys
from pathlib import Path
from typing import Dict

# 添加 scripts 到 path
sys.path.insert(0, str(Path(__file__).parent.parent))


class SetupGenerator:
    """setup.py 生成器"""

    def __init__(self, case_path: str):
        self.case_path = Path(case_path)
        self.repo_path = self.case_path / "repo"

    def load_metadata(self) -> Dict:
        """加载 metadata.json"""
        metadata_file = self.case_path / "metadata.json"
        if not metadata_file.exists():
            raise FileNotFoundError(f"未找到 metadata.json: {metadata_file}")

        with open(metadata_file, 'r', encoding='utf-8') as f:
            metadata = json.load(f)

        print(f"✅ 已加载 metadata.json")
        return metadata

    def generate_setup_py(self, metadata: Dict) -> str:
        """生成 setup.py 脚本"""
        # 从 metadata 中提取测试路径
        frozen_paths = metadata.get('frozen_paths', [])
        test_paths = [p for p in frozen_paths if 'test' in p.lower()]

        # 如果没有找到测试路径，尝试从 test_command 推断
        if not test_paths:
            test_command = metadata.get('test_command', '')
            if 'pytest' in test_command:
                parts = test_command.split()
                for part in parts:
                    if part.startswith('tests/'):
                        test_paths.append(part)

        setup_script = '''#!/usr/bin/env python3
"""
Setup script for evaluation workspace

功能：
1. 为每个待评测模型创建独立工作空间
2. 复制仓库代码到工作空间
3. 删除单元测试文件（避免 agent 查看测试实现）
4. 创建必要的目录结构
"""

import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path


def load_metadata(case_path: Path) -> dict:
    """加载 metadata.json"""
    metadata_file = case_path / "metadata.json"
    with open(metadata_file) as f:
        return json.load(f)


def _init_git_baseline(repo_dest: Path):
    """在工作空间的 repo 里初始化 git 并提交 baseline（供 agent 用 git diff 生成 patch）"""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@local",
        "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@local",
    }
    try:
        subprocess.run(["git", "init", "-q"], cwd=repo_dest, check=True, env=env)
        subprocess.run(["git", "add", "-A"], cwd=repo_dest, check=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", "baseline", "--no-verify"],
            cwd=repo_dest, check=True, env=env,
        )
        print(f"  ✅ 已初始化 git baseline")
    except Exception as e:
        print(f"  ⚠️  git baseline 初始化失败: {e}")


def create_workspace(case_path: Path, workspace_name: str, test_paths: list) -> Path:
    """按给定的工作空间名创建工作空间

    workspace_name 由调用方（04_run_refactor.py）生成并下发，
    格式为 `模型名__时间戳_4位随机`，是全流程的唯一命名来源，
    这样 04 校验、写入 .workspace_info.json 以及 05/06 读取都能对齐。
    """
    workspace = case_path / "candidates" / workspace_name
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"创建工作空间: {workspace}")

    # 复制 repo
    base_repo = case_path / "repo"
    repo_dest = workspace / "repo"

    if repo_dest.exists():
        shutil.rmtree(repo_dest)

    shutil.copytree(base_repo, repo_dest, symlinks=True)
    print(f"  ✅ 已复制仓库代码")

    # 删除单元测试
    deleted_count = 0
    for test_path in test_paths:
        test_file = repo_dest / test_path
        if test_file.exists():
            if test_file.is_dir():
                shutil.rmtree(test_file)
                deleted_count += 1
                print(f"  🗑️  已删除测试目录: {test_path}")
            else:
                test_file.unlink()
                deleted_count += 1
                print(f"  🗑️  已删除测试文件: {test_path}")

    if deleted_count == 0:
        print(f"  ℹ️  无需删除测试文件")

    # 初始化 git 仓库并建立 baseline commit：
    # agent 的工作目录就是这个 repo/，重构后用 `git diff` 即可干净地生成 patch；
    # 每个工作空间是独立 git 仓库，并行时边界更清晰。
    _init_git_baseline(repo_dest)

    # 创建其他目录
    (workspace / "agent_session").mkdir(exist_ok=True)
    (workspace / "patches").mkdir(exist_ok=True)

    # 复制 prompt.md
    prompt_file = case_path / "prompt.md"
    if prompt_file.exists():
        shutil.copy(prompt_file, workspace / "prompt.md")
        print(f"  ✅ 已复制 prompt.md")

    return workspace


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Setup evaluation workspace')
    parser.add_argument('--case', required=True, help='任务目录路径')
    parser.add_argument('--names', nargs='+',
                       help='要创建的工作空间目录名（由 04 下发，唯一命名来源）。'
                            '指定后忽略 --models/--iterations')
    parser.add_argument('--models', nargs='+', default=['claude-opus-4'],
                       help='模型列表（仅在未提供 --names 时用于自动生成名字）')
    parser.add_argument('--iterations', type=int, default=1,
                       help='每个模型的迭代次数（仅在未提供 --names 时生效）')

    args = parser.parse_args()

    case_path = Path(args.case)
    if not case_path.exists():
        print(f"❌ 任务目录不存在: {case_path}")
        return 1

    # 获取测试路径（生成时固化，运行期不依赖 metadata.json）
    test_paths = %TEST_PATHS%
    print(f"\\n测试路径配置: {test_paths if test_paths else '无'}")

    # 确定要创建的工作空间名列表
    if args.names:
        # 首选路径：04 已生成好名字并下发
        workspace_names = args.names
    else:
        # 后备路径：独立运行时自行生成 `模型名__时间戳_随机`
        workspace_names = []
        used = set()
        for model_name in args.models:
            for _ in range(args.iterations):
                while True:
                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    suffix = f"{random.randint(0, 9999):04d}"
                    name = f"{model_name}__{timestamp}_{suffix}"
                    if name not in used:
                        used.add(name)
                        break
                workspace_names.append(name)

    # 创建工作空间
    print(f"\\n开始创建工作空间...")
    print(f"工作空间列表: {workspace_names}")
    print("=" * 80)

    for workspace_name in workspace_names:
        create_workspace(case_path, workspace_name, test_paths)
        print()

    print("=" * 80)
    print("✅ 所有工作空间创建完成")

    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

        # 替换测试路径
        test_paths_str = json.dumps(test_paths, ensure_ascii=False)
        setup_script = setup_script.replace('%TEST_PATHS%', test_paths_str)

        return setup_script

    def save_setup_py(self, setup_py: str):
        """保存 setup.py"""
        setup_py_path = self.case_path / "setup.py"
        with open(setup_py_path, 'w', encoding='utf-8') as f:
            f.write(setup_py)

        # 添加可执行权限
        setup_py_path.chmod(0o755)
        print(f"✅ 已保存 setup.py: {setup_py_path}")

    def run(self):
        """运行生成流程"""
        print("=" * 80)
        print("Step 6: 生成 setup.py")
        print("=" * 80)

        # 1. 加载 metadata
        print("\n📚 加载材料...")
        metadata = self.load_metadata()

        # 2. 生成 setup.py
        print("\n📝 生成 setup.py...")
        setup_py = self.generate_setup_py(metadata)
        print("✅ 生成完成")

        # 3. 保存输出
        self.save_setup_py(setup_py)

        # 4. 总结
        print("\n" + "=" * 80)
        print("生成完成")
        print("=" * 80)
        print(f"\n下一步：")
        print(f"1. 查看生成的 setup.py: {self.case_path}/setup.py")
        print(f"2. 生成 Dockerfile: python scripts/data_synthesis/07_generate_dockerfile.py --case {self.case_path}")
        print(f"3. 创建工作空间: python {self.case_path}/setup.py --case {self.case_path} --models claude-opus-4")

        return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Step 6: 生成 setup.py')
    parser.add_argument('--case', required=True, help='任务目录路径，如: manual_tasks/fastapi__fastapi__c48e67b__16013')

    args = parser.parse_args()

    generator = SetupGenerator(args.case)
    return generator.run()


if __name__ == '__main__':
    sys.exit(main())

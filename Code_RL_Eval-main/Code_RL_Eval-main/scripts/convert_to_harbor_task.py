#!/usr/bin/env python3
"""
将 manual_tasks/<case> 转换为 Harbor 标准 task 格式

用法:
    # 转换单个 case
    python convert_to_harbor_task.py \
        --case manual_tasks/Mathics3__mathics-core__a34ebdf__1643 \
        --output harbor_eval/tasks

    # 批量转换
    python convert_to_harbor_task.py \
        --batch manual_tasks \
        --output harbor_eval/tasks
"""

import argparse
import json
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================================
# 常量定义
# ============================================================================

REQUIRED_FILES = [
    "prompt.md",
    "Dockerfile",
    "metadata.json",
    "verifier.py",
    "hard_gates.py",
    "objective_metrics.py",
    "rubric.py",
    "repo",
]

VERIFIER_FILES = ("verifier.py", "hard_gates.py", "objective_metrics.py", "rubric.py")

REPO_MOUNT = "/workspace/repo"


# ============================================================================
# 数据类
# ============================================================================

@dataclass
class ConversionResult:
    """转换结果"""
    success: bool
    task_id: str
    input_path: Path
    output_path: Optional[Path] = None
    error: Optional[str] = None
    skipped: bool = False


# ============================================================================
# Task 转换器
# ============================================================================

class TaskConverter:
    """Harbor Task 转换器"""

    def __init__(
        self,
        output_root: Path,
        cache_enabled: bool = True,
    ):
        self.output_root = Path(output_root)
        self.cache_enabled = cache_enabled
        self.output_root.mkdir(parents=True, exist_ok=True)

    def convert(self, case_dir: Path, force: bool = False) -> ConversionResult:
        """转换单个 case"""
        case_dir = case_dir.resolve()
        task_id = case_dir.name
        output_dir = self.output_root / task_id

        logger.info(f"开始转换: {task_id}")

        # 检查缓存
        if self.cache_enabled and not force and output_dir.exists():
            logger.info(f"跳过（已存在）: {task_id}")
            return ConversionResult(
                success=True,
                task_id=task_id,
                input_path=case_dir,
                output_path=output_dir,
                skipped=True,
            )

        # 验证输入
        validation_error = self._validate_case(case_dir)
        if validation_error:
            logger.error(f"验证失败: {validation_error}")
            return ConversionResult(
                success=False,
                task_id=task_id,
                input_path=case_dir,
                error=validation_error,
            )

        try:
            # 创建输出目录
            if output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True)

            # 读取 metadata
            metadata_path = case_dir / "metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            # 1. 生成 task.toml
            self._generate_task_toml(output_dir, metadata)

            # 2. 生成 instruction.md
            self._generate_instruction(case_dir, output_dir)

            # 3. 创建 environment/
            self._create_environment(case_dir, output_dir)

            # 4. 创建 tests/
            self._create_tests(case_dir, output_dir)

            logger.info(f"✅ 转换成功: {task_id}")
            return ConversionResult(
                success=True,
                task_id=task_id,
                input_path=case_dir,
                output_path=output_dir,
            )

        except Exception as e:
            logger.error(f"转换失败: {e}", exc_info=True)
            return ConversionResult(
                success=False,
                task_id=task_id,
                input_path=case_dir,
                error=str(e),
            )

    def batch_convert(
        self, case_dirs: List[Path], force: bool = False
    ) -> List[ConversionResult]:
        """批量转换"""
        results = []
        for case_dir in case_dirs:
            if not case_dir.is_dir():
                continue
            result = self.convert(case_dir, force=force)
            results.append(result)
        return results

    def _validate_case(self, case_dir: Path) -> Optional[str]:
        """验证 case 是否包含必需文件"""
        missing = []
        for required in REQUIRED_FILES:
            path = case_dir / required
            if not path.exists():
                missing.append(required)

        if missing:
            return f"缺少必需文件: {', '.join(missing)}"
        return None

    def _generate_task_toml(self, output_dir: Path, metadata: Dict):
        """生成 task.toml"""
        task_name = metadata.get("task_name", "unknown")
        language = metadata.get("language", "unknown")
        description = metadata.get("objective", "")

        toml_content = f"""# 由 convert_to_harbor_task.py 从 metadata.json 生成
# task 名: {task_name}
# language: {language}
# 来源: {description[:80]}
schema_version = "1.3"
artifacts = []

[metadata]
refactor_type = "{metadata.get('refactor_type', 'unknown')}"

[verifier]
# tests/test.sh 调 verifier.py，把 reward(∈[0,1]) 写到 /logs/verifier/reward.txt
timeout_sec = 1800.0
collect = []

[verifier.env]

[agent]
timeout_sec = 3600.0

[environment]
# 需要联网：build 时装依赖、agent 运行时访问模型 gateway
network_mode = "public"
build_timeout_sec = 1800.0
os = "linux"
mcp_servers = []

[environment.env]
# 清空 Docker Desktop 注入的宿主机代理（否则容器运行时 apt/pip 会撞死代理超时）
HTTP_PROXY = ""
HTTPS_PROXY = ""
http_proxy = ""
https_proxy = ""
ALL_PROXY = ""
all_proxy = ""
NO_PROXY = "*"
no_proxy = "*"

[solution.env]
"""
        (output_dir / "task.toml").write_text(toml_content)
        logger.debug(f"已生成: task.toml")

    def _generate_instruction(self, case_dir: Path, output_dir: Path):
        """生成 instruction.md"""
        prompt_path = case_dir / "prompt.md"
        prompt_content = prompt_path.read_text()

        header = """> **工作区说明**：本仓库源码位于 `./repo/` 子目录，请 `cd repo` 后在其中完成重构。
> 只改动 `repo/` 内的生产代码，**不要修改任何测试文件**，也无需自行运行评测脚本。
>
> **⚠️ 严禁操作 git**：不要执行 `git init`、`rm -rf .git`、`git commit`、`git reset` 等任何修改 git 仓库状态的命令。评测系统依赖原始 git 历史来对比你的改动。你只需直接修改文件即可，改动会被自动收集。

---

"""
        instruction_content = header + prompt_content
        (output_dir / "instruction.md").write_text(instruction_content)
        logger.debug(f"已生成: instruction.md")

    def _create_environment(self, case_dir: Path, output_dir: Path):
        """创建 environment/"""
        env_dir = output_dir / "environment"
        env_dir.mkdir()

        # 复制并改造 Dockerfile
        dockerfile_src = case_dir / "Dockerfile"
        dockerfile_dst = env_dir / "Dockerfile"
        dockerfile_content = dockerfile_src.read_text()

        # 改造 Dockerfile
        dockerfile_content = self._transform_dockerfile(dockerfile_content)
        dockerfile_dst.write_text(dockerfile_content)
        logger.debug(f"已生成: environment/Dockerfile")

        # 复制 repo
        repo_src = case_dir / "repo"
        repo_dst = env_dir / "repo"
        shutil.copytree(repo_src, repo_dst, symlinks=True)
        logger.debug(f"已复制: environment/repo/")

        # 复制 Claude Code 安装脚本（如果存在）
        for script in ["install_claude_code.sh", "install_codex.sh", "codex_config.toml"]:
            script_src = case_dir / script
            if script_src.exists():
                shutil.copy2(script_src, env_dir / script)
                logger.debug(f"已复制: environment/{script}")

    def _transform_dockerfile(self, dockerfile: str) -> str:
        """改造 Dockerfile"""
        lines = dockerfile.splitlines()
        new_lines = []

        skip_next = False
        for line in lines:
            # 跳过 settings.json 相关行
            if skip_next:
                skip_next = False
                continue

            if "settings.json" in line and "COPY" in line:
                skip_next = True  # 跳过下一行 RUN mkdir
                continue

            new_lines.append(line)

        return "\n".join(new_lines)

    def _create_tests(self, case_dir: Path, output_dir: Path):
        """创建 tests/"""
        tests_dir = output_dir / "tests"
        tests_dir.mkdir()

        # 复制 verifier 全家桶
        for verifier_file in VERIFIER_FILES:
            src = case_dir / verifier_file
            dst = tests_dir / verifier_file
            shutil.copy2(src, dst)
            logger.debug(f"已复制: tests/{verifier_file}")

        # 生成 test.sh
        test_sh_content = """#!/bin/bash
set -e

# Harbor 约定：当前目录是 /workspace
# verifier.py 会将 reward 写入 /logs/verifier/reward.txt

cd /workspace/repo
python3 /workspace/tests/verifier.py

exit 0
"""
        test_sh_path = tests_dir / "test.sh"
        test_sh_path.write_text(test_sh_content)
        test_sh_path.chmod(0o755)
        logger.debug(f"已生成: tests/test.sh")


# ============================================================================
# 命令行接口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="将 manual_tasks 转换为 Harbor 标准 task 格式",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 转换单个 case
  %(prog)s --case manual_tasks/case_001 --output harbor_eval/tasks

  # 批量转换
  %(prog)s --batch manual_tasks --output harbor_eval/tasks

  # 强制重新转换
  %(prog)s --case manual_tasks/case_001 --output harbor_eval/tasks --force
        """,
    )

    parser.add_argument(
        "--case",
        type=Path,
        help="单个 case 目录路径",
    )

    parser.add_argument(
        "--batch",
        type=Path,
        help="批量转换：manual_tasks 根目录",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("harbor_eval/tasks"),
        help="输出目录（默认: harbor_eval/tasks）",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新转换（忽略缓存）",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细输出（DEBUG 日志）",
    )

    args = parser.parse_args()

    # 配置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 检查参数
    if not args.case and not args.batch:
        parser.error("必须指定 --case 或 --batch")

    if args.case and args.batch:
        parser.error("--case 和 --batch 不能同时使用")

    # 初始化转换器
    converter = TaskConverter(output_root=args.output)

    # 执行转换
    if args.case:
        # 单个转换
        result = converter.convert(args.case, force=args.force)
        if result.success:
            if result.skipped:
                print(f"⏭️  跳过（已存在）: {result.output_path}")
            else:
                print(f"✅ 转换成功: {result.output_path}")
            return 0
        else:
            print(f"❌ 转换失败: {result.error}")
            return 1

    elif args.batch:
        # 批量转换
        case_dirs = [d for d in args.batch.iterdir() if d.is_dir()]
        print(f"找到 {len(case_dirs)} 个 cases")

        results = converter.batch_convert(case_dirs, force=args.force)

        # 统计结果
        success_count = sum(1 for r in results if r.success and not r.skipped)
        skipped_count = sum(1 for r in results if r.skipped)
        failed_count = sum(1 for r in results if not r.success)

        print(f"\n转换完成:")
        print(f"  ✅ 成功: {success_count}")
        print(f"  ⏭️  跳过: {skipped_count}")
        print(f"  ❌ 失败: {failed_count}")

        if failed_count > 0:
            print("\n失败的 cases:")
            for r in results:
                if not r.success:
                    print(f"  - {r.task_id}: {r.error}")
            return 1

        return 0


if __name__ == "__main__":
    sys.exit(main())
